"""Tier 1 runtime gates (plan §3.9, LLD §17.2): inline, every request, deterministic and lexical only (no LLM judge on
the hot path).

UC2  pre-generation  `retrieval`         candidates = passages with reranker score >= candidate_floor (1.0); decline
                                         WEAK_RETRIEVAL WITHOUT calling the generation model when there is no candidate
                                         or top < top_min (2.0, sub-reason top_below_threshold), or when the context
                                         relevance CRel = |kept| / |candidates| < context_relevance_min (0.5,
                                         low_context_relevance), kept = score >= max(keep_min, top - keep_margin).
                                         Only the kept passages go to the prompt.
     post-generation `faithfulness`      every sentence cites a kept passage, F_rt (sentences supported by their cited
                                         passage: content-word support >= min_support and no number absent from it)
                                         >= faithfulness_min (0.9), Content Safety groundedness if it ran
                     `answer_relevancy`  AR_rt >= answer_relevancy_min (0.7), evaluated only after faithfulness passed
                     on a failure: retry once with the strict prompt if >= retry_min_remaining_ms (4,000) of the
                     deadline remain, else decline NO_TIME_FOR_RETRY; after the retry FAILED_GROUNDEDNESS or
                     LOW_ANSWER_RELEVANCY. INSUFFICIENT_EVIDENCE from the model: MODEL_INSUFFICIENT_EVIDENCE.
UC1  `profile_present`                   the CRM profile must be present, else no brief
     `placeholder`, `numeric`, `policy`  per model line; a failing line is dropped (`dropped`), all dropped: `degraded`

Outcomes: passed, retried, declined, degraded, dropped, skipped. Every outcome is recorded with its scores (numbers
only) in the reply (`evaluation: {gates, scores}`) and, in the stack, in trace spans, `rmc_runtime_gate_total
{use_case,gate,outcome}` and `rmc_runtime_gate_score{use_case,gate}`.

Thresholds live in App Configuration under the LLD §17.2 keys (`KEYS`); only the pipeline writes them, after a passing
release gate. Values outside the sanity range are rejected (the default is used and the rejection counted). These
defaults apply in-process and when the keys are unreadable.
"""
from __future__ import annotations

from contextlib import nullcontext
from typing import Protocol

GATE_PREFIX = "rmc:orchestrator:gate:"
KEYS = {  # short name -> App Configuration key (LLD §17.2)
    "version": "rmc:orchestrator:gate:version",
    "candidate_floor": "rmc:tool-gateway:search:candidate_floor",
    "top_min": "rmc:orchestrator:gate:retrieval:top_min",
    "keep_min": "rmc:orchestrator:gate:retrieval:keep_min",
    "keep_margin": "rmc:orchestrator:gate:retrieval:keep_margin",
    "context_relevance_min": "rmc:orchestrator:gate:retrieval:context_relevance_min",
    "faithfulness_min": "rmc:orchestrator:gate:faithfulness:min",
    "min_support": "rmc:orchestrator:gate:faithfulness:min_support",
    "answer_relevancy_min": "rmc:orchestrator:gate:answer_relevancy:min",
    "retry_min_remaining_ms": "rmc:orchestrator:uc2:retry_min_remaining_ms",
}
# LLD §17.2 defaults are on the calibrated 0-4 semantic-ranker scale (floor 1.0, top_min 2.0, keep_min 2.0, margin 1.0).
# The AI Search mock's reranker score is BM25 x query coverage (roughly 0-20), so the prototype publishes the same rule
# with values re-tuned on the golden set (grid search over floor and margin, `evals/run_evals.py --tune-gate`).
RUNTIME_THRESHOLDS = {
    "version": "gates/2026.11.1-mock",
    "candidate_floor": 4.0,
    "top_min": 4.0,
    "keep_min": 4.0,
    "keep_margin": 5.0,
    "context_relevance_min": 0.5,
    "faithfulness_min": 0.9,
    "min_support": 0.6,
    "answer_relevancy_min": 0.7,
    "retry_min_remaining_ms": 4000.0,
}
SANITY = {  # LLD §17.2 sanity ranges; the retrieval ones widened for the mock reranker scale (see above)
    "candidate_floor": (0.5, 6.0), "top_min": (1.0, 6.0), "keep_min": (1.0, 6.0), "keep_margin": (0.25, 10.0),
    "context_relevance_min": (0.2, 1.0), "faithfulness_min": (0.8, 1.0), "min_support": (0.4, 0.9),
    "answer_relevancy_min": (0.5, 0.95), "retry_min_remaining_ms": (4000.0, 4000.0),
}
OUTCOMES = ("passed", "retried", "declined", "degraded", "dropped", "skipped")
DECLINE_CODES = ("WEAK_RETRIEVAL", "MODEL_INSUFFICIENT_EVIDENCE", "FAILED_GROUNDEDNESS", "LOW_ANSWER_RELEVANCY", "NO_TIME_FOR_RETRY")


