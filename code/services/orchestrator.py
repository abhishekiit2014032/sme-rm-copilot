"""orchestrator: UC1/UC2/UC3 workflows (plan §3.2, §4).

Runs the same `rm_copilot.Copilot` workflows as the in-process demo and evals, with HTTP adapters
(`orchestrator_adapters.py`) for the model (APIM AI gateway), the tool layer (tool-gateway) and the safety
services (Content Safety, AI Language). It holds no keys and cannot mint approvals.

Inbound: `Authorization` = web-bff workload token (role `Copilot.Invoke`); `X-RM-Authorization` = the RM's OBO
token for tool-gateway, validated here for the RM's `oid` (quota key, audit) and forwarded unchanged.
Deadlines come from web-bff in `x-deadline-ms`; every hop derives its timeout from what is left. Feature flags
and kill switches come from App Configuration (cached `flags_ttl_s`; on outage the last known value is used,
and writes fail closed). Output is buffered and verified before it is returned (no streaming, plan C18).
"""
from __future__ import annotations

import json
import time
import urllib.parse

from rm_copilot import tools as t
from rm_copilot.orchestrator import Copilot
from services.common.config import StackConfig
from services.common.contracts import JTI_RE, RemoteAuditSink, valid_customer_id
from services.common.flags import FLAG_DEFAULTS, FeatureFlags  # noqa: F401 - re-exported
from services.common.httpkit import HttpError, Request, Response, TransportError, request_context, time_left
from services.common.service import Service
from services.common.telemetry import current_span
from services.orchestrator_adapters import ApimChatModel, Budget, HttpGuard, HttpTools, rm_user


def decline_code(reason: str) -> str:
    for needle, code in (("no approved document", "weak_retrieval"), ("insufficient", "insufficient_evidence"),
                         ("no time to retry", "ungrounded_no_time"), ("twice", "ungrounded"), ("content safety", "harm")):
        if needle in reason:
            return code
    return "other"


