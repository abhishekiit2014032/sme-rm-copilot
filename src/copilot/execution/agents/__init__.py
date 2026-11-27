"""Diagram box: Brief, Policy and Follow-up agents (lane B), the shared agent base.
An agent has its own identity, calls tools only through the tool gateway and models only through the model gateway.
Agents report only to the orchestrator (hub and spoke); they never call each other.
"""
from __future__ import annotations

import re
from typing import Any

from copilot.contracts.context import RunContext
from copilot.contracts.plan import AgentName, Task
from copilot.contracts.results import AgentResult
from copilot.contracts.tools import ToolCall, ToolResult
from copilot.execution.model_gateway import ModelGateway
from copilot.execution.tool_gateway import ToolGateway

WORKLOAD_ID = "wl-orchestrator"   # agents run inside the orchestrator workload
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")


class Agent:
    name: AgentName
    agent_id: str

    def __init__(self, tools: ToolGateway, models: ModelGateway) -> None:
        self._tools, self._models = tools, models

    def run(self, task: Task, ctx: RunContext) -> AgentResult:
        raise NotImplementedError

    def call_tool(self, server: str, tool: str, ctx: RunContext, **args: Any) -> ToolResult:
        call = ToolCall(server=server, tool=tool, args=args, agent_id=self.agent_id, workload_id=WORKLOAD_ID)
        return self._tools.call(call, ctx)


def fill_placeholders(text: str, facts: dict[str, str]) -> str:
    """Numbers, names and dates come from tool data, never from the model: the model writes {{key}}."""
    return PLACEHOLDER.sub(lambda m: facts.get(m.group(1), "[not available]"), text)
