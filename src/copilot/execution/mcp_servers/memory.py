"""Diagram box: Tool gateway (MCP) (lane B), the memory MCP server.
Lets agents recall RM-approved episodes for a customer and the RM's recent session turns.
Mocked: reads the in-memory stores in copilot/state/memory.
"""
from __future__ import annotations

from typing import Callable

from copilot.contracts.context import RunContext
from copilot.contracts.tools import ToolResult
from copilot.execution.mcp_servers import McpServer
from copilot.state.memory.episodic import EpisodicMemory
from copilot.state.memory.session import SessionMemory


class MemoryServer(McpServer):
    name = "memory"

    def __init__(self, episodic: EpisodicMemory, session: SessionMemory) -> None:
        self.episodic, self.session = episodic, session

    def tools(self) -> dict[str, Callable[..., ToolResult]]:
        return {"recall_episodes": self.recall_episodes, "get_session": self.get_session}

    def system_for(self, tool: str) -> str:
        return "Memory store"

    def recall_episodes(self, customer_id: str, ctx: RunContext) -> ToolResult:
        episodes = self.episodic.recall(customer_id, ctx.identity.user_id)
        return ToolResult(ok=True, data=[e.model_dump() for e in episodes], source_system="Memory store (episodic)",
                          as_of="RM-approved summaries, 90 days")

    def get_session(self, ctx: RunContext) -> ToolResult:
        turns = self.session.recent(ctx.identity.user_id)
        return ToolResult(ok=True, data=[t.model_dump() for t in turns], source_system="Memory store (session)",
                          as_of="last 24 h")
