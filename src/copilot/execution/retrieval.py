"""Diagram box: Retrieval / RAG and Vector + keyword index (lane B).
Hybrid search: metadata filter, BM25 (sparse) + vector (dense), fusion, rerank, then a retrieval gate.
Mocked: the index is in memory, the embedding is a hashing stand-in and the reranker is lexical.
"""
from __future__ import annotations

import math
import re
from collections import Counter
from datetime import date
from typing import Callable

from pydantic import BaseModel, Field

from copilot.contracts.trace import Component, Trace

# Common English function words only (no domain words): "go" is a light verb here, as in "can we go to AED 3.5m".
STOPWORDS = set("""a an and are as at be by can could do does for from go has have how i if in is it its me my
need needs not of on or our per should so than that the their them then there this to up us we what when
which who will with would you your about any all also into one only out over under""".split())

GATE_THRESHOLD = 0.45   # best reranked clause must reach this score, or the agent declines
TOP_K = 3


class PolicyClause(BaseModel):
    """One chunk = one clause, with the metadata used for filtering and citation."""

    doc_id: str
    doc_title: str
    clause: str
    heading: str
    text: str
    version: str
    effective_from: date
    status: str            # "current" or "superseded"
    clearance: str         # "public", "internal" or "restricted"
    embedding: list[float] = Field(default_factory=list, repr=False)

    @property
    def ref(self) -> str:
        return f"{self.doc_id}@{self.version}#{self.clause}"


class ScoredClause(BaseModel):
    clause: PolicyClause
    bm25: float
    dense: float
    score: float           # reranked score in [0, 1]


class RetrievalResult(BaseModel):
    query: str
    clauses: list[ScoredClause]
    best_score: float
    threshold: float
    passed_gate: bool
    filtered_out: int


def tokenize(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9]+(?:\.[0-9]+)?", text.lower())
    return [_stem(w) for w in words if w not in STOPWORDS and len(w) > 1]


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[: -len(suffix)]
    return word


def bm25_scores(query: list[str], documents: list[list[str]], k1: float = 1.5, b: float = 0.75) -> list[float]:
    """Classic Okapi BM25 over a small in-memory corpus."""
    n = len(documents)
    avg_len = sum(len(d) for d in documents) / n if n else 0.0
    doc_freq = Counter(term for doc in documents for term in set(doc))
    scores = []
    for doc in documents:
        counts, score = Counter(doc), 0.0
        for term in query:
            if term not in counts:
                continue
            idf = math.log(1 + (n - doc_freq[term] + 0.5) / (doc_freq[term] + 0.5))
            tf = counts[term]
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(doc) / avg_len))
        scores.append(score)
    return scores


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))   # vectors are already unit length


class ClauseIndex:
    """Diagram box: Vector + keyword index. Holds every version; the filter decides what is searchable."""

    def __init__(self) -> None:
        self._by_doc: dict[str, list[PolicyClause]] = {}

    def replace_document(self, doc_id: str, clauses: list[PolicyClause]) -> None:
        self._by_doc[doc_id] = clauses

    def all_clauses(self) -> list[PolicyClause]:
        return [c for clauses in self._by_doc.values() for c in clauses]


class HybridRetriever:
    def __init__(self, index: ClauseIndex, embed: Callable[[str, Trace], list[float]], today: date,
                 threshold: float = GATE_THRESHOLD, top_k: int = TOP_K) -> None:
        self.index, self._embed, self._today = index, embed, today
        self.threshold, self.top_k = threshold, top_k

    def search(self, query: str, clearances: set[str], trace: Trace) -> RetrievalResult:
        with trace.span(Component.RETRIEVAL, "hybrid search") as span:
            candidates, filtered_out = self._metadata_filter(clearances)
            fused = self._hybrid_rank(query, candidates, trace)
            reranked = self._rerank(query, fused)
            best = reranked[0].score if reranked else 0.0
            passed = best >= self.threshold
            kept = [c for c in reranked if c.score >= self.threshold][: self.top_k] if passed else []
            span.status = "ok" if passed else "declined"
            span.detail = (f"best {best:.2f} vs gate {self.threshold:.2f}; "
                           + (", ".join(f"§{c.clause.clause}" for c in kept) if passed else "no clause passes"))
            span.attrs.update(best_score=round(best, 3), threshold=self.threshold, candidates=len(candidates),
                              filtered_out=filtered_out)
            return RetrievalResult(query=query, clauses=kept, best_score=round(best, 3), threshold=self.threshold,
                                   passed_gate=passed, filtered_out=filtered_out)

    # ---- steps ----------------------------------------------------------------------------
    def _metadata_filter(self, clearances: set[str]) -> tuple[list[PolicyClause], int]:
        """Only current, already effective clauses that the caller is cleared to see."""
        everything = self.index.all_clauses()
        kept = [c for c in everything
                if c.status == "current" and c.effective_from <= self._today and c.clearance in clearances]
        return kept, len(everything) - len(kept)

    def _hybrid_rank(self, query: str, candidates: list[PolicyClause], trace: Trace) -> list[ScoredClause]:
        """BM25 and dense rankings fused with reciprocal rank fusion (k = 60); keeps the top 6."""
        if not candidates:
            return []
        query_vector = self._embed(query, trace)
        with trace.span(Component.INDEX, "BM25 + vector query") as span:
            sparse = bm25_scores(tokenize(query), [tokenize(c.heading + " " + c.text) for c in candidates])
            dense = [cosine(query_vector, c.embedding) for c in candidates]
            span.detail = f"{len(candidates)} clauses searched"
        sparse_rank = _ranks(sparse)
        dense_rank = _ranks(dense)
        fused = sorted(range(len(candidates)),
                       key=lambda i: -(1 / (60 + sparse_rank[i]) + 1 / (60 + dense_rank[i])))
        return [ScoredClause(clause=candidates[i], bm25=round(sparse[i], 3), dense=round(dense[i], 3), score=0.0)
                for i in fused[:6]]

    def _rerank(self, query: str, fused: list[ScoredClause]) -> list[ScoredClause]:
        """Stand-in for a cross-encoder reranker: query-term coverage blended with dense similarity."""
        terms = set(tokenize(query))
        for item in fused:
            clause_terms = set(tokenize(item.clause.heading + " " + item.clause.text))
            coverage = len(terms & clause_terms) / len(terms) if terms else 0.0
            item.score = round(0.75 * coverage + 0.25 * max(item.dense, 0.0), 3)
        return sorted(fused, key=lambda s: -s.score)


def _ranks(scores: list[float]) -> list[int]:
    order = sorted(range(len(scores)), key=lambda i: -scores[i])
    ranks = [0] * len(scores)
    for rank, index in enumerate(order, start=1):
        ranks[index] = rank
    return ranks
