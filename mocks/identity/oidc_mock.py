"""Diagram box: API gateway + identity (lane A), the identity provider behind it.
Issues and verifies OIDC-style signed tokens, and answers "may this RM see this customer?".
Mocked: HMAC-signed JWT-like tokens and a portfolio table from customers.json, in place of the Bank's IdP.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

_SIGNING_KEY = b"mock-idp-signing-key-not-a-secret"
AUDIENCE = "sme-rm-copilot"


class InvalidToken(Exception):
    pass


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class MockIdentityProvider:
    """Signs tokens for the fictional users in customers.json."""

    def __init__(self, dataset: dict[str, Any], lifetime_s: int = 3600) -> None:
        self._users = {u["user_id"]: u for u in dataset["users"]}
        self._lifetime_s = lifetime_s

    def issue_token(self, user_id: str) -> str:
        user = self._users[user_id]
        claims = {"sub": user_id, "name": user["name"], "roles": user["roles"], "aud": AUDIENCE,
                  "exp": time.time() + self._lifetime_s}
        header = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        body = _b64(json.dumps(claims).encode())
        return f"{header}.{body}.{self._sign(header + '.' + body)}"

    def verify(self, token: str) -> dict[str, Any]:
        try:
            header, body, signature = token.split(".")
        except ValueError as exc:
            raise InvalidToken("malformed token") from exc
        if not hmac.compare_digest(signature, self._sign(header + "." + body)):
            raise InvalidToken("bad signature")
        claims = json.loads(_unb64(body))
        if claims.get("aud") != AUDIENCE:
            raise InvalidToken("wrong audience")
        if claims["exp"] < time.time():
            raise InvalidToken("token expired")
        return claims

    @staticmethod
    def _sign(message: str) -> str:
        return _b64(hmac.new(_SIGNING_KEY, message.encode(), hashlib.sha256).digest())


class MockEntitlementService:
    """Customer entitlement: an RM sees only customers in their own portfolio."""

    def __init__(self, dataset: dict[str, Any]) -> None:
        self._owner = {c["customer_id"]: c["rm_id"] for c in dataset["customers"]}

    def is_entitled(self, user_id: str, customer_id: str) -> bool:
        return self._owner.get(customer_id) == user_id

    def portfolio(self, user_id: str) -> list[str]:
        return [cid for cid, owner in self._owner.items() if owner == user_id]
