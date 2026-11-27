"""Diagram box: Router + orchestrator (lane A), the route and the plan.
The planner LLM must return a Plan that validates against these models, or the plan is rejected.
Nothing is mocked here.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AgentName = Literal["brief", "policy", "followup"]


class Task(BaseModel):
    agent: AgentName
    instruction: str
    customer_id: str | None = None


class StepCondition(BaseModel):
    """Run a step only if an earlier step ended with a given outcome (e.g. step 1 said "no")."""

    step_id: int
    outcome: str


class PlanStep(BaseModel):
    step_id: int
    task: Task
    run_if: StepCondition | None = None


class Plan(BaseModel):
    goal: str
    steps: list[PlanStep] = Field(min_length=1, max_length=5)


class RouteDecision(BaseModel):
    path: Literal["fast", "planner"]
    reason: str
    task: Task | None = None   # set on the fast path only
