"""Runtime operations metrics (plan §3.9, §5; LLD §11.4, §11.8): recorded on every request, metadata only.

Recorded (Prometheus text per service; Azure Monitor managed Prometheus scrapes them in production):
  rmc_request_duration_seconds{use_case,outcome}                web-bff, end to end
  rmc_stage_duration_seconds{use_case,stage,call}               edge (web-bff), entitlement (tool-gateway), tool_fanout,
                                                                retrieval, pii, prompt_shields, retrieval_gate,
                                                                model (call = prompt template id), post_gates
                                                                (orchestrator), judge (eval-worker)
  rmc_tokens_per_request{use_case,type}                         input | output, chat models, once per request
  rmc_llm_tokens_total{deployment,use_case,agent,type}          per model call (use_case "eval" for the judge)
  rmc_cost_aed_total{use_case,deployment,component}             estimated from the price table in App Configuration
  rmc_cost_aed_today{use_case}                                  prototype: the same since 00:00 UTC (no PromQL here)
  rmc_runtime_gate_total{use_case,gate,outcome}                 Tier 1 gate evaluations
  rmc_runtime_gate_score{use_case,gate}                         score per gate evaluation
  rmc_budget_exceeded_total{use_case,budget,action}             request_input_tokens, request_output_tokens,
                                                                tool_steps, hops, rm_daily, deadline
  rmc_rm_feedback_total{use_case,rating,reason}                 RM thumbs up / down (web-bff)
  rmc_ops_alert{rule,use_case}                                  in-process alert rules (eval-worker; /api/ops/summary)

The price table (`rmc:finops:*`, LLD §11.8.3) is ILLUSTRATIVE in the prototype: round AED numbers per 1,000 tokens,
not a quote and not Azure list prices; finance owns the real one. Alert baselines (`rmc:ops:baselines`) stand in for
the 7-day Prometheus baselines of LLD §11.8.5; end-to-end budgets are `rmc:orchestrator:stage_budget_ms:<uc>:end_to_end`.
"""
from __future__ import annotations

import math
import re
import threading
import time
from contextlib import contextmanager
from typing import Iterator

from .httpkit import request_context
from .telemetry import Metrics

FINOPS_PREFIX = "rmc:finops:"
STAGE_BUDGET_PREFIX = "rmc:orchestrator:stage_budget_ms:"
BASELINES_KEY = "rmc:ops:baselines"
ONLINE_PREFIX = "rmc:eval:online:"

FINOPS = {  # ILLUSTRATIVE prototype values (published by stack.py as the pipeline would)
    "rmc:finops:price_table_version": "illustrative-2026-11",
    "rmc:finops:illustrative": True,
    "rmc:finops:price:gen-large:input_per_1k_aed": 0.0092, "rmc:finops:price:gen-large:output_per_1k_aed": 0.0367,
    "rmc:finops:price:gen-small:input_per_1k_aed": 0.0006, "rmc:finops:price:gen-small:output_per_1k_aed": 0.0022,
    "rmc:finops:price:embed:per_1k_aed": 0.0005,
    "rmc:finops:daily_budget_aed": 250.0,        # user traffic (~5,400 requests/day at scale)
    "rmc:finops:daily_budget_aed:eval": 25.0,    # online judge
}
STAGE_BUDGETS = {f"{STAGE_BUDGET_PREFIX}{uc}:end_to_end": ms for uc, ms in (("uc1", 8000), ("uc2", 7000), ("uc3", 10000), ("commit", 2000))}
BASELINES = {  # stand-in for the 7-day baselines (illustrative; the drift job refreshes them)
    "tokens_input_p95": {"uc1": 260, "uc2": 520, "uc3": 180},
    "retrieval_decline_rate": 0.2,
    "min_requests": 20,
}
ONLINE = {  # LLD §17.3 keys
    "rmc:eval:online:sample_rate": 0.05, "rmc:eval:online:max_per_day": 200, "rmc:eval:online:min_items_window": 10,
    "rmc:eval:online:thresholds:faithfulness_1h": 0.90, "rmc:eval:online:thresholds:answer_relevancy_1h": 0.80,
    "rmc:eval:online:thresholds:faithfulness_3d": 0.93,
}
ALERT_RATIOS = {"tokens": 1.2, "cost": 0.8, "decline_points": 0.05, "faithfulness_failures": 0.05}

