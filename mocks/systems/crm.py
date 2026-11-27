"""Diagram box: Systems of record (lane B), the CRM.
Reads profile, interactions and open requests; commits approved writes with an idempotency key.
Mocked: an in-memory CRM. `lose_next_ack` simulates a write that lands but whose reply is lost.
"""
from __future__ import annotations

from typing import Any

from mocks.systems import MockSystem, SystemUnavailable


class MockCrm(MockSystem):
    system_name = "CRM"
    data_key = "crm"

    def __init__(self, dataset: dict[str, Any]) -> None:
        super().__init__(dataset)
        self.committed: dict[str, dict[str, Any]] = {}   # idempotency key -> stored write
        self.lose_next_ack = False

    def get_profile(self, customer_id: str) -> dict[str, Any]:
        return self._record(customer_id)["profile"]

    def get_interactions(self, customer_id: str) -> list[dict[str, Any]]:
        return self._record(customer_id).get("interactions", [])

    def get_open_requests(self, customer_id: str) -> list[dict[str, Any]]:
        return self._record(customer_id).get("open_requests", [])

    def commit(self, write: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        """Store a write once per key. A repeated key returns the first result (no duplicate)."""
        if self.down:
            raise SystemUnavailable("CRM did not respond (timeout)")
        if idempotency_key in self.committed:
            return {**self.committed[idempotency_key], "duplicate": True}
        record = {"crm_id": f"CRM-{len(self.committed) + 1:05d}", "write": write, "duplicate": False}
        self.committed[idempotency_key] = record
        if self.lose_next_ack:
            self.lose_next_ack = False
            raise SystemUnavailable("CRM stored the write but the reply was lost (timeout)")
        return record
