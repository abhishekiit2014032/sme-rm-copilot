"""Diagram box: RM and Answer to RM (lane A), the request and the final answer.
AskRequest is the API body; Request is the screened, PII-masked version used inside.
Nothing is mocked here.
"""
from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from copilot.contracts.results import Citation, Draft, SourceRef
from copilot.contracts.trace import Trace

UiAction = Literal["prepare_brief", "draft_follow_up"]


class AskRequest(BaseModel):
    """What the CRM sidebar or chat sends. A UI button sets `action`; free text sets `text`."""

    text: str = ""
    action: UiAction | None = None
    customer_id: str | None = None


class Request(BaseModel):
    """The request after the gateway and input guardrails: identity checked, PII masked."""

    request_id: str
    user_id: str
    masked_text: str
    action: UiAction | None = None
    customer_id: str | None = None


class AnswerStatus(str, Enum):
    ANSWERED = "answered"
    DEGRADED = "degraded"              # answered, but a non-critical source was unavailable
    DECLINED = "declined"              # not enough evidence, or a check failed twice
    BLOCKED = "blocked"                # input guardrails stopped it
    DENIED = "denied"                  # identity, rate limit or entitlement check failed
    AWAITING_APPROVAL = "awaiting_approval"
    COMMITTED = "committed"
    REJECTED = "rejected"              # approval token invalid, expired or replayed


class Answer(BaseModel):
    request_id: str
    status: AnswerStatus
    path: Literal["fast", "planner", "cache", "none"] = "none"
    text: str
    citations: list[Citation] = Field(default_factory=list)
    sources: list[SourceRef] = Field(default_factory=list)
    drafts: list[Draft] = Field(default_factory=list)
    approval_token: str | None = None
    notices: list[str] = Field(default_factory=list)
    trace: Trace
