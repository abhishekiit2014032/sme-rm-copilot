"""Diagram box: API gateway + identity (lane A).
Verifies the RM's OIDC token and applies per-user rate limits and daily token limits.
Mocked: the identity provider (mocks/identity/oidc_mock.py); production is an API gateway with the Bank's IdP.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable

from copilot.contracts.identity import Identity, Role
from copilot.contracts.ports import IdentityProvider
from copilot.contracts.trace import Component, Trace


# The Bank's business day for daily limits: Gulf Standard Time (UTC+4, no daylight saving).
BANK_TIMEZONE = timezone(timedelta(hours=4))


class GatewayDenied(Exception):
    def __init__(self, status_code: int, reason: str) -> None:
        super().__init__(reason)
        self.status_code, self.reason = status_code, reason


class ApiGateway:
    def __init__(self, idp: IdentityProvider, requests_per_minute: int = 30, daily_token_limit: int = 200_000,
                 clock: Callable[[], float] = time.time) -> None:
        self._idp = idp
        self._requests_per_minute = requests_per_minute
        self._daily_token_limit = daily_token_limit
        self._clock = clock
        self._recent_requests: dict[str, list[float]] = {}
        self._tokens_by_day: dict[str, tuple[date, int]] = {}    # user -> (business day, tokens used that day)

    def admit(self, bearer_token: str, trace: Trace) -> Identity:
        with trace.span(Component.API_GATEWAY, "verify token + limits") as span:
            claims = self._verify(bearer_token, span)
            user_id = claims["sub"]
            self._check_rate(user_id, span)
            self._check_tokens(user_id, span)
            span.detail = f"OIDC ok ({user_id}), rate {len(self._recent_requests[user_id])}/" \
                          f"{self._requests_per_minute} per min, tokens today {self._tokens_today(user_id)}"
            return Identity(user_id=user_id, name=claims["name"], roles=[Role(r) for r in claims["roles"]],
                            token=bearer_token)

    def record_tokens(self, user_id: str, tokens: int) -> None:
        self._tokens_by_day[user_id] = (self._business_day(), self._tokens_today(user_id) + tokens)

    def _business_day(self) -> date:
        return datetime.fromtimestamp(self._clock(), BANK_TIMEZONE).date()

    def _tokens_today(self, user_id: str) -> int:
        """Tokens used on the current business day; the count starts again at midnight Bank time."""
        day, used = self._tokens_by_day.get(user_id, (None, 0))
        return used if day == self._business_day() else 0

    def _verify(self, token: str, span: Any) -> dict[str, Any]:
        try:
            return self._idp.verify(token)
        except Exception as exc:  # the IdP adapter raises its own error types
            span.status, span.detail = "denied", f"invalid token: {exc}"
            raise GatewayDenied(401, "Sign-in could not be verified.") from exc

    def _check_rate(self, user_id: str, span: Any) -> None:
        now = self._clock()
        window = [t for t in self._recent_requests.get(user_id, []) if now - t < 60]
        if len(window) >= self._requests_per_minute:
            span.status, span.detail = "denied", "rate limit exceeded"
            raise GatewayDenied(429, "Too many requests; try again in a minute.")
        self._recent_requests[user_id] = window + [now]

    def _check_tokens(self, user_id: str, span: Any) -> None:
        if self._tokens_today(user_id) >= self._daily_token_limit:
            span.status, span.detail = "denied", "daily token limit reached"
            raise GatewayDenied(429, "Daily usage limit reached.")
