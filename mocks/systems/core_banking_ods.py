"""Diagram box: Systems of record (lane B), core banking through its read-only ODS view.
Returns account balances and monthly credit turnover.
Mocked: data from customers.json; set `down = True` to simulate an outage.
"""
from __future__ import annotations

from typing import Any

from mocks.systems import MockSystem


class MockCoreBankingOds(MockSystem):
    system_name = "Core banking (ODS)"
    data_key = "core_banking"

    def get_accounts_summary(self, customer_id: str) -> dict[str, Any]:
        return self._record(customer_id)
