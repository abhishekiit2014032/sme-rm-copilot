"""Diagram box: Output guardrails (lane A).
Proves an answer is released only when grounded: policy answers cite retrieved clauses, every figure matches a
source, runtime scores pass; a failed check gets one stricter retry and then a safe decline.
"""
from __future__ import annotations

from copilot.contracts import AgentResult, AnswerStatus, Citation, Draft
from copilot.operations.flags import Flags
from copilot.request_path.output_guardrails import OutputGuardrails
from copilot.request_path.pipeline import SAFE_DECLINE
from copilot.wiring import build_copilot
from helpers import FEE_QUESTION, ask, run_context
from mocks.llm.scripted_llm import ScriptedLlm

CLAUSE_TEXT = "Each amendment to a letter of credit costs AED 350."
CITATION = Citation(doc_id="fees", doc_title="SME Fees and Charges", clause="1.3", heading="LC amendment fee",
                    version="2026.2", clearance="public")


def policy_answer(text: str, citations: list[Citation] | None = None) -> AgentResult:
    return AgentResult(agent="policy", status="ok", text=text,
                       citations=[CITATION] if citations is None else citations, evidence=[CLAUSE_TEXT])


def check(copilot, result: AgentResult, question: str = FEE_QUESTION):
    return OutputGuardrails().check(result, question, run_context(copilot))


def test_grounded_cited_answer_passes_with_runtime_scores(copilot):
    verdict = check(copilot, policy_answer("Each amendment to a letter of credit costs AED 350 (§1.3)."))
    assert verdict.passed and verdict.scores.faithfulness >= 0.9


def test_policy_answer_without_citation_is_rejected(copilot):
    verdict = check(copilot, policy_answer("Each amendment costs AED 350.", citations=[]))
    assert not verdict.passed and "policy answer has no citation" in verdict.failures


def test_clause_mentioned_but_not_retrieved_is_rejected(copilot):
    verdict = check(copilot, policy_answer("Each amendment to a letter of credit costs AED 350 (§1.3, §7.7)."))
    assert "mentions §7.7 which was not retrieved" in verdict.failures


def test_figure_not_in_any_source_is_rejected(copilot):
    verdict = check(copilot, policy_answer("Each amendment to a letter of credit costs AED 400 (§1.3)."))
    assert not verdict.passed
    assert any("AED 400" in f for f in verdict.failures)


def test_figure_taken_from_the_question_is_allowed(copilot):
    result = AgentResult(agent="brief", status="ok", text="You asked about AED 3.5m.", evidence=[])
    assert check(copilot, result, question="Can we go to AED 3.5m?").passed


def test_figures_in_drafts_are_checked_too(copilot):
    draft = Draft(draft_id="d1", kind="email", customer_id="C-1001", title="t", body="Fee is AED 12,345.")
    result = AgentResult(agent="followup", status="needs_approval", text="Drafted.", drafts=[draft])
    assert not check(copilot, result).passed


def test_decline_is_always_safe_to_release(copilot):
    result = AgentResult(agent="policy", status="declined", text="Please check with the credit desk.")
    assert check(copilot, result).passed


def test_invented_figure_triggers_one_stricter_retry_then_passes(copilot):
    copilot.llm.unsupported_number_once = True
    answer = ask(copilot, "rm-1", FEE_QUESTION)
    assert answer.status == AnswerStatus.ANSWERED and "9,999" not in answer.text
    assert [s.name for s in answer.trace.spans if s.status == "retry"] == ["retry once, stricter"]
    temperatures = [s.attrs["temperature"] for s in answer.trace.spans if "policy.answer" in s.name]
    assert temperatures == [0.1, 0.0]


class AlwaysInventsFigures(ScriptedLlm):
    def _policy_answer(self, v):
        result = super()._policy_answer(v)
        result["answer"] += " A processing fee of AED 9,999 also applies."
        return result


def test_answer_that_fails_twice_is_replaced_by_a_safe_decline():
    copilot = build_copilot(flags=Flags(), llm=AlwaysInventsFigures())
    answer = ask(copilot, "rm-1", FEE_QUESTION)
    assert answer.status == AnswerStatus.DECLINED and answer.text == SAFE_DECLINE
    assert any("AED 9,999" in n for n in answer.notices)
