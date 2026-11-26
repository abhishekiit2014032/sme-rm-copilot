"""Azure Cosmos DB for NoSQL mock (REST data plane): sessions, drafts, jti replay store, approvals, outbox.

Imitates:
  POST   /dbs/{db}/colls/{coll}/docs           create (409 Conflict if the id exists: the atomic jti check);
                                               upsert with `x-ms-documentdb-is-upsert: true`;
                                               query with `x-ms-documentdb-isquery: true` and
                                               `{"query": "SELECT * FROM c WHERE c.a = @a AND c.b = @b", "parameters": [...]}`
  GET    /dbs/{db}/colls/{coll}/docs           change feed with `A-IM: Incremental feed`; continuation in
                                               `If-None-Match` / response `etag`; 304 when nothing new
  GET|PUT|DELETE /dbs/{db}/colls/{coll}/docs/{id}   read / replace (optional `If-Match` etag, 412) / delete
Per-container default TTL and per-item `ttl` (seconds); expired items are invisible, as in Cosmos DB.

Differences: in memory, single partition (partition key header accepted, ignored), SQL limited to equality
filters joined by AND, change feed starts from the beginning when no continuation is given (real default: now),
bearer (Entra) auth only, server-side RBAC per container instead of Cosmos data-plane role definitions.
"""
from __future__ import annotations

import re
import secrets
import threading
import time

from services.common.config import AUD, StackConfig
from services.common.httpkit import HttpError, Request, Response

from ._base import Mock

DAY = 86400
CONTAINERS = {  # container -> default TTL (plan §7)
    "sessions": DAY, "drafts": 30 * DAY, "jti": DAY, "approvals": 30 * DAY, "outbox": 30 * DAY, "leases": None,
}
RBAC = {
    "web-bff": {"sessions", "drafts"},
    "tool-gateway": {"jti", "approvals", "outbox"},
    "outbox-worker": {"outbox", "approvals", "leases"},
}
_COND = re.compile(r"c\.(\w+)\s*=\s*(@\w+|'[^']*'|-?\d+|true|false)", re.I)


