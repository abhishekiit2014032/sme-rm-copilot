"""Load test: fire N mixed requests with C concurrent workers and compare p50/p95 per use case with the plan §5
latency budgets (UC1 8 s, UC2 7 s, UC3 draft 10 s, commit 2 s, verified output end to end).

    python3 loadtest.py                          # starts the stack in-process with realistic mock latencies
    python3 loadtest.py -n 120 -c 16             # more requests, more concurrency
    python3 loadtest.py --url http://127.0.0.1:8712   # against a running stack (web-bff URL)

The mix follows the pilot: briefs and policy questions dominate; every follow-up draft is approved and committed.
Mock latencies (azmock/_base.py REALISTIC_*) are illustrative p50s for in-region private endpoints; the result
shows the orchestration overhead and the budget headroom, not real Azure performance. The local stack runs every
service in one Python process, so CPU-bound work (JWT/ECDSA) competes for one interpreter lock.

Per use case it also reports p95 tokens per request (input and output, from each reply's `ops` metadata) and the
estimated cost per request and in total, from the ILLUSTRATIVE price table in App Configuration (`rmc:finops:*`).
"""
from __future__ import annotations

import argparse
import random
import shutil
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from services.common.config import Settings
from stack import ApiError, ConsoleClient, Stack

BUDGET_S = {"uc1": 8.0, "uc2": 7.0, "uc3": 10.0, "commit": 2.0}
MIX = [("uc1", 0.4), ("uc2", 0.4), ("uc3", 0.2)]
PORTFOLIO = {"layla@bank.example": ["harbourline", "crescent"], "omar@bank.example": ["gulfstar"], "sara@bank.example": ["harbourline"]}
QUESTIONS = ["Can a company trading for 2 years get an unsecured working capital line?", "Who approves an exception for unsecured SME lending of AED 800k?",
             "When does high utilisation trigger a review?", "What documents do I need to open an SME account?", "What is our crypto custody policy?"]
NOTES = ("Met the finance director. I promised to send the limit increase checklist by Friday and to set up an intro with our FX desk next week. "
         "They plan 2 new trucks.")


