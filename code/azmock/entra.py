"""Microsoft Entra ID mock (v2.0 endpoints).

Imitates:
  GET  /{tenant}/v2.0/.well-known/openid-configuration   OpenID metadata
  GET  /{tenant}/discovery/v2.0/keys                      JWKS (RS256 signing key, `kid`)
  POST /{tenant}/oauth2/v2.0/token                        grant types:
       client_credentials                                  workload identity -> app token (`idtyp=app`, `roles`)
       urn:ietf:params:oauth:grant-type:jwt-bearer         On-Behalf-Of (`requested_token_use=on_behalf_of`)
       password                                            RM sign-in for the offline console only (see below)

Tokens are real RS256 JWTs with the v2 claim set the services rely on: `iss`, `aud`, `tid`, `oid`, `sub`,
`name`, `preferred_username`, `roles`, `scp`, `azp`, `idtyp`, `iat`, `nbf`, `exp`, plus `auth_time` for step-up.

Differences from the real service (deliberate, documented in README):
* RM sign-in uses ROPC with a fixed demo password so the offline console needs no browser redirect. Production
  uses MSAL authorization code + PKCE (CRM sidebar) or Teams SSO, with MFA and Conditional Access.
* Workloads authenticate with client secrets generated at stack start-up (in memory). Production uses AKS
  workload identity (federated credentials); no secrets exist.
* `auth_time` is put in the access token; real Entra conveys fresh authentication via ID tokens / auth context
  (`acrs`) claims. web-bff's step-up check would read that instead.
* No consent, no Conditional Access, no token revocation, no group overage. Directory = the fictional RMs.
"""
from __future__ import annotations

import secrets
import threading
import time
import uuid

from rm_copilot import data
from services.common import jose, rsa
from services.common.config import AUD, StackConfig, user_oid
from services.common.httpkit import HttpError, Request, Response

from ._base import Mock

CONSOLE_CLIENT_ID = "rm-copilot-console"
APP_ID_URIS = {"web-bff": AUD["web-bff"], "orchestrator": AUD["orchestrator"], "tool-gateway": AUD["tool-gateway"]}
WORKLOADS = ("web-bff", "orchestrator", "tool-gateway", "outbox-worker", "audit-writer", "ingestion-worker", "apim", "ops-admin")
# App roles granted to workload identities on our own APIs (client credentials).
APP_ROLES = {
    "web-bff": {AUD["orchestrator"]: ["Copilot.Invoke"]},
    "orchestrator": {AUD["tool-gateway"]: ["Tools.Invoke", "Audit.Append"], AUD["apim"]: ["AiGateway.Invoke"]},
    "tool-gateway": {AUD["apim"]: ["SystemApis.Read", "SystemApis.Write", "AiGateway.Invoke"]},
    "outbox-worker": {AUD["apim"]: ["SystemApis.Read", "SystemApis.Write"], AUD["tool-gateway"]: ["Audit.Append"]},
    "ingestion-worker": {AUD["apim"]: ["AiGateway.Invoke"]},
    "ops-admin": {AUD["audit"]: ["Audit.ReadContent", "Audit.Erase"]},  # PIM-activated operator role in production
}
OWN_APIS = {AUD["web-bff"], AUD["orchestrator"], AUD["tool-gateway"], AUD["apim"], AUD["audit"]}
# Delegated permissions a client may obtain for the signed-in RM (OBO and sign-in).
DELEGATED = {
    CONSOLE_CLIENT_ID: {AUD["web-bff"]: {"access_as_user"}},
    "web-bff": {AUD["tool-gateway"]: {"Tools.Read", "Tools.Write"}},
    "tool-gateway": {AUD["graph"]: {"Calendars.ReadBasic"}},
}


def upn_for(rm_id: str) -> str:
    return f"{rm_id}@bank.example"


