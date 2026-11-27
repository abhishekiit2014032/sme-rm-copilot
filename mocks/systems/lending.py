"""Diagram box: Systems of record (lane B), the lending system.
Returns credit facilities with limits, utilisation and security.
Mocked: data from customers.json.
"""
from __future__ import annotations

from typing import Any

from mocks.systems import MockSystem


class MockLending(MockSystem):
    system_name = "Lending"
    data_key = "lending"

    def get_facilities(self, customer_id: str) -> dict[str, Any]:
        return self._record(customer_id)