class Orchestrator(Service):
    name = "orchestrator"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self.audit = RemoteAuditSink(cfg, self.client, self.tokens, self.name, transport_key=self.transport_key)
        self._transport: tuple[float, dict] | None = None
        self.tools = HttpTools(self)
        self.copilot = Copilot(ApimChatModel(self), self.tools, HttpGuard(self), time_left=time_left)
        self.flags = FeatureFlags(self)
        m = self.metrics
        self.m_requests = m.counter("usecase_requests_total", "Use-case requests by outcome", ("use_case", "outcome"))
        self.m_declines = m.counter("declines_total", "UC2 declines by reason", ("use_case", "reason"))
        self.m_degraded = m.counter("degraded_responses_total", "Degraded-mode responses (model unavailable)", ("use_case",))
        self.m_rejected = m.counter("guardrail_events_total", "Guardrail detections", ("type",))
        a = self.app.route
        a("POST", "/v1/brief")(lambda r: self._run(r, "uc1", self._brief))
        a("POST", "/v1/ask")(lambda r: self._run(r, "uc2", self._ask))
        a("POST", "/v1/followup")(lambda r: self._run(r, "uc3", self._followup))
        a("POST", "/v1/commit")(lambda r: self._run(r, "commit", self._commit))
        a("GET", "/v1/writes/{jti}")(lambda r: self._run(r, "status", self._writes))
        a("GET", "/v1/meetings")(lambda r: self._run(r, "meetings", lambda r, u: {"value": self.tools.meetings()}))

    def transport_key(self) -> dict:
        """Public half of Key Vault key `audit-transport`, published in App Configuration (cached 5 min)."""
        if self._transport and self._transport[0] > time.monotonic():
            return self._transport[1]
        key = urllib.parse.quote("audit/transport-public-key", safe="")
        try:
            r = self.client.request("GET", f"{self.cfg.url('appconfig')}/kv/{key}?api-version=2023-11-01",
                                    headers=self.auth_headers("appconfig"), timeout=1.0, peer="appconfig")
        except TransportError:
            r = None
        if r is None or not r.ok:
            if self._transport:
                return self._transport[1]
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "audit transport key unavailable", details={"reason": "AUDIT"})
        self._transport = (time.monotonic() + 300, json.loads(r.json()["value"]))
        return self._transport[1]

    # ------------------------------------------------------------------ plumbing
    def _run(self, r: Request, use_case: str, fn) -> Response | dict:
        self.validator("orchestrator").validate(r.bearer(), scopes=("Copilot.Invoke",), azp=("web-bff",), app_only=True)
        rm_token = r.bearer("x-rm-authorization")
        claims = self.validator("tool-gateway").validate(rm_token, scopes=("Tools.Read",), app_only=False)
        request_context().data.update(rm_token=rm_token, rm_oid=claims["oid"], use_case=use_case, budget=Budget(), lineage={})
        if (sp := current_span()) is not None:
            sp.set(**{"rmc.use_case": use_case})
        flag = {"uc1": "uc1", "uc2": "uc2", "uc3": "uc3", "commit": "writes"}.get(use_case)
        if flag and not self.flags.enabled(flag):
            self.m_requests.inc(use_case=use_case, outcome="feature_disabled")
            raise HttpError(503, "FEATURE_DISABLED", "This feature is switched off right now.", details={"flag": flag}, retryable=False)
        try:
            result = fn(r, rm_user(claims))
        except t.NotEntitled:
            self.m_requests.inc(use_case=use_case, outcome="not_entitled")
            raise HttpError(403, "NOT_ENTITLED", "You are not entitled to this customer.") from None
        except HttpError as e:
            self.m_requests.inc(use_case=use_case, outcome=e.code.lower())
            raise
        if isinstance(result, dict) and use_case in ("uc1", "uc2", "uc3"):
            degraded = bool(result.get("degraded")) or result.get("status") == "degraded"
            warnings = [{"code": "DEGRADED", "reason": "MODEL_UNAVAILABLE"}] if degraded else []
            warnings += [{"code": "SOURCE_UNAVAILABLE", "section": s} for s in result.get("partial", [])]
            result.update(mode="degraded" if degraded else ("partial" if result.get("partial") else "full"), warnings=warnings)
        if isinstance(result, dict):
            outcome = "degraded" if result.get("degraded") or result.get("status") == "degraded" else str(result.get("status", "ok"))
            self.m_requests.inc(use_case=use_case, outcome=outcome)
            if outcome == "degraded":
                self.m_degraded.inc(use_case=use_case)
        return result

    @staticmethod
    def _body(r: Request) -> dict:
        body = r.json()
        if not isinstance(body, dict):
            raise HttpError(400, "VALIDATION_FAILED", "the request body must be a JSON object", details={"reason": "SCHEMA"})
        return body

    def _customer(self, r: Request) -> str:
        # A plain id only: it is interpolated into tool-gateway URLs and names the audit content key (C21).
        cid = self._body(r).get("customer_id", "")
        if not valid_customer_id(cid):
            raise HttpError(400, "VALIDATION_FAILED", "customer_id is required", details={"reason": "SCHEMA", "field": "/customer_id"})
        request_context().data["customer_id"] = cid
        return cid

    # ------------------------------------------------------------------ use cases
    def _brief(self, r: Request, user: t.User) -> dict:
        out = self.copilot.brief(user, self._customer(r))
        if out["rejected_model_output"]:
            self.m_rejected.inc(len(out["rejected_model_output"]), type="model_output_rejected")
        return out

    def _ask(self, r: Request, user: t.User) -> dict:
        q = str(self._body(r).get("question", "")).strip()
        if not q or len(q) > 2000:
            raise HttpError(400, "VALIDATION_FAILED", "question is required (max 2000 characters)", details={"reason": "SCHEMA"})
        out = self.copilot.ask(user, q)
        if out["status"] == "declined":
            self.m_declines.inc(use_case="uc2", reason=decline_code(out["reason"]))
        return out

    def _followup(self, r: Request, user: t.User) -> dict:
        notes = str(self._body(r).get("notes", ""))
        if not notes.strip() or len(notes) > 8000:
            raise HttpError(400, "VALIDATION_FAILED", "notes are required (max 8000 characters)", details={"reason": "SCHEMA"})
        return self.copilot.draft_followup(user, self._customer(r), notes)

    def _commit(self, r: Request, user: t.User) -> Response:
        body = self._body(r)
        cid = self._customer(r)
        draft = body.get("draft") or {}
        # The full approved draft (incl. email text) travels unchanged: the token's hash covers all of it.
        result = self.tools.create_call_report(user, cid, draft, approval_token=body.get("approval_token"))
        return Response(result.pop("http_status", 200), result)

    def _writes(self, r: Request, user: t.User) -> Response:
        if not JTI_RE.fullmatch(r.params["jti"]):
            raise HttpError(404, "NOT_FOUND", "no write with this id for you")
        resp = self.tools.write_status(r.params["jti"])
        return Response(resp.status, resp.json())
