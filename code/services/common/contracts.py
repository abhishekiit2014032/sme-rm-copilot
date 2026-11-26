"""Contracts shared between services: the approval token, the draft digest, the Outlook deeplink, audit events.

In production these live in a small versioned contract library used by web-bff (signer) and tool-gateway
(verifier); changing any of them is a `schema` / contract version bump.
"""
from __future__ import annotations

import re
import secrets
import time
import unicodedata
import urllib.parse
from dataclasses import dataclass

from rm_copilot import guardrails
from rm_copilot import tools as rm_tools

from . import jose
from .auth import TokenProvider
from .config import AUD, StackConfig, scope
from .httpkit import HttpClient, HttpError, TransportError, current_trace_id, request_context
from .telemetry import current_span

APPROVAL_OP = "crm.commit_followup"
APPROVAL_SCHEMA = "followup/1"
APPROVAL_TYP = "approval+jwt"
APPROVAL_ISSUER = "web-bff"        # literal values, plan §4.2 / LLD §4.1
APPROVAL_AUDIENCE = "tool-gateway"
TOOL_CONTRACT_VERSION = rm_tools.TOOL_CONTRACT_VERSION
GUARDRAIL_VERSION = guardrails.GUARDRAIL_VERSION


# Identifiers that travel in URLs, audit metadata and content-key names. Plain ids only: no separators, so a
# customer can never be aliased ("x/../y", "x?") into another URL or another crypto-shred key (C21).
CUSTOMER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,63}$")
JTI_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
MAX_TASKS = 10  # LLD §5.1: items t1..t10
MAX_TEXT = {"call_report": 8000, "email": 8000, "title": 300, "due": 32, "type": 32}


def valid_customer_id(value: object) -> bool:
    return isinstance(value, str) and bool(CUSTOMER_ID_RE.fullmatch(value))


def _nfc(value: object) -> str:
    return unicodedata.normalize("NFC", str(value))


def validate_followup(draft: object) -> dict:
    """Schema `followup/1` (LLD §4.4 step 4): raises ValueError(field) if the submitted draft does not conform."""
    if not isinstance(draft, dict):
        raise ValueError("/draft")
    for k in ("call_report", "email"):
        if not isinstance(draft.get(k, ""), str) or len(draft.get(k, "")) > MAX_TEXT[k]:
            raise ValueError(f"/draft/{k}")
    tasks = draft.get("tasks", [])
    if not isinstance(tasks, list) or len(tasks) > MAX_TASKS:
        raise ValueError("/draft/tasks")
    for i, t in enumerate(tasks):
        if not isinstance(t, dict):
            raise ValueError(f"/draft/tasks/{i}")
        for k in ("type", "title", "due"):
            if not isinstance(t.get(k, ""), (str, type(None))) or len(t.get(k) or "") > MAX_TEXT[k]:
                raise ValueError(f"/draft/tasks/{i}/{k}")
    return followup_payload(draft)


def followup_payload(draft: dict) -> dict:
    """The approved draft (schema followup/1): call report, tasks with type and due date, and the email text.
    Everything the RM approved is bound by the hash, so the compose link can only carry approved text.
    Strings are Unicode NFC (LLD §4.2 step 2), so canonically equivalent text hashes and is written identically."""
    tasks = [{"type": _nfc(t.get("type") or "follow_up"), "title": _nfc(t.get("title", "")), "due": _nfc(t.get("due", ""))}
             for t in draft.get("tasks", []) if isinstance(t, dict)]
    return {"call_report": _nfc(draft.get("call_report", "")), "tasks": tasks, "email": _nfc(draft.get("email", ""))}


def draft_sha256(customer_id: str, draft: dict) -> str:
    """SHA-256 over the RFC 8785-style canonical JSON of {schema, customer_id, approved draft}."""
    return jose.sha256_hex(jose.canonical_json({"schema": APPROVAL_SCHEMA, "customer_id": customer_id, **followup_payload(draft)}))


def approval_claims(oid: str, customer_id: str, draft: dict, kid: str, ttl_s: int = 300) -> dict:
    now = int(time.time())
    return {"iss": APPROVAL_ISSUER, "aud": APPROVAL_AUDIENCE, "sub": oid, "customer_id": customer_id, "op": APPROVAL_OP,
            "draft_sha256": draft_sha256(customer_id, draft), "schema": APPROVAL_SCHEMA, "iat": now, "exp": now + ttl_s,
            "jti": secrets.token_urlsafe(16), "kid": kid}


def outlook_compose_link(subject: str, body: str) -> str:
    """Opens a new message in Outlook on the web for the RM to review and send. Nothing is written to the mailbox."""
    return "https://outlook.office.com/mail/deeplink/compose?" + urllib.parse.urlencode({"subject": subject, "body": body}, quote_via=urllib.parse.quote)


