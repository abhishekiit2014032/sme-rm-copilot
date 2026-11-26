"""Thin Cosmos DB REST client used by web-bff, tool-gateway and outbox-worker.

Production equivalent: the `azure-cosmos` SDK with `DefaultAzureCredential` (Entra auth, `disableLocalAuth`).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .httpkit import ClientResponse, HttpError, TransportError

if TYPE_CHECKING:
    from .service import Service

DATABASE = "rmcopilot"


class CosmosClient:
    def __init__(self, svc: "Service"):
        self.svc = svc

    def _req(self, method: str, coll: str, doc_id: str | None = None, body: dict | None = None, headers: dict | None = None) -> ClientResponse:
        url = f"{self.svc.cfg.url('cosmos')}/dbs/{DATABASE}/colls/{coll}/docs" + (f"/{doc_id}" if doc_id else "")
        try:
            return self.svc.client.request(method, url, json_body=body, headers={**self.svc.auth_headers("cosmos"), **(headers or {})},
                                           timeout=2.0, peer="cosmos")
        except TransportError:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "state store unavailable", details={"reason": "STATE_STORE"}) from None

    def create(self, coll: str, doc: dict) -> ClientResponse:
        """Atomic create: 409 if the id already exists (used for the jti replay check)."""
        return self._req("POST", coll, body=doc)

    def upsert(self, coll: str, doc: dict) -> dict:
        r = self._req("POST", coll, body=doc, headers={"x-ms-documentdb-is-upsert": "true"})
        if not r.ok:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", f"upsert failed ({r.status})", details={"reason": "STATE_STORE"})
        return r.json()

    def read(self, coll: str, doc_id: str) -> dict | None:
        r = self._req("GET", coll, doc_id)
        if r.status == 404:
            return None
        if not r.ok:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", f"read failed ({r.status})", details={"reason": "STATE_STORE"})
        return r.json()

    def query(self, coll: str, sql: str, **params) -> list[dict]:
        r = self._req("POST", coll, body={"query": sql, "parameters": [{"name": "@" + k, "value": v} for k, v in params.items()]},
                      headers={"x-ms-documentdb-isquery": "true", "content-type": "application/query+json"})
        if not r.ok:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", f"query failed ({r.status})", details={"reason": "STATE_STORE"})
        return r.json().get("Documents", [])

    def change_feed(self, coll: str, continuation: str | None) -> tuple[list[dict], str | None]:
        r = self._req("GET", coll, headers={"a-im": "Incremental feed", **({"if-none-match": continuation} if continuation else {})})
        if r.status == 304:
            return [], r.headers.get("etag", continuation)
        if not r.ok:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", f"change feed failed ({r.status})", details={"reason": "STATE_STORE"})
        return r.json().get("Documents", []), r.headers.get("etag")
