"""eval-worker: Tier 2 online sampled evaluation and the runtime operations alert rules (plan §3.9).

* Online evaluation: reads the sampled UC1/UC2 answers the orchestrator sent to the audit content store
  (`GET /audit/eval-samples` on audit-writer, role `Audit.ReadSampled`: sampled answers only, never other content),
  scores them with the in-region judge (`gen-large` through the APIM AI gateway, fixed rubric prompts,
  `rm_copilot.ragas.LLMJudgeScorer`; lexical fallback if the judge fails), at most `max_per_day` per day, and publishes
  scores and rolling-window means (LLD §17.3):
      rmc_online_eval_score{use_case,metric}            histogram, one observation per judged item (UC2: faithfulness,
                                                        answer_relevancy, context_precision, context_utilisation; UC1:
                                                        faithfulness, context_utilisation)
      rmc_online_eval_window_mean{use_case,metric,window}  rolling mean (1h; 3d for faithfulness)
      rmc_online_eval_alert{use_case,metric,window}     1 when faithfulness < 0.90 or answer relevancy < 0.80 over 1 h,
                                                        or faithfulness < 0.93 over 3 days, with >= min_items_window items
                                                        (the 1 h window extends to the 10 most recent items, up to 4 h)
      rmc_online_eval_items_total{use_case,outcome}     judged, skipped_cap, judge_error
  Settings: App Configuration `rmc:eval:online:*` (sample_rate, max_per_day, min_items_window, thresholds:*).
* Ops alert rules: scrapes `/metrics` of web-bff, orchestrator and tool-gateway, computes the operator summary
  (`services/common/opsmetrics.py`) and sets `rmc_ops_alert{rule,use_case}`; writes a metadata-only snapshot to
  `<ops_dir>/snapshots/<date>.jsonl` for the drift report (`python3 -m evals.scheduled --drift`).

Telemetry stays metadata only: scores, counts and metric names; never question, answer or passage text.
Production: a small AKS deployment (or Container Apps job) with its own workload identity; judge calls go to the
pre-provisioned in-region `gen-large` deployment with its own APIM quota, so the judge never competes with RMs.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from pathlib import Path

from rm_copilot.llm import ModelUnavailable
from rm_copilot.ragas import LLMJudgeScorer, evaluate
from services.common.config import StackConfig
from services.common.flags import ConfigValues
from services.common.httpkit import RequestContext, TransportError, run_parallel, set_request_context
from services.common.opsmetrics import (BASELINES_KEY, FINOPS_PREFIX, ONLINE_PREFIX, SCORE_BUCKETS, STAGE_BUDGET_PREFIX, OpsRecorder, alerts, budgets_from,
                                        online_from, prices_from, summarize)
from services.common.service import Service

ONLINE_METRICS = ("faithfulness", "answer_relevancy", "context_precision", "context_utilisation")
METRICS_BY_USE_CASE = {"uc2": ONLINE_METRICS, "uc1": ("faithfulness", "context_utilisation")}  # LLD §17.1 "UC1 and UC3"
WINDOWS = {"1h": 3600.0, "3d": 3 * 86400.0}
MAX_EXTENDED_S = 4 * 3600.0
SCRAPED = ("web-bff", "orchestrator", "tool-gateway")


class JudgeModel:
    """The judge's `LLMClient`: APIM AI gateway -> `gen-large`, with the eval-worker's own workload identity."""

    def __init__(self, svc: "EvalWorker"):
        self.svc = svc

    def complete(self, system: str, user: str) -> str:
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}], "temperature": 0, "max_tokens": 400, "store": False}
        with self.svc.ops.stage("judge"):
            try:
                r = self.svc.client.request("POST", f"{self.svc.cfg.url('apim')}/openai/deployments/gen-large/chat/completions?api-version=2024-10-21",
                                            json_body=body, headers={**self.svc.auth_headers("apim"), "x-use-case": "eval"}, timeout=20.0, peer="apim")
            except TransportError as e:
                raise ModelUnavailable(f"judge call {e.kind}") from None
        if not r.ok:
            raise ModelUnavailable(f"judge call returned {r.status}")
        j = r.json()
        usage = j.get("usage", {})
        self.svc.ops.model_call(self.svc.prices(), "gen-large", "eval-judge", "eval", int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0)))
        return j["choices"][0]["message"]["content"]


class EvalWorker(Service):
    name = "eval-worker"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self.config = ConfigValues(self)
        self.ops = OpsRecorder(self.metrics)
        self.judge = LLMJudgeScorer(JudgeModel(self), embed=self._embed)
        self.ops_dir = Path(cfg.settings.ops_dir or Path(cfg.settings.run_dir) / "ops")
        self._next_seq = 0
        self._window: deque[tuple[float, str, dict]] = deque(maxlen=10000)
        self._day, self._scored_today = "", 0
        self._last_snapshot = 0.0
        self._lock = threading.Lock()
        self.last_summary: dict | None = None
        m = self.metrics
        self.m_score = m.histogram("rmc_online_eval_score", "Online (sampled, judged) evaluation score per item", ("use_case", "metric"),
                                   tuple(b for b in SCORE_BUCKETS if b <= 1.0))
        self.m_mean = m.gauge("rmc_online_eval_window_mean", "Rolling-window mean of the online evaluation", ("use_case", "metric", "window"))
        self.m_alert = m.gauge("rmc_online_eval_alert", "1 when the window breaches its threshold with enough items", ("use_case", "metric", "window"))
        self.m_items = m.counter("rmc_online_eval_items_total", "Online evaluation items by outcome", ("use_case", "outcome"))
        self.m_fallbacks = m.counter("rmc_online_eval_judge_fallbacks_total", "Judge calls that fell back to the lexical scorer", ())
        self.m_ops_alert = m.gauge("rmc_ops_alert", "Runtime operations alert rules (1 = firing)", ("rule", "use_case"))
        for uc in ("uc1", "uc2"):
            for metric, window in (("faithfulness", "1h"), ("answer_relevancy", "1h"), ("faithfulness", "3d")):
                if metric in METRICS_BY_USE_CASE[uc]:
                    self.m_alert.set(0, use_case=uc, metric=metric, window=window)
        self.app.route("GET", "/eval/online")(lambda r: self.online_status())

    def start(self) -> None:
        self.run_loop("online-eval", self.cfg.settings.eval_poll_s, self.tick)
        self.run_loop("ops-alerts", self.cfg.settings.ops_eval_s, self.ops_tick)

    def stop(self) -> None:
        super().stop()
        try:
            self.ops_tick(force_snapshot=True)
        except Exception:  # noqa: BLE001 - the stack may already be down
            pass

    # ------------------------------------------------------------------ config
    def settings(self) -> dict:
        return online_from(self.config.get_prefix(ONLINE_PREFIX))

    def prices(self) -> dict:
        return prices_from(self.config.get_prefix(FINOPS_PREFIX))

    def _embed(self, texts: list[str]) -> list[list[float]]:
        r = self.client.request("POST", f"{self.cfg.url('apim')}/openai/deployments/embed/embeddings?api-version=2024-10-21",
                                json_body={"input": texts, "dimensions": 256}, headers={**self.auth_headers("apim"), "x-use-case": "eval"},
                                timeout=5.0, peer="apim")
        if not r.ok:
            raise ModelUnavailable("embeddings unavailable")
        return [d["embedding"] for d in r.json()["data"]]

    # ------------------------------------------------------------------ Tier 2
    def tick(self) -> int:
        """Read new sampled answers from the audit content store and score them."""
        set_request_context(RequestContext("eval-online"))
        r = self.client.request("GET", f"{self.cfg.url('audit-writer')}/audit/eval-samples?from_seq={self._next_seq}&limit=50",
                                headers=self.auth_headers("audit"), timeout=5.0, peer="audit-writer")
        if not r.ok:
            return 0
        j = r.json()
        for s in j.get("value", []):
            self.ingest(s.get("content") or {}, ts=s.get("ts"))
        self._next_seq = int(j.get("next_seq", self._next_seq))
        return len(j.get("value", []))

    def ingest(self, sample: dict, ts: float | None = None) -> dict | None:
        """Score one sampled answer with the judge (within the daily cap) and refresh the gauges. Returns the scores."""
        use_case = str(sample.get("use_case") or "uc2")
        cfg = self.settings()
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            if day != self._day:
                self._day, self._scored_today = day, 0
            if self._scored_today >= cfg["max_per_day"]:
                self.m_items.inc(use_case=use_case, outcome="skipped_cap")
                return None
            self._scored_today += 1
        before = self.judge.fallbacks
        try:
            with self.telemetry.span("eval.online score", **{"rmc.use_case": use_case}):
                scores = evaluate({**sample, "kind": "brief" if use_case == "uc1" else "answer"}, self.judge, metrics=ONLINE_METRICS)
        except Exception:  # noqa: BLE001 - one bad item never stops the worker
            self.m_items.inc(use_case=use_case, outcome="judge_error")
            return None
        if self.judge.fallbacks > before:
            self.m_fallbacks.inc(self.judge.fallbacks - before)
        scores = {k: v for k, v in scores.items() if k in METRICS_BY_USE_CASE.get(use_case, ONLINE_METRICS)}
        for metric, v in scores.items():
            self.m_score.observe(v, use_case=use_case, metric=metric)
        with self._lock:
            self._window.append((float(ts or time.time()), use_case, scores))
        self.m_items.inc(use_case=use_case, outcome="judged")
        self.publish()
        return scores

    def _rows(self, use_case: str, window: str, min_items: int) -> list[dict]:
        now = time.time()
        with self._lock:
            mine = sorted(((ts, s) for ts, uc, s in self._window if uc == use_case), key=lambda x: x[0], reverse=True)
        rows = [s for ts, s in mine if ts >= now - WINDOWS[window]]
        if window == "1h" and len(rows) < min_items:  # extend back to the `min_items` most recent items, up to 4 h
            rows = [s for ts, s in mine if ts >= now - MAX_EXTENDED_S][:min_items]
        return rows

    def publish(self) -> dict:
        cfg = self.settings()
        out: dict = {"items": {}, "scores": {}, "alerts": []}
        with self._lock:
            use_cases = sorted({uc for _, uc, _ in self._window})
        for uc in use_cases:
            for window in WINDOWS:
                rows = self._rows(uc, window, cfg["min_items"])
                if window == "1h":
                    out["items"][uc] = len(rows)
                for metric in METRICS_BY_USE_CASE.get(uc, ONLINE_METRICS):
                    vals = [s[metric] for s in rows if metric in s]
                    if not vals:
                        continue
                    mean = round(sum(vals) / len(vals), 4)
                    self.m_mean.set(mean, use_case=uc, metric=metric, window=window)
                    if window == "1h":
                        out["scores"].setdefault(metric, {})[uc] = mean
                    threshold = cfg["thresholds"].get((metric, window))
                    if threshold is not None:
                        firing = len(vals) >= cfg["min_items"] and mean < threshold
                        self.m_alert.set(1 if firing else 0, use_case=uc, metric=metric, window=window)
                        out["alerts"].append({"use_case": uc, "metric": metric, "window": window, "mean": mean, "items": len(vals),
                                              "threshold": threshold, "firing": firing})
        return out

    def online_status(self) -> dict:
        return self.publish()

    # ------------------------------------------------------------------ ops alert rules and snapshots
    def scrape(self) -> list[str]:
        targets = {n: self.cfg.url(n) for n in SCRAPED if n in self.cfg.urls}
        got = run_parallel({n: (lambda u=u: self.client.request("GET", f"{u}/metrics", timeout=2.0, peer=n)) for n, u in targets.items()})
        texts = [resp.body.decode() for resp, err in got.values() if err is None and resp.ok]
        return texts + [self.metrics.render()]

    def ops_tick(self, force_snapshot: bool = False) -> dict:
        set_request_context(RequestContext("eval-ops"))
        budgets = budgets_from(self.config.get_prefix(STAGE_BUDGET_PREFIX), self.config.get(BASELINES_KEY))
        summary = summarize(self.scrape(), budgets, self.prices())
        rules = alerts(summary, budgets)
        for a in rules:
            self.m_ops_alert.set(1 if a["firing"] else 0, rule=a["rule"], use_case=a["use_case"])
        summary["alerts"] = rules
        self.last_summary = summary
        now = time.time()
        if force_snapshot or now - self._last_snapshot >= self.cfg.settings.ops_snapshot_s:
            self._last_snapshot = now
            self.snapshot(summary)
        return summary

    def snapshot(self, summary: dict) -> Path:
        """Append the metadata-only summary to `<ops_dir>/snapshots/<UTC date>.jsonl` (drift report input)."""
        folder = self.ops_dir / "snapshots"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (time.strftime("%Y-%m-%d", time.gmtime(summary["generated_at"])) + ".jsonl")
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(summary, sort_keys=True) + "\n")
        return path