class CosmosMock(Mock):
    name = "cosmos"

    def __init__(self, cfg: StackConfig, database: str = "rmcopilot"):
        super().__init__(cfg)
        self.database = database
        self._colls: dict[str, dict[str, dict]] = {c: {} for c in CONTAINERS}
        self._lsn = 0
        self._lock = threading.Lock()
        base = "/dbs/{db}/colls/{coll}/docs"
        self.app.route("POST", base)(self._post)
        self.app.route("GET", base)(self._feed)
        self.app.route("GET", base + "/{id}")(self._read)
        self.app.route("PUT", base + "/{id}")(self._replace)
        self.app.route("DELETE", base + "/{id}")(self._delete)

    # ------------------------------------------------------------------ helpers
    def _container(self, r: Request) -> dict[str, dict]:
        self.simulate("cosmos")
        claims = self.caller(r, AUD["cosmos"])
        coll = r.params["coll"]
        if r.params["db"] != self.database or coll not in self._colls:
            raise HttpError(404, "NotFound", "Resource Not Found")
        if coll not in RBAC.get(claims.get("azp", ""), set()):
            raise HttpError(403, "Forbidden", "Request blocked by Auth: principal has no data-plane role on this container",
                            details={"innererror": {"code": "ForbiddenByRbac"}})
        r.state["coll"] = coll
        return self._colls[coll]

    def _alive(self, coll: str, doc: dict) -> bool:
        ttl = doc.get("ttl", CONTAINERS[coll])
        return ttl in (None, -1) or doc["_ts"] + int(ttl) > time.time()

    def _stamp(self, doc: dict) -> dict:
        self._lsn += 1
        return {**doc, "_ts": int(time.time()), "_etag": f'"{secrets.token_hex(8)}"', "_lsn": self._lsn, "_rid": secrets.token_urlsafe(6)}

    @staticmethod
    def _public(doc: dict) -> dict:
        return {k: v for k, v in doc.items() if k != "_lsn"}

    # ------------------------------------------------------------------ routes
    def _post(self, r: Request) -> Response:
        coll = self._container(r)
        name = r.state["coll"]
        body = r.json()
        if (r.header("x-documentdb-isquery") or r.header("x-ms-documentdb-isquery") or "").lower() == "true":
            return Response(200, self._query(name, coll, body))
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not body["id"]:
            raise HttpError(400, "BadRequest", "document must have a string id")
        upsert = (r.header("x-ms-documentdb-is-upsert") or "").lower() == "true"
        with self._lock:
            existing = coll.get(body["id"])
            if existing and self._alive(name, existing) and not upsert:
                raise HttpError(409, "Conflict", "Entity with the specified id already exists in the system.")
            doc = coll[body["id"]] = self._stamp(body)
        return Response(200 if (existing and upsert) else 201, self._public(doc), headers={"etag": doc["_etag"]})

    def _query(self, name: str, coll: dict[str, dict], body: dict) -> dict:
        q = str(body.get("query", ""))
        params = {p["name"]: p["value"] for p in body.get("parameters", [])}
        if not re.match(r"^\s*SELECT \* FROM c(\s+WHERE\s+.+)?$", q, re.I | re.S):
            raise HttpError(400, "BadRequest", "mock supports SELECT * FROM c WHERE c.x = @p [AND ...]")
        conds = []
        for field, raw in _COND.findall(q):
            if raw.startswith("@"):
                value = params.get(raw)
            elif raw.startswith("'"):
                value = raw[1:-1]
            elif raw.lower() in ("true", "false"):
                value = raw.lower() == "true"
            else:
                value = int(raw)
            conds.append((field, value))
        with self._lock:
            docs = [self._public(d) for d in coll.values() if self._alive(name, d) and all(d.get(f) == v for f, v in conds)]
        return {"Documents": docs, "_count": len(docs)}

    def _feed(self, r: Request) -> Response:
        coll = self._container(r)
        name = r.state["coll"]
        if (r.header("a-im") or "").lower() != "incremental feed":
            with self._lock:
                docs = [self._public(d) for d in coll.values() if self._alive(name, d)]
            return Response(200, {"Documents": docs, "_count": len(docs)})
        since = int((r.header("if-none-match") or "0").strip('"') or 0)
        limit = int(r.header("x-ms-max-item-count") or 100)
        with self._lock:
            changed = sorted((d for d in coll.values() if d["_lsn"] > since), key=lambda d: d["_lsn"])[:limit]
            latest = changed[-1]["_lsn"] if changed else since
        if not changed:
            return Response(304, None, headers={"etag": f'"{since}"'})
        return Response(200, {"Documents": [self._public(d) for d in changed], "_count": len(changed)}, headers={"etag": f'"{latest}"'})

    def _read(self, r: Request) -> dict:
        coll = self._container(r)
        with self._lock:
            doc = coll.get(r.params["id"])
            if not doc or not self._alive(r.state["coll"], doc):
                raise HttpError(404, "NotFound", "Entity with the specified id does not exist in the system.")
            return self._public(doc)

    def _replace(self, r: Request) -> Response:
        coll = self._container(r)
        body = r.json()
        if body.get("id") != r.params["id"]:
            raise HttpError(400, "BadRequest", "id in body must match the URL")
        with self._lock:
            doc = coll.get(r.params["id"])
            if not doc or not self._alive(r.state["coll"], doc):
                raise HttpError(404, "NotFound", "Entity with the specified id does not exist in the system.")
            if r.header("if-match") and r.header("if-match") != doc["_etag"]:
                raise HttpError(412, "PreconditionFailed", "Operation cannot be performed because one of the specified precondition is not met.")
            new = coll[r.params["id"]] = self._stamp(body)
        return Response(200, self._public(new), headers={"etag": new["_etag"]})

    def _delete(self, r: Request) -> Response:
        coll = self._container(r)
        with self._lock:
            if coll.pop(r.params["id"], None) is None:
                raise HttpError(404, "NotFound", "Entity with the specified id does not exist in the system.")
        return Response(204, None)
