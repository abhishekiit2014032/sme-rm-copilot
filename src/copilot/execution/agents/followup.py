"""Diagram box: Follow-up agent (lane B).
Turns the RM's notes into a CRM note, tasks and an email DRAFT; nothing is saved until the RM approves.
Mocked: the model (scripted) and the CRM draft store; the approval and commit flow is real.
"""
from __future__ import annotations

from pydantic import BaseModel

from copilot.contracts.context import RunContext
from copilot.contracts.plan import Task
from copilot.contracts.results import AgentResult, Draft, SourceRef
from copilot.contracts.trace import Component
from copilot.execution.agents import Agent, fill_placeholders
from copilot.execution.model_gateway import ModelTier


class FollowUpTask(BaseModel):
    title: str
    due_date: str
    owner: str


class FollowUpExtraction(BaseModel):
    """Structured output the model must return."""

    note: str
    tasks: list[FollowUpTask]
    email_subject: str
    email_body: str


class FollowUpAgent(Agent):
    name = "followup"
    agent_id = "agent-followup"

    def __init__(self, *args, today: str, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._today = today

    def run(self, task: Task, ctx: RunContext) -> AgentResult:
        with ctx.trace.span(Component.FOLLOWUP_AGENT, "draft follow-up") as span:
            customer_id = task.customer_id or ""
            profile = self.call_tool("customer-data", "get_customer_profile", ctx, customer_id=customer_id)
            if not profile.ok:
                span.status = "declined"
                return AgentResult(agent=self.name, status="declined",
                                   text="The CRM is unavailable, so the follow-up cannot be drafted yet.")
            notes = _strip_command(task.instruction)
            extraction = self._models.generate(ModelTier.MINI, "followup.extract",
                                               {"notes": notes, "today": self._today}, FollowUpExtraction, ctx)
            names = {"contact_first_name": profile.data["key_contact"].split()[0], "rm_name": ctx.identity.name}
            drafts = self._create_drafts(customer_id, profile.data["contact_email"], extraction, names, ctx)
            span.detail = f"{len(drafts)} drafts created, waiting for RM approval"
            text = (f"I drafted a CRM note, {len(extraction.tasks)} tasks and an email for {profile.data['name']}. "
                    "Nothing is saved to the CRM until you approve.")
            return AgentResult(
                agent=self.name, status="needs_approval", drafts=drafts, text=text,
                evidence=[notes, text],      # the summary line is written by code from the drafts, not by a model
                sources=[SourceRef(system=profile.source_system, detail="customer profile", as_of=profile.as_of)],
            )

    def _create_drafts(self, customer_id: str, email: str, extraction: FollowUpExtraction, names: dict[str, str],
                       ctx: RunContext) -> list[Draft]:
        results = [
            self.call_tool("crm-drafts", "create_note_draft", ctx, customer_id=customer_id, text=extraction.note),
            self.call_tool("crm-drafts", "create_task_drafts", ctx, customer_id=customer_id,
                           tasks=[t.model_dump() for t in extraction.tasks]),
            self.call_tool("crm-drafts", "create_email_draft", ctx, customer_id=customer_id, to=email,
                           subject=extraction.email_subject, body=fill_placeholders(extraction.email_body, names)),
        ]
        return [Draft.model_validate(d) for r in results for d in r.data]


def _strip_command(text: str) -> str:
    for prefix in ("draft follow-up:", "draft follow up:", "follow-up:"):
        if text.lower().startswith(prefix):
            return text[len(prefix):].strip()
    return text.strip()
