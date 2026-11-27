"""Security: customer entitlements (Tool gateway (MCP) RBAC, lane B; Human in the loop; Session + memory store).
Proves an RM can reach only their own portfolio: other RMs' customers are denied before any system of record is
read, for every use case, and approval tokens, checkpoints and memories never cross RMs.
"""
from __future__ import annotations

import pytest

from copilot.contracts import AnswerStatus
from helpers import AL_NOOR, GULF_STAR, PALM_COAST, ask, draft_follow_up


@pytest.mark.parametrize("user, customer", [("rm-2", GULF_STAR), ("rm-2", AL_NOOR), ("rm-1", PALM_COAST),
                                            ("cd-1", GULF_STAR)])
def test_brief_on_a_customer_outside_the_portfolio_is_denied(copilot, user, customer):
    answer = ask(copilot, user, action="prepare_brief", customer_id=customer)
    assert answer.status == AnswerStatus.DENIED
    assert answer.notices == [f"entitlement: {user} is not entitled to customer {customer}"]


def test_denied_request_reads_no_system_of_record(copilot):
    ask(copilot, "rm-2", action="prepare_brief", customer_id=GULF_STAR)
    assert copilot.crm.calls == copilot.core_banking.calls == copilot.lending.calls == 0


def test_denied_request_leaks_no_customer_data_in_the_answer(copilot):
    answer = ask(copilot, "rm-2", action="prepare_brief", customer_id=GULF_STAR)
    assert "Gulf Star" not in answer.model_dump_json()


def test_follow_up_on_another_rms_customer_is_denied_and_drafts_nothing(copilot):
    answer = draft_follow_up(copilot, user="rm-2")
    assert answer.status == AnswerStatus.DENIED and copilot.crm_drafts.drafts == {}


def test_denial_is_audited_against_the_customer(copilot):
    ask(copilot, "rm-2", action="prepare_brief", customer_id=GULF_STAR)
    tool_denials = [r for r in copilot.audit.records if r.outcome == "denied (entitlement)"]
    assert [(r.actor, r.target) for r in tool_denials] == [("rm-2", GULF_STAR)]


def test_approval_token_of_one_rm_cannot_be_used_by_another(copilot):
    token = draft_follow_up(copilot).approval_token
    answer = copilot.pipeline.approve(copilot.token_for("rm-2"), token)
    assert answer.status == AnswerStatus.REJECTED and copilot.crm.committed == {}


def test_approved_memory_of_one_rm_is_not_recalled_for_another(copilot):
    token = draft_follow_up(copilot).approval_token
    copilot.pipeline.approve(copilot.token_for("rm-1"), token)
    recalled = copilot.pipeline.orchestrator._episodic.recall(GULF_STAR, "rm-2")
    assert recalled == []