def pct(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    return s[min(len(s) - 1, max(0, round(p / 100 * len(s) + 0.5) - 1))]


def run(base_client_factory, n: int, concurrency: int, seed: int = 7) -> dict:
    rnd = random.Random(seed)
    clients = {upn: base_client_factory(upn) for upn in PORTFOLIO}
    results: dict[str, list[float]] = {k: [] for k in BUDGET_S}
    tokens: dict[str, list[tuple[int, int]]] = {k: [] for k in BUDGET_S}
    cost: dict[str, list[float]] = {k: [] for k in BUDGET_S}
    gates: dict[str, int] = {}
    errors: dict[str, list[str]] = {k: [] for k in BUDGET_S}
    modes: dict[str, int] = {}
    lock = threading.Lock()

    def timed(kind: str, fn):
        t0 = time.perf_counter()
        try:
            out = fn()
            dt = time.perf_counter() - t0
            with lock:
                results[kind].append(dt)
                mode = out.get("mode") or out.get("status")
                modes[f"{kind}:{mode}"] = modes.get(f"{kind}:{mode}", 0) + 1
                ops = out.get("ops") or {}
                if "tokens" in ops:
                    tokens[kind].append((ops["tokens"].get("input", 0), ops["tokens"].get("output", 0)))
                    cost[kind].append(float(ops.get("cost_aed", 0.0)))
                for gate in (out.get("evaluation") or {}).get("gates", []):
                    key = f"{gate['gate']}:{gate['outcome']}"
                    gates[key] = gates.get(key, 0) + 1
            return out
        except ApiError as e:
            with lock:
                errors[kind].append(f"{e.status} {e.body.get('error', {}).get('code')}")
            return None

    def one(i: int) -> None:
        r = rnd.random()
        upn = rnd.choice(list(PORTFOLIO))
        c, cid = clients[upn], rnd.choice(PORTFOLIO[upn])
        kind = "uc1" if r < MIX[0][1] else ("uc2" if r < MIX[0][1] + MIX[1][1] else "uc3")
        if kind == "uc1":
            timed("uc1", lambda: c.brief(cid))
        elif kind == "uc2":
            timed("uc2", lambda: c.ask(rnd.choice(QUESTIONS)))
        else:
            d = timed("uc3", lambda: c.draft(cid, NOTES))
            if d:
                timed("commit", lambda: c.approve(d))

    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as ex:
        list(ex.map(one, range(n)))
    return {"wall_s": time.perf_counter() - t0, "results": results, "errors": errors, "modes": modes, "tokens": tokens, "cost": cost, "gates": gates}


def report(out: dict, n: int, concurrency: int) -> bool:
    print(f"\n{n} requests · concurrency {concurrency} · wall {out['wall_s']:.1f}s\n")
    print(f"{'use case':<10}{'ok':>5}{'err':>5}{'p50 s':>9}{'p95 s':>9}{'max s':>9}{'budget':>9}  result")
    ok_all = True
    for k, budget in BUDGET_S.items():
        xs = out["results"][k]
        p95 = pct(xs, 95)
        ok = bool(xs) and p95 <= budget and not out["errors"][k]
        ok_all &= ok
        print(f"{k:<10}{len(xs):>5}{len(out['errors'][k]):>5}{pct(xs, 50):>9.2f}{p95:>9.2f}{max(xs) if xs else float('nan'):>9.2f}{budget:>9.1f}  {'PASS' if ok else 'FAIL'}")
    print(f"\n{'use case':<10}{'p95 tok in':>11}{'p95 tok out':>12}{'budget in/out':>15}{'AED/request':>13}{'AED total':>11}   (illustrative prices)")
    for k in BUDGET_S:
        tk = out["tokens"][k]
        if not tk:
            continue
        c = out["cost"][k]
        print(f"{k:<10}{pct([x[0] for x in tk], 95):>11}{pct([x[1] for x in tk], 95):>12}{'12000/1500':>15}{sum(c) / len(c):>13.5f}{sum(c):>11.4f}")
    print("\nOutcomes:", ", ".join(f"{k}={v}" for k, v in sorted(out["modes"].items())))
    print("Runtime gates:", ", ".join(f"{k}={v}" for k, v in sorted(out["gates"].items())))
    for k, errs in out["errors"].items():
        if errs:
            print(f"Errors in {k}: {sorted(set(errs))}")
    return ok_all


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-n", type=int, default=60, help="number of top-level requests")
    ap.add_argument("-c", type=int, default=10, help="concurrent workers")
    ap.add_argument("--url", help="web-bff URL of a running stack (default: start one in-process)")
    ap.add_argument("--latency", choices=("none", "realistic"), default="realistic")
    args = ap.parse_args(argv)
    stack = None
    if args.url:
        factory = lambda upn: ConsoleClient.for_url(args.url, upn).sign_in()  # noqa: E731
    else:
        # Raise the per-RM gateway limits so the test measures latency, not the (separately tested) throttling.
        stack = Stack(Settings.from_env(latency=args.latency, apim_tokens_per_minute=1_000_000, apim_calls_per_minute=10_000,
                                        apim_mcp_rm_calls_per_minute=10_000, apim_mcp_agent_calls_per_minute=10_000)).start()
        print(f"Stack started in-process · latency profile: {args.latency}")
        factory = lambda upn: ConsoleClient(stack, upn).sign_in()  # noqa: E731
    try:
        out = run(factory, args.n, args.c)
    finally:
        if stack:
            stack.stop()
            shutil.rmtree(stack.settings.run_dir, ignore_errors=True)
    return 0 if report(out, args.n, args.c) else 1


if __name__ == "__main__":
    sys.exit(main())
