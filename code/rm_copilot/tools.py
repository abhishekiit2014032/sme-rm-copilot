"""Tool layer: the security boundary between the model and bank systems.

Design rules shown here:
  * Entitlements are enforced in code, per call, using the RM's identity. Never in a prompt.
  * Read tools return only the fields the task needs; sensitive identifiers are masked.
  * Write tools refuse to commit without a signed approval token minted by the UI backend
    after the RM clicks "Approve". The orchestrator and the model never hold the signing key.
  * Every call is written to an audit log.
"""
from __future__ import annotations

import copy
import functools
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Callable

from . import data
from .retrieval import search


class NotEntitled(PermissionError):
    """The user may not access this customer."""


class ApprovalRequired(PermissionError):
    """A write was attempted without a valid RM approval."""


@dataclass(frozen=True)
class User:
    id: str
    name: str
    portfolio: frozenset
    clearance: str = "internal"

    @classmethod
    def load(cls, user_id: str) -> "User":
        rm = data.RMS[user_id]
        return cls(user_id, rm["name"], frozenset(rm["portfolio"]), rm["clearance"])


# --------------------------------------------------------------------------- audit
AUDIT_LOG: list[dict] = []


def audit(event: str, **fields) -> None:
    # Production: append-only store (e.g. WORM bucket or ledger table), linked to the request trace id.
    AUDIT_LOG.append({"ts": round(time.time(), 3), "event": event, **fields})


# --------------------------------------------------------------------------- registry
TOOLS: dict[str, Callable] = {}


def tool(kind: str):
    """Register a tool. kind is 'read', 'draft' or 'write'."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(user: User, *args, **kwargs):
            audit("tool_call", tool=fn.__name__, kind=kind, user=user.id, args=[a for a in args if isinstance(a, str)])
            return fn(user, *args, **kwargs)
        wrapper.kind = kind
        TOOLS[fn.__name__] = wrapper
        return wrapper
    return deco


def require_entitlement(user: User, customer_id: str) -> None:
    if customer_id not in user.portfolio:
        audit("entitlement_denied", user=user.id, customer=customer_id)
        raise NotEntitled(f"{user.name} is not entitled to customer {customer_id}")


# --------------------------------------------------------------------------- read tools
@tool("read")
def get_customer_profile(user: User, customer_id: str) -> dict:
    require_entitlement(user, customer_id)
    c = data.CUSTOMERS[customer_id]
    # Field-level minimisation: the national ID is never returned to the orchestrator.
    return {k: c[k] for k in ("name", "crm_id", "sector", "since", "turnover_aed", "revenue_yoy_pct", "contact")}


@tool("read")
def get_facilities(user: User, customer_id: str) -> list[dict]:
    require_entitlement(user, customer_id)
    return copy.deepcopy(data.CUSTOMERS[customer_id]["facilities"])


@tool("read")
def get_kyc_status(user: User, customer_id: str) -> dict:
    require_entitlement(user, customer_id)
    return copy.deepcopy(data.CUSTOMERS[customer_id]["kyc"])


@tool("read")
def get_transaction_signals(user: User, customer_id: str) -> list[dict]:
    require_entitlement(user, customer_id)
    return copy.deepcopy(data.CUSTOMERS[customer_id]["signals"])


@tool("read")
def search_policies(user: User, query: str, k: int = 3) -> list[dict]:
    # Version and classification filters are applied inside retrieval, before ranking.
    return search(query, data.POLICY_PASSAGES, clearance=user.clearance, k=k)


# --------------------------------------------------------------------------- approvals
class ApprovalService:
    """Lives in the UI backend. Mints a short-lived token bound to one user and one exact draft."""

    def __init__(self, key: bytes | None = None, ttl_s: int = 300):
        self._key = key or secrets.token_bytes(32)  # production: KMS/HSM-held key
        self.ttl_s = ttl_s

    @staticmethod
    def _digest(draft: dict) -> str:
        return hashlib.sha256(json.dumps(draft, sort_keys=True).encode()).hexdigest()

    def issue(self, user: User, draft: dict) -> str:
        body = json.dumps({"u": user.id, "d": self._digest(draft), "exp": int(time.time()) + self.ttl_s})
        sig = hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()
        audit("approval_issued", user=user.id, draft=self._digest(draft)[:12])
        return body + "." + sig

    def verify(self, token: str | None, user: User, draft: dict) -> bool:
        if not token or "." not in token:
            return False
        body, sig = token.rsplit(".", 1)
        if not hmac.compare_digest(sig, hmac.new(self._key, body.encode(), hashlib.sha256).hexdigest()):
            return False
        claims = json.loads(body)
        return claims["u"] == user.id and claims["d"] == self._digest(draft) and claims["exp"] >= time.time()


APPROVALS = ApprovalService()
CRM_WRITES: list[dict] = []  # stands in for the CRM API


# --------------------------------------------------------------------------- write tools
@tool("draft")
def draft_email(user: User, customer_id: str, body: str) -> dict:
    """Returns a draft only. There is deliberately no send_email tool."""
    require_entitlement(user, customer_id)
    return {"to_customer": customer_id, "body": body, "status": "draft"}


@tool("write")
def create_call_report(user: User, customer_id: str, draft: dict, approval_token: str | None = None) -> dict:
    require_entitlement(user, customer_id)
    if not APPROVALS.verify(approval_token, user, draft):
        audit("write_blocked", user=user.id, tool="create_call_report", reason="missing or invalid approval")
        raise ApprovalRequired("create_call_report needs a valid RM approval token for this exact draft")
    record = {"customer": customer_id, "report": draft["call_report"], "tasks": draft["tasks"], "approved_by": user.id}
    CRM_WRITES.append(record)
    audit("write_committed", user=user.id, tool="create_call_report", customer=customer_id)
    return {"status": 201, "record": record}
