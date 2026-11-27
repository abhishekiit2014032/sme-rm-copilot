"""Diagram box: Systems of record, Primary / fallback LLM, identity provider (lane B), the ports.
Every external system sits behind one of these interfaces (ports and adapters).
Today each port has a mock adapter in mocks/; Azure adapters plug in later without code changes here.
"""
from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel


class LlmCompletion(BaseModel):
    text: str
    tokens_in: int
    tokens_out: int


class RenderedPrompt(BaseModel):
    prompt_id: str
    version: str
    system: str
    user: str
    variables: dict[str, Any]


class LlmClient(Protocol):
    def complete(self, model: str, prompt: RenderedPrompt, params: dict[str, Any]) -> LlmCompletion: ...

    def embed(self, model: str, text: str) -> list[float]: ...


class IdentityProvider(Protocol):
    def verify(self, token: str) -> dict[str, Any]: ...


class EntitlementService(Protocol):
    def is_entitled(self, user_id: str, customer_id: str) -> bool: ...


class SystemOfRecord(Protocol):
    system_name: str
    as_of: str          # when the data was last refreshed at the source


class CrmSystem(SystemOfRecord, Protocol):
    def get_profile(self, customer_id: str) -> dict[str, Any]: ...

    def get_interactions(self, customer_id: str) -> list[dict[str, Any]]: ...

    def get_open_requests(self, customer_id: str) -> list[dict[str, Any]]: ...

    def commit(self, write: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...


class CoreBankingSystem(SystemOfRecord, Protocol):
    def get_accounts_summary(self, customer_id: str) -> dict[str, Any]: ...


class LendingSystem(SystemOfRecord, Protocol):
    def get_facilities(self, customer_id: str) -> dict[str, Any]: ...


class TradeFinanceSystem(SystemOfRecord, Protocol):
    def get_trade_positions(self, customer_id: str) -> dict[str, Any]: ...


class CaseManagementSystem(SystemOfRecord, Protocol):
    def get_cases(self, customer_id: str) -> dict[str, Any]: ...
