"""Diagram box: Observability (lane D), the shape of a trace.
Defines lanes, diagram components and the trace span every component writes.
Nothing is mocked here; production exports the same spans to Langfuse and Azure Monitor.
"""
from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from enum import Enum
from typing import Any, Iterator

from pydantic import BaseModel, Field


class Lane(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    D = "D"


LANE_TITLES = {
    Lane.A: "Online request path",
    Lane.B: "Controlled execution",
    Lane.C: "State + data freshness",
    Lane.D: "Production operations",
}


class Component(str, Enum):
    """One value per box on the diagram. Span component names use these exact labels."""

    API_GATEWAY = "API gateway + identity"
    INPUT_GUARDRAILS = "Input guardrails"
    SEMANTIC_CACHE = "Semantic cache"
    ROUTER_ORCHESTRATOR = "Router + orchestrator"
    OUTPUT_GUARDRAILS = "Output guardrails"
    ANSWER = "Answer to RM"
    BRIEF_AGENT = "Brief agent"
    POLICY_AGENT = "Policy agent"
    FOLLOWUP_AGENT = "Follow-up agent"
    RETRIEVAL = "Retrieval / RAG"
    TOOL_GATEWAY = "Tool gateway (MCP)"
    MODEL_GATEWAY = "Model gateway"
    HUMAN_IN_THE_LOOP = "Human in the loop"
    SYSTEMS_OF_RECORD = "Systems of record"
    INDEX = "Vector + keyword index"
    LLM = "Primary / fallback LLM"
    INGESTION = "Knowledge ingestion"
    MEMORY_STORE = "Session + memory store"
    SECURE_STORAGE = "Secure storage"
    OBSERVABILITY = "Observability"
    EVALUATION = "Evaluation"
    RELIABILITY = "Reliability + scale"
    RELEASE = "Release + recovery"


COMPONENT_LANE: dict[Component, Lane] = {
    **{c: Lane.A for c in (Component.API_GATEWAY, Component.INPUT_GUARDRAILS, Component.SEMANTIC_CACHE,
                           Component.ROUTER_ORCHESTRATOR, Component.OUTPUT_GUARDRAILS, Component.ANSWER)},
    **{c: Lane.B for c in (Component.BRIEF_AGENT, Component.POLICY_AGENT, Component.FOLLOWUP_AGENT,
                           Component.RETRIEVAL, Component.TOOL_GATEWAY, Component.MODEL_GATEWAY,
                           Component.HUMAN_IN_THE_LOOP, Component.SYSTEMS_OF_RECORD, Component.INDEX,
                           Component.LLM)},
    **{c: Lane.C for c in (Component.INGESTION, Component.MEMORY_STORE, Component.SECURE_STORAGE)},
    **{c: Lane.D for c in (Component.OBSERVABILITY, Component.EVALUATION, Component.RELIABILITY,
                           Component.RELEASE)},
}


class TraceSpan(BaseModel):
    """One step in a request. Prompt text and customer payloads are never stored here."""

    span_id: int
    lane: Lane
    component: Component
    name: str
    status: str = "ok"
    detail: str = ""
    start_ms: float = 0.0
    latency_ms: float = 0.0
    attrs: dict[str, Any] = Field(default_factory=dict)


class Trace(BaseModel):
    trace_id: str = Field(default_factory=lambda: "tr-" + uuid.uuid4().hex[:10])
    request_id: str = ""
    user_id: str = ""
    spans: list[TraceSpan] = Field(default_factory=list)
    started_at: float = Field(default_factory=time.perf_counter, exclude=True)

    @contextmanager
    def span(self, component: Component, name: str, **attrs: Any) -> Iterator[TraceSpan]:
        """Record a timed span. Callers may set status, detail and attrs on the yielded span."""
        record = self._new_span(component, name, attrs)
        start = time.perf_counter()
        try:
            yield record
        except Exception as exc:
            if record.status == "ok":
                # An exception may name its own status (a ToolDenied span reads "denied", not "error").
                record.status = getattr(exc, "trace_status", "error")
            record.detail = record.detail or f"{type(exc).__name__}: {exc}"
            raise
        finally:
            record.latency_ms = round((time.perf_counter() - start) * 1000, 2)

    def event(self, component: Component, name: str, detail: str = "", status: str = "ok",
              **attrs: Any) -> TraceSpan:
        """Record an instant span (no duration), for decisions such as a cache miss."""
        record = self._new_span(component, name, attrs)
        record.detail, record.status = detail, status
        return record

    def _new_span(self, component: Component, name: str, attrs: dict[str, Any]) -> TraceSpan:
        record = TraceSpan(
            span_id=len(self.spans) + 1,
            lane=COMPONENT_LANE[component],
            component=component,
            name=name,
            start_ms=round((time.perf_counter() - self.started_at) * 1000, 2),
            attrs=dict(attrs),
        )
        self.spans.append(record)
        return record

    def total(self, attr: str) -> float:
        return sum(float(s.attrs.get(attr, 0) or 0) for s in self.spans)

    def by_lane(self) -> dict[Lane, list[TraceSpan]]:
        return {lane: [s for s in self.spans if s.lane == lane] for lane in Lane}
