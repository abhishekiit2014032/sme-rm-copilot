"""Ports: the two small interfaces the copilot workflows depend on, plus their in-process adapters.

`Copilot` (orchestrator.py) only talks to a `ToolPort` (the tool layer) and a `GuardPort` (safety services), so
the same workflow code runs in two places:

* in-process, with `LocalTools` and `LocalGuard` below (demo, unit tests, evals), and
* inside the `orchestrator` service of the local stack, with HTTP adapters (`services/adapters.py`) that call
  `tool-gateway`, Azure AI Content Safety and Azure AI Language (or their mocks) over REST.
"""
from __future__ import annotations

from typing import Protocol

from . import guardrails as g
from . import tools as t


class ToolPort(Protocol):
    def brief_inputs(self, user: t.User, customer_id: str) -> dict: ...
    def get_customer_profile(self, user: t.User, customer_id: str) -> dict: ...
    def search_policies(self, user: t.User, query: str, k: int = 3) -> list[dict]: ...
    def draft_email(self, user: t.User, customer_id: str, body: str) -> dict: ...
    def create_call_report(self, user: t.User, customer_id: str, draft: dict, approval_token: str | None = None) -> dict: ...
    def audit(self, event: str, **fields) -> None: ...


class GuardPort(Protocol):
    def prompt_attack(self, text: str) -> bool: ...
    def shield_documents(self, documents: list[str]) -> list[bool]: ...
    def quarantine_quotes(self, text: str) -> tuple[str, list[str]]: ...
    def mask_pii(self, text: str) -> tuple[str, list[str]]: ...
    def groundedness(self, answer: str, passages: list[dict]) -> dict: ...
    def harmful(self, text: str) -> bool: ...


class LocalTools:
    """In-process tool layer (rm_copilot.tools). Entitlement is checked by every tool before data moves."""

    def brief_inputs(self, user: t.User, customer_id: str) -> dict:
        profile = t.get_customer_profile(user, customer_id)  # raises NotEntitled before any other call
        return {"profile": profile, "facilities": t.get_facilities(user, customer_id), "kyc": t.get_kyc_status(user, customer_id),
                "signals": t.get_transaction_signals(user, customer_id), "news": t.get_news(user, customer_id), "partial": []}

    def get_customer_profile(self, user: t.User, customer_id: str) -> dict:
        return t.get_customer_profile(user, customer_id)

    def search_policies(self, user: t.User, query: str, k: int = 3) -> list[dict]:
        return t.search_policies(user, query, k=k)

    def draft_email(self, user: t.User, customer_id: str, body: str) -> dict:
        return t.draft_email(user, customer_id, body)

    def create_call_report(self, user: t.User, customer_id: str, draft: dict, approval_token: str | None = None) -> dict:
        return t.create_call_report(user, customer_id, draft, approval_token=approval_token)

    def audit(self, event: str, **fields) -> None:
        t.audit(event, **fields)


class LocalGuard:
    """Deterministic rule-based guardrails. These also run as the floor behind the Azure services (plan C2)."""

    def prompt_attack(self, text: str) -> bool:
        return bool(g.detect_injection(text))

    def shield_documents(self, documents: list[str]) -> list[bool]:
        return [bool(g.detect_injection(d)) for d in documents]

    def quarantine_quotes(self, text: str) -> tuple[str, list[str]]:
        return g.quarantine_quotes(text)

    def mask_pii(self, text: str) -> tuple[str, list[str]]:
        return g.mask_pii(text)

    def groundedness(self, answer: str, passages: list[dict]) -> dict:
        return g.groundedness(answer, passages)

    def harmful(self, text: str) -> bool:
        return False  # no local harm classifier; Content Safety text:analyze covers this in the stack
