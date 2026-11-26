"""Identity plumbing for services: validate Entra ID tokens locally (JWKS) and acquire tokens (client
credentials and On-Behalf-Of).

Production equivalent: `azure-identity` (WorkloadIdentityCredential on AKS) and MSAL `acquire_token_on_behalf_of`
to get tokens; Microsoft.Identity.Web or a JOSE library with the tenant's OpenID metadata to validate them.
Difference: locally each workload authenticates with a client secret generated at stack start-up (held in memory
only) instead of a federated Kubernetes service-account token.
"""
from __future__ import annotations

import hashlib
import threading
import time

from . import jose
from .httpkit import HttpClient, HttpError, TransportError


def unauthorized(reason: str, message: str = "") -> HttpError:
    return HttpError(401, "UNAUTHENTICATED", message or "authentication failed", details={"reason": reason.upper()},
                     headers={"WWW-Authenticate": f'Bearer error="invalid_token", error_description="{reason}"'})


class Jwks:
    """Fetches and caches signing keys; refreshes on an unknown `kid` at most every 5 s (key rotation)."""

    def __init__(self, client: HttpClient, url: str):
        self.client, self.url = client, url
        self._keys: dict[str, jose.VerifyingKey] = {}
        self._fetched = 0.0
        self._lock = threading.Lock()

    def keys(self, kid: str | None = None) -> dict[str, jose.VerifyingKey]:
        with self._lock:
            if (not self._keys or (kid and kid not in self._keys)) and time.monotonic() - self._fetched > 5:
                try:
                    r = self.client.request("GET", self.url, timeout=2.0, peer="entra")
                except TransportError:
                    r = None
                self._fetched = time.monotonic()
                if r is not None and r.ok:
                    self._keys = {k["kid"]: jose.VerifyingKey.from_jwk(k) for k in r.json().get("keys", [])}
            return dict(self._keys)


class TokenValidator:
    """Validates Entra-style access tokens for one audience. Results are cached until the token expires."""

    def __init__(self, jwks: Jwks, issuer: str, audience: str, tenant_id: str, leeway_s: int = 60):
        self.jwks, self.issuer, self.audience, self.tenant_id, self.leeway = jwks, issuer, audience, tenant_id, leeway_s
        self._cache: dict[str, dict] = {}
        self._lock = threading.Lock()

    def validate(self, token: str | None, *, scopes: tuple[str, ...] = (), azp: tuple[str, ...] = (), app_only: bool | None = None) -> dict:
        if not token:
            raise unauthorized("missing_token", "a bearer token is required")
        key = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            claims = self._cache.get(key)
        if claims is None or claims["exp"] + self.leeway < time.time():
            claims = self._verify(token)
            with self._lock:
                if len(self._cache) > 5000:
                    self._cache.clear()
                self._cache[key] = claims
        is_app = claims.get("idtyp") == "app"
        if app_only is True and not is_app:
            raise unauthorized("app_token_required", "a workload identity token is required")
        if app_only is False and is_app:
            raise unauthorized("user_token_required", "a delegated user token is required")
        if azp and claims.get("azp") not in azp:
            raise HttpError(403, "INSUFFICIENT_SCOPE", "this caller may not call this API", details={"reason": "WORKLOAD_NOT_ALLOWED"})
        granted = set(str(claims.get("scp", "")).split()) | set(claims.get("roles", []) if is_app else [])
        missing = [s for s in scopes if s not in granted]
        if missing:
            raise HttpError(403, "INSUFFICIENT_SCOPE", f"missing scope {missing[0]}", details={"reason": "MISSING_SCOPE"})
        return claims

    def _verify(self, token: str) -> dict:
        try:
            kid = jose.decode_unverified(token).header.get("kid")
            d = jose.verify(token, self.jwks.keys(kid))
        except jose.JoseError as e:
            raise unauthorized(e.code) from None
        c, now = d.claims, time.time()
        if c.get("iss") != self.issuer:
            raise unauthorized("bad_issuer")
        if c.get("tid") != self.tenant_id:
            raise unauthorized("bad_tenant")
        aud = c.get("aud")
        if aud != self.audience and not (isinstance(aud, list) and self.audience in aud):
            raise unauthorized("bad_audience")
        if not isinstance(c.get("exp"), (int, float)) or c["exp"] + self.leeway < now:
            raise unauthorized("token_expired")
        if isinstance(c.get("nbf"), (int, float)) and c["nbf"] - self.leeway > now:
            raise unauthorized("token_not_yet_valid")
        return c


class TokenProvider:
    """Acquires and caches tokens from the Entra token endpoint for one workload identity."""

    def __init__(self, client: HttpClient, token_url: str, client_id: str, client_secret: str):
        self.client, self.token_url, self.client_id, self._secret = client, token_url, client_id, client_secret
        self._cache: dict[tuple[str, str], tuple[str, float]] = {}
        self._lock = threading.Lock()

    def _client_auth(self) -> dict:
        return {"client_secret": self._secret}

    def _request(self, form: dict) -> tuple[str, float]:
        import urllib.parse
        body = urllib.parse.urlencode({"client_id": self.client_id, **self._client_auth(), **form}).encode()
        r = self.client.request("POST", self.token_url, data=body, headers={"content-type": "application/x-www-form-urlencoded"},
                                timeout=3.0, peer="entra")
        if not r.ok:
            raise HttpError(401 if r.status in (400, 401) else 502, "token_acquisition_failed", r.json().get("error", "token request failed"))
        j = r.json()
        return j["access_token"], time.time() + int(j.get("expires_in", 3600))

    def _cached(self, key: tuple[str, str], form: dict) -> str:
        with self._lock:
            hit = self._cache.get(key)
        if hit and hit[1] - 120 > time.time():
            return hit[0]
        token, exp = self._request(form)
        with self._lock:
            if len(self._cache) > 5000:
                self._cache.clear()
            self._cache[key] = (token, exp)
        return token

    def app_token(self, scope: str) -> str:
        return self._cached(("app", scope), {"grant_type": "client_credentials", "scope": scope})

    def obo_token(self, assertion: str, scope: str) -> str:
        key = (hashlib.sha256(assertion.encode()).hexdigest(), scope)
        return self._cached(key, {"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "requested_token_use": "on_behalf_of",
                                  "assertion": assertion, "scope": scope})


class FederatedTokenProvider(TokenProvider):
    """Tokens for an agent identity, which has no secret: the client authenticates with a federated credential, the
    workload assertion of the pod it runs in (`assertion()`; production: the projected service-account token AKS
    workload identity mounts, `azure-identity` WorkloadIdentityCredential / Entra Agent ID SDK)."""

    def __init__(self, client: HttpClient, token_url: str, client_id: str, assertion):
        super().__init__(client, token_url, client_id, "")
        self._assertion = assertion

    def _client_auth(self) -> dict:
        from .identities import JWT_BEARER_ASSERTION
        return {"client_assertion_type": JWT_BEARER_ASSERTION, "client_assertion": self._assertion()}
