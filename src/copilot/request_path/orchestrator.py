"""Diagram box: Router + orchestrator (lane A), the plan-and-execute loop, pause for approval and resume.
Plain Python for readability; production runs the same nodes as a LangGraph graph with a persistent checkpointer:
route -> plan -> execute_step (loop) -> compose -> await_approval (interrupt) -> commit. Nothing is mocked here.
"""
from __future__ import annotations

from pydantic import BaseModel

from copilot.contracts.context import RunContext
from copilot.contracts.plan import AgentName, Plan, PlanStep, RouteDecision, Task
from copilot.contracts.request import Request
from copilot.contracts.results import AgentResult, Draft
from copilot.contracts.trace import Component
from copilot.execution.agents import Agent
from copilot.execution.approvals import ApprovalService, CommitOutbox, CommitReport
from copilot.execution.model_gateway import ModelGateway, ModelTier
from copilot.request_path.input_guardrails import unmask_pii
from copilot.state.memory.checkpointer import Checkpoint, Checkpointer
from copilot.state.memory.episodic import EpisodicMemory

MAX_STEPS = 4


class OrchestratorResult(BaseModel):
    path: str
    result: AgentResult
    plan: Plan | None = None
    approval_token: str | None = None


class Orchestrator:
    def __init__(self, agents: dict[AgentName, Agent], models: ModelGateway, checkpointer: Checkpointer,
                 approvals: ApprovalService, outbox: CommitOutbox, episodic: EpisodicMemory) -> None:
        self._agents, self._models = agents, models
        self._checkpointer, self._approvals = checkpointer, approvals
        self._outbox, self._episodic = outbox, episodic

    # ---- run --------------------------------------------------------------------------------
    def run(self, route: RouteDecision, request: Request, ctx: RunContext) -> OrchestratorResult:
        if route.path == "fast" and route.task:
            result, plan = self._run_agent(route.task, ctx), None
        else:
            plan = self._plan(request, ctx)
            result = self._execute(plan, ctx)
        token = self._pause_for_approval(result, ctx) if result.status == "needs_approval" else None
        return OrchestratorResult(path=route.path, result=result, plan=plan, approval_token=token)

    def _plan(self, request: Request, ctx: RunContext) -> Plan:
        """The planner LLM returns a Plan; Pydantic validation rejects anything malformed."""
        plan = self._models.generate(ModelTier.PLANNER, "planner.plan",
                                     {"text": request.masked_text, "customer_id": request.customer_id}, Plan, ctx)
        steps = "; ".join(f"{s.step_id}. {s.task.agent}" + (f" (if step {s.run_if.step_id} = {s.run_if.outcome})"
                                                              if s.run_if else "") for s in plan.steps)
        ctx.trace.event(Component.ROUTER_ORCHESTRATOR, "plan", detail=steps, steps=len(plan.steps))
        return plan

    def _execute(self, plan: Plan, ctx: RunContext) -> AgentResult:
        """The agent loop: run each step in order, skip steps whose condition is not met, stop at the limits."""
        results: dict[int, AgentResult] = {}
        notices: list[str] = []
        for number, step in enumerate(plan.steps, start=1):
            if number > MAX_STEPS or ctx.time_left() <= 0:
                notices.append("Stopped early: step limit or time budget reached.")
                ctx.trace.event(Component.ROUTER_ORCHESTRATOR, "stop", detail=notices[-1], status="stopped")
                break
            if not _condition_met(step, results):
                ctx.trace.event(Component.ROUTER_ORCHESTRATOR, f"step {step.step_id}: skipped",
                                detail=f"step {step.run_if.step_id} outcome was not '{step.run_if.outcome}'",
                                status="skipped")
                continue
            ctx.trace.event(Component.ROUTER_ORCHESTRATOR, f"step {step.step_id}: {step.task.agent} agent",
                            detail=step.task.instruction)
            # The customer comes from the request, never from the planner's output.
            task = step.task.model_copy(update={"customer_id": ctx.customer_id})
            results[step.step_id] = self._run_agent(task, ctx)
        return _compose(list(results.values()), notices)

    def _run_agent(self, task: Task, ctx: RunContext) -> AgentResult:
        return self._agents[task.agent].run(task, ctx)

    # ---- pause and resume (human in the loop) -----------------------------------------------
    def _pause_for_approval(self, result: AgentResult, ctx: RunContext) -> str:
        state = {"customer_id": ctx.customer_id, "drafts": [d.model_dump() for d in result.drafts],
                 "pii_vault": ctx.pii_vault}
        self._checkpointer.save(ctx.request_id, "await_approval", ctx.identity.user_id, state, ctx.trace)
        return self._approvals.issue(ctx.request_id, [d.draft_id for d in result.drafts], ctx.identity.user_id,
                                     ctx.trace)

    def resume(self, approval_token: str, ctx: RunContext) -> CommitReport:
        """Verify the token, load the checkpoint, commit through the outbox, then use the token up and remember the
        approved note. If the CRM is down the writes stay in the outbox and the token stays valid for a retry."""
        claims = self._approvals.verify(approval_token, ctx.identity, ctx.trace)
        checkpoint = self._checkpointer.load(claims.thread_id, ctx.identity.user_id, ctx.trace)
        if checkpoint is None:
            raise LookupError("nothing to resume for this approval")
        drafts = _approved_drafts(checkpoint, claims.draft_ids)
        self._outbox.enqueue(drafts, claims.thread_id, ctx.trace)
        report = self._outbox.dispatch(ctx.trace)
        if report.pending:
            return report
        self._approvals.consume(claims, ctx.trace)
        self._remember(checkpoint, drafts, ctx)
        self._checkpointer.complete(claims.thread_id)
        return report

    def _remember(self, checkpoint: Checkpoint, drafts: list[Draft], ctx: RunContext) -> None:
        note = next((d for d in drafts if d.kind == "note"), None)
        if note:
            self._episodic.add_approved(checkpoint.state["customer_id"], ctx.identity.user_id, note.body)
            ctx.trace.event(Component.MEMORY_STORE, "episodic memory updated",
                            detail="RM-approved meeting note kept for 90 days")


