"""Diagram box: Session + memory store (lane C), the checkpointer.
Saves workflow state at a pause (waiting for RM approval) so the follow-up can resume later, even on another node.
Mocked: an in-memory dict; production uses a per-RM, encrypted document store (the LangGraph checkpointer).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel

from copilot.contracts.trace import Component, Trace


class Checkpoint(BaseModel):
    thread_id: str
    node: str                  # where the workflow paused, e.g. "await_approval"
    owner_id: str              # the RM the state belongs to; never shared across RMs
    state: dict[str, Any]
    saved_at: str


class Checkpointer:
    def __init__(self) -> None:
        self._store: dict[str, Checkpoint] = {}

    def save(self, thread_id: str, node: str, owner_id: str, state: dict[str, Any], trace: Trace) -> Checkpoint:
        checkpoint = Checkpoint(thread_id=thread_id, node=node, owner_id=owner_id, state=state,
                                saved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
        self._store[thread_id] = checkpoint
        trace.event(Component.MEMORY_STORE, f"checkpoint saved: {node}", detail=f"thread {thread_id}")
        return checkpoint

    def load(self, thread_id: str, owner_id: str, trace: Trace) -> Checkpoint | None:
        checkpoint = self._store.get(thread_id)
        if checkpoint is None or checkpoint.owner_id != owner_id:
            trace.event(Component.MEMORY_STORE, "checkpoint not found", status="error", detail=thread_id)
            return None
        trace.event(Component.MEMORY_STORE, f"checkpoint loaded: {checkpoint.node}", detail=f"thread {thread_id}")
        return checkpoint

    def complete(self, thread_id: str) -> None:
        self._store.pop(thread_id, None)
