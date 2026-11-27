"""Use case: Follow-up actions with human in the loop, end to end.
Proves the copilot drafts and pauses at a checkpoint, the RM approves with a signed single-use token,
code (not an agent) commits through the idempotent outbox, and a replayed or foreign token is rejected.
A CRM outage keeps the writes in the outbox for a later approval; a planner ask with a follow-up step also pauses.
"""
from __future__ import annotations

from copilot.contracts import AnswerStatus
from copilot.operations.flags import Flags
from helpers import FOLLOW_UP_NOTES, GULF_STAR, ask, draft_follow_up

PLANNER_ASK = f"What collateral is acceptable? {FOLLOW_UP_NOTES}"


def approve(copilot, token: str, user: str = "rm-1"):
    return copilot.pipeline.approve(copilot.token_for(user), token)


def test_pause_approve_commit_then_replay_is_rejected(copilot):
    drafted = draft_follow_up(copilot)
    assert drafted.status == AnswerStatus.AWAITING_APPROVAL and drafted.approval_token
    assert copilot.crm.committed == {}
    assert "checkpoint saved: await_approval" in [s.name for s in drafted.trace.spans]

    committed = approve(copilot, drafted.approval_token)
    assert committed.status == AnswerStatus.COMMITTED
    assert {w["write"]["draft_id"] for w in copilot.crm.committed.values()} == {d.draft_id for d in drafted.drafts}
    assert "checkpoint loaded: await_approval" in [s.name for s in committed.trace.spans]

    replay = approve(copilot, drafted.approval_token)
    assert replay.status == AnswerStatus.REJECTED and "replay" in replay.text
    assert len(copilot.crm.committed) == 4


def test_lost_crm_reply_is_retried_without_a_duplicate_write(copilot):
    token = draft_follow_up(copilot).approval_token
    copilot.crm.lose_next_ack = True
    answer = approve(copilot, token)
    assert answer.status == AnswerStatus.COMMITTED and len(copilot.crm.committed) == 4
    assert "1 duplicate write(s) were ignored" in answer.text


def test_foreign_or_forged_tokens_commit_nothing(copilot):
    token = draft_follow_up(copilot).approval_token
    assert approve(copilot, token, user="rm-2").status == AnswerStatus.REJECTED
    body, _ = token.split(".")
    assert approve(copilot, f"{body}.{'0' * 64}").status == AnswerStatus.REJECTED
    assert copilot.crm.committed == {}


def test_crm_write_kill_switch_keeps_drafts_and_token_for_later(copilot):
    token = draft_follow_up(copilot).approval_token
    copilot.pipeline.flags = Flags(crm_writes=False)
    assert approve(copilot, token).status == AnswerStatus.DECLINED and copilot.crm.committed == {}
    copilot.pipeline.flags = Flags()
    assert approve(copilot, token).status == AnswerStatus.COMMITTED


def test_crm_outage_during_approval_keeps_the_writes_in_the_outbox(copilot):
    token = draft_follow_up(copilot).approval_token
    copilot.crm.down = True
    answer = approve(copilot, token)
    assert answer.status == AnswerStatus.PENDING and "4 approved write(s) wait in the outbox" in answer.text
    assert answer.notices == ["CRM unavailable: writes kept in the outbox"] and copilot.crm.committed == {}
    assert copilot.audit.records[-1].outcome == "pending"


def test_rm_can_approve_again_after_a_crm_outage_and_only_then_is_the_token_used_up(copilot):
    token = draft_follow_up(copilot).approval_token
    copilot.crm.down = True
    assert approve(copilot, token).status == AnswerStatus.PENDING
    copilot.crm.down = False
    assert approve(copilot, token).status == AnswerStatus.COMMITTED and len(copilot.crm.committed) == 4
    assert approve(copilot, token).status == AnswerStatus.REJECTED


def test_a_planner_ask_with_a_follow_up_step_still_pauses_for_approval(copilot):
    answer = ask(copilot, "rm-1", PLANNER_ASK, customer_id=GULF_STAR)
    assert (answer.path, answer.status) == ("planner", AnswerStatus.AWAITING_APPROVAL)
    assert answer.citations and len(answer.drafts) == 4 and answer.approval_token
    assert approve(copilot, answer.approval_token).status == AnswerStatus.COMMITTED


def test_the_stricter_retry_never_reruns_an_agent_that_created_drafts(copilot):
    copilot.llm.unsupported_number_once = True     # the policy step invents a figure, so output guardrails fail
    answer = ask(copilot, "rm-1", PLANNER_ASK, customer_id=GULF_STAR)
    assert answer.status == AnswerStatus.DECLINED and not answer.drafts
    assert [c for c in copilot.llm.calls if c.endswith("followup.extract")] == ["mini-llm:followup.extract"]
