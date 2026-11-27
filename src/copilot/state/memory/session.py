"""Diagram box: Session + memory store (lane C), session memory.
Keeps each RM's recent turns for 24 hours so follow-up questions in one preparation session have context.
Mocked: an in-memory dict with an injectable clock; production uses the document store with a 24 h TTL.
"""
from __future__ import annotations

import time
from typing import Callable

from pydantic import BaseModel

from copilot.contracts.trace import Component, Trace

SESSION_TTL_S = 24 * 3600
MAX_TURNS = 10


class Turn(BaseModel):
    at: float
    text: str          # masked text only
    status: str


class SessionMemory:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self._turns: dict[str, list[Turn]] = {}
        self._clock = clock

    def add_turn(self, user_id: str, text: str, status: str, trace: Trace) -> None:
        turns = self._turns.setdefault(user_id, [])
        turns.append(Turn(at=self._clock(), text=text, status=status))
        del turns[:-MAX_TURNS]
        trace.event(Component.MEMORY_STORE, "session turn saved", detail=f"{len(turns)} recent turns (24 h)")

    def recent(self, user_id: str) -> list[Turn]:
        cutoff = self._clock() - SESSION_TTL_S
        return [t for t in self._turns.get(user_id, []) if t.at >= cutoff]
