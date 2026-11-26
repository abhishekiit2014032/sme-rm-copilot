"""Approval token verification for the tool-gateway write API (plan §4.2 step 8, C6, C7).

The verifier holds only the PUBLIC key (fetched from Key Vault by `kid`, cached) and checks, in order:
structure and `alg` = ES256 (never `none` or HMAC); `kid` under the configured vault and key; signature;
`iss` = web-bff; `aud` = tool-gateway; `op` and `schema`; `exp` / `iat` with 30 s skew; `sub` = the `oid` of the RM
token on the call; `customer_id` = the customer in the request; `draft_sha256` = hash of the canonical submitted
draft. The `jti` replay check (atomic create in Cosmos DB) is done by the caller after these pass.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from services.common import jose
from services.common.config import StackConfig
from services.common.contracts import APPROVAL_AUDIENCE, APPROVAL_ISSUER, APPROVAL_OP, APPROVAL_SCHEMA, draft_sha256
from services.common.httpkit import HttpClient, HttpError, TransportError

APPROVAL_KEY = "approval-signing"


class ApprovalRejected(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class ApprovalVerifier:
    cfg: StackConfig
    client: HttpClient
    vault_headers: callable  # () -> auth headers for Key Vault

    def __post_init__(self) -> None:
        self._keys: dict[str, jose.VerifyingKey] = {}
        self._lock = threading.Lock()

    def _key(self, kid: str) -> jose.VerifyingKey:
        prefix = f"{self.cfg.url('keyvault')}/keys/{APPROVAL_KEY}/"
        if not isinstance(kid, str) or not kid.startswith(prefix) or "/" in kid[len(prefix):]:
            raise ApprovalRejected("unknown_kid")
        with self._lock:
            if kid in self._keys:
                return self._keys[kid]
        try:
            r = self.client.request("GET", f"{kid}?api-version=7.4", headers=self.vault_headers(), timeout=1.5, peer="keyvault")
        except TransportError:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "cannot fetch the approval public key", details={"reason": "KEY_VAULT"}) from None
        if r.status == 404:
            raise ApprovalRejected("unknown_kid")
        if not r.ok:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "cannot fetch the approval public key", details={"reason": "KEY_VAULT"})
        key = jose.VerifyingKey.from_jwk(r.json()["key"])
        with self._lock:
            self._keys[kid] = key
        return key

    def verify(self, token: str | None, *, oid: str, customer_id: str, draft: dict) -> dict:
        if not token:
            raise ApprovalRejected("missing")
        try:
            d = jose.decode_unverified(token)
        except jose.JoseError:
            raise ApprovalRejected("malformed") from None
        if d.header.get("alg") != "ES256" or d.header.get("typ") != "approval+jwt":
            raise ApprovalRejected("bad_header")
        kid = d.header.get("kid")
        try:
            jose.verify(token, {kid: self._key(kid)})
        except jose.JoseError as e:
            raise ApprovalRejected("bad_signature" if e.code == "bad_signature" else "bad_header") from None
        c, now, skew = d.claims, time.time(), self.cfg.settings.approval_skew_s
        checks = [
            (c.get("iss") == APPROVAL_ISSUER, "bad_issuer"),
            (c.get("aud") == APPROVAL_AUDIENCE, "bad_audience"),
            (c.get("op") == APPROVAL_OP, "bad_op"),
            (c.get("schema") == APPROVAL_SCHEMA, "bad_schema"),
            (c.get("kid") == kid, "bad_header"),
            (isinstance(c.get("exp"), int) and c["exp"] + skew >= now, "expired"),
            (isinstance(c.get("iat"), int) and c["iat"] - skew <= now and c.get("exp", 0) - c["iat"] <= self.cfg.settings.approval_ttl_s, "bad_lifetime"),
            (c.get("sub") == oid, "wrong_rm"),
            (c.get("customer_id") == customer_id, "wrong_customer"),
            (c.get("draft_sha256") == draft_sha256(customer_id, draft), "draft_mismatch"),
            (isinstance(c.get("jti"), str) and 16 <= len(c["jti"]) <= 64, "bad_jti"),
        ]
        for ok, reason in checks:
            if not ok:
                raise ApprovalRejected(reason)
        return c
