"""Use case: Follow-up actions with human in the loop, end to end.
Proves the copilot drafts and pauses at a checkpoint, the RM approves with a signed single-use token,
code (not an agent) commits through the idempotent outbox, and a replayed or foreign token is rejected.
"""
from __future__ import annotations

import pytest

from copilot.contracts import AnswerStatus
from copilot.operations.flags import Flags
from copilot.operations.resilience import TransientError
from helpers import draft_follow_up


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


@pytest.mark.xfail(strict=True, raises=TransientError,
                   reason="BUG request_path/pipeline.py RequestPipeline.approve and execution/approvals.py "
                          "CommitOutbox.dispatch: a CRM outage during approve escapes as an unhandled exception "
                          "(HTTP 500) instead of an Answer")
def test_crm_outage_during_approval_returns_an_answer(copilot):
    token = draft_follow_up(copilot).approval_token
    copilot.crm.down = True
    assert approve(copilot, token).status != AnswerStatus.COMMITTED


@pytest.mark.xfail(strict=True,
                   reason="BUG execution/approvals.py ApprovalService.verify burns the nonce before the outbox "
                          "commits; after a CRM outage the RM's approval is rejected as a replay and the drafts "
                          "can never be saved")
def test_rm_can_approve_again_after_a_crm_outage(copilot):
    token = draft_follow_up(copilot).approval_token
    copilot.crm.down = True
    try:
        approve(copilot, token)
    except TransientError:
        pass                                   # the outage itself is the bug above
    copilot.crm.down = False
    assert approve(copilot, token).status == AnswerStatus.COMMITTED