class EntraMock(Mock):
    name = "entra"
    cors = True  # the console calls the token endpoint from the browser, as an MSAL SPA would

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self._key = rsa.PrivateKey.generate()
        self.kid = secrets.token_hex(10)
        self._lock = threading.Lock()
        self.issued = 0
        t = "/{tenant}"
        self.app.route("GET", t + "/v2.0/.well-known/openid-configuration")(self._metadata)
        self.app.route("GET", t + "/discovery/v2.0/keys")(lambda r: {"keys": [jose.rsa_jwk(self._key.public_key(), self.kid)]})
        self.app.route("POST", t + "/oauth2/v2.0/token")(self._token)

    # ------------------------------------------------------------------ helpers
    @property
    def users(self) -> dict[str, dict]:
        return {upn_for(rm): {"rm": rm, "name": v["name"], "oid": user_oid(upn_for(rm))} for rm, v in data.RMS.items()}

    def _sign(self, claims: dict) -> str:
        with self._lock:
            self.issued += 1
        return jose.encode({"typ": "JWT", "alg": "RS256", "kid": self.kid}, claims, self._key.sign)

    def _base_claims(self, aud: str, lifetime: int) -> dict:
        now = int(time.time())
        return {"aud": aud, "iss": self.cfg.issuer, "iat": now, "nbf": now, "exp": now + lifetime, "tid": self.cfg.settings.tenant_id,
                "ver": "2.0", "uti": secrets.token_urlsafe(12)}

    def issue_user_token(self, upn: str, aud: str = AUD["web-bff"], scp: str = "access_as_user", azp: str = CONSOLE_CLIENT_ID,
                         auth_age_s: int = 0, lifetime: int = 3600, auth_time: int | None = None) -> str:
        """In-process helper (tests): the token an RM gets at sign-in."""
        u = self.users[upn]
        c = self._base_claims(aud, lifetime)
        c.update({"azp": azp, "name": u["name"], "oid": u["oid"], "sub": u["oid"], "preferred_username": upn, "roles": ["RM"],
                  "scp": scp, "auth_time": auth_time if auth_time is not None else int(time.time()) - auth_age_s, "amr": ["pwd", "mfa"]})
        return self._sign(c)

    def _metadata(self, r: Request) -> dict:
        base = f"{self.cfg.url('entra')}/{r.params['tenant']}"
        return {"issuer": self.cfg.issuer, "token_endpoint": f"{base}/oauth2/v2.0/token", "jwks_uri": f"{base}/discovery/v2.0/keys",
                "id_token_signing_alg_values_supported": ["RS256"], "tenant_region_scope": "AS"}

    @staticmethod
    def _error(status: int, error: str, description: str) -> Response:
        return Response(status, {"error": error, "error_description": description, "error_codes": [70000 + status],
                                 "timestamp": time.strftime("%Y-%m-%d %H:%M:%SZ", time.gmtime())})

    def _client_ok(self, form: dict) -> bool:
        cid, secret = form.get("client_id", ""), form.get("client_secret", "")
        expected = self.cfg.secrets.get(cid)
        return bool(expected) and secrets.compare_digest(expected, secret)

    @staticmethod
    def _parse_scope(scope: str) -> tuple[str, set[str]]:
        resources, names = set(), set()
        for s in scope.split():
            res, _, name = s.rpartition("/")
            resources.add(res)
            names.add(name)
        if len(resources) != 1:
            raise HttpError(400, "invalid_scope", "scopes must target exactly one resource")
        return resources.pop(), names

    # ------------------------------------------------------------------ token endpoint
    def _token(self, r: Request) -> Response:
        self.simulate("entra")
        if r.params["tenant"] != self.cfg.settings.tenant_id:
            return self._error(400, "invalid_request", "AADSTS90002: Tenant not found.")
        form = r.form()
        grant = form.get("grant_type", "")
        try:
            resource, names = self._parse_scope(form.get("scope", ""))
        except HttpError as e:
            return self._error(400, "invalid_scope", e.message)
        if grant == "client_credentials":
            return self._client_credentials(form, resource, names)
        if grant == "urn:ietf:params:oauth:grant-type:jwt-bearer" and form.get("requested_token_use") == "on_behalf_of":
            return self._obo(form, resource, names)
        if grant == "password":
            return self._password(form, resource, names)
        return self._error(400, "unsupported_grant_type", "AADSTS70003: unsupported grant type")

    def _token_response(self, token: str, claims_lifetime: int, scope: str) -> Response:
        return Response(200, {"token_type": "Bearer", "expires_in": claims_lifetime, "ext_expires_in": claims_lifetime,
                              "access_token": token, "scope": scope}, headers={"Cache-Control": "no-store"})

    def _client_credentials(self, form: dict, resource: str, names: set[str]) -> Response:
        if not self._client_ok(form):
            return self._error(401, "invalid_client", "AADSTS7000215: Invalid client secret provided.")
        if names != {".default"}:
            return self._error(400, "invalid_scope", "AADSTS1002012: client credentials require the /.default scope")
        cid = form["client_id"]
        if resource in OWN_APIS and resource not in APP_ROLES.get(cid, {}):
            return self._error(400, "invalid_grant", "AADSTS501051: application is not assigned a role for this API")
        c = self._base_claims(resource, 3600)
        sp_oid = str(uuid.uuid5(uuid.NAMESPACE_URL, "rm-copilot-sp:" + cid))
        c.update({"azp": cid, "appid": cid, "idtyp": "app", "oid": sp_oid, "sub": sp_oid, "roles": APP_ROLES.get(cid, {}).get(resource, [])})
        return self._token_response(self._sign(c), 3600, f"{resource}/.default")

    def _obo(self, form: dict, resource: str, names: set[str]) -> Response:
        if not self._client_ok(form):
            return self._error(401, "invalid_client", "AADSTS7000215: Invalid client secret provided.")
        cid = form["client_id"]
        try:
            d = jose.verify(form.get("assertion", ""), {self.kid: jose.VerifyingKey.from_jwk(jose.rsa_jwk(self._key.public_key(), self.kid))})
        except jose.JoseError:
            return self._error(400, "invalid_grant", "AADSTS50013: Assertion failed signature validation.")
        a = d.claims
        if a.get("exp", 0) < time.time():
            return self._error(400, "invalid_grant", "AADSTS500133: Assertion is not within its valid time range.")
        if a.get("aud") != APP_ID_URIS.get(cid) or a.get("idtyp") == "app":
            return self._error(400, "invalid_grant", "AADSTS50013: Assertion audience does not match the client application.")
        allowed = DELEGATED.get(cid, {}).get(resource, set())
        granted = allowed if names == {".default"} else names
        if not granted or not granted <= allowed:
            return self._error(400, "invalid_grant", "AADSTS65001: The user or administrator has not consented to use the application.")
        c = self._base_claims(resource, 3600)
        c.update({k: a[k] for k in ("name", "oid", "sub", "preferred_username", "roles", "auth_time", "amr") if k in a})
        c.update({"azp": cid, "appid": cid, "scp": " ".join(sorted(granted))})
        return self._token_response(self._sign(c), 3600, " ".join(f"{resource}/{n}" for n in sorted(granted)))

    def _password(self, form: dict, resource: str, names: set[str]) -> Response:
        if form.get("client_id") != CONSOLE_CLIENT_ID:
            return self._error(400, "unauthorized_client", "AADSTS700016: application not found for this grant")
        upn = form.get("username", "").lower()
        if upn not in self.users or not secrets.compare_digest(form.get("password", ""), self.cfg.settings.dev_password):
            return self._error(400, "invalid_grant", "AADSTS50126: Error validating credentials due to invalid username or password.")
        allowed = DELEGATED[CONSOLE_CLIENT_ID].get(resource, set())
        if not names or not names <= allowed:
            return self._error(400, "invalid_scope", "AADSTS65001: scope not granted")
        token = self.issue_user_token(upn, aud=resource, scp=" ".join(sorted(names)))
        return self._token_response(token, 3600, form["scope"])
