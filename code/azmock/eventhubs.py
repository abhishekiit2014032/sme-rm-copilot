"""Azure Event Hubs mock: the audit event stream.

Imitates the REST send API:
  POST /{hub}/messages        one event (JSON body) or a batch (`application/vnd.microsoft.servicebus.json`,
                              `[{"Body": ..., "UserProperties": {...}}]`); 201 = durably acknowledged.
The W3C trace context travels as the `Diagnostic-Id`/`traceparent` application property, as the Azure SDKs do.

Mock-only read API, standing in for the AMQP consumer client (Event Hubs has no REST receive):
  GET /{hub}/consumergroups/{group}/partitions/0/events?fromSequenceNumber=N&maxCount=M

Differences: one partition, in memory (no Capture, no retention period, no zone redundancy), no checkpoint store
(the consumer keeps its own checkpoint). RBAC: tool-gateway = Data Sender (sole producer), audit-writer = Data Receiver.
"""
from __future__ import annotations

import json
import threading
import time

from services.common.config import AUD, StackConfig
from services.common.httpkit import HttpError, Request, Response

from ._base import Mock

RBAC = {"tool-gateway": {"send"}, "audit-writer": {"receive"}}  # tool-gateway is the only producer (LLD §6.4)


class EventHubsMock(Mock):
    name = "eventhubs"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self._hubs: dict[str, list[dict]] = {"audit": []}
        self._lock = threading.Lock()
        self.app.route("POST", "/{hub}/messages")(self._send)
        self.app.route("GET", "/{hub}/consumergroups/{group}/partitions/{partition}/events")(self._receive)

    def _send(self, r: Request) -> Response:
        self.simulate("eventhubs")
        self.rbac(self.caller(r, AUD["eventhubs"]), RBAC, "send")
        hub = self._hubs.get(r.params["hub"])
        if hub is None:
            raise HttpError(404, "EntityNotFound", "event hub not found")
        body = r.json()
        batch = body if "vnd.microsoft.servicebus.json" in (r.header("content-type") or "") and isinstance(body, list) else \
            [{"Body": body, "UserProperties": {"Diagnostic-Id": r.header("traceparent")}}]
        with self._lock:
            for item in batch:
                b = item.get("Body")
                hub.append({"sequenceNumber": len(hub), "enqueuedTimeUtc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                            "body": b if not isinstance(b, str) else json.loads(b), "properties": item.get("UserProperties") or {}})
        return Response(201, None)

    def _receive(self, r: Request) -> dict:
        self.rbac(self.caller(r, AUD["eventhubs"]), RBAC, "receive")
        hub = self._hubs.get(r.params["hub"])
        if hub is None or r.params["partition"] != "0":
            raise HttpError(404, "EntityNotFound", "event hub or partition not found")
        start, count = int(r.query.get("fromSequenceNumber", 0)), min(int(r.query.get("maxCount", 100)), 500)
        with self._lock:
            return {"value": hub[start:start + count]}
