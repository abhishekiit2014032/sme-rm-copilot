"""Diagram box: Tool gateway (MCP) (lane B), the customer-data MCP server.
Read-only tools over CRM, core banking, lending, trade finance and case management; data is read live, never copied.
Mocked: the five systems are mocks/systems/*; production calls REST APIs or ODS views.
"""
from __future__ import annotations

from typing import Any, Callable

from copilot.contracts.context import RunContext
from copilot.contracts.ports import (CaseManagementSystem, CoreBankingSystem, CrmSystem, LendingSystem,
                                     SystemOfRecord, TradeFinanceSystem)
from copilot.contracts.tools import ToolResult
from copilot.execution.mcp_servers import McpServer


class CustomerDataServer(McpServer):
    name = "customer-data"

    def __init__(self, crm: CrmSystem, core_banking: CoreBankingSystem, lending: LendingSystem,
                 trade: TradeFinanceSystem, cases: CaseManagementSystem) -> None:
        self.crm, self.core_banking, self.lending, self.trade, self.cases = crm, core_banking, lending, trade, cases

    def tools(self) -> dict[str, Callable[..., ToolResult]]:
        return {
            "get_customer_profile": self.get_customer_profile,
            "get_interactions": self.get_interactions,
            "get_open_requests": self.get_open_requests,
            "get_accounts_summary": self.get_accounts_summary,
            "get_facilities": self.get_facilities,
            "get_trade_positions": self.get_trade_positions,
        }

    def system_for(self, tool: str) -> str:
        return {"get_accounts_summary": "Core banking (ODS)", "get_facilities": "Lending",
                "get_trade_positions": "Trade finance"}.get(tool, "CRM")

    def get_customer_profile(self, customer_id: str, ctx: RunContext) -> ToolResult:
        return _result(self.crm, self.crm.get_profile(customer_id))

    def get_interactions(self, customer_id: str, ctx: RunContext) -> ToolResult:
        return _result(self.crm, self.crm.get_interactions(customer_id))

    def get_open_requests(self, customer_id: str, ctx: RunContext) -> ToolResult:
        """CRM requests plus open complaints and service requests from case management."""
        cases = self.cases.get_cases(customer_id)
        data = {
            "requests": self.crm.get_open_requests(customer_id),
            "complaints": [c for c in cases.get("complaints", []) if c["status"] != "Closed"],
            "service_requests": [s for s in cases.get("service_requests", []) if s["status"] != "Closed"],
        }
        return _result(self.crm, data, system="CRM + case management")

    def get_accounts_summary(self, customer_id: str, ctx: RunContext) -> ToolResult:
        return _result(self.core_banking, self.core_banking.get_accounts_summary(customer_id))

    def get_facilities(self, customer_id: str, ctx: RunContext) -> ToolResult:
        return _result(self.lending, self.lending.get_facilities(customer_id))

    def get_trade_positions(self, customer_id: str, ctx: RunContext) -> ToolResult:
        return _result(self.trade, self.trade.get_trade_positions(customer_id))


def _result(source: SystemOfRecord, data: Any, system: str | None = None) -> ToolResult:
    return ToolResult(ok=True, data=data, source_system=system or source.system_name, as_of=source.as_of)