LATENCY_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0,
                   12.0, 15.0, 20.0)
TOKEN_BUCKETS = (25, 50, 100, 150, 200, 250, 300, 400, 500, 750, 1000, 2000, 4000, 6000, 8000, 10000, 12000, 14000, 16000)
SCORE_BUCKETS = (0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0)
GATE_SCORES = {"retrieval": ("top_reranker", "context_relevance"), "faithfulness": ("faithfulness",), "answer_relevancy": ("answer_relevancy",)}


# --------------------------------------------------------------------------- configuration views
def prices_from(values: dict | None) -> dict:
    """`rmc:finops:*` key-values (defaults where missing) -> {version, per_1k: {dep: (in, out)}, budgets}."""
    v = {**FINOPS, **(values or {})}
    per = {}
    for dep in ("gen-large", "gen-small"):
        per[dep] = (float(v.get(f"{FINOPS_PREFIX}price:{dep}:input_per_1k_aed", 0.0)), float(v.get(f"{FINOPS_PREFIX}price:{dep}:output_per_1k_aed", 0.0)))
    per["embed"] = (float(v.get(f"{FINOPS_PREFIX}price:embed:per_1k_aed", 0.0)), 0.0)
    return {"version": str(v.get(f"{FINOPS_PREFIX}price_table_version")), "illustrative": bool(v.get(f"{FINOPS_PREFIX}illustrative")), "per_1k": per,
            "daily_budget_aed": float(v.get(f"{FINOPS_PREFIX}daily_budget_aed", 0.0)),
            "daily_budget_aed_eval": float(v.get(f"{FINOPS_PREFIX}daily_budget_aed:eval", 0.0))}


def online_from(values: dict | None) -> dict:
    v = {**ONLINE, **(values or {})}
    p = ONLINE_PREFIX
    return {"sample_rate": float(v[p + "sample_rate"]), "max_per_day": int(v[p + "max_per_day"]), "min_items": int(v[p + "min_items_window"]),
            "thresholds": {("faithfulness", "1h"): float(v[p + "thresholds:faithfulness_1h"]),
                           ("answer_relevancy", "1h"): float(v[p + "thresholds:answer_relevancy_1h"]),
                           ("faithfulness", "3d"): float(v[p + "thresholds:faithfulness_3d"])}}


def budgets_from(stage_budgets: dict | None, baselines: object = None) -> dict:
    sb = {**STAGE_BUDGETS, **(stage_budgets or {})}
    latency = {k[len(STAGE_BUDGET_PREFIX):].split(":")[0]: float(ms) / 1000 for k, ms in sb.items() if k.endswith(":end_to_end")}
    base = {**BASELINES, **(baselines if isinstance(baselines, dict) else {})}
    return {"latency_p95_s": latency, "tokens_input_p95": dict(base["tokens_input_p95"]), "retrieval_decline_rate": float(base["retrieval_decline_rate"]),
            "min_requests": int(base["min_requests"])}


def cost_aed(prices: dict, deployment: str, input_tokens: int, output_tokens: int) -> float:
    pin, pout = prices["per_1k"].get(deployment, (0.0, 0.0))
    return (input_tokens * pin + output_tokens * pout) / 1000.0


