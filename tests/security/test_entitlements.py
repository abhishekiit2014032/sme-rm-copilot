"""Security: customer entitlements (Tool gateway (MCP) RBAC, lane B; Human in the loop; Session + memory store).
Proves an RM can reach only their own portfolio: other RMs' customers are denied before any system of record is
read, for every use case, and approval tokens and memories never cross RMs.
"""
from __future__ import annotations

from copilot.contracts import AnswerStatus
from helpers import AL_NOOR, GULF_STAR, PALM_COAST, ask, draft_follow_up


def test_brief_outside_the_portfolio_is_denied_audited_and_reads_no_system(copilot):
    for user, customer in (("rm-2", GULF_STAR), ("rm-2", AL_NOOR), ("rm-1", PALM_COAST), ("cd-1", GULF_STAR)):
        answer = ask(copilot, user, action="prepare_brief", customer_id=customer)
        assert answer.status == AnswerStatus.DENIED
        assert answer.notices == [f"entitlement: {user} is not entitled to customer {customer}"]
        assert copilot.audit.records[-2].outcome == "denied (entitlement)"     # the tool call, then the request
    assert copilot.crm.calls == copilot.core_banking.calls == copilot.lending.calls == 0
    assert "Gulf Star" not in answer.model_dump_json()


def test_follow_up_on_another_rms_customer_is_denied_and_drafts_nothing(copilot):
    answer = draft_follow_up(copilot, user="rm-2")
    assert answer.status == AnswerStatus.DENIED and copilot.crm_drafts.drafts == {}


def test_approval_and_approved_memory_never_cross_rms(copilot):
    token = draft_follow_up(copilot).approval_token
    assert copilot.pipeline.approve(copilot.token_for("rm-2"), token).status == AnswerStatus.REJECTED
    assert copilot.crm.committed == {}
    copilot.pipeline.approve(copilot.token_for("rm-1"), token)
    rm2_memory = copilot.pipeline.orchestrator._episodic.recall(GULF_STAR, "rm-2")
    assert rm2_memory == []
