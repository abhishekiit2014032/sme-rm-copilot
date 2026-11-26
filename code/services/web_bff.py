"""web-bff: backend-for-frontend for the CRM sidebar and the Teams personal tab (plan §3.2, §3.4, §4.2 steps 5-6).

* Validates the RM's Entra token (audience web-bff) and exchanges it On-Behalf-Of for a token with audience
  tool-gateway (`Tools.Read`; `Tools.Read Tools.Write` only on approve). That token travels to the orchestrator
  in `X-RM-Authorization`; web-bff authenticates itself with its own workload token.
* Creates the request id and trace, sets the request deadline (UC1 8 s, UC2 7 s, UC3 10 s, commit 2 s).
* Session in Cosmos DB with a CSRF token. Approve requires: allowed `Origin`, matching CSRF token, and
  `auth_time` no older than 10 minutes (else 401 `step_up_required`).
* Mints approval tokens: canonical draft -> SHA-256 -> claims -> Key Vault `sign` (ES256). It never sees the key.
* Serves the console with a strict Content-Security-Policy; the console renders model output as text only.
* `GET /api/trace/{request_id}`: the span tree for one request, collected from every component's telemetry
  endpoint (local stand-in for an Application Insights end-to-end transaction view).
"""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
import urllib.parse
from collections import OrderedDict
from pathlib import Path

from rm_copilot import data
from services.common import jose
from services.common.config import AUD, StackConfig
from services.common.contracts import (APPROVAL_TYP, FOLLOWUP_SUBJECT, approval_claims, followup_payload, outlook_compose_link, valid_customer_id,
                                       validate_followup)
from services.common.cosmos_client import CosmosClient
from services.common.httpkit import HttpError, Request, Response, TransportError, current_trace_id, request_context, run_parallel, time_left
from services.common.service import Service

WEB_DIR = Path(__file__).with_name("web")
STATIC = {"console.js": "application/javascript; charset=utf-8", "console.css": "text/css; charset=utf-8"}
APPROVAL_KEY = "approval-signing"


def _origin(url: str) -> str:
    u = urllib.parse.urlsplit(url)
    return f"{u.scheme}://{u.netloc}"


