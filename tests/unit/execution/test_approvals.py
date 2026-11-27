"""Diagram box: Human in the loop (lane B), approval tokens and the commit outbox.
Proves the approval token is signed, single use, expires, is bound to one RM and one set of drafts,
and that the outbox commits each write exactly once even when the CRM reply is lost.
"""
from __future__ import annotations

import base64
import json

import pytest

from copilot.contracts import Draft, Trace
from copilot.execution.approvals import TOKEN_TTL_S, ApprovalError, ApprovalService, CommitOutbox
from helpers import run_context
from mocks.data import load_json
from mocks.systems.crm import MockCrm


class Clock:
    now = 1_000.0

    def __call__(self) -> float:
        return self.now


def verify(copilot, service: ApprovalService, token: str, user: str = "rm-1"):
    return service.verify(token, run_context(copilot, user).identity, Trace())


def issue(service: ApprovalService) -> str:
    return service.issue("thread-1", ["dr-1", "dr-2"], "rm-1", Trace())


def test_token_is_used_up_only_after_the_commit_then_a_replay_is_rejected(copilot):
    service = ApprovalService()
    token = issue(service)
    assert verify(copilot, service, token).draft_ids == ["dr-1", "dr-2"]
    claims = verify(copilot, service, token)            # still valid: nothing committed yet
    service.consume(claims, Trace())
    with pytest.raises(ApprovalError, match="replay"):
        verify(copilot, service, token)


def test_token_expires_after_five_minutes(copilot):
    clock = Clock()
    service = ApprovalService(clock=clock)
    token = issue(service)
    clock.now += TOKEN_TTL_S + 1
    with pytest.raises(ApprovalError, match="expired"):
        verify(copilot, service, token)


def test_token_is_bound_to_its_rm_and_drafts_and_a_failed_check_does_not_burn_it(copilot):
    service = ApprovalService()
    token = issue(service)
    with pytest.raises(ApprovalError, match="another RM"):
        verify(copilot, service, token, user="rm-2")
    body, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body))
    claims["draft_ids"].append("dr-smuggled")
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    with pytest.raises(ApprovalError, match="signature"):
        verify(copilot, service, f"{forged}.{signature}")
    assert verify(copilot, service, token).rm_id == "rm-1"


def drafts() -> list[Draft]:
    return [Draft(draft_id=f"dr-{i}", kind="task", customer_id="C-1001", title=f"Task {i}", body="b")
            for i in (1, 2)]


def test_outbox_commits_each_draft_once_with_an_idempotency_key():
    crm = MockCrm(load_json("customers.json"))
    outbox = CommitOutbox(crm)
    outbox.enqueue(drafts(), "thread-1", Trace())
    assert len(outbox.dispatch(Trace()).committed) == 2
    assert set(crm.committed) == {"thread-1:dr-1", "thread-1:dr-2"}


def test_outbox_keeps_writes_while_the_crm_is_down_and_never_queues_one_twice():
    crm = MockCrm(load_json("customers.json"))
    crm.down = True
    outbox = CommitOutbox(crm)
    outbox.enqueue(drafts(), "thread-1", Trace())
    trace = Trace()
    assert outbox.dispatch(trace).pending == 2 and trace.spans[0].status == "unavailable"
    outbox.enqueue(drafts(), "thread-1", Trace())                # the RM approves again
    crm.down = False
    report = outbox.dispatch(Trace())
    assert (len(report.committed), report.pending, len(crm.committed)) == (2, 0, 2)


def test_outbox_is_idempotent_when_the_crm_reply_is_lost():
    crm = MockCrm(load_json("customers.json"))
    crm.lose_next_ack = True
    outbox = CommitOutbox(crm)
    outbox.enqueue(drafts(), "thread-1", Trace())
    trace = Trace()
    report = outbox.dispatch(trace)
    assert len(crm.committed) == 2 and report.duplicates_ignored == 1
    assert any(s.status == "retry" for s in trace.spans)