# --------------------------------------------------------------------------- recording
class OpsRecorder:
    """The runtime operations metrics of one service. Per-request values also go to the request context
    (`stages`, `cost_aed`) so the reply can carry them as metadata."""

    def __init__(self, metrics: Metrics):
        m = metrics
        self.request_seconds = m.histogram("rmc_request_duration_seconds", "End-to-end request duration at web-bff", ("use_case", "outcome"), LATENCY_BUCKETS)
        self.stage_seconds = m.histogram("rmc_stage_duration_seconds", "Duration of one stage of a request", ("use_case", "stage", "call"), LATENCY_BUCKETS)
        self.tokens_request = m.histogram("rmc_tokens_per_request", "Chat-model tokens per request", ("use_case", "type"), TOKEN_BUCKETS)
        self.tokens = m.counter("rmc_llm_tokens_total", "Model tokens per call", ("deployment", "use_case", "agent", "type"))
        self.cost = m.counter("rmc_cost_aed_total", "Estimated cost in AED (illustrative price table)", ("use_case", "deployment", "component"))
        self.cost_today = m.gauge("rmc_cost_aed_today", "Estimated cost in AED since 00:00 UTC (illustrative price table)", ("use_case",))
        self.gates = m.counter("rmc_runtime_gate_total", "Tier 1 runtime gate evaluations", ("use_case", "gate", "outcome"))
        self.gate_score = m.histogram("rmc_runtime_gate_score", "Score per runtime gate evaluation", ("use_case", "gate"), SCORE_BUCKETS)
        self.budget = m.counter("rmc_budget_exceeded_total", "Requests stopped by a budget guard", ("use_case", "budget", "action"))
        self._day = ""
        self._today: dict[str, float] = {}
        self._lock = threading.Lock()

    @contextmanager
    def stage(self, name: str, call: str = "-", use_case: str | None = None) -> Iterator[None]:
        data = request_context().data
        uc = use_case or str(data.get("use_case") or "-")
        t0 = time.perf_counter()
        try:
            yield
        finally:
            dt = time.perf_counter() - t0
            self.stage_seconds.observe(dt, use_case=uc, stage=name, call=call)
            key = name if call == "-" else f"{name}.{call}"
            stages = data.setdefault("stages", {})
            stages[key] = round(stages.get(key, 0.0) + dt * 1000, 1)

    def model_call(self, prices: dict, deployment: str, agent: str, use_case: str, input_tokens: int, output_tokens: int) -> float:
        self.tokens.inc(input_tokens, deployment=deployment, use_case=use_case, agent=agent, type="input")
        self.tokens.inc(output_tokens, deployment=deployment, use_case=use_case, agent=agent, type="output")
        aed = cost_aed(prices, deployment, input_tokens, output_tokens)
        self.add_cost(use_case, deployment, "model" if deployment != "embed" else "embed", aed)
        data = request_context().data
        data["cost_aed"] = data.get("cost_aed", 0.0) + aed
        return aed

    def add_cost(self, use_case: str, deployment: str, component: str, aed: float) -> None:
        self.cost.inc(aed, use_case=use_case, deployment=deployment, component=component)
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            if day != self._day:
                self._day, self._today = day, {}
                with self.cost_today._lock:
                    self.cost_today.values.clear()
            self._today[use_case] = self._today.get(use_case, 0.0) + aed
            value = self._today[use_case]
        self.cost_today.set(round(value, 6), use_case=use_case)

    def request_tokens(self, use_case: str, input_tokens: int, output_tokens: int) -> None:
        self.tokens_request.observe(input_tokens, use_case=use_case, type="input")
        self.tokens_request.observe(output_tokens, use_case=use_case, type="output")

    def gate(self, use_case: str, gate: str, outcome: str, scores: dict) -> None:
        self.gates.inc(use_case=use_case, gate=gate, outcome=outcome)
        for name in GATE_SCORES.get(gate, ()):
            if isinstance(scores.get(name), (int, float)):
                self.gate_score.observe(float(scores[name]), use_case=use_case, gate=name)


# --------------------------------------------------------------------------- reading (a PromQL stand-in)
_LINE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})? (-?[0-9.eE+-]+|NaN|\+Inf|-Inf)$')
_LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse(text: str) -> dict[str, list[tuple[dict, float]]]:
    out: dict[str, list[tuple[dict, float]]] = {}
    for ln in text.splitlines():
        m = _LINE.match(ln.strip())
        if not m:
            continue
        labels = {k: v.replace('\\"', '"').replace("\\\\", "\\") for k, v in _LABEL.findall(m.group(2) or "")}
        out.setdefault(m.group(1), []).append((labels, float(m.group(3))))
    return out


def merge(parsed: list[dict]) -> dict[str, list[tuple[dict, float]]]:
    out: dict[str, list[tuple[dict, float]]] = {}
    for p in parsed:
        for k, v in p.items():
            out.setdefault(k, []).extend(v)
    return out


def histogram_quantile(q: float, buckets: list[tuple[float, float]]) -> float:
    """PromQL `histogram_quantile`: linear interpolation inside the bucket that holds the q-th observation.
    `buckets` = [(upper bound, cumulative count)], +Inf included. NaN without observations."""
    b = sorted(buckets)
    if not b or b[-1][1] <= 0:
        return float("nan")
    rank = q * b[-1][1]
    prev_le, prev_c = 0.0, 0.0
    for le, c in b:
        if c >= rank:
            if math.isinf(le):
                return prev_le  # in the +Inf bucket: the highest finite bound, as Prometheus does
            return prev_le + (le - prev_le) * ((rank - prev_c) / (c - prev_c) if c > prev_c else 1.0)
        prev_le, prev_c = le, c
    return prev_le


