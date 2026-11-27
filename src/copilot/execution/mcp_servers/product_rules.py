"""Diagram box: Tool gateway (MCP) (lane B), the product-rules MCP server.
Turns customer signals (utilisation, import volume, balances) into product suggestions with a reason.
Mocked: rules come from mocks/data/product_rules.json instead of the product team's rules service.
"""
from __future__ import annotations

import operator
from typing import Any, Callable

from copilot.contracts.context import RunContext
from copilot.contracts.money import format_aed, format_pct
from copilot.contracts.tools import ToolResult
from copilot.execution.mcp_servers import McpServer

OPERATORS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt}


class ProductRulesServer(McpServer):
    name = "product-rules"

    def __init__(self, rules: list[dict[str, Any]]) -> None:
        self.rules = rules

    def tools(self) -> dict[str, Callable[..., ToolResult]]:
        return {"get_product_suggestions": self.get_product_suggestions}

    def system_for(self, tool: str) -> str:
        return "Product rules"

    def get_product_suggestions(self, signals: dict[str, float], ctx: RunContext) -> ToolResult:
        suggestions = []
        for rule in self.rules:
            value = signals.get(rule["signal"])
            if value is not None and OPERATORS[rule["operator"]](value, rule["threshold"]):
                shown = format_pct(value).rstrip("%") if rule["signal"].endswith("_pct") else format_aed(value)
                suggestions.append({"rule_id": rule["id"], "product": rule["product"],
                                    "reason": rule["reason"].format(value=shown)})
        return ToolResult(ok=True, data=suggestions, source_system="Product rules", as_of="rules v2026.11")
