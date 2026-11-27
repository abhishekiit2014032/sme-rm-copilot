"""Diagram box: Input guardrails (lane A).
Blocks prompt injection and out-of-scope asks, and masks personal data before any model call.
Mocked: regex detectors stand in for a prompt-injection classifier, a safety classifier and a PII recogniser.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from copilot.contracts.trace import Component, Trace

INJECTION_PATTERNS = [
    r"ignore (all |any )?(previous|prior|above|earlier) (instructions|rules)",
    r"disregard (the |your )?(instructions|rules|system prompt)",
    r"(reveal|print|show) (me )?(the |your )?(system prompt|hidden instructions)",
    r"you are now\b",
    r"developer mode|jailbreak",
    r"(every|all) customers?'?s?\b.*\b(balances?|accounts?|data)",
]

OUT_OF_SCOPE_PATTERNS = [
    r"\b(poem|joke|recipe|horoscope|football score|weather)\b",
    r"\b(buy|sell) (shares|stocks|crypto)\b.*\bfor me\b",
]

PII_PATTERNS = {
    "EMAIL": r"[\w.+-]+@[\w-]+\.[\w.-]+",
    "PHONE": r"\+?971[\s-]?\d{1,2}[\s-]?\d{3}[\s-]?\d{4}",
    "IBAN": r"\bAE\d{2}(?:\s?\d{4}){4}\s?\d{3}\b",
    "EMIRATES_ID": r"\b784-\d{4}-\d{7}-\d\b",
}


class InputBlocked(Exception):
    def __init__(self, category: str, message: str) -> None:
        super().__init__(message)
        self.category = category


class ScreenedInput(BaseModel):
    masked_text: str
    pii_vault: dict[str, str]      # placeholder -> original, kept only for this request


def mask_pii(text: str) -> tuple[str, dict[str, str]]:
    """Replace personal data with numbered placeholders such as <EMAIL_1>."""
    vault: dict[str, str] = {}
    for kind, pattern in PII_PATTERNS.items():
        def replace(match: re.Match[str], kind: str = kind) -> str:
            placeholder = f"<{kind}_{sum(k.startswith(f'<{kind}_') for k in vault) + 1}>"
            vault[placeholder] = match.group(0)
            return placeholder
        text = re.sub(pattern, replace, text)
    return text, vault


def unmask_pii(text: str, vault: dict[str, str]) -> str:
    for placeholder, original in vault.items():
        text = text.replace(placeholder, original)
    return text


class InputGuardrails:
    def screen(self, text: str, trace: Trace) -> ScreenedInput:
        with trace.span(Component.INPUT_GUARDRAILS, "screen input") as span:
            if _matches(INJECTION_PATTERNS, text):
                span.status, span.detail = "blocked", "prompt injection detected"
                raise InputBlocked("prompt_injection", "This request looks like an attempt to override the "
                                                       "copilot's instructions, so it was blocked.")
            if _matches(OUT_OF_SCOPE_PATTERNS, text):
                span.status, span.detail = "blocked", "out of scope"
                raise InputBlocked("out_of_scope", "This is outside what the RM copilot can help with.")
            masked, vault = mask_pii(text)
            span.detail = f"injection: none, scope: ok, PII masked: {len(vault)}"
            span.attrs["pii_masked"] = len(vault)
            return ScreenedInput(masked_text=masked, pii_vault=vault)


def _matches(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)
