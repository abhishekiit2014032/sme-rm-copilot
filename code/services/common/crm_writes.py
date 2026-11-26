"""CRM write adapter shared by tool-gateway (first attempt) and outbox-worker (retries). Plan §4.2 step 9, C7.

Each approved draft becomes a saga of items: the call report (`{jti}:report`) and each task (`{jti}:task-N`),
written separately with that external reference. A retry first reads the CRM by external reference and only
posts if nothing is there, so a write whose response was lost is never posted twice.
"""
from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING

from .httpkit import HttpError, TransportError

if TYPE_CHECKING:
    from .service import Service

COMMITTED, QUEUED, FAILED = "committed", "queued", "failed"


def saga_items(jti: str, customer_id: str, oid: str, draft: dict) -> list[dict]:
    items = [{"item": "report", "kind": "call-report", "payload": {"text": draft["call_report"]}}]
    items += [{"item": f"task-{i}", "kind": "task", "payload": {"title": t["title"], "dueDate": t["due"]}} for i, t in enumerate(draft["tasks"])]
    return [{"id": f"{jti}:{x['item']}", "jti": jti, "customer_id": customer_id, "oid": oid, "status": QUEUED, "attempts": 0,
             "next_attempt_at": 0.0, "crm_id": None, **x} for x in items]


def _sor(svc: "Service", method: str, path: str, oid: str, body: dict | None, timeout: float):
    headers = {**svc.auth_headers("apim"), "x-rm-oid": oid}
    return svc.client.request(method, f"{svc.cfg.url('apim')}{path}", json_body=body, headers=headers, timeout=timeout, peer="apim")


def lookup(svc: "Service", item: dict, timeout: float) -> str | None:
    """Read-before-retry: the CRM id of an existing record with this external reference, if any."""
    coll = "call-reports" if item["kind"] == "call-report" else "tasks"
    r = _sor(svc, "GET", f"/crm/api/v1/{coll}?externalReference={urllib.parse.quote(item['id'])}", item["oid"], None, timeout)
    if not r.ok:
        raise HttpError(r.status, "CRM_LOOKUP_FAILED", "lookup failed")
    found = r.json().get("value", [])
    return found[0]["id"] if found else None


def post(svc: "Service", item: dict, timeout: float) -> tuple[str, str | None]:
    """One write attempt. Returns (committed|queued|failed, crm id)."""
    coll = "call-reports" if item["kind"] == "call-report" else "tasks"
    body = {"externalReference": item["id"], "aiAssisted": True, **item["payload"]}
    try:
        r = _sor(svc, "POST", f"/crm/api/v1/customers/{item['customer_id']}/{coll}", item["oid"], body, timeout)
    except TransportError:
        return QUEUED, None
    if r.status in (200, 201):
        return COMMITTED, r.json().get("id")
    if r.status >= 500 or r.status == 429:
        return QUEUED, None
    return FAILED, None
