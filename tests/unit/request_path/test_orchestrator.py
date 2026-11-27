"""Diagram box: Router + orchestrator (lane A), the orchestrator.
Proves the plan-and-execute loop respects the step limit, the time budget and step conditions,
and that agents are reached only through the orchestrator (hub and spoke).
"""
from __future__ import annotations

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


def run_plan(copilot, plan: Plan, agent: StubAgent, **ctx_overrides):
    dataset = load_json("customers.json")
    orchestrator = Orchestrator({"policy": agent}, StubPlanner(plan), Checkpointer(), ApprovalService(),
                                CommitOutbox(MockCrm(dataset)), EpisodicMemory(dataset, BUSINESS_DATE))
    ctx = run_context(copilot, **ctx_overrides)
    request = Request(request_id="rq-test", user_id="rm-1", masked_text="ask")
    return orchestrator.run(RouteDecision(path="planner", reason="test"), request, ctx), ctx


def test_step_limit_stops_the_loop_and_says_so(copilot):
    agent = StubAgent()
    outcome, _ = run_plan(copilot, plan_of(MAX_STEPS + 1), agent)
    assert agent.tasks == [f"step {i}" for i in range(1, MAX_STEPS + 1)]
    assert outcome.result.notices[0].startswith("Stopped early")


def test_exhausted_time_budget_runs_no_step_and_declines(copilot):
    agent = StubAgent()
    outcome, ctx = run_plan(copilot, plan_of(2), agent, time_budget_s=0.0)
    assert agent.tasks == [] and outcome.result.status == "declined"
    assert any(s.status == "stopped" for s in ctx.trace.spans)


def test_conditional_step_runs_only_when_the_earlier_outcome_matches(copilot):
    plan = plan_of(2, {2: StepCondition(step_id=1, outcome="no")})
    said_yes, said_no = StubAgent(outcome="yes"), StubAgent(outcome="no")
    _, ctx = run_plan(copilot, plan, said_yes)
    run_plan(copilot, plan, said_no)
    assert said_yes.tasks == ["step 1"] and said_no.tasks == ["step 1", "step 2"]
    assert any(s.status == "skipped" for s in ctx.trace.spans)


def test_agents_are_reached_only_through_the_orchestrator(copilot):
    """Hub and spoke: no agent holds another agent, no agent is a tool, and every agent span follows an
    orchestrator decision in a real trace."""
    for agent in copilot.pipeline.orchestrator._agents.values():
        assert not [value for value in vars(agent).values() if isinstance(value, Agent)]
    tool_servers = {t.split(".")[0] for tools in AGENT_ALLOW_LIST.values() for t in tools}
    assert not tool_servers & {"brief", "policy", "followup", *AGENT_ALLOW_LIST}
    trace = ask(copilot, "rm-1", POLICY_QUESTION).trace
    agent_spans = [i for i, s in enumerate(trace.spans) if s.component.value.endswith("agent")]
    assert agent_spans and all(trace.spans[i - 1].component.value == "Router + orchestrator" for i in agent_spans)