class WebBff(Service):
    name = "web-bff"

    def __init__(self, cfg: StackConfig):
        entra = _origin(cfg.url("entra"))
        csp = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'none'; "
               f"connect-src 'self' {entra}; base-uri 'none'; form-action 'none'; frame-ancestors 'self'")
        super().__init__(cfg, response_headers={"Content-Security-Policy": csp, "X-Content-Type-Options": "nosniff",
                                                "Referrer-Policy": "no-referrer", "Cache-Control": "no-store"}, edge=True)
        self.cosmos = CosmosClient(self)
        self._traces: OrderedDict[str, tuple[str, str]] = OrderedDict()  # request_id -> (trace_id, oid)
        self._kid: str | None = None
        self._lock = threading.Lock()
        m = self.metrics
        self.m_issued = m.counter("approval_tokens_issued_total", "Approval tokens minted after an RM approval", ())
        self.m_stepup = m.counter("step_up_required_total", "Approvals refused for stale authentication", ())
        self.m_csrf = m.counter("csrf_rejections_total", "Approvals refused by Origin/CSRF checks", ("reason",))
        a = self.app.route
        a("GET", "/")(lambda r: self._file("console.html", "text/html; charset=utf-8"))
        a("GET", "/favicon.ico")(lambda r: Response(204, None))
        a("GET", "/static/{name}")(lambda r: self._file(r.params["name"], STATIC.get(r.params["name"])))
        a("GET", "/api/config")(self._config)
        a("POST", "/api/session")(self._session)
        a("GET", "/api/meetings")(lambda r: self._proxy(r, "GET", "/v1/meetings", None, self.cfg.settings.deadline_uc1_s))
        a("POST", "/api/uc1/brief")(lambda r: self._proxy(r, "POST", "/v1/brief", r.json(), self.cfg.settings.deadline_uc1_s))
        a("POST", "/api/uc2/ask")(lambda r: self._proxy(r, "POST", "/v1/ask", r.json(), self.cfg.settings.deadline_uc2_s))
        a("POST", "/api/uc3/draft")(self._draft)
        a("POST", "/api/uc3/approval-token")(self._approval_token)
        a("POST", "/api/uc3/approve")(self._approve)
        a("GET", "/api/uc3/writes/{jti}")(lambda r: self._proxy(r, "GET", f"/v1/writes/{urllib.parse.quote(r.params['jti'], safe='')}", None, self.cfg.settings.deadline_commit_s))
        a("GET", "/api/trace/{request_id}")(self._trace)

    # ------------------------------------------------------------------ static and config
    def _file(self, name: str, ctype: str | None) -> Response:
        path = WEB_DIR / name
        if ctype is None or not path.is_file():
            raise HttpError(404, "NotFound", "no such file")
        return Response(200, path.read_bytes(), content_type=ctype)

    def _config(self, r: Request) -> dict:
        return {"token_url": self.cfg.token_url, "client_id": "rm-copilot-console", "scope": f"{AUD['web-bff']}/access_as_user",
                "mock_password": self.cfg.settings.dev_password,  # mock sign-in only; production uses MSAL / Teams SSO
                "users": [{"upn": f"{rm}@bank.example", "name": v["name"], "team": v["team"]} for rm, v in data.RMS.items()],
                "customers": [{"id": cid, "name": c["name"]} for cid, c in data.CUSTOMERS.items()]}

    # ------------------------------------------------------------------ identity
    def _user(self, r: Request) -> tuple[dict, str]:
        token = r.bearer()
        claims = self.validator("web-bff").validate(token, scopes=("access_as_user",), app_only=False)
        ctx = request_context()
        ctx.data.update(rm_oid=claims["oid"])
        tid = current_trace_id()
        if tid:
            with self._lock:
                self._traces[ctx.request_id] = (tid, claims["oid"])
                while len(self._traces) > 5000:
                    self._traces.popitem(last=False)
        return claims, token  # type: ignore[return-value]

    def _set_deadline(self, seconds: float) -> None:
        ctx = request_context()
        d = time.monotonic() + seconds
        ctx.deadline = d if ctx.deadline is None else min(ctx.deadline, d)

    def _session(self, r: Request) -> dict:
        claims, _ = self._user(r)
        sid, csrf = secrets.token_urlsafe(18), secrets.token_urlsafe(24)
        self.cosmos.upsert("sessions", {"id": sid, "oid": claims["oid"], "csrf": csrf, "created": int(time.time())})
        return {"session_id": sid, "csrf_token": csrf, "name": claims.get("name"), "upn": claims.get("preferred_username"),
                "auth_time": claims.get("auth_time")}

    # ------------------------------------------------------------------ calls to the orchestrator
    def _orch(self, method: str, path: str, body: dict | None, user_token: str, write: bool = False) -> Response:
        names = "Tools.Read Tools.Write" if write else "Tools.Read"
        rm_token = self.tokens.obo_token(user_token, " ".join(f"{AUD['tool-gateway']}/{n}" for n in names.split()))
        headers = {**self.auth_headers("orchestrator"), "x-rm-authorization": "Bearer " + rm_token}
        try:
            resp = self.client.request(method, f"{self.cfg.url('orchestrator')}{path}", json_body=body, headers=headers,
                                       timeout=max(0.1, time_left()), peer="orchestrator")
        except TransportError as e:
            raise HttpError(504, "DEPENDENCY_TIMEOUT", "The copilot did not answer in time.") if e.kind in ("timeout", "deadline") else \
                HttpError(503, "DEPENDENCY_UNAVAILABLE", "The copilot is unavailable.", details={"reason": "ORCHESTRATOR"})
        out = resp.json()
        if isinstance(out, dict):
            out.update(request_id=request_context().request_id, trace_id=current_trace_id())
        hdrs = {k: v for k, v in resp.headers.items() if k in ("retry-after",)}
        return Response(resp.status, out, hdrs)

    def _proxy(self, r: Request, method: str, path: str, body: dict | None, deadline_s: float) -> Response:
        _, token = self._user(r)
        self._set_deadline(deadline_s)
        return self._orch(method, path, body, token)

    def _draft(self, r: Request) -> Response:
        claims, token = self._user(r)
        self._set_deadline(self.cfg.settings.deadline_uc3_s)
        resp = self._orch("POST", "/v1/followup", r.json(), token)
        if resp.status == 200 and isinstance(resp.body, dict):
            draft_id = secrets.token_urlsafe(12)
            self.cosmos.upsert("drafts", {"id": draft_id, "oid": claims["oid"], "customer_id": resp.body.get("customer_id"),
                                          "model_draft": followup_payload({**resp.body, "email": resp.body.get("email_draft", "")}), "status": "drafted", "created": int(time.time())})
            resp.body["draft_id"] = draft_id
        return resp

    # ------------------------------------------------------------------ approval
    def _approval_checks(self, r: Request) -> tuple[dict, str]:
        claims, token = self._user(r)
        allowed = {_origin(self.cfg.url("web-bff")), _origin(self.cfg.url("web-bff")).replace("127.0.0.1", "localhost")}
        if (r.header("origin") or "") not in allowed:
            self.m_csrf.inc(reason="origin")
            raise HttpError(403, "ORIGIN_REJECTED", "Approvals are accepted only from the copilot UI.")
        session = self.cosmos.read("sessions", r.header("x-session-id") or "-")
        if not session or session.get("oid") != claims["oid"] or not secrets.compare_digest(str(session.get("csrf")), r.header("x-csrf-token") or ""):
            self.m_csrf.inc(reason="csrf")
            raise HttpError(403, "CSRF_FAILED", "Session check failed. Reload the copilot.")
        if time.time() - int(claims.get("auth_time", 0)) > self.cfg.settings.step_up_max_age_s:
            self.m_stepup.inc()
            raise HttpError(401, "STEP_UP_REQUIRED", "Sign in again to approve.",
                            headers={"WWW-Authenticate": 'Bearer error="insufficient_claims", error_description="fresh authentication required"'})
        return claims, token

    def _signing_kid(self) -> str:
        if self._kid is None:
            resp = self.client.request("GET", f"{self.cfg.url('keyvault')}/keys/{APPROVAL_KEY}?api-version=7.4", headers=self.auth_headers("vault"),
                                       timeout=1.0, peer="keyvault")
            if not resp.ok:
                raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "Approvals are unavailable right now.", details={"reason": "KEY_VAULT"})
            self._kid = resp.json()["key"]["kid"]
        return self._kid

    def mint(self, oid: str, customer_id: str, draft: dict) -> tuple[str, dict]:
        kid = self._signing_kid()
        claims = approval_claims(oid, customer_id, draft, kid, self.cfg.settings.approval_ttl_s)
        header = {"alg": "ES256", "typ": APPROVAL_TYP, "kid": kid}

        def kv_sign(signing_input: bytes) -> bytes:
            digest = jose.b64u(hashlib.sha256(signing_input).digest())
            resp = self.client.request("POST", f"{kid}/sign?api-version=7.4", json_body={"alg": "ES256", "value": digest},
                                       headers=self.auth_headers("vault"), timeout=1.0, peer="keyvault")
            if not resp.ok:
                raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "Approvals are unavailable right now.", details={"reason": "KEY_VAULT"})
            return jose.b64u_decode(resp.json()["value"])
        token = jose.encode(header, claims, kv_sign)
        self.m_issued.inc()  # the approval_issued audit event is produced by tool-gateway from the verified token (LLD §6.4)
        return token, claims

    def _approval_request(self, r: Request) -> tuple[dict, str, str, dict]:
        claims, token = self._approval_checks(r)
        body = r.json()
        cid = body.get("customer_id", "") if isinstance(body, dict) else ""
        if not valid_customer_id(cid):
            raise HttpError(400, "VALIDATION_FAILED", "customer_id is required", details={"reason": "SCHEMA", "field": "/customer_id"})
        request_context().data["customer_id"] = cid
        try:
            draft = validate_followup(body.get("draft") or {})
        except ValueError as e:
            raise HttpError(400, "VALIDATION_FAILED", "The draft does not match schema followup/1.", details={"reason": "SCHEMA", "field": str(e)}) from None
        return claims, token, cid, draft

    def _approval_token(self, r: Request) -> dict:
        self._set_deadline(self.cfg.settings.deadline_commit_s)
        claims, _, cid, draft = self._approval_request(r)
        token, c = self.mint(claims["oid"], cid, draft)
        return {"approval_token": token, "jti": c["jti"], "exp": c["exp"]}

    def _approve(self, r: Request) -> Response:
        self._set_deadline(self.cfg.settings.deadline_commit_s)
        claims, user_token, cid, draft = self._approval_request(r)
        approval, c = self.mint(claims["oid"], cid, draft)
        resp = self._orch("POST", "/v1/commit", {"customer_id": cid, "draft": draft, "approval_token": approval}, user_token, write=True)
        body = r.json()
        if isinstance(resp.body, dict):
            email = draft["email"]  # only the approved, hash-bound email text goes into the compose link
            resp.body["email"] = {"body": email, "compose_link": outlook_compose_link(FOLLOWUP_SUBJECT, email)} if email and resp.status in (201, 202) else None
            if body.get("draft_id"):
                doc = self.cosmos.read("drafts", str(body["draft_id"]))
                if doc and doc.get("oid") == claims["oid"]:
                    doc = {k: v for k, v in doc.items() if not k.startswith("_")}
                    doc.update(status=resp.body.get("status"), jti=c["jti"], edited=doc.get("model_draft") != draft)
                    self.cosmos.upsert("drafts", doc)
        return resp

    # ------------------------------------------------------------------ trace view
    def _trace(self, r: Request) -> dict:
        claims, _ = self._user(r)
        with self._lock:
            entry = self._traces.get(r.params["request_id"])
        if not entry or entry[1] != claims["oid"]:
            raise HttpError(404, "NotFound", "no trace for this request")
        trace_id = entry[0]
        bases = sorted({_origin(u) for u in self.cfg.urls.values()})
        fetched = run_parallel({b: (lambda b=b: self.client.request("GET", f"{b}/telemetry/spans?trace_id={trace_id}", timeout=2.0, peer="telemetry"))
                                for b in bases}, max_workers=16)
        spans = []
        for resp, err in fetched.values():
            if err is None and resp.ok:
                spans += resp.json().get("value", [])
        spans = [s for s in spans if s["name"] != "GET /telemetry/spans"]
        spans.sort(key=lambda s: s["start"])
        by_id = {s["span_id"]: s for s in spans}
        for s in spans:
            depth, p = 0, s.get("parent_id")
            while p in by_id and depth < 50:
                depth, p = depth + 1, by_id[p].get("parent_id")
            s["depth"] = depth
        t0 = spans[0]["start"] if spans else 0
        for s in spans:
            s["offset_ms"] = round((s["start"] - t0) * 1000, 1)
        return {"trace_id": trace_id, "request_id": r.params["request_id"], "components": sorted({s["service"] for s in spans}), "spans": spans}
