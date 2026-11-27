"""Diagram box: Evaluation (lane D), the release gate.
Runs the golden set through the whole copilot and blocks the release unless every quality threshold passes.
Mocked: RAGAS-style metrics are computed by the deterministic lexical judge; production uses RAGAS + a judge model.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from statistics import mean

from pydantic import BaseModel

from copilot.contracts import Answer, AskRequest
from copilot.operations.evals.runtime import score_answer
from copilot.operations.flags import Flags
from copilot.wiring import Copilot, build_copilot

GOLDEN_SET = Path(__file__).with_name("golden_set.jsonl")

# Metric -> minimum value. "No pass, no deploy."
THRESHOLDS = {
    "status_accuracy": 1.0,        # answered / declined / blocked / denied as expected
    "content_checks": 1.0,         # must_include and must_not_include phrases
    "faithfulness": 0.9,           # answer sentences supported by the cited clauses
    "answer_relevancy": 0.5,       # answer addresses the question
    "context_precision": 0.8,      # cited clauses that are expected
    "context_recall": 0.9,         # expected clauses that are cited
    "current_version_only": 1.0,   # no citation of a superseded clause
}


class CaseResult(BaseModel):
    case_id: str
    status_ok: bool
    content_ok: bool
    faithfulness: float | None = None
    answer_relevancy: float | None = None
    precision: float | None = None
    recall: float | None = None
    current_only: bool = True
    status: str = ""


class GateReport(BaseModel):
    metrics: dict[str, float]
    passed: bool
    failures: list[str]
    cases: list[CaseResult]


def load_golden_set(path: Path = GOLDEN_SET) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def evaluate_case(copilot: Copilot, case: dict) -> CaseResult:
    answer = copilot.pipeline.ask(copilot.token_for(case["user"]), AskRequest(**case["request"]))
    expect = case["expect"]
    text = answer.text
    result = CaseResult(
        case_id=case["id"], status=answer.status.value,
        status_ok=answer.status.value == expect["status"] and answer.path == expect.get("path", answer.path),
        content_ok=all(p in text for p in expect.get("must_include", []))
        and not any(p in text for p in expect.get("must_not_include", [])),
    )
    if answer.citations:
        _score_grounding(copilot, case, answer, result)
    return result


def _score_grounding(copilot: Copilot, case: dict, answer: Answer, result: CaseResult) -> None:
    cited = {f"{c.doc_id}#{c.clause}" for c in answer.citations}
    expected = set(case["expect"].get("clauses", []))
    current = {(c.doc_id, c.clause): c for c in copilot.retriever.index.all_clauses() if c.status == "current"}
    evidence = [f"{current[k].heading}. {current[k].text}" for c in answer.citations
                if (k := (c.doc_id, c.clause)) in current]
    scores = score_answer(case["request"].get("text", ""), answer.text, evidence)
    result.faithfulness, result.answer_relevancy = scores.faithfulness, scores.answer_relevancy
    result.precision = len(cited & expected) / len(cited) if cited else 0.0
    result.recall = len(cited & expected) / len(expected) if expected else 1.0
    result.current_only = all(current.get((c.doc_id, c.clause)) is not None
                              and current[(c.doc_id, c.clause)].version == c.version for c in answer.citations)


def summarise(cases: list[CaseResult]) -> dict[str, float]:
    grounded = [c for c in cases if c.faithfulness is not None]
    return {
        "status_accuracy": mean(c.status_ok for c in cases),
        "content_checks": mean(c.content_ok for c in cases),
        "faithfulness": mean(c.faithfulness for c in grounded) if grounded else 1.0,
        "answer_relevancy": mean(c.answer_relevancy for c in grounded) if grounded else 1.0,
        "context_precision": mean(c.precision for c in grounded) if grounded else 1.0,
        "context_recall": mean(c.recall for c in grounded) if grounded else 1.0,
        "current_version_only": mean(c.current_only for c in cases),
    }


def run_release_gate() -> GateReport:
    copilot = build_copilot(flags=Flags())   # release candidate configuration, cache off
    cases = [evaluate_case(copilot, case) for case in load_golden_set()]
    metrics = {k: round(float(v), 3) for k, v in summarise(cases).items()}
    failures = [f"{name} {metrics[name]:.3f} < {minimum}" for name, minimum in THRESHOLDS.items()
                if metrics[name] < minimum]
    return GateReport(metrics=metrics, passed=not failures, failures=failures, cases=cases)


def print_report(report: GateReport) -> None:
    print("Release gate: golden set\n")
    print(f"  {'case':<5} {'status':<10} {'ok':<4} {'faith':>6} {'relev':>6} {'prec':>6} {'recall':>6}")
    for c in report.cases:
        ok = "yes" if c.status_ok and c.content_ok and c.current_only else "NO"
        nums = [c.faithfulness, c.answer_relevancy, c.precision, c.recall]
        shown = " ".join(f"{n:>6.2f}" if n is not None else f"{'-':>6}" for n in nums)
        print(f"  {c.case_id:<5} {c.status:<10} {ok:<4} {shown}")
    print("\n  metric                 value   minimum")
    for name, minimum in THRESHOLDS.items():
        mark = "pass" if report.metrics[name] >= minimum else "FAIL"
        print(f"  {name:<22} {report.metrics[name]:>5.3f}   {minimum:>5.2f}  {mark}")
    print("\n  RESULT: " + ("PASS, release may proceed" if report.passed else "FAIL, release blocked: "
                            + "; ".join(report.failures)))


def main() -> int:
    report = run_release_gate()
    print_report(report)
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main())
