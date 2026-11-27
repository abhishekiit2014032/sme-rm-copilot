"""Diagram box: Human in the loop (lane B), plus the outbox that commits approved writes.
Issues and verifies signed, single-use, 5-minute approval tokens; only code commits to the CRM, through an outbox
with an idempotency key. Mocked: HMAC signing (production: HSM-held key) and the CRM (mocks/systems/crm.py).
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from typing import Any, Callable

from pydantic import BaseModel

from copilot.contracts.approval import ApprovalToken
from copilot.contracts.identity import Identity
from copilot.contracts.ports import CrmSystem
from copilot.contracts.results import Draft
from copilot.contracts.trace import Component, Trace
from copilot.operations.resilience import CircuitBreaker, CircuitOpen, RetryPolicy, TransientError, \
    call_with_resilience

TOKEN_TTL_S = 300


class ApprovalError(Exception):
    pass


class ApprovalService:
    def __init__(self, signing_key: bytes = b"mock-approval-key", clock: Callable[[], float] = time.time) -> None:
        self._key, self._clock = signing_key, clock
        self._used_nonces: set[str] = set()

    def issue(self, thread_id: str, draft_ids: list[str], rm_id: str, trace: Trace) -> str:
        claims = ApprovalToken(thread_id=thread_id, draft_ids=draft_ids, rm_id=rm_id, nonce=uuid.uuid4().hex,
                               expires_at=self._clock() + TOKEN_TTL_S)
        body = base64.urlsafe_b64encode(claims.model_dump_json().encode()).decode()
        trace.event(Component.HUMAN_IN_THE_LOOP, "approval requested",
                    detail=f"{len(draft_ids)} drafts; signed token, single use, valid 5 min", status="paused")
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str, identity: Identity, trace: Trace) -> ApprovalToken:
        """Signature, expiry, same RM, not used before. Verifying does not use the token up: consume() does,
        once the writes are committed, so the RM can approve again after a CRM outage."""
        with trace.span(Component.HUMAN_IN_THE_LOOP, "verify approval token") as span:
            try:
                claims = self._check(token, identity)
            except ApprovalError as exc:
                span.status, span.detail = "rejected", str(exc)
                raise
            span.detail = "signature ok, not expired, same RM, not used yet"
            return claims

    def consume(self, claims: ApprovalToken, trace: Trace) -> None:
        """Burn the nonce after a successful commit; any later use of the token is a replay. Two approvals racing
        before this point cannot duplicate a write: the outbox's idempotency key stops it."""
        self._used_nonces.add(claims.nonce)
        trace.event(Component.HUMAN_IN_THE_LOOP, "approval token used up", detail="writes committed; replay blocked")

    def _check(self, token: str, identity: Identity) -> ApprovalToken:
        body, _, signature = token.partition(".")
        if not signature or not hmac.compare_digest(signature, self._sign(body)):
            raise ApprovalError("approval token signature is invalid")
        claims = ApprovalToken.model_validate_json(base64.urlsafe_b64decode(body.encode()))
        if claims.expires_at < self._clock():
            raise ApprovalError("approval token has expired")
        if claims.rm_id != identity.user_id:
            raise ApprovalError("approval token belongs to another RM")
        if claims.nonce in self._used_nonces:
            raise ApprovalError("approval token was already used (replay)")
        return claims

    def _sign(self, body: str) -> str:
        return hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()


class CommitReport(BaseModel):
    committed: list[dict[str, Any]]
    duplicates_ignored: int
    pending: int = 0           # writes still in the outbox because the CRM did not respond


class CommitOutbox:
    """Writes wait in the outbox; the dispatcher sends each with an idempotency key, so a retry never duplicates."""

    def __init__(self, crm: CrmSystem) -> None:
        self._crm = crm
        self._pending: list[tuple[str, dict[str, Any]]] = []
        self._breaker = CircuitBreaker("CRM writes")

    def enqueue(self, drafts: list[Draft], thread_id: str, trace: Trace) -> None:
        """Add each write once: approving again after an outage does not queue the same write twice."""
        queued = {key for key, _ in self._pending}
        for draft in drafts:
            key = self.key_for(thread_id, draft.draft_id)
            if key not in queued:
                self._pending.append((key, draft.model_dump()))
        trace.event(Component.RELIABILITY, "outbox enqueue", detail=f"{len(drafts)} writes, idempotency keys set")

    def dispatch(self, trace: Trace) -> CommitReport:
        """Send pending writes in order. If the CRM stays down, stop and keep the rest in the outbox."""
        committed, duplicates = [], 0
        with trace.span(Component.SYSTEMS_OF_RECORD, "CRM: commit approved writes") as span:
            while self._pending:
                key, write = self._pending[0]
                try:
                    result = call_with_resilience(lambda: self._crm.commit(write, key), breaker=self._breaker,
                                                  trace=trace, policy=RetryPolicy(attempts=3))
                except (TransientError, CircuitOpen):
                    break
                self._pending.pop(0)
                committed.append({"crm_id": result["crm_id"], "kind": write["kind"], "title": write["title"]})
                duplicates += int(result["duplicate"])
            span.detail = f"{len(committed)} writes committed, {duplicates} duplicate(s) ignored by idempotency key"
            if self._pending:
                span.status = "unavailable"
                span.detail += f"; CRM unavailable, {len(self._pending)} kept in the outbox"
        return CommitReport(committed=committed, duplicates_ignored=duplicates, pending=len(self._pending))

    @staticmethod
    def key_for(thread_id: str, draft_id: str) -> str:
        return f"{thread_id}:{draft_id}"
