"""Diagram box: RM (lane A), the terminal demo.
Runs every scenario and prints the answer and its trace grouped by lane A/B/C/D (make demo, or pass scenario ids).
Mocked: fictional bank, fictional customers, scripted model.
"""
from __future__ import annotations

import sys
import textwrap

from copilot.contracts import LANE_TITLES, Trace
from demo.scenarios import SCENARIOS, ScenarioRun, StepResult, run_scenario

WIDTH = 100


def print_trace(trace: Trace) -> None:
    tokens = int(trace.total("tokens_in") + trace.total("tokens_out"))
    print(f"  trace {trace.trace_id}: {len(trace.spans)} spans, {tokens} tokens, ${trace.total('cost_usd'):.4f}")
    for lane, spans in trace.by_lane().items():
        if not spans:
            continue
        print(f"    {lane.value} {LANE_TITLES[lane]}")
        for s in spans:
            status = "" if s.status == "ok" else f" [{s.status}]"
            line = f"      {s.component.value:<24} {s.name}{status}"
            if s.detail:
                line += f": {s.detail}"
            print(line if len(line) <= WIDTH + 20 else line[: WIDTH + 16] + " ...")


def print_step(number: int, step: StepResult) -> None:
    print(f"\n  Step {number}: {step.label}")
    print(f"  {step.actor} > {step.input}")
    if step.answer:
        a = step.answer
        print(f"  status: {a.status.value} · path: {a.path}")
        print(textwrap.indent(a.text, "  | "))
        if a.citations:
            print("  citations: " + "; ".join(c.label for c in a.citations))
        if a.drafts:
            for d in a.drafts:
                print(f"  draft {d.kind}: {d.title}" + (f" (due {d.due_date})" if d.due_date else ""))
        if a.approval_token:
            print(f"  approval token: {a.approval_token[:24]}... (signed, single use, 5 min)")
        if a.notices:
            print("  notices: " + "; ".join(a.notices))
    if step.event:
        e = step.event
        print(f"  ingested {e.doc_id} versions {', '.join(e.versions)}: {e.clauses_indexed} clauses, "
              f"cache entries invalidated: {e.cache_entries_invalidated}")
    print_trace(step.trace)


def print_run(run: ScenarioRun) -> None:
    print("\n" + "=" * WIDTH)
    print(f"{run.title}\n{run.summary}")
    print("=" * WIDTH)
    for number, step in enumerate(run.steps, start=1):
        print_step(number, step)


def main(ids: list[str]) -> None:
    for scenario in SCENARIOS:
        if not ids or scenario.id in ids:
            print_run(run_scenario(scenario.id))


if __name__ == "__main__":
    main(sys.argv[1:])
