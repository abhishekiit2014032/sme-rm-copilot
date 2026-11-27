"""Diagram box: Systems of record (lane B), the mock CRM, core banking, lending, trade and case systems.
Each mock reads fictional data from mocks/data/customers.json and can be switched "down" for failure demos.
Production replaces each mock with a REST or ODS adapter behind the same port.
"""
from __future__ import annotations

from typing import Any

from copilot.operations.resilience import TransientError


class SystemUnavailable(TransientError):
    """Raised by a mock system that is switched down or timing out."""


class MockSystem:
    """Shared behaviour: per-customer data lookup, an as-of time and a 'down' switch."""

    system_name = "system"
    data_key = ""

    def __init__(self, dataset: dict[str, Any]) -> None:
        self._by_customer = {c["customer_id"]: c.get(self.data_key, {}) for c in dataset["customers"]}
        self.as_of: str = dataset["as_of"].get(self.data_key, "")
        self.down = False
        self.calls = 0

    def _record(self, customer_id: str) -> dict[str, Any]:
        self.calls += 1
        if self.down:
            raise SystemUnavailable(f"{self.system_name} did not respond (timeout)")
        if customer_id not in self._by_customer:
            raise KeyError(f"unknown customer {customer_id}")
        return self._by_customer[customer_id]