FOLLOWUP_SUBJECT = "Follow-up from today's meeting"


# --------------------------------------------------------------------------- audit events
AUDIT_SCHEMA = "audit/1"
AUDIT_EVENTS = frozenset({
    "tool_call", "entitlement_denied", "brief_built", "groundedness", "declined", "degraded", "injection_flagged", "followup_drafted",
    "model_call", "approval_issued", "approval_verified", "write_blocked", "write_committed", "write_queued", "write_failed",
})


def _lineage_now() -> dict:
    lineage = dict(request_context().data.get("lineage") or {})
    if isinstance(lineage.get("models"), set):
        lineage["models"] = sorted(lineage["models"])
    return lineage


@dataclass
class EventHubAuditSink:
    """tool-gateway only (the sole Event Hubs producer, LLD §6.4). Sends and waits for the acknowledgement
    (audit RPO 0); fails closed. Event = metadata (ids, decisions, lineage; never free text) + optional
    `content_envelope` (content already encrypted by the producer, see envelope.py)."""
    cfg: StackConfig
    client: HttpClient
    tokens: TokenProvider
    source: str

    def emit_many(self, events: list[dict], posted_by: str | None = None, actor: dict | None = None) -> None:
        ctx = request_context()
        span = current_span()
        batch = []
        for e in events:
            body = {"schema": AUDIT_SCHEMA, "event_id": secrets.token_hex(12), "ts": round(time.time(), 3), "event": e["event"],
                    "producer": {"service": self.source, "posted_by": posted_by or self.source},
                    "actor": actor or {"type": "rm", "oid": ctx.data.get("rm_oid")},
                    "request_id": ctx.request_id, "trace_id": current_trace_id(),
                    "customer": e.get("customer") or ctx.data.get("customer_id"), "metadata": e.get("metadata") or {},
                    "lineage": {**(e.get("lineage") or _lineage_now()), "guardrails": GUARDRAIL_VERSION, "tool_contract": TOOL_CONTRACT_VERSION}}
            if e.get("content_envelope"):
                body["content_envelope"] = e["content_envelope"]
            batch.append({"Body": body, "UserProperties": {"Diagnostic-Id": span.context.traceparent() if span else None}})
        try:
            r = self.client.request("POST", f"{self.cfg.url('eventhubs')}/audit/messages", json_body=batch,
                                    headers={"authorization": "Bearer " + self.tokens.app_token(scope("eventhubs")),
                                             "content-type": "application/vnd.microsoft.servicebus.json"}, timeout=2.0, peer="eventhubs")
        except TransportError:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "the audit stream did not acknowledge; nothing was released", details={"reason": "AUDIT"}) from None
        if r.status != 201:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "the audit stream rejected the event", details={"reason": "AUDIT"})

    def emit(self, event: str, content: dict | None = None, **metadata) -> None:
        if content:
            raise ValueError("tool-gateway events carry no content")
        customer = metadata.pop("customer", None)
        self.emit_many([{"event": event, "metadata": metadata, "customer": customer}])


@dataclass
class RemoteAuditSink:
    """orchestrator and outbox-worker: post events to tool-gateway `POST /internal/v1/audit`. Content is encrypted
    here, before it leaves the producer, with the audit transport public key from App Configuration."""
    cfg: StackConfig
    client: HttpClient
    tokens: TokenProvider
    source: str
    transport_key: "callable | None" = None  # () -> RSA public JWK of `audit-transport`

    def emit(self, event: str, content: dict | None = None, on_behalf_of: str | None = None, **metadata) -> None:
        import json as _json
        from .envelope import seal_envelope
        ctx = request_context()
        e = {"event": event, "metadata": metadata, "customer": metadata.pop("customer", None) or ctx.data.get("customer_id"), "lineage": _lineage_now()}
        if content:
            if self.transport_key is None:
                raise ValueError("this producer may not send content")
            e["content_envelope"] = seal_envelope(self.transport_key(), _json.dumps(content).encode())
        headers = {"authorization": "Bearer " + self.tokens.app_token(scope("tool-gateway"))}
        if ctx.data.get("rm_token"):
            headers["x-rm-authorization"] = "Bearer " + ctx.data["rm_token"]
        try:
            r = self.client.request("POST", f"{self.cfg.url('tool-gateway')}/internal/v1/audit", json_body={"events": [e], "on_behalf_of": on_behalf_of},
                                    headers=headers, timeout=2.0, peer="tool-gateway")
        except TransportError:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "the audit stream did not acknowledge; nothing was released", details={"reason": "AUDIT"}) from None
        if r.status != 202:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "the audit stream rejected the event", details={"reason": "AUDIT"})
