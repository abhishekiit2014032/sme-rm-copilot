"""Diagram box: Output guardrails (lane A), shared money formatting and parsing.
Formats AED figures from tool data and parses figures back out of text for the number-match check.
Nothing is mocked here.
"""
from __future__ import annotations

import re

AED_IN_TEXT = re.compile(r"AED\s?(\d[\d,]*(?:\.\d+)?)\s?(m|million|k)?\b", re.IGNORECASE)
PERCENT_IN_TEXT = re.compile(r"(\d+(?:\.\d+)?)\s?%")
ANY_NUMBER = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s?(m|million|k)?\b", re.IGNORECASE)
_SCALE = {"m": 1_000_000, "million": 1_000_000, "k": 1_000}


def format_aed(amount: float) -> str:
    """AED 2.0m for millions, AED 780,000 below that."""
    if amount >= 1_000_000:
        millions = amount / 1_000_000
        text = f"{millions:.2f}".rstrip("0")
        return f"AED {text}0m" if text.endswith(".") else f"AED {text}m"
    return f"AED {amount:,.0f}"


def format_pct(value: float) -> str:
    return f"{value:g}%"


def _to_value(number: str, scale: str | None) -> float:
    return float(number.replace(",", "")) * _SCALE.get((scale or "").lower(), 1)


def figures_in_answer(text: str) -> list[tuple[str, float]]:
    """Money amounts and percentages that the answer states, as (original text, value)."""
    found = [(m.group(0).strip(), _to_value(m.group(1), m.group(2))) for m in AED_IN_TEXT.finditer(text)]
    found += [(m.group(0).strip(), float(m.group(1))) for m in PERCENT_IN_TEXT.finditer(text)]
    return found


def numbers_in_evidence(text: str) -> set[float]:
    """Every number in the source text, read both as written and with an m/k scale applied."""
    values: set[float] = set()
    for m in ANY_NUMBER.finditer(text):
        raw = float(m.group(1).replace(",", ""))
        values.add(raw)
        values.add(_to_value(m.group(1), m.group(2)))
    return values
