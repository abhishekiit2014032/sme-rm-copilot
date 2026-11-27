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


@pytest.fixture
def drafted(copilot):
    return draft_follow_up(copilot)


def test_drafting_pauses_for_approval_and_writes_nothing(copilot, drafted):
    assert drafted.status == AnswerStatus.AWAITING_APPROVAL and drafted.approval_token
    assert sorted(d.kind for d in drafted.drafts) == ["email", "note", "task", "task"]
    assert copilot.crm.committed == {}
    assert any(s.status == "paused" and s.component.value == "Human in the loop" for s in drafted.trace.spans)


def test_approval_commits_every_draft_to_the_crm(copilot, drafted):
    answer = approve(copilot, drafted.approval_token)
    assert answer.status == AnswerStatus.COMMITTED
    assert len(copilot.crm.committed) == 4
    stored = {w["write"]["draft_id"] for w in copilot.crm.committed.values()}
    assert stored == {d.draft_id for d in drafted.drafts}


def test_lost_crm_reply_is_retried_without_a_duplicate_write(copilot, drafted):
    copilot.crm.lose_next_ack = True
    answer = approve(copilot, drafted.approval_token)
    assert answer.status == AnswerStatus.COMMITTED and len(copilot.crm.committed) == 4
    assert "1 duplicate write(s) were ignored" in answer.text


def test_replayed_approval_is_rejected_and_writes_nothing_more(copilot, drafted):
    approve(copilot, drafted.approval_token)
    replay = approve(copilot, drafted.approval_token)
    assert replay.status == AnswerStatus.REJECTED and "replay" in replay.text
    assert len(copilot.crm.committed) == 4


def test_another_rm_cannot_approve_the_drafts(copilot, drafted):
    answer = approve(copilot, drafted.approval_token, user="rm-2")
    assert answer.status == AnswerStatus.REJECTED and copilot.crm.committed == {}


def test_forged_token_is_rejected(copilot, drafted):
    body, _ = drafted.approval_token.split(".")
    answer = approve(copilot, f"{body}.{'0' * 64}")
    assert answer.status == AnswerStatus.REJECTED and "signature" in answer.text


def test_crm_write_kill_switch_keeps_drafts_and_token_for_later(copilot, drafted):
    copilot.pipeline.flags = Flags(crm_writes=False)
    paused = approve(copilot, drafted.approval_token)
    assert paused.status == AnswerStatus.DECLINED and copilot.crm.committed == {}
    copilot.pipeline.flags = Flags()
    assert approve(copilot, drafted.approval_token).status == AnswerStatus.COMMITTED


def test_approval_resumes_from_the_checkpoint_and_audits_the_commit(copilot, drafted):
    answer = approve(copilot, drafted.approval_token)
    names = [s.name for s in answer.trace.spans]
    assert "checkpoint loaded: await_approval" in names and "outbox enqueue" in names
    assert copilot.audit.records[-1].action == "approve" and copilot.audit.records[-1].outcome == "committed"


@pytest.mark.xfail(strict=True, raises=TransientError,
                   reason="BUG request_path/pipeline.py:115 and execution/approvals.py:96: a CRM outage during "
                          "approve escapes as an unhandled exception (HTTP 500) instead of an Answer")
def test_crm_outage_during_approval_returns_an_answer(copilot, drafted):
    copilot.crm.down = True
    assert approve(copilot, drafted.approval_token).status != AnswerStatus.COMMITTED


@pytest.mark.xfail(strict=True,
                   reason="BUG execution/approvals.py:52 burns the nonce before the outbox commits; after a CRM "
                          "outage the RM's approval is rejected as a replay and the drafts can never be saved")
def test_rm_can_approve_again_after_a_crm_outage(copilot, drafted):
    copilot.crm.down = True
    try:
        approve(copilot, drafted.approval_token)
    except TransientError:
        pass                                   # the outage itself is the bug above
    copilot.crm.down = False
    assert approve(copilot, drafted.approval_token).status == AnswerStatus.COMMITTED
