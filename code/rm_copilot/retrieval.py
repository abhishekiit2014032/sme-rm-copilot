"""Retrieval for the policy copilot.

The sample uses BM25 keyword scoring so it runs with no dependencies. Production swaps in hybrid
search (BM25 + vector embeddings, fused with reciprocal-rank fusion) and a cross-encoder reranker,
but keeps the same contract: filter first (current version, user clearance), then rank.
"""
from __future__ import annotations

import math
import re

CLEARANCE_RANK = {"public": 0, "internal": 1, "restricted": 2}
STOP = set("a an the of to for and or in on at is are be can i we our my me you your do does what who when how with by from it its this that as get need any there".split())
SYNONYMS = {"utilization": "utilisation", "license": "licence", "yrs": "year", "old": "year", "approves": "approval", "approve": "approval",
            "docs": "document", "company": "business", "companies": "business", "businesses": "business", "firm": "business"}


def _stem(w: str) -> str:
    # Light plural folding; production uses a proper analyser per language (English and Arabic).
    return w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w


def tokenize(text: str) -> list[str]:
    words = re.split(r"[^a-z0-9%]+", text.lower())
    return [_stem(SYNONYMS.get(w, w)) for w in words if w and w not in STOP]


def eligible(passages: list[dict], clearance: str) -> list[dict]:
    limit = CLEARANCE_RANK[clearance]
    return [p for p in passages if p["status"] == "current" and CLEARANCE_RANK[p["classification"]] <= limit]


def bm25(query: str, passages: list[dict], k1: float = 1.4, b: float = 0.75) -> list[tuple[float, dict]]:
    docs = [tokenize(p["doc"] + " " + p["text"]) for p in passages]
    if not docs:
        return []
    n, avg = len(docs), sum(map(len, docs)) / len(docs)
    df: dict[str, int] = {}
    for d in docs:
        for w in set(d):
            df[w] = df.get(w, 0) + 1
    scored = []
    for p, d in zip(passages, docs):
        tf: dict[str, int] = {}
        for w in d:
            tf[w] = tf.get(w, 0) + 1
        s = 0.0
        for q in set(tokenize(query)):
            if q in tf:
                idf = math.log(1 + (n - df[q] + 0.5) / (df[q] + 0.5))
                s += idf * tf[q] * (k1 + 1) / (tf[q] + k1 * (1 - b + b * len(d) / avg))
        scored.append((s, p))
    return sorted(scored, key=lambda x: x[0], reverse=True)


def rerank(query: str, scored: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
    """Stand-in for a cross-encoder reranker: reward passages that cover more of the question."""
    q = set(tokenize(query))
    out = []
    for s, p in scored:
        coverage = len(q & set(tokenize(p["doc"] + " " + p["text"]))) / max(len(q), 1)
        out.append((s * (0.5 + coverage), p))
    return sorted(out, key=lambda x: x[0], reverse=True)


def search(query: str, passages: list[dict], clearance: str, k: int = 3, candidates: int = 20) -> list[dict]:
    pool = eligible(passages, clearance)                                     # 1. filter first
    first_pass = [(s, p) for s, p in bm25(query, pool) if s > 0][:candidates]  # 2. recall
    ranked = rerank(query, first_pass)[:k]                                     # 3. precision
    return [{**p, "score": round(s, 2)} for s, p in ranked]
