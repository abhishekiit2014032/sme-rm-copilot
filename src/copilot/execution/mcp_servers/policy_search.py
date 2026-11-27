"""Diagram box: Tool gateway (MCP) (lane B), the policy-search MCP server.
One tool, search_policy, over the hybrid retriever; clearance comes from the caller's role.
Mocked: nothing beyond the in-memory index; production fronts the managed search service.
"""
from __future__ import annotations

from typing import Callable

from copilot.contracts.context import RunContext
from copilot.contracts.identity import Role
from copilot.contracts.tools import ToolResult
from copilot.execution.mcp_servers import McpServer
from copilot.execution.retrieval import HybridRetriever

ROLE_CLEARANCES = {
    Role.RM: {"public", "internal"},
    Role.CREDIT_DESK: {"public", "internal", "restricted"},
}


class PolicySearchServer(McpServer):
    name = "policy-search"

    def __init__(self, retriever: HybridRetriever) -> None:
        self.retriever = retriever

    def tools(self) -> dict[str, Callable[..., ToolResult]]:
        return {"search_policy": self.search_policy}

    def system_for(self, tool: str) -> str:
        return "Policy index"

    def search_policy(self, query: str, ctx: RunContext) -> ToolResult:
        clearances = set().union(*(ROLE_CLEARANCES[r] for r in ctx.identity.roles))
        result = self.retriever.search(query, clearances, ctx.trace)
        return ToolResult(ok=True, data=result.model_dump(mode="json"), source_system="Policy index",
                          as_of="indexed on change (CDC)")
