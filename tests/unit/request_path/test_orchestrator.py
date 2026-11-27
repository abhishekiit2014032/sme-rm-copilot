"""Diagram box: Router + orchestrator (lane A), the orchestrator.
Proves the plan-and-execute loop respects the step limit, the time budget and step conditions,
and that agents are reached only through the orchestrator (hub and spoke).
"""
from __future__ import annotations

import pytest

from copilot.contracts import AgentResult, Plan, PlanStep, Request, RouteDecision, StepCondition, Task
from copilot.execution.agents import Agent
from copilot.execution.approvals import ApprovalService, CommitOutbox
from copilot.execution.tool_gateway import AGENT_ALLOW_LIST
from copilot.request_path.orchestrator import MAX_STEPS, Orchestrator
from copilot.state.memory.checkpointer import Checkpointer
from copilot.state.memory.episodic import EpisodicMemory
from helpers import POLICY_QUESTION, ask, run_context
from mocks.data import BUSINESS_DATE, load_json
from mocks.systems.crm import MockCrm


class StubAgent:
    """Records each task it runs and answers with a fixed outcome."""

    def __init__(self, outcome: str = "yes") -> None:
        self.outcome, self.tasks = outcome, []

    def run(self, task, ctx) -> AgentResult:
        self.tasks.append(task.instruction)
        return AgentResult(agent="policy", status="ok", text=task.instruction, outcome=self.outcome)


class StubPlanner:
    """Stands in for the model gateway: always returns the given plan."""

    def __init__(self, plan: Plan) -> None:
        self.plan = plan

    def generate(self, tier, prompt_id, variables, output, ctx):
        return self.plan


def plan_of(n: int, conditions: dict[int, StepCondition] | None = None) -> Plan:
    conditions = conditions or {}
    return Plan(goal="test", steps=[PlanStep(step_id=i, task=Task(agent="policy", instruction=f"step {i}"),
                                             run_if=conditions.get(i)) for i in range(1, n + 1)])


def orchestrator_with(plan: Plan, agent: StubAgent) -> Orchestrator:
    dataset = load_json("customers.json")
    return Orchestrator({"policy": agent}, StubPlanner(plan), Checkpointer(), ApprovalService(),
                        CommitOutbox(MockCrm(dataset)), EpisodicMemory(dataset, BUSINESS_DATE))


def run_planner(copilot, orchestrator: Orchestrator, **ctx_overrides):
    ctx = run_context(copilot, **ctx_overrides)
    request = Request(request_id="rq-test", user_id="rm-1", masked_text="ask")
    return orchestrator.run(RouteDecision(path="planner", reason="test"), request, ctx), ctx


def test_planner_runs_every_step_in_order(copilot):
    agent = StubAgent()
    outcome, _ = run_planner(copilot, orchestrator_with(plan_of(3), agent))
    assert agent.tasks == ["step 1", "step 2", "step 3"]
    assert outcome.path == "planner" and outcome.result.status == "ok"


def test_step_limit_stops_the_loop(copilot):
    agent = StubAgent()
    outcome, _ = run_planner(copilot, orchestrator_with(plan_of(MAX_STEPS + 1), agent))
    assert len(agent.tasks) == MAX_STEPS
    assert "Stopped early" in outcome.result.notices[0]


def test_exhausted_time_budget_runs_no_step_and_declines(copilot):
    agent = StubAgent()
    outcome, ctx = run_planner(copilot, orchestrator_with(plan_of(2), agent), time_budget_s=0.0)
    assert agent.tasks == []
    assert outcome.result.status == "declined"
    assert any(s.status == "stopped" for s in ctx.trace.spans)


def test_conditional_step_is_skipped_when_condition_not_met(copilot):
    plan = plan_of(2, {2: StepCondition(step_id=1, outcome="no")})
    agent = StubAgent(outcome="yes")
    _, ctx = run_planner(copilot, orchestrator_with(plan, agent))
    assert agent.tasks == ["step 1"]
    assert any(s.status == "skipped" for s in ctx.trace.spans)


def test_conditional_step_runs_when_condition_met(copilot):
    plan = plan_of(2, {2: StepCondition(step_id=1, outcome="no")})
    agent = StubAgent(outcome="no")
    run_planner(copilot, orchestrator_with(plan, agent))
    assert agent.tasks == ["step 1", "step 2"]


def test_plan_longer_than_five_steps_is_rejected_by_the_contract():
    with pytest.raises(ValueError):
        plan_of(6)


def test_agents_hold_no_reference_to_another_agent(copilot):
    """Hub and spoke: agents report to the orchestrator and never call each other."""
    agents = copilot.pipeline.orchestrator._agents.values()
    for agent in agents:
        assert not [value for value in vars(agent).values() if isinstance(value, Agent)]


def test_no_agent_tool_is_exposed_through_the_tool_gateway():
    agent_servers = {"brief", "policy", "followup", "agent-brief", "agent-policy", "agent-followup"}
    for tools in AGENT_ALLOW_LIST.values():
        assert not {t.split(".")[0] for t in tools} & agent_servers


def test_every_agent_span_follows_an_orchestrator_span_in_a_real_trace(copilot):
    trace = ask(copilot, "rm-1", POLICY_QUESTION).trace
    agent_spans = [i for i, s in enumerate(trace.spans) if s.component.value.endswith("agent")]
    assert agent_spans
    for index in agent_spans:
        assert trace.spans[index - 1].component.value == "Router + orchestrator"
