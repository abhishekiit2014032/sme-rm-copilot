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
from copilot.operations.resilience import CircuitBreaker, RetryPolicy, call_with_resilience

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
        """Signature, expiry, same RM, never used before. The nonce is burned only when all checks pass."""
        with trace.span(Component.HUMAN_IN_THE_LOOP, "verify approval token") as span:
            try:
                claims = self._check(token, identity)
            except ApprovalError as exc:
                span.status, span.detail = "rejected", str(exc)
                raise
            self._used_nonces.add(claims.nonce)
            span.detail = "signature ok, not expired, same RM, first use"
            return claims

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


class CommitOutbox:
    """Writes wait in the outbox; the dispatcher sends each with an idempotency key, so a retry never duplicates."""

    def __init__(self, crm: CrmSystem) -> None:
        self._crm = crm
        self._pending: list[tuple[str, dict[str, Any]]] = []
        self._breaker = CircuitBreaker("CRM writes")

    def enqueue(self, drafts: list[Draft], thread_id: str, trace: Trace) -> None:
        for draft in drafts:
            self._pending.append((self.key_for(thread_id, draft.draft_id), draft.model_dump()))
        trace.event(Component.RELIABILITY, "outbox enqueue", detail=f"{len(drafts)} writes, idempotency keys set")

    def dispatch(self, trace: Trace) -> CommitReport:
        committed, duplicates = [], 0
        with trace.span(Component.SYSTEMS_OF_RECORD, "CRM: commit approved writes") as span:
            while self._pending:
                key, write = self._pending[0]
                result = call_with_resilience(lambda: self._crm.commit(write, key), breaker=self._breaker,
                                              trace=trace, policy=RetryPolicy(attempts=3))
                self._pending.pop(0)
                committed.append({"crm_id": result["crm_id"], "kind": write["kind"], "title": write["title"]})
                duplicates += int(result["duplicate"])
            span.detail = f"{len(committed)} writes committed, {duplicates} duplicate(s) ignored by idempotency key"
        return CommitReport(committed=committed, duplicates_ignored=duplicates)

    @staticmethod
    def key_for(thread_id: str, draft_id: str) -> str:
        return f"{thread_id}:{draft_id}"
