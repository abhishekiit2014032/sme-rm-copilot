"""Diagram box: shared contracts (all lanes).
Pydantic models passed across every boundary, plus the ports to external systems.
Nothing is mocked here.
"""
from copilot.contracts.approval import ApprovalToken
from copilot.contracts.identity import Identity, Role
from copilot.contracts.plan import AgentName, Plan, PlanStep, RouteDecision, StepCondition, Task
from copilot.contracts.request import Answer, AnswerStatus, AskRequest, Request
from copilot.contracts.results import AgentResult, Citation, Draft, SourceRef
from copilot.contracts.tools import ToolCall, ToolResult
from copilot.contracts.trace import COMPONENT_LANE, LANE_TITLES, Component, Lane, Trace, TraceSpan

__all__ = [
    "AgentName", "AgentResult", "Answer", "AnswerStatus", "ApprovalToken", "AskRequest", "Citation",
    "COMPONENT_LANE", "Component", "Draft", "Identity", "LANE_TITLES", "Lane", "Plan", "PlanStep",
    "Request", "Role", "RouteDecision", "SourceRef", "StepCondition", "Task", "ToolCall", "ToolResult",
    "Trace", "TraceSpan",
]
