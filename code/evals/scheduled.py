"""Tier 3 scheduled evaluation (plan §3.9; LLD §17.4): the nightly golden-set regression and the drift report.

    python3 -m evals.scheduled --nightly                 # golden set in-process; JSON + Markdown report
    python3 -m evals.scheduled --nightly --stack         # ... against a local stack started in-process (HTTP, real gates)
    python3 -m evals.scheduled --nightly --url http://127.0.0.1:8712   # ... against a running stack (web-bff URL)
    python3 -m evals.scheduled --nightly --judge         # score with the LLM-judge interface (mock judge offline)
    python3 -m evals.scheduled --drift [--days 7]        # compare the last N days of runtime metrics snapshots

The nightly report has every RAGAS metric with references (faithfulness, answer relevancy, context precision, context
recall, context utilisation, answer correctness) plus citation accuracy, correct-decline, the security counts and the
replayed (or measured) p95 latency and tokens, with the release-gate verdicts; a regression is what opens an incident
in production. Reports go to `--out` (default `$RMC_EVAL_REPORT_DIR` or `<temp>/rmc-eval-reports`).

The drift report reads the metadata-only snapshots the eval-worker writes to `<ops_dir>/snapshots/<date>.jsonl`
(default `$RMC_OPS_DIR` or `<temp>/rmc-ops`, the stack's default) and compares the latest day with the mean of the
earlier days: decline rate, gate failure rate, p95 latency and p95 input tokens per request.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from evals.run_evals import THRESHOLDS, InProcess, Stack, load, print_table, run, verdicts
from rm_copilot import ragas
from rm_copilot.llm import MockLLM

DRIFT_RULES = {  # metric -> (kind, threshold): "points" = absolute rise, "ratio" = latest / baseline
    "retrieval_decline_rate": ("points", 0.05), "faithfulness_gate_failure_rate": ("points", 0.05),
    "latency_p95_s": ("ratio", 1.2), "tokens_input_p95": ("ratio", 1.2),
}


def default_dir(env: str, name: str) -> Path:
    return Path(os.environ.get(env) or Path(tempfile.gettempdir()) / name)


# --------------------------------------------------------------------------- nightly
def nightly_markdown(result: dict, ok: dict, started: str) -> str:
    i, m = result["info"], result["metrics"]
    lines = [f"# Nightly evaluation report ({started} UTC)", "",
             f"Golden set {result['cases']} cases · model {result['model']} · {i['executor']} · scorer {i['scorer']} · gates {i['gate_version']}",
             f"**Release thresholds: {'all hold' if all(ok.values()) else 'REGRESSION: ' + ', '.join(k for k, v in ok.items() if not v)}**", "",
             "## RAGAS metrics (with references)", "", "| Metric | Value | Bound | n | Threshold | Result |", "|---|---|---|---|---|---|"]
    for k in ("faithfulness", "answer_relevancy", "context_precision", "context_recall", "answer_correctness", "citation_accuracy", "correct_decline"):
        v, (thr, kind) = m[k], THRESHOLDS[k]
        lines.append(f"| {k} ({kind}) | {v['value']:.3f} | {v.get('bound', 0):.3f} | {v.get('n', '')} | {thr} | {'PASS' if ok[k] else 'FAIL'} |")
    lines.append(f"| context_utilisation (mean, no threshold) | {i['context_utilisation']} | - | - | - | info |")
    lines += ["", "## Security, latency and tokens", "", "| Metric | Value | Threshold | Result |", "|---|---|---|---|"]
    for k in ("entitlement_leaks", "numeric_mismatches", "injection_successes", "latency_p95_uc1_s", "latency_p95_uc2_s", "tokens_in_p95", "tokens_out_p95"):
        lines.append(f"| {k} | {m[k]['value']} | {THRESHOLDS[k][0]} | {'PASS' if ok[k] else 'FAIL'} |")
    lines += ["", "## By category (UC2)", "", "| Category | Items | Answered | Mean faithfulness | Mean answer correctness |", "|---|---|---|---|---|"]
    cats: dict = {}
    for it in result["items"]:
        if it.get("category"):
            cats.setdefault(it["category"], []).append(it)
    for cat, its in sorted(cats.items()):
        ans = [x for x in its if x["status"] == "answered"]
        mf = [x["faithfulness"] for x in ans if "faithfulness" in x]
        ac = [x["answer_correctness"] for x in ans if "answer_correctness" in x]
        lines.append(f"| {cat} | {len(its)} | {len(ans)} | {round(sum(mf) / len(mf), 3) if mf else '-'} | {round(sum(ac) / len(ac), 3) if ac else '-'} |")
    lines += ["", f"Info: false-decline rate {i['false_decline_rate']}; model faithfulness before the runtime gates {i['model_faithfulness_first_attempt']}; "
              f"UC1 faithfulness {i['uc1_faithfulness']}.", "", f"## Items to review ({len(result['failures'])})", ""]
    lines += [f"- `{cid}`: {why}" for cid, why in result["failures"]] or ["- none"]
    lines += ["", "Metadata only: ids, scores and reasons; no question, answer or passage text. Lexical metrics stand in for the RAGAS "
              "judge metrics; see README \"Evaluation\"."]
    return "\n".join(lines) + "\n"


def nightly(executor=None, judge: bool = False, out_dir: Path | None = None) -> tuple[dict, Path, Path]:
    started = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime())
    scorer = ragas.LLMJudgeScorer(MockLLM()) if judge else ragas.LexicalScorer()
    result = run(scorer=scorer, executor=executor or InProcess(MockLLM()), cases=load())
    ok = verdicts(result)
    out_dir = out_dir or default_dir("RMC_EVAL_REPORT_DIR", "rmc-eval-reports")
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = "nightly-" + started.replace(":", "").replace("-", "")
    jpath, mpath = out_dir / f"{stem}.json", out_dir / f"{stem}.md"
    report = {"suite": "nightly", "started": started, "verdicts": ok, "passed": all(ok.values()),
              **{k: result[k] for k in ("metrics", "info", "cases", "model", "failures", "items")}}
    jpath.write_text(json.dumps(report, indent=1))
    mpath.write_text(nightly_markdown(result, ok, started))
    return report, jpath, mpath


# --------------------------------------------------------------------------- drift
def _day_values(snapshot: dict) -> dict:
    v = {"retrieval_decline_rate": snapshot.get("retrieval_decline_rate"), "faithfulness_gate_failure_rate": snapshot.get("faithfulness_gate_failure_rate")}
    for uc, lat in (snapshot.get("latency_s") or {}).items():
        v[f"latency_p95_s:{uc}"] = lat.get("p95")
    for uc, tok in (snapshot.get("tokens_per_request") or {}).items():
        v[f"tokens_input_p95:{uc}"] = (tok.get("input") or {}).get("p95")
    return {k: x for k, x in v.items() if isinstance(x, (int, float))}


def drift(ops_dir: Path | None = None, days: int = 7) -> dict:
    ops_dir = ops_dir or default_dir("RMC_OPS_DIR", "rmc-ops")
    files = sorted((ops_dir / "snapshots").glob("*.jsonl"))[-days:]
    per_day = {}
    for f in files:
        rows = [json.loads(x) for x in f.read_text().splitlines() if x.strip()]
        if rows:
            per_day[f.stem] = _day_values(rows[-1])  # the day's last snapshot
    out = {"ops_dir": str(ops_dir), "days": list(per_day), "latest": None, "baseline_days": [], "shifts": [], "alerts": []}
    if not per_day:
        out["note"] = "no snapshots yet: run the stack (the eval-worker writes one every ops_snapshot_s)"
        return out
    latest_day = list(per_day)[-1]
    base_days = list(per_day)[:-1]
    out.update(latest=latest_day, baseline_days=base_days, latest_values=per_day[latest_day])
    if not base_days:
        out["note"] = "only one day of snapshots: no baseline yet"
        return out
    for key, value in sorted(per_day[latest_day].items()):
        metric = key.split(":")[0]
        base_vals = [per_day[d][key] for d in base_days if key in per_day[d]]
        if not base_vals or metric not in DRIFT_RULES:
            continue
        base = sum(base_vals) / len(base_vals)
        kind, thr = DRIFT_RULES[metric]
        shift = value - base if kind == "points" else (value / base if base else float("inf"))
        firing = shift > thr
        row = {"metric": key, "latest": round(value, 4), "baseline": round(base, 4), "shift": round(shift, 4), "rule": f"{kind} > {thr}", "drift": firing}
        out["shifts"].append(row)
        if firing:
            out["alerts"].append(key)
    return out


def drift_markdown(d: dict) -> str:
    lines = [f"# Drift report: latest {d['latest']} vs baseline {', '.join(d['baseline_days']) or 'none'}", ""]
    if d.get("note"):
        lines.append(d["note"])
    lines += ["| Metric | Latest | Baseline (mean) | Shift | Rule | Drift |", "|---|---|---|---|---|---|"]
    lines += [f"| {r['metric']} | {r['latest']} | {r['baseline']} | {r['shift']} | {r['rule']} | {'YES' if r['drift'] else 'no'} |" for r in d["shifts"]]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Tier 3 scheduled evaluation: nightly golden set and drift report.")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--nightly", action="store_true")
    mode.add_argument("--drift", action="store_true")
    ap.add_argument("--stack", action="store_true", help="nightly: start a local stack in-process and run through web-bff")
    ap.add_argument("--url", help="nightly: web-bff URL of a running stack")
    ap.add_argument("--judge", action="store_true", help="nightly: score with the LLM-judge interface (mock judge offline)")
    ap.add_argument("--out", help="report directory")
    ap.add_argument("--ops-dir", help="drift: snapshot directory (default: the stack's ops dir)")
    ap.add_argument("--days", type=int, default=7)
    args = ap.parse_args(argv)
    out_dir = Path(args.out) if args.out else None
    if args.drift:
        d = drift(Path(args.ops_dir) if args.ops_dir else None, args.days)
        md = drift_markdown(d)
        target = out_dir or default_dir("RMC_EVAL_REPORT_DIR", "rmc-eval-reports")
        target.mkdir(parents=True, exist_ok=True)
        stem = "drift-" + time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        (target / f"{stem}.json").write_text(json.dumps(d, indent=1))
        (target / f"{stem}.md").write_text(md)
        print(md)
        print(f"Drift report: {target / (stem + '.json')} and .md · {len(d['alerts'])} drift alert(s)")
        return 0
    stack = None
    executor = None
    if args.url or args.stack:
        from stack import ConsoleClient
        if args.url:
            executor = Stack(lambda upn: ConsoleClient.for_url(args.url, upn).sign_in())
        else:
            from services.common.config import Settings
            from stack import Stack as LocalStack
            stack = LocalStack(Settings.from_env(apim_tokens_per_minute=1_000_000, apim_calls_per_minute=10_000, apim_mcp_rm_calls_per_minute=10_000,
                                                 apim_mcp_agent_calls_per_minute=10_000)).start()
            executor = Stack(lambda upn: ConsoleClient(stack, upn).sign_in())
    try:
        report, jpath, mpath = nightly(executor, args.judge, out_dir)
    finally:
        if stack:
            stack.stop()
            shutil.rmtree(stack.settings.run_dir, ignore_errors=True)
    print_table({**report, "items": report["items"]})
    print(f"\nNightly report: {jpath}\n                {mpath}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
