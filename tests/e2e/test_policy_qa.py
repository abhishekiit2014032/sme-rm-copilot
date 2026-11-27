"""Use case: Policy and product Q&A, end to end.
Proves the two-part question is planned into conditional steps and answered with the current §4.2, §4.5 and
§4.6, that single questions are cited, and that weak or restricted evidence is declined rather than guessed.
"""
from __future__ import annotations

from copilot.contracts import AnswerStatus
from helpers import FEE_QUESTION, GULF_STAR, POLICY_QUESTION, ask


def test_two_part_question_is_planned_and_cites_sections_4_2_4_5_and_4_6(copilot):
    answer = ask(copilot, "rm-1", POLICY_QUESTION, customer_id=GULF_STAR)
    assert (answer.status, answer.path) == (AnswerStatus.ANSWERED, "planner")
    plan = next(s for s in answer.trace.spans if s.name == "plan")
    assert plan.attrs["steps"] == 2 and "(if step 1 = no)" in plan.detail
    assert [(c.clause, c.version) for c in answer.citations] == [("4.2", "2026.2"), ("4.5", "2026.2"),
                                                                  ("4.6", "2026.2")]
    assert answer.text.startswith("No. AED 3.5m is above the limit")
    assert "must not exceed AED 2.5m (§4.2)" in answer.text
    for expected in ("§4.2", "§4.5", "§4.6", "property mortgage", "cash margin", "assignment of receivables"):
        assert expected in answer.text
    assert "1.5m" not in answer.text            # the superseded cap is never used


def test_single_questions_are_answered_with_their_clause(copilot):
    within_cap = ask(copilot, "rm-1", "Can we lend AED 2m unsecured to an SME borrower?")
    assert within_cap.text.startswith("Yes.") and [c.clause for c in within_cap.citations] == ["4.2"]
    fee = ask(copilot, "rm-1", FEE_QUESTION)
    assert fee.text == "Each amendment to a letter of credit costs AED 350 (§1.3)."
    assert fee.citations[0].label == "SME Fees and Charges §1.3 (v2026.2)"


def test_weak_or_restricted_evidence_is_declined_for_an_rm(copilot):
    weak = ask(copilot, "rm-1", "What is our policy on lending to cryptocurrency mining companies?")
    assert weak.status == AnswerStatus.DECLINED and weak.citations == [] and "credit desk" in weak.text
    floors = "What are the pricing floors for each risk grade?"
    rm = ask(copilot, "rm-1", floors)
    assert rm.status == AnswerStatus.DECLINED and "Minimum margins" not in rm.text
    desk = ask(copilot, "cd-1", floors)
    assert desk.status == AnswerStatus.ANSWERED and desk.citations[0].clause == "9.1"