def bucket_series(m: dict, name: str, by: tuple[str, ...], where: dict | None = None) -> dict[tuple, list[tuple[float, float]]]:
    """Histogram buckets grouped by the `by` labels, summed over every other label (and service)."""
    acc: dict[tuple, dict[float, float]] = {}
    for labels, v in m.get(name + "_bucket", []):
        if where and any(labels.get(k) != x for k, x in where.items()):
            continue
        key = tuple(labels.get(x, "") for x in by)
        le = float("inf") if labels.get("le") == "+Inf" else float(labels.get("le", "inf"))
        acc.setdefault(key, {})
        acc[key][le] = acc[key].get(le, 0.0) + v
    return {k: sorted(v.items()) for k, v in acc.items()}


def counter_sum(m: dict, name: str, by: tuple[str, ...] = ()) -> dict[tuple, float]:
    out: dict[tuple, float] = {}
    for labels, v in m.get(name, []):
        key = tuple(labels.get(x, "") for x in by)
        out[key] = out.get(key, 0.0) + v
    return out


def _q(b: list[tuple[float, float]], *qs: float) -> dict:
    return {f"p{int(q * 100)}": (round(histogram_quantile(q, b), 3) if b and b[-1][1] else None) for q in qs}


def summarize(texts: list[str], budgets: dict | None = None, prices: dict | None = None) -> dict:
    """The operator view computed from the scraped `/metrics` of web-bff, orchestrator, tool-gateway and eval-worker."""
    budgets = budgets or budgets_from(None)
    prices = prices or prices_from(None)
    m = merge([parse(t) for t in texts])
    latency = {}
    for (uc,), b in sorted(bucket_series(m, "rmc_request_duration_seconds", ("use_case",)).items()):
        latency[uc] = {**_q(b, 0.5, 0.95, 0.99), "count": int(b[-1][1]), "budget_p95_s": budgets["latency_p95_s"].get(uc)}
    stages = {}
    for (st, call), b in sorted(bucket_series(m, "rmc_stage_duration_seconds", ("stage", "call")).items()):
        stages[st if call in ("", "-") else f"{st}.{call}"] = {**_q(b, 0.5, 0.95, 0.99), "count": int(b[-1][1])}
    stages_by_uc: dict = {}
    for (uc, st, call), b in sorted(bucket_series(m, "rmc_stage_duration_seconds", ("use_case", "stage", "call")).items()):
        stages_by_uc.setdefault(uc, {})[st if call in ("", "-") else f"{st}.{call}"] = _q(b, 0.95)["p95"]
    tokens: dict = {}
    for (uc, typ), b in sorted(bucket_series(m, "rmc_tokens_per_request", ("use_case", "type")).items()):
        tokens.setdefault(uc, {})[typ] = {**_q(b, 0.5, 0.95), "count": int(b[-1][1])}
    cost_today = {uc: round(v, 4) for (uc,), v in counter_sum(m, "rmc_cost_aed_today", ("use_case",)).items()}
    user_today = round(sum(v for k, v in cost_today.items() if k != "eval"), 4)
    gates: dict = {}
    for (uc, gate, outcome), v in counter_sum(m, "rmc_runtime_gate_total", ("use_case", "gate", "outcome")).items():
        gates.setdefault(uc, {}).setdefault(gate, {})[outcome] = int(v)
    retrieval = {k: v for (g, k), v in counter_sum(m, "rmc_runtime_gate_total", ("gate", "outcome")).items() if g == "retrieval"}
    faith = {k: v for (g, k), v in counter_sum(m, "rmc_runtime_gate_total", ("gate", "outcome")).items() if g == "faithfulness"}
    n_ret, n_faith = sum(retrieval.values()), sum(faith.values())
    online: dict = {}
    for labels, v in m.get("rmc_online_eval_window_mean", []):
        if labels.get("window") == "1h":
            online.setdefault(labels.get("use_case", ""), {})[labels.get("metric", "")] = round(v, 3)
    items = {f"{uc}:{o}": int(v) for (uc, o), v in counter_sum(m, "rmc_online_eval_items_total", ("use_case", "outcome")).items()}
    return {
        "generated_at": int(time.time()),
        "window": "since process start",
        "price_table_version": prices["version"],
        "latency_s": latency,
        "stages_s": stages,
        "stages_p95_s_by_use_case": stages_by_uc,
        "tokens_per_request": tokens,
        "cost_aed": {"today": cost_today, "user_traffic_today": user_today, "eval_today": round(cost_today.get("eval", 0.0), 4),
                     "daily_budget": prices["daily_budget_aed"], "daily_budget_eval": prices["daily_budget_aed_eval"],
                     "share_of_budget": round(user_today / prices["daily_budget_aed"], 4) if prices["daily_budget_aed"] else None,
                     "illustrative_prices": prices["illustrative"]},
        "runtime_gates": gates,
        "retrieval_decline_rate": round(retrieval.get("declined", 0) / n_ret, 4) if n_ret else None,
        "retrieval_requests": int(n_ret),
        "faithfulness_gate_failure_rate": round((n_faith - faith.get("passed", 0)) / n_faith, 4) if n_faith else None,
        "faithfulness_evaluations": int(n_faith),
        "budget_exceeded": {f"{uc}:{b}": int(v) for (uc, b), v in counter_sum(m, "rmc_budget_exceeded_total", ("use_case", "budget")).items()},
        "rm_feedback": {f"{uc}:{r}": int(v) for (uc, r), v in counter_sum(m, "rmc_rm_feedback_total", ("use_case", "rating")).items()},
        "online_eval": online,
        "online_eval_items": items,
    }


