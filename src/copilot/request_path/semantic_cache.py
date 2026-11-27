"""Diagram box: Semantic cache (lane A).
Reuses verified answers to public policy questions; TTL, version-tagged, invalidated by CDC; never customer data.
Mocked: an in-memory list searched by cosine similarity; production uses a managed cache. OFF by default.
"""
from __future__ import annotations

import time
from typing import Callable

from pydantic import BaseModel

from copilot.contracts.results import AgentResult
from copilot.contracts.trace import Component, Trace
from copilot.execution.retrieval import cosine


class CacheEntry(BaseModel):
    question: str
    embedding: list[float]
    result: AgentResult
    doc_ids: set[str]
    stored_at: float


class SemanticCache:
    def __init__(self, embed: Callable[[str, Trace], list[float]], ttl_s: float = 3600, similarity: float = 0.92,
                 clock: Callable[[], float] = time.time) -> None:
        self._embed, self._ttl_s, self._similarity, self._clock = embed, ttl_s, similarity, clock
        self._entries: list[CacheEntry] = []

    def lookup(self, question: str, trace: Trace) -> AgentResult | None:
        vector = self._embed(question, trace)
        now = self._clock()
        self._entries = [e for e in self._entries if now - e.stored_at < self._ttl_s]
        best = max(self._entries, key=lambda e: cosine(vector, e.embedding), default=None)
        score = cosine(vector, best.embedding) if best else 0.0
        if best and score >= self._similarity:
            trace.event(Component.SEMANTIC_CACHE, "HIT", detail=f"similarity {score:.2f}; still validated",
                        status="hit")
            return best.result
        trace.event(Component.SEMANTIC_CACHE, "MISS", detail=f"best similarity {score:.2f}", status="miss")
        return None

    def store(self, question: str, result: AgentResult, trace: Trace) -> bool:
        """Store only grounded answers whose every citation comes from a public document."""
        if result.status != "ok" or not result.citations:
            return False
        if any(c.clearance != "public" for c in result.citations):
            trace.event(Component.SEMANTIC_CACHE, "not stored", detail="cites non-public clauses")
            return False
        self._entries.append(CacheEntry(question=question, embedding=self._embed(question, trace), result=result,
                                        doc_ids={c.doc_id for c in result.citations}, stored_at=self._clock()))
        trace.event(Component.SEMANTIC_CACHE, "stored", detail=f"{len(self._entries)} entries")
        return True

    def invalidate_doc(self, doc_id: str) -> int:
        before = len(self._entries)
        self._entries = [e for e in self._entries if doc_id not in e.doc_ids]
        return before - len(self._entries)
