"""Diagram box: Systems of record (lane B), the trade finance system.
Returns letters of credit, guarantees and twelve-month import volume.
Mocked: data from customers.json (production would sit behind a REST or SOAP facade).
"""
from __future__ import annotations

from typing import Any

from mocks.systems import MockSystem


class MockTradeFinance(MockSystem):
    system_name = "Trade finance"
    data_key = "trade_finance"

    def get_trade_positions(self, customer_id: str) -> dict[str, Any]:
        return self._record(customer_id)