def alerts(summary: dict, budgets: dict | None = None) -> list[dict]:
    """Simple in-process alert rules (plan §3.9, LLD §11.8.5). Production: Azure Monitor alert rules on managed
    Prometheus with the sustain windows (15 min, 1 h) and 7-day baselines; here each rule is evaluated on the counters
    since the process started and the baselines come from `rmc:ops:baselines`."""
    b = budgets or budgets_from(None)
    out = []
    n_min = b["min_requests"]
    for uc, lat in summary["latency_s"].items():
        budget = b["latency_p95_s"].get(uc)
        if budget is not None and lat.get("p95") is not None:
            out.append({"rule": "RmcRequestLatencyP95", "use_case": uc, "value": lat["p95"], "threshold": budget,
                        "firing": lat["count"] >= n_min and lat["p95"] > budget})
    for uc, d in summary["tokens_per_request"].items():
        base = b["tokens_input_p95"].get(uc)
        tin = d.get("input") or {}
        if base and tin.get("p95") is not None:
            thr = round(base * ALERT_RATIOS["tokens"], 1)
            out.append({"rule": "RmcTokensPerRequestCreep", "use_case": uc, "value": tin["p95"], "threshold": thr,
                        "firing": tin["count"] >= n_min and tin["p95"] > thr})
    cost = summary["cost_aed"]
    out.append({"rule": "RmcDailyCost80", "use_case": "all", "value": cost["user_traffic_today"],
                "threshold": round(cost["daily_budget"] * ALERT_RATIOS["cost"], 2),
                "firing": cost["user_traffic_today"] > cost["daily_budget"] * ALERT_RATIOS["cost"]})
    out.append({"rule": "RmcEvalCost80", "use_case": "eval", "value": cost["eval_today"], "threshold": round(cost["daily_budget_eval"] * ALERT_RATIOS["cost"], 2),
                "firing": cost["eval_today"] > cost["daily_budget_eval"] * ALERT_RATIOS["cost"]})
    rate = summary["retrieval_decline_rate"]
    if rate is not None:
        thr = b["retrieval_decline_rate"] + ALERT_RATIOS["decline_points"]
        out.append({"rule": "RmcRetrievalGateDeclineRise", "use_case": "uc2", "value": rate, "threshold": round(thr, 4),
                    "firing": summary["retrieval_requests"] >= n_min and rate > thr})
    ff = summary["faithfulness_gate_failure_rate"]
    if ff is not None:
        out.append({"rule": "RmcFaithfulnessGateFailures", "use_case": "uc2", "value": ff, "threshold": ALERT_RATIOS["faithfulness_failures"],
                    "firing": summary["faithfulness_evaluations"] >= n_min and ff > ALERT_RATIOS["faithfulness_failures"]})
    return out
