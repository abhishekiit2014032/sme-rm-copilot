"""Diagram box: Tool gateway (MCP) (lane B), the MCP servers behind it.
Each server exposes a few typed tools over one or more systems; it is only ever called by the tool gateway.
Mocked: servers are in-process classes instead of separate MCP processes.
"""
from __future__ import annotations

from typing import Any, Callable

from copilot.contracts.context import RunContext
from copilot.contracts.tools import ToolResult


class McpServer:
    """Base class: a name, a table of tools, and which system of record each tool reads."""

    name = "server"

    def tools(self) -> dict[str, Callable[..., ToolResult]]:
        raise NotImplementedError

    def system_for(self, tool: str) -> str:
        return self.name

    def call(self, tool: str, args: dict[str, Any], ctx: RunContext) -> ToolResult:
        """Tools act on behalf of the signed-in RM: ctx carries the identity and the trace."""
        return self.tools()[tool](ctx=ctx, **args)
