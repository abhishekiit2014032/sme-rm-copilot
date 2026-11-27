"""Diagram box: Systems of record (lane B), case management.
Returns complaints and service requests with their status.
Mocked: data from customers.json.
"""
from __future__ import annotations

from typing import Any

from mocks.systems import MockSystem


class MockCaseManagement(MockSystem):
    system_name = "Case management"
    data_key = "case_management"

    def get_cases(self, customer_id: str) -> dict[str, Any]:
        return self._record(customer_id)