def _condition_met(step: PlanStep, results: dict[int, AgentResult]) -> bool:
    if step.run_if is None:
        return True
    earlier = results.get(step.run_if.step_id)
    return earlier is not None and earlier.outcome == step.run_if.outcome


def _compose(results: list[AgentResult], notices: list[str]) -> AgentResult:
    """Merge step results in order. Drafts carry through, so a plan with a follow-up step still pauses for
    approval. The answer is declined only if every step declined."""
    if not results:
        return AgentResult(agent="orchestrator", status="declined", text="No step could be run.", notices=notices)
    statuses = {r.status for r in results}
    status = ("needs_approval" if "needs_approval" in statuses else "declined" if statuses == {"declined"}
              else "degraded" if "degraded" in statuses else "ok")
    citations = {c.label: c for r in results for c in r.citations}
    return AgentResult(
        agent=results[0].agent if len({r.agent for r in results}) == 1 else "orchestrator",
        status=status,
        text="\n\n".join(r.text for r in results),
        outcome=results[0].outcome,
        citations=list(citations.values()),
        sources=list({(s.system, s.detail): s for r in results for s in r.sources}.values()),
        evidence=[e for r in results for e in r.evidence],
        drafts=[d for r in results for d in r.drafts],
        notices=notices + [n for r in results for n in r.notices],
    )


def _approved_drafts(checkpoint: Checkpoint, draft_ids: list[str]) -> list[Draft]:
    """Only the drafts named in the signed token, with masked PII restored for the CRM."""
    vault = checkpoint.state.get("pii_vault", {})
    drafts = [Draft.model_validate(d) for d in checkpoint.state["drafts"] if d["draft_id"] in draft_ids]
    return [d.model_copy(update={"body": unmask_pii(d.body, vault), "title": unmask_pii(d.title, vault)})
            for d in drafts]
