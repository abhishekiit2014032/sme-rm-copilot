"""outbox-worker: delivers queued CRM writes from the Cosmos DB outbox (plan §3.2, §4.2 step 9, C7).

Reads the outbox container's change feed (continuation kept in the `leases` container, like the change feed
processor), keeps the queued items in memory, and for each due item:
  1. reads the CRM by external reference (`jti:item`); if the record exists, marks it committed (no second post);
  2. otherwise posts it; success -> committed; timeout/5xx -> attempts + 1 with exponential backoff;
     after `outbox_max_attempts` -> failed (visible to the RM, alert in production).
The approval verdict was persisted by tool-gateway, so delivery after the token's `exp` is still valid.
Each delivery continues the trace of the original commit (traceparent stored with the item).
"""
from __future__ import annotations

import time

from services.common import crm_writes
from services.common.config import StackConfig
from services.common.contracts import RemoteAuditSink
from services.common.cosmos_client import CosmosClient
from services.common.httpkit import HttpError, RequestContext, TransportError, set_request_context
from services.common.service import Service
from services.common.telemetry import parse_traceparent

LEASE_ID = "outbox-worker"


class OutboxWorker(Service):
    name = "outbox-worker"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self.cosmos = CosmosClient(self)
        self.audit = RemoteAuditSink(cfg, self.client, self.tokens, self.name)  # via tool-gateway, no content
        self.pending: dict[str, dict] = {}
        self._continuation: str | None = None
        m = self.metrics
        self.m_deliveries = m.counter("outbox_deliveries_total", "Outbox delivery attempts", ("outcome",))
        self.m_pending = m.gauge("outbox_pending_items", "Queued items waiting for delivery", ())
        self.app.route("GET", "/outbox/pending")(lambda r: {"value": [{"id": k, "attempts": v["attempts"]} for k, v in self.pending.items()]})

    def start(self) -> None:
        self.run_loop("outbox", self.cfg.settings.outbox_poll_s, self.tick)

    def tick(self) -> None:
        set_request_context(RequestContext("outbox-" + str(int(time.time() * 1000))))
        if self._continuation is None:
            lease = self.cosmos.read("leases", LEASE_ID)
            self._continuation = (lease or {}).get("continuation") or ""
        docs, cont = self.cosmos.change_feed("outbox", self._continuation or None)
        for d in docs:
            if d.get("status") == crm_writes.QUEUED:
                self.pending[d["id"]] = d
            else:
                self.pending.pop(d["id"], None)
        if cont and cont != self._continuation:
            self._continuation = cont
            self.cosmos.upsert("leases", {"id": LEASE_ID, "continuation": cont})
        now = time.time()
        for doc_id, doc in list(self.pending.items()):
            if doc.get("next_attempt_at", 0) <= now:
                self.deliver(doc)
        self.m_pending.set(len(self.pending))

    def deliver(self, doc: dict) -> None:
        with self.telemetry.span("outbox.deliver", kind="consumer", parent=parse_traceparent(doc.get("traceparent")), **{"rmc.attempt": doc["attempts"] + 1}) as sp:
            set_request_context(RequestContext("outbox-" + doc["jti"][:8], time.monotonic() + 5.0, {"customer_id": doc["customer_id"], "rm_oid": doc["oid"]}))
            try:
                existing = crm_writes.lookup(self, doc, timeout=2.0)
                status, crm_id = (crm_writes.COMMITTED, existing) if existing else crm_writes.post(self, doc, timeout=2.0)
                found = bool(existing)
            except (TransportError, HttpError):
                status, crm_id, found = crm_writes.QUEUED, None, False
            attempts = doc["attempts"] + 1
            if status == crm_writes.QUEUED and attempts >= self.cfg.settings.outbox_max_attempts:
                status = crm_writes.FAILED
            backoff = min(30.0, self.cfg.settings.outbox_backoff_s * (2 ** min(attempts, 6)))
            doc = {k: v for k, v in doc.items() if not k.startswith("_")}
            doc.update(status=status, crm_id=crm_id, attempts=attempts, next_attempt_at=time.time() + backoff)
            self.cosmos.upsert("outbox", doc)
            if status == crm_writes.QUEUED:
                self.pending[doc["id"]] = doc
            else:
                self.pending.pop(doc["id"], None)
            self.m_deliveries.inc(outcome=("found_existing" if found else status))
            sp.set(**{"rmc.outcome": status})
            if status != crm_writes.QUEUED:
                self.audit.emit("write_committed" if status == crm_writes.COMMITTED else "write_failed", op="crm.commit_followup",
                                item=doc["item"], kind=doc["kind"], jti=doc["jti"], delivered_by="outbox-worker", found_existing=found,
                                on_behalf_of=doc["oid"])
