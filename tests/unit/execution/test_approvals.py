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
from copilot.operations.resilience import TransientError
from helpers import run_context
from mocks.data import load_json
from mocks.systems.crm import MockCrm


class Clock:
    now = 1_000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def service(clock) -> ApprovalService:
    return ApprovalService(clock=clock)


def identity(copilot, user: str = "rm-1"):
    return run_context(copilot, user).identity


def issue(service: ApprovalService, rm: str = "rm-1") -> str:
    return service.issue("thread-1", ["dr-1", "dr-2"], rm, Trace())


def test_valid_token_verifies_once_with_its_claims(copilot, service):
    claims = service.verify(issue(service), identity(copilot), Trace())
    assert (claims.thread_id, claims.draft_ids, claims.rm_id) == ("thread-1", ["dr-1", "dr-2"], "rm-1")


def test_replayed_token_is_rejected(copilot, service):
    token = issue(service)
    service.verify(token, identity(copilot), Trace())
    with pytest.raises(ApprovalError, match="replay"):
        service.verify(token, identity(copilot), Trace())


def test_expired_token_is_rejected(copilot, service, clock):
    token = issue(service)
    clock.now += TOKEN_TTL_S + 1
    with pytest.raises(ApprovalError, match="expired"):
        service.verify(token, identity(copilot), Trace())


def test_token_is_bound_to_the_rm_who_drafted(copilot, service):
    with pytest.raises(ApprovalError, match="another RM"):
        service.verify(issue(service, rm="rm-1"), identity(copilot, "rm-2"), Trace())


def test_token_is_bound_to_its_drafts_so_editing_the_draft_list_breaks_the_signature(copilot, service):
    body, signature = issue(service).split(".")
    claims = json.loads(base64.urlsafe_b64decode(body))
    claims["draft_ids"].append("dr-smuggled")
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    with pytest.raises(ApprovalError, match="signature"):
        service.verify(f"{forged}.{signature}", identity(copilot), Trace())


def test_token_signed_with_another_key_is_rejected(copilot, service):
    other = ApprovalService(signing_key=b"attacker-key").issue("thread-1", ["dr-1"], "rm-1", Trace())
    with pytest.raises(ApprovalError, match="signature"):
        service.verify(other, identity(copilot), Trace())


def test_failed_verification_does_not_burn_the_token(copilot, service):
    token = issue(service, rm="rm-1")
    with pytest.raises(ApprovalError):
        service.verify(token, identity(copilot, "rm-2"), Trace())
    assert service.verify(token, identity(copilot, "rm-1"), Trace()).rm_id == "rm-1"


def drafts() -> list[Draft]:
    return [Draft(draft_id=f"dr-{i}", kind="task", customer_id="C-1001", title=f"Task {i}", body="b")
            for i in (1, 2)]


def test_outbox_commits_each_draft_once_with_an_idempotency_key():
    crm = MockCrm(load_json("customers.json"))
    outbox = CommitOutbox(crm)
    outbox.enqueue(drafts(), "thread-1", Trace())
    report = outbox.dispatch(Trace())
    assert len(report.committed) == 2 and report.duplicates_ignored == 0
    assert set(crm.committed) == {"thread-1:dr-1", "thread-1:dr-2"}


def test_outbox_is_idempotent_when_the_crm_reply_is_lost():
    crm = MockCrm(load_json("customers.json"))
    crm.lose_next_ack = True
    outbox = CommitOutbox(crm)
    outbox.enqueue(drafts(), "thread-1", Trace())
    trace = Trace()
    report = outbox.dispatch(trace)
    assert len(crm.committed) == 2                  # stored once each, despite the retry
    assert report.duplicates_ignored == 1
    assert any(s.status == "retry" for s in trace.spans)


def test_outbox_keeps_the_write_pending_when_the_crm_stays_down():
    crm = MockCrm(load_json("customers.json"))
    crm.down = True
    outbox = CommitOutbox(crm)
    outbox.enqueue(drafts()[:1], "thread-1", Trace())
    with pytest.raises(TransientError):
        outbox.dispatch(Trace())
    crm.down = False
    assert len(outbox.dispatch(Trace()).committed) == 1
