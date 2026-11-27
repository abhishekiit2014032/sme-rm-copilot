"""Use case: Policy and product Q&A, end to end.
Proves the two-part question is planned into conditional steps and answered with the current §4.2, §4.5 and
§4.6, that single questions are cited, and that weak or restricted evidence is declined rather than guessed.
"""
from __future__ import annotations

import pytest

from copilot.contracts import AnswerStatus
from helpers import FEE_QUESTION, GULF_STAR, POLICY_QUESTION, ask


@pytest.fixture
def two_part(copilot):
    return ask(copilot, "rm-1", POLICY_QUESTION, customer_id=GULF_STAR)


def test_two_part_question_takes_the_planner_path_with_a_conditional_second_step(two_part):
    assert two_part.status == AnswerStatus.ANSWERED and two_part.path == "planner"
    plan = next(s for s in two_part.trace.spans if s.name == "plan")
    assert plan.attrs["steps"] == 2 and "(if step 1 = no)" in plan.detail


def test_two_part_answer_cites_sections_4_2_4_5_and_4_6_current_version(two_part):
    assert [(c.clause, c.version) for c in two_part.citations] == [("4.2", "2026.2"), ("4.5", "2026.2"),
                                                                    ("4.6", "2026.2")]
    for clause in ("§4.2", "§4.5", "§4.6"):
        assert clause in two_part.text


def test_two_part_answer_says_no_and_lists_acceptable_collateral(two_part):
    assert two_part.text.startswith("No. AED 3.5m is above the unsecured cap of AED 2.5m")
    for item in ("property mortgage", "cash margin", "assignment of receivables"):
        assert item in two_part.text
    assert "1.5m" not in two_part.text      # the superseded cap is never used


def test_within_cap_question_says_yes_citing_4_2(copilot):
    answer = ask(copilot, "rm-1", "Can we lend AED 2m unsecured to an SME borrower?")
    assert answer.status == AnswerStatus.ANSWERED and answer.text.startswith("Yes.")
    assert [c.clause for c in answer.citations] == ["4.2"]


def test_public_fee_question_cites_the_fee_schedule(copilot):
    answer = ask(copilot, "rm-1", FEE_QUESTION)
    assert answer.text == "Each amendment to a letter of credit costs AED 350 (§1.3)."
    assert answer.citations[0].label == "SME Fees and Charges §1.3 (v2026.2)"


def test_weak_evidence_is_declined_with_no_citation(copilot):
    answer = ask(copilot, "rm-1", "What is our policy on lending to cryptocurrency mining companies?")
    assert answer.status == AnswerStatus.DECLINED and answer.citations == []
    assert "credit desk" in answer.text


def test_restricted_clause_is_declined_for_an_rm_but_answered_for_the_credit_desk(copilot):
    question = "What are the pricing floors for each risk grade?"
    rm = ask(copilot, "rm-1", question)
    desk = ask(copilot, "cd-1", question)
    assert rm.status == AnswerStatus.DECLINED and "Minimum margins" not in rm.text
    assert desk.status == AnswerStatus.ANSWERED and desk.citations[0].clause == "9.1"
