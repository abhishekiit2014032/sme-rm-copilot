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


def failures(copilot, text: str, citations: list[Citation] | None = None, agent: str = "policy",
             drafts: list[Draft] | None = None) -> list[str]:
    result = AgentResult(agent=agent, status="ok", text=text, evidence=[CLAUSE_TEXT], drafts=drafts or [],
                         citations=[CITATION] if citations is None else citations)
    return OutputGuardrails().check(result, FEE_QUESTION, run_context(copilot)).failures


def test_grounded_cited_answer_passes(copilot):
    assert failures(copilot, "Each amendment to a letter of credit costs AED 350 (§1.3).") == []


def test_missing_or_unretrieved_citation_is_rejected(copilot):
    assert "policy answer has no citation" in failures(copilot, "It costs AED 350.", citations=[])
    assert "mentions §7.7 which was not retrieved" in failures(
        copilot, "Each amendment to a letter of credit costs AED 350 (§1.3, §7.7).")


def test_figure_not_in_any_source_is_rejected_in_answers_and_drafts(copilot):
    assert any("AED 400" in f for f in failures(copilot, "Each amendment to a letter of credit costs AED 400 (§1.3)."))
    draft = Draft(draft_id="d1", kind="email", customer_id="C-1001", title="t", body="The fee is AED 12,345.")
    assert any("AED 12,345" in f for f in failures(copilot, "Drafted.", agent="followup", drafts=[draft]))


def test_invented_figure_gets_one_stricter_retry_and_the_clean_answer_is_released(copilot):
    copilot.llm.unsupported_number_once = True
    answer = ask(copilot, "rm-1", FEE_QUESTION)
    assert answer.status == AnswerStatus.ANSWERED and "9,999" not in answer.text
    temperatures = [s.attrs["temperature"] for s in answer.trace.spans if "policy.answer" in s.name]
    assert temperatures == [0.1, 0.0]


class AlwaysInventsFigures(ScriptedLlm):
    def _policy_answer(self, v):
        result = super()._policy_answer(v)
        result["answer"] += " A processing fee of AED 9,999 also applies."
        return result


def test_answer_that_fails_twice_is_replaced_by_a_safe_decline():
    answer = ask(build_copilot(flags=Flags(), llm=AlwaysInventsFigures()), "rm-1", FEE_QUESTION)
    assert (answer.status, answer.text) == (AnswerStatus.DECLINED, SAFE_DECLINE)
    assert any("AED 9,999" in n for n in answer.notices)
