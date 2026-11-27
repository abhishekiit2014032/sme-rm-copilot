"""Diagram box: Policy agent (lane B).
Answers policy and product questions from retrieved clauses only, citing each clause; declines below the gate.
Mocked: the model (scripted) and the index (in memory); the logic is the production logic.
"""
from __future__ import annotations

from pydantic import BaseModel

from copilot.contracts.context import RunContext
from copilot.contracts.plan import Task
from copilot.contracts.results import AgentResult, Citation, SourceRef
from copilot.contracts.trace import Component
from copilot.execution.agents import Agent
from copilot.execution.model_gateway import ModelTier
from copilot.execution.retrieval import RetrievalResult, ScoredClause


class PolicyAnswerDraft(BaseModel):
    """Structured output the model must return."""

    outcome: str               # "yes", "no", "info" or "unknown"
    answer: str
    cited_refs: list[str]


class PolicyAgent(Agent):
    name = "policy"
    agent_id = "agent-policy"

    def run(self, task: Task, ctx: RunContext) -> AgentResult:
        with ctx.trace.span(Component.POLICY_AGENT, task.instruction) as span:
            found = self.call_tool("policy-search", "search_policy", ctx, query=task.instruction)
            retrieval = RetrievalResult.model_validate(found.data)
            if not retrieval.passed_gate:
                span.status, span.detail = "declined", "retrieval gate: no clause strong enough"
                return _decline(retrieval)
            draft = self._models.generate(ModelTier.PRIMARY, "policy.answer", _prompt_vars(task, retrieval),
                                          PolicyAnswerDraft, ctx)
            citations = _citations(draft.cited_refs, retrieval.clauses)
            span.detail = f"outcome {draft.outcome}; cites " + ", ".join(f"§{c.clause}" for c in citations)
            return AgentResult(
                agent=self.name, status="ok", text=draft.answer, outcome=draft.outcome, citations=citations,
                evidence=[task.instruction] + [f"{s.clause.heading}. {s.clause.text}" for s in retrieval.clauses],
                sources=[SourceRef(system="Policy index", detail=f"{c.doc_title} v{c.version}",
                                   as_of="current version") for c in citations],
            )


def _prompt_vars(task: Task, retrieval: RetrievalResult) -> dict:
    clauses = [{"ref": s.clause.ref, "ref_label": f"§{s.clause.clause}", "heading": s.clause.heading,
                "text": s.clause.text} for s in retrieval.clauses]
    return {"question": task.instruction, "clauses": clauses}


def _citations(refs: list[str], scored: list[ScoredClause]) -> list[Citation]:
    """Keep only citations that point at clauses we actually retrieved."""
    by_ref = {s.clause.ref: s for s in scored}
    return [Citation(doc_id=s.clause.doc_id, doc_title=s.clause.doc_title, clause=s.clause.clause,
                     heading=s.clause.heading, version=s.clause.version, clearance=s.clause.clearance,
                     score=s.score)
            for ref in refs if (s := by_ref.get(ref))]


def _decline(retrieval: RetrievalResult) -> AgentResult:
    return AgentResult(
        agent="policy", status="declined", outcome="unknown",
        text=(f"I could not find a policy clause that answers this with enough confidence "
              f"(best match {retrieval.best_score:.2f}, needs {retrieval.threshold:.2f}). "
              "Please check with the credit desk."),
    )
