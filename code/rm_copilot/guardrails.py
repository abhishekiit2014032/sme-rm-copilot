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
    # Separators as people write them: hyphens, spaces or dots; IBANs in any case; UAE phone numbers with +971, 00971,
    # 971 or a trunk 0, an optional "(0)", brackets around the area code and any digit grouping.
    "national_id": re.compile(r"\b784[-. ]?\d{4}[-. ]?\d{7}[-. ]?\d\b"),
    "iban": re.compile(r"\b[Aa][Ee]\d{2}[ -]?(?:\d{4}[ -]?){4}\d{3}\b"),
    "card": re.compile(r"\b(?:\d[ -]?){15,16}\b"),
    "trade_licence": re.compile(r"\b(?:TL|CN)-\d{6,7}\b"),
    "email": re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b"),
    "phone": re.compile(r"(?:(?:\+|\b00)971|\b971)[ -]?(?:\(0\)[ -]?)?\(?(?:5\d|[2-9])\)?(?:[ -]?\d){7}\b"
                       r"|(?:\(0|\b0)(?:5\d|[2-9])\)?(?:[ -]?\d){7}\b"),
}
GUARDRAIL_VERSION = "guardrails/2026.11.4"  # recorded in audit lineage; bump on any rule change

# Number words the model must not write itself (plan C19). English plus basic Arabic, with the common
# attached prefixes (wa-, bi-, li-). Digits in any script are caught by \d (Unicode-aware).
_EN_NUMBER_WORDS = ("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen "
                    "seventeen eighteen nineteen twenty thirty forty fifty sixty seventy eighty ninety hundred thousand million "
                    "billion dozen half quarter percent").split()
_AR_NUMBER_WORDS = ("صفر واحد واحدة اثنان اثنين اثنتان ثلاث ثلاثة أربع أربعة خمس خمسة ست ستة سبع سبعة ثمان ثمانية تسع تسعة "
                    "عشر عشرة عشرون عشرين مائة مئة ألف آلاف مليون ملايين مليار نصف ربع بالمائة بالمئة").split()
NUMBER_WORDS = re.compile(r"(?<!\w)(?:(?:" + "|".join(_EN_NUMBER_WORDS) + r")s?|(?:و|ب|ل)?(?:" + "|".join(_AR_NUMBER_WORDS) + r"))(?!\w)", re.I)

# Commitments only the credit or pricing process can make (plan C19: policy check on UC1 and UC3 output).
POLICY_PROMISE = re.compile(r"approv\w*|guarantee\w*|pricing|\bprice[sd]?\b|discount\w*|\bwaive\w*|increase (?:the |your )?limit|"
                            r"we will (?:lend|extend|offer credit)", re.I)


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


# ADR-012: Arabic-Indic (U+0660-0669) and Eastern Arabic-Indic (U+06F0-06F9) digits are normalised to ASCII for
# detection. The mapping is one character to one character, so spans found in the normalised text mask the original.
DIGITS_TO_ASCII = str.maketrans("\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"
                                "\u06f0\u06f1\u06f2\u06f3\u06f4\u06f5\u06f6\u06f7\u06f8\u06f9", "01234567890123456789")


def normalise_digits(text: str) -> str:
    return text.translate(DIGITS_TO_ASCII)


def pii_spans(text: str) -> list[tuple[int, int, str]]:
    """Non-overlapping (start, end, name) in pattern priority order, detected on the digit-normalised text."""
    norm, taken = normalise_digits(text), []
    for name, pattern in PII_PATTERNS.items():
        for m in pattern.finditer(norm):
            if not any(m.start() < e and s < m.end() for s, e, _ in taken):
                taken.append((m.start(), m.end(), name))
    return sorted(taken)


def mask_pii(text: str) -> tuple[str, list[str]]:
    spans = pii_spans(text)
    for s, e, name in reversed(spans):
        text = text[:s] + f"[{name.upper()}]" + text[e:]
    return text, [n for n in PII_PATTERNS if any(name == n for _, _, name in spans)]


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
    """Digits or number words (English, basic Arabic) the model wrote itself, outside {placeholders}.
    Should always be empty: every figure must come from a tool via a placeholder."""
    text = re.sub(r"\{[^}]+\}", "", model_text)
    return re.findall(r"\d+", text) + [m.group(0) for m in NUMBER_WORDS.finditer(text)]


def policy_violations(model_text: str) -> list[str]:
    """Promises of approval, pricing or limits written by the model. Should always be empty."""
    return [m.group(0) for m in POLICY_PROMISE.finditer(re.sub(r"\{[^}]+\}", "", model_text))]
