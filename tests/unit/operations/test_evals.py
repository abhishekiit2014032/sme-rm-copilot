"""Diagram box: Evaluation (lane D).
Proves runtime scoring separates supported from unsupported answers and is recorded on every cited answer,
and that the release gate passes on the golden set but blocks a release whose model gives broken answers.
"""
from __future__ import annotations

from copilot.operations.evals import release_gate
from copilot.operations.evals.runtime import score_answer
from copilot.wiring import build_copilot
from helpers import FEE_QUESTION, ask
from mocks.llm.scripted_llm import ScriptedLlm

CLAUSE = "Each amendment to a letter of credit costs AED 350."


def test_supported_answer_scores_full_faithfulness_and_good_relevancy():
    scores = score_answer(FEE_QUESTION, "Each amendment to a letter of credit costs AED 350 (§1.3).", [CLAUSE])
    assert scores.faithfulness == 1.0 and scores.passed


def test_unsupported_sentence_lowers_faithfulness_below_threshold():
    answer = "Each amendment to a letter of credit costs AED 350. Weekend courier surcharges double every invoice."
    scores = score_answer(FEE_QUESTION, answer, [CLAUSE])
    assert scores.faithfulness == 0.5 and not scores.passed


def test_off_topic_answer_has_low_relevancy():
    scores = score_answer(FEE_QUESTION, "Business fixed deposits need a minimum amount.", [CLAUSE])
    assert scores.answer_relevancy < 0.3


def test_runtime_scores_are_recorded_in_the_trace_of_a_cited_answer(copilot):
    trace = ask(copilot, "rm-1", FEE_QUESTION).trace
    event = next(s for s in trace.spans if s.name == "runtime scores")
    assert event.lane.value == "D" and event.attrs["faithfulness"] >= 0.9


def test_golden_set_covers_answers_declines_blocks_and_denials():
    statuses = {case["expect"]["status"] for case in release_gate.load_golden_set()}
    assert statuses >= {"answered", "declined", "blocked", "denied"}


def test_release_gate_passes_on_the_golden_set():
    report = release_gate.run_release_gate()
    assert report.passed, report.failures
    assert all(report.metrics[name] >= minimum for name, minimum in release_gate.THRESHOLDS.items())


class BrokenPolicyModel(ScriptedLlm):
    """A deliberately broken release candidate: policy answers state a stale cap with no support."""

    def _policy_answer(self, v):
        top = v["clauses"][0]
        return {"outcome": "yes", "answer": f"Unsecured lending up to AED 1.5m is always fine ({top['ref_label']}).",
                "cited_refs": [top["ref"]]}


def test_release_gate_blocks_a_release_with_broken_answers(monkeypatch):
    monkeypatch.setattr(release_gate, "build_copilot", lambda flags: build_copilot(flags=flags,
                                                                                   llm=BrokenPolicyModel()))
    report = release_gate.run_release_gate()
    assert not report.passed
    assert any(f.startswith("status_accuracy") for f in report.failures)


def test_content_check_fails_a_case_whose_answer_contains_a_forbidden_phrase(copilot):
    case = {"id": "T1", "user": "rm-1", "request": {"text": "What is the unsecured lending cap for one borrower?"},
            "expect": {"status": "answered", "must_not_include": ["AED 2.5m"]}}
    assert release_gate.evaluate_case(copilot, case).content_ok is False
