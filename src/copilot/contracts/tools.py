"""Diagram box: Tool gateway (MCP) (lane B), a typed tool call and its result.
Every call names the agent, the workload and the tool; the gateway checks all of them.
Nothing is mocked here.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    server: str                 # MCP server, e.g. "customer-data"
    tool: str                   # e.g. "get_facilities"
    args: dict[str, Any] = Field(default_factory=dict)
    agent_id: str               # agent identity, e.g. "agent-brief"
    workload_id: str            # workload identity of the calling service

    @property
    def qualified_name(self) -> str:
        return f"{self.server}.{self.tool}"


class ToolResult(BaseModel):
    ok: bool
    data: Any = None
    error: str | None = None
    source_system: str = ""
    as_of: str = ""
