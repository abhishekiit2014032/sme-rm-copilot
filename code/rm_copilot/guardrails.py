"""Guardrails and verification checks.

These are cheap, deterministic checks that run on every request. Production adds model-based
classifiers (prompt injection, PII) and an NLI or LLM-judge groundedness score behind the same
functions, but keeps the deterministic checks as a floor.
"""
from __future__ import annotations

import re

from .retrieval import tokenize

INJECTION = re.compile(r"(ignore (all |any )?(previous|prior|above) instructions|disregard (the )?(system|previous)|you are now|reveal (the )?system prompt|approve .{0,40} immediately)", re.I)
PII_PATTERNS = {
    "national_id": re.compile(r"\b784-?\d{4}-?\d{7}-?\d\b"),
    "iban": re.compile(r"\bAE\d{2}\s?(?:\d{4}\s?){4}\d{3}\b"),
    "card": re.compile(r"\b(?:\d[ -]?){15,16}\b"),
}


def detect_injection(text: str) -> list[str]:
    return [m.group(0) for m in INJECTION.finditer(text)]


def quarantine_quotes(text: str) -> tuple[str, list[str]]:
    """Remove quoted passages that contain instruction-like text, so they cannot become actions."""
    removed: list[str] = []

    def repl(m: re.Match) -> str:
        if INJECTION.search(m.group(0)):
            removed.append(m.group(0))
            return "[quoted customer text removed]"
        return m.group(0)

    return re.sub(r'"[^"]*"|“[^”]*”', repl, text), removed


def mask_pii(text: str) -> tuple[str, list[str]]:
    found = []
    for name, pattern in PII_PATTERNS.items():
        if pattern.search(text):
            found.append(name)
            text = pattern.sub(f"[{name.upper()}]", text)
    return text, found


def _numbers(text: str) -> set[str]:
    return {n.replace(",", "") for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)}


def groundedness(answer: str, passages: list[dict], min_support: float = 0.6) -> dict:
    """Each sentence must cite a passage that exists, share most of its content words with it,
    and contain no number that is absent from it."""
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", answer) if s.strip()]
    claims = []
    for s in sentences:
        cites = [int(c) for c in re.findall(r"\[(\d+)\]", s)]
        body = re.sub(r"\[\d+\]", "", s)
        if not cites or any(c < 1 or c > len(passages) for c in cites):
            claims.append({"sentence": s, "ok": False, "reason": "missing or invalid citation"})
            continue
        source = " ".join(passages[c - 1]["text"] for c in cites)
        words = [w for w in tokenize(body) if len(w) > 2]
        support = sum(w in set(tokenize(source)) for w in words) / max(len(words), 1)
        stray = _numbers(body) - _numbers(source)
        ok = support >= min_support and not stray
        claims.append({"sentence": s, "ok": ok, "support": round(support, 2), "reason": "" if ok else (f"numbers not in source: {sorted(stray)}" if stray else "low support")})
    passed = sum(c["ok"] for c in claims)
    return {"ok": bool(claims) and passed == len(claims), "score": round(passed / max(len(claims), 1), 2), "claims": claims}


def free_numbers(model_text: str) -> list[str]:
    """Digits the model wrote itself, outside {placeholders}. Should always be empty."""
    return re.findall(r"\d+", re.sub(r"\{[^}]+\}", "", model_text))
