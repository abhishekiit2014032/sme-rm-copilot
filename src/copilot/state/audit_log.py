"""Diagram box: Secure storage (lane C), the write-once audit log.
Appends hash-chained records of requests, tool calls and approvals; any edit breaks the chain.
Mocked: an in-memory list; production writes to immutable (WORM) storage. Records never hold payload content.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from pydantic import BaseModel

from copilot.contracts.trace import Component, Trace

GENESIS = "0" * 64


class AuditRecord(BaseModel):
    seq: int
    at: str
    actor: str
    action: str
    target: str
    outcome: str
    request_id: str
    prev_hash: str
    hash: str = ""

    def compute_hash(self) -> str:
        body = self.model_dump(exclude={"hash"})
        return hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()


class AuditLog:
    """Append-only. There is deliberately no update or delete method."""

    def __init__(self) -> None:
        self._records: list[AuditRecord] = []

    def append(self, actor: str, action: str, target: str, outcome: str, request_id: str,
               trace: Trace | None = None) -> AuditRecord:
        record = AuditRecord(
            seq=len(self._records) + 1,
            at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            actor=actor, action=action, target=target, outcome=outcome, request_id=request_id,
            prev_hash=self._records[-1].hash if self._records else GENESIS,
        )
        record.hash = record.compute_hash()
        self._records.append(record)
        if trace is not None:
            trace.event(Component.SECURE_STORAGE, f"audit #{record.seq}: {action}",
                        detail=f"{actor} -> {target}: {outcome}", hash=record.hash[:12])
        return record

    @property
    def records(self) -> tuple[AuditRecord, ...]:
        return tuple(r.model_copy() for r in self._records)

    def verify_chain(self) -> bool:
        previous = GENESIS
        for record in self._records:
            if record.prev_hash != previous or record.compute_hash() != record.hash:
                return False
            previous = record.hash
        return True
