"""Diagram box: Brief, Policy and Follow-up agents (lane B), what an agent returns.
AgentResult carries the text, the citations and the source data that output guardrails check against.
Nothing is mocked here.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AgentStatus = Literal["ok", "degraded", "declined", "needs_approval"]


class Citation(BaseModel):
    """A policy clause the answer relies on."""

    doc_id: str
    doc_title: str
    clause: str          # for example "4.2"
    heading: str
    version: str
    clearance: str = "internal"   # "public" answers may be cached; others never are
    score: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.doc_title} §{self.clause} (v{self.version})"


class SourceRef(BaseModel):
    """A system of record that supplied data, with the time the data was read."""

    system: str
    detail: str
    as_of: str
    available: bool = True


class Draft(BaseModel):
    """A CRM write prepared by the follow-up agent. Nothing reaches the CRM until the RM approves."""

    draft_id: str
    kind: Literal["note", "task", "email"]
    customer_id: str
    title: str
    body: str
    due_date: str | None = None


class AgentResult(BaseModel):
    agent: str
    status: AgentStatus
    text: str
    outcome: str | None = None                        # e.g. "yes" / "no" for a policy check
    citations: list[Citation] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)  # clause and tool text the answer may rely on
    drafts: list[Draft] = Field(default_factory=list)
    notices: list[str] = Field(default_factory=list)
