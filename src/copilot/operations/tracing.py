"""Diagram box: Observability (lane D).
Starts a trace per request, prices tokens per model and closes each trace with a summary span.
Mocked: traces are kept in memory; production exports the same spans to Langfuse and Azure Monitor.
"""
from __future__ import annotations

from copilot.contracts.trace import Component, Trace

# Illustrative prices in USD per 1,000 tokens, used only to show cost per request in traces.
PRICE_PER_1K_TOKENS = {
    "primary-llm": 0.0050,
    "fallback-llm": 0.0030,
    "mini-llm": 0.0006,
    "judge-llm": 0.0006,
    "embedding-model": 0.0001,
}


def cost_usd(model: str, tokens: int) -> float:
    return round(PRICE_PER_1K_TOKENS.get(model, 0.0) * tokens / 1000, 6)


class Tracer:
    def __init__(self) -> None:
        self._traces: dict[str, Trace] = {}

    def start(self, request_id: str, user_id: str = "") -> Trace:
        trace = Trace(request_id=request_id, user_id=user_id)
        self._traces[trace.trace_id] = trace
        return trace

    def finish(self, trace: Trace) -> Trace:
        tokens = int(trace.total("tokens_in") + trace.total("tokens_out"))
        trace.event(
            Component.OBSERVABILITY, "trace exported",
            detail=f"{len(trace.spans) + 1} spans, {tokens} tokens, ${trace.total('cost_usd'):.4f}",
            spans=len(trace.spans) + 1, tokens=tokens, cost_usd_total=round(trace.total("cost_usd"), 6),
        )
        return trace

    def get(self, trace_id: str) -> Trace | None:
        return self._traces.get(trace_id)