def thresholds(override: object = None, rejected: list | None = None) -> dict:
    """Defaults overlaid with published values, given by App Configuration key or short name. A value outside its
    sanity range is rejected (default kept) and its short name appended to `rejected`."""
    out = dict(RUNTIME_THRESHOLDS)
    if not isinstance(override, dict):
        return out
    by_key = {v: k for k, v in KEYS.items()}
    for k, v in override.items():
        name = by_key.get(k, k)
        if name == "version" and isinstance(v, str):
            out["version"] = v[:40]
        elif name in SANITY and isinstance(v, (int, float)) and not isinstance(v, bool):
            lo, hi = SANITY[name]
            if lo <= float(v) <= hi:
                out[name] = float(v)
            elif rejected is not None:
                rejected.append(name)
    return out


def retrieval_gate(hits: list[dict], th: dict) -> dict:
    """LLD §17.2 step 4 on reranked hits. Returns {passed, sub_reason, top, context_relevance, candidates, kept}."""
    cand = [h for h in hits if h["score"] >= th["candidate_floor"]]
    top = max((h["score"] for h in cand), default=0.0)
    if not cand or top < th["top_min"]:
        return {"passed": False, "sub_reason": "top_below_threshold", "top": top, "context_relevance": 0.0, "candidates": cand, "kept": []}
    tau = max(th["keep_min"], top - th["keep_margin"])
    kept = [h for h in cand if h["score"] >= tau]
    crel = len(kept) / len(cand)
    ok = crel >= th["context_relevance_min"]
    return {"passed": ok, "sub_reason": None if ok else "low_context_relevance", "top": top, "context_relevance": round(crel, 4),
            "candidates": cand, "kept": kept}


class Observer(Protocol):
    def gate(self, gate: str, outcome: str, scores: dict) -> None: ...
    def sample(self, use_case: str, question: str, answer: str, contexts: list[str]) -> None: ...
    def stage(self, name: str): ...


class NullObserver:
    def gate(self, gate: str, outcome: str, scores: dict) -> None:
        pass

    def sample(self, use_case: str, question: str, answer: str, contexts: list[str]) -> None:
        pass

    def stage(self, name: str):
        return nullcontext()


def numbers_only(scores: dict) -> dict:
    return {k: (int(v) if isinstance(v, (bool, int)) else round(float(v), 4)) for k, v in scores.items()
            if isinstance(v, (int, float)) and v == v}  # NaN dropped


class GateLog:
    """The gate outcomes of one request."""

    def __init__(self, observer: Observer | None = None, version: str = RUNTIME_THRESHOLDS["version"]):
        self.observer = observer or NullObserver()
        self.version = version
        self.gates: list[dict] = []
        self.scores: dict = {}

    def record(self, gate: str, outcome: str, **scores) -> None:
        assert outcome in OUTCOMES, outcome
        s = numbers_only(scores)
        self.gates.append({"gate": gate, "outcome": outcome, "scores": s})
        self.scores.update({k: v for k, v in s.items() if k not in ("attempt",)})
        self.observer.gate(gate, outcome, s)

    def as_dict(self) -> dict:
        return {"gates": list(self.gates), "scores": dict(self.scores), "gate_version": self.version}
