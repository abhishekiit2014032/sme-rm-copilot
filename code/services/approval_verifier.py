"""Approval token verification for the tool-gateway write API (plan §4.2 step 8, C6, C7).

The verifier holds only the PUBLIC key (fetched from Key Vault by `kid`, cached) and checks, in order:
size <= 2 KiB, structure, header members exactly {alg, typ, kid}, `alg` = ES256 (never `none` or HMAC); `kid` under
the configured vault and key; signature; the exact claim set (LLD §4.1); `iss` = web-bff; `aud` = tool-gateway;
`op` and `schema`; `exp` / `iat` with 30 s skew and 0 < exp - iat <= 300; `sub` = the `oid` of the RM token on the call;
`customer_id` = the customer in the request; `draft_sha256` = hash of the canonical submitted draft (constant time). The `jti` replay check (atomic create in Cosmos DB) is done by the caller after these pass.
"""
from __future__ import annotations

import hmac
import threading
import time
from dataclasses import dataclass

from services.common import jose
from services.common.config import StackConfig
from services.common.contracts import APPROVAL_AUDIENCE, APPROVAL_ISSUER, APPROVAL_OP, APPROVAL_SCHEMA, draft_sha256
from services.common.httpkit import HttpClient, HttpError, TransportError

APPROVAL_KEY = "approval-signing"
CLAIMS = frozenset({"iss", "aud", "sub", "customer_id", "op", "draft_sha256", "schema", "iat", "exp", "jti", "kid"})  # LLD §4.1: exact set
HEADER = frozenset({"alg", "typ", "kid"})  # no jku, x5u, jwk, x5c, crit (LLD §4.4 step 7)
MAX_TOKEN_BYTES = 2048


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
        if not isinstance(token, str) or len(token) > MAX_TOKEN_BYTES:
            raise ApprovalRejected("malformed")
        try:
            d = jose.decode_unverified(token)
        except jose.JoseError:
            raise ApprovalRejected("malformed") from None
        if d.header.get("alg") != "ES256" or d.header.get("typ") != "approval+jwt" or set(d.header) != HEADER:
            raise ApprovalRejected("bad_header")
        kid = d.header.get("kid")
        try:
            jose.verify(token, {kid: self._key(kid)})
        except jose.JoseError as e:
            raise ApprovalRejected("bad_signature" if e.code == "bad_signature" else "bad_header") from None
        c, now, skew = d.claims, time.time(), self.cfg.settings.approval_skew_s
        if CLAIMS - set(c):
            raise ApprovalRejected("missing_claim")
        if set(c) - CLAIMS:
            raise ApprovalRejected("unexpected_claim")
        ints = all(isinstance(c[k], int) and not isinstance(c[k], bool) for k in ("iat", "exp"))
        checks = [
            (c.get("iss") == APPROVAL_ISSUER, "bad_issuer"),
            (c.get("aud") == APPROVAL_AUDIENCE, "bad_audience"),
            (c.get("op") == APPROVAL_OP, "bad_op"),
            (c.get("schema") == APPROVAL_SCHEMA, "bad_schema"),
            (c.get("kid") == kid, "bad_header"),
            (ints, "malformed"),
            (ints and c["exp"] + skew >= now, "expired"),
            (ints and c["iat"] - skew <= now and 0 < c["exp"] - c["iat"] <= self.cfg.settings.approval_ttl_s, "bad_lifetime"),
            (c.get("sub") == oid, "wrong_rm"),
            (c.get("customer_id") == customer_id, "wrong_customer"),
            (isinstance(c.get("draft_sha256"), str) and hmac.compare_digest(c["draft_sha256"], draft_sha256(customer_id, draft)), "draft_mismatch"),
            (isinstance(c.get("jti"), str) and 16 <= len(c["jti"]) <= 64, "bad_jti"),
        ]
        for ok, reason in checks:
            if not ok:
                raise ApprovalRejected(reason)
        return c
