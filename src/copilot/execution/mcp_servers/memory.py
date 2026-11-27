"""Diagram box: Tool gateway (MCP) (lane B), the memory MCP server.
Lets the brief agent recall RM-approved meeting summaries (episodic memory) for a customer.
Mocked: reads the in-memory stores in copilot/state/memory.
"""
from __future__ import annotations

from typing import Callable

from copilot.contracts.context import RunContext
from copilot.contracts.tools import ToolResult
from copilot.execution.mcp_servers import McpServer
from copilot.state.memory.episodic import EpisodicMemory


class MemoryServer(McpServer):
    name = "memory"

    def __init__(self, episodic: EpisodicMemory) -> None:
        self.episodic = episodic

    def tools(self) -> dict[str, Callable[..., ToolResult]]:
        return {"recall_episodes": self.recall_episodes}

    def system_for(self, tool: str) -> str:
        return "Memory store"

    def recall_episodes(self, customer_id: str, ctx: RunContext) -> ToolResult:
        episodes = self.episodic.recall(customer_id, ctx.identity.user_id)
        return ToolResult(ok=True, data=[e.model_dump() for e in episodes], source_system="Memory store (episodic)",
                          as_of="RM-approved summaries, 90 days")
