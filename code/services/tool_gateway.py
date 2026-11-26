"""tool-gateway: the tool layer and security boundary (plan §3.2, §3.4, §4.1 steps 4-6, §4.2 steps 8-10).

Every call carries two tokens: the orchestrator's workload token (`Authorization`, role `Tools.Invoke`) and the
RM's OBO token for this audience (`X-RM-Authorization`, scope `Tools.Read`, `Tools.Write` for commits). The RM is
the `oid` of that user token; an RM id in a body or header is never trusted. Then, in this order:

1. entitlement check FIRST (CRM entitlements incl. delegations, in-process cache <= 60 s). Denied -> 403
   `NOT_ENTITLED`, and no customer data is requested from any system of record;
2. reads fan out in parallel through the APIM system API facade, each with a deadline-derived timeout and one
   retry for idempotent reads; only the CRM profile is critical, LOS, ODS, KYC and news degrade to flagged sections;
3. field minimisation (`rm_copilot.tools.minimise_profile`: no national ID);
4. one audit event per system call to Event Hubs, acknowledged before the response (fail closed);
5. writes: approval token verification (public key only), atomic `jti` create in Cosmos DB (409 = replay),
   verdict + draft persisted, saga per item with external reference `jti:item`, Cosmos outbox on timeout/5xx.

Production equivalent: an AKS deployment with workload identity; Redis is not used in Phase 1 (plan §2).
"""
from __future__ import annotations

import threading
import time

from rm_copilot.retrieval import CLEARANCE_RANK
from rm_copilot.tools import minimise_profile
from services.approval_verifier import ApprovalRejected, ApprovalVerifier
from services.common import crm_writes
from services.common.config import StackConfig
from services.common.contracts import AUDIT_EVENTS, APPROVAL_OP, TOOL_CONTRACT_VERSION, EventHubAuditSink, followup_payload
from services.common.cosmos_client import CosmosClient
from services.common.httpkit import HttpError, Request, Response, TransportError, request_context, run_parallel, time_left
from services.common.service import Service
from services.common.telemetry import current_span

SEARCH_API_VERSION = "2024-07-01"
CRITICAL_SOURCES = ("crm",)  # LLD §2.3: only the CRM profile is critical; other sections degrade
SECTION = {"los": "facilities", "ods": "signals", "kyc": "kyc", "news": "news"}


class SourceError(Exception):
    def __init__(self, system: str, status: int | str):
        super().__init__(f"{system}:{status}")
        self.system, self.status = system, status


class ToolGateway(Service):
    name = "tool-gateway"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg, response_headers={"x-tool-contract": TOOL_CONTRACT_VERSION})
        self.cosmos = CosmosClient(self)
        self.audit = EventHubAuditSink(cfg, self.client, self.tokens, self.name)  # the only Event Hubs producer
        self.verifier = ApprovalVerifier(cfg, self.client, lambda: self.auth_headers("vault"))
        self._entitlements: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()
        m = self.metrics
        self.m_denied = m.counter("entitlement_denials_total", "Requests denied by the entitlement check", ())
        self.m_ent_cache = m.counter("entitlement_cache_total", "Entitlement cache lookups", ("result",))
        self.m_verified = m.counter("approval_tokens_verified_total", "Approval tokens that passed verification", ())
        self.m_rejected = m.counter("approval_tokens_rejected_total", "Approval tokens rejected", ("reason",))
        self.m_writes = m.counter("crm_write_items_total", "CRM write items by first-attempt outcome", ("outcome",))
        self.m_sources = m.counter("source_calls_total", "System-of-record calls via APIM", ("system", "outcome"))
        a = self.app.route
        a("GET", "/tools/customers/{cid}/brief-inputs")(self._brief_inputs)
        a("GET", "/tools/customers/{cid}/profile")(self._profile)
        a("POST", "/tools/policies/search")(self._search)
        a("GET", "/tools/meetings")(self._meetings)
        a("POST", "/tools/customers/{cid}/call-reports")(self._commit)
        a("GET", "/tools/writes/{jti}")(self._write_status)
        a("POST", "/internal/v1/audit")(self._append_audit)

    # ------------------------------------------------------------------ identity and entitlements
    def _auth(self, r: Request, write: bool = False) -> dict:
        self.validator("tool-gateway").validate(r.bearer(), scopes=("Tools.Invoke",), azp=("orchestrator",), app_only=True)
        rm = self.validator("tool-gateway").validate(r.bearer("x-rm-authorization"), scopes=("Tools.Write",) if write else ("Tools.Read",), app_only=False)
        ctx = request_context()
        ctx.data.update(rm_oid=rm["oid"], rm_token=r.bearer("x-rm-authorization"))
        return rm

    def entitlements(self, oid: str) -> dict:
        now = time.monotonic()
        with self._lock:
            hit = self._entitlements.get(oid)
        if hit and hit[0] > now:
            self.m_ent_cache.inc(result="hit")
            return hit[1]
        self.m_ent_cache.inc(result="miss")
        try:
            rec = self._sor_json("crm", f"/crm/api/v1/rms/{oid}/entitlements", oid, retry=True)
        except SourceError:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "cannot confirm entitlements right now", details={"reason": "ENTITLEMENT_SOURCE"}) from None
        rec["allowed"] = sorted(set(rec.get("customers", [])) | {d["customer"] for d in rec.get("delegations", [])})
        with self._lock:
            self._entitlements[oid] = (now + self.cfg.settings.entitlement_ttl_s, rec)
        return rec

    def require_entitled(self, oid: str, customer_id: str, tool: str) -> dict:
        ent = self.entitlements(oid)
        if customer_id not in ent["allowed"]:
            self.m_denied.inc()
            self.audit.emit("entitlement_denied", tool=tool, customer=customer_id)
            raise HttpError(403, "NOT_ENTITLED", "You are not entitled to this customer.")
        request_context().data["customer_id"] = customer_id
        return ent

    # ------------------------------------------------------------------ systems of record via APIM
    def _sor_json(self, system: str, path: str, oid: str, retry: bool = True) -> dict:
        attempts = 2 if retry else 1
        last: SourceError | None = None
        for _ in range(attempts):
            timeout = min(self.cfg.settings.tool_timeout_s, time_left())
            if timeout <= 0.05:
                break
            try:
                r = self.client.request("GET", f"{self.cfg.url('apim')}{path}", headers={**self.auth_headers("apim"), "x-rm-oid": oid},
                                        timeout=timeout, peer="apim")
            except TransportError as e:
                last = SourceError(system, e.kind)
                continue
            if r.ok:
                self.m_sources.inc(system=system, outcome="ok")
                return r.json()
            last = SourceError(system, r.status)
            if r.status < 500:
                break
        self.m_sources.inc(system=system, outcome="error")
        raise last or SourceError(system, "deadline")

    # ------------------------------------------------------------------ read tools
    def _brief_inputs(self, r: Request) -> dict:
        rm, cid = self._auth(r), r.params["cid"]
        oid = rm["oid"]
        self.require_entitled(oid, cid, "brief_inputs")
        calls = {
            "crm": lambda: self._sor_json("crm", f"/crm/api/v1/customers/{cid}", oid),
            "los": lambda: self._sor_json("los", f"/los/api/v1/customers/{cid}/facilities", oid),
            "kyc": lambda: self._sor_json("kyc", f"/kyc/api/v1/customers/{cid}", oid),
            "ods": lambda: self._sor_json("ods", f"/ods/api/v1/customers/{cid}/transaction-signals", oid),
            "news": lambda: self._sor_json("news", f"/news/api/v1/companies/{cid}/headlines", oid),
        }
        results = run_parallel(calls)
        self.audit.emit_many([{"event": "tool_call", "customer": cid, "metadata": {"tool": "brief_inputs", "system": k, "outcome": "ok" if err is None else "error"}}
                              for k, (_, err) in results.items()])
        failed = {k for k, (_, err) in results.items() if err is not None}
        critical = sorted(failed & set(CRITICAL_SOURCES))
        if critical:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "A required system is unavailable; the brief cannot be built.", details={"reason": "SOURCE", "source": critical[0]})
        get = lambda k: None if k in failed else results[k][0]  # noqa: E731
        partial = sorted(SECTION[k] for k in failed)
        if (sp := current_span()) is not None:
            sp.set(**{"rmc.partial": ",".join(partial)})
        return {"profile": minimise_profile(get("crm")), "facilities": (get("los") or {}).get("value"), "kyc": get("kyc"),
                "signals": (get("ods") or {}).get("value"), "news": (get("news") or {}).get("value") or [], "partial": partial}

    def _profile(self, r: Request) -> dict:
        rm, cid = self._auth(r), r.params["cid"]
        self.require_entitled(rm["oid"], cid, "get_customer_profile")
        try:
            rec = self._sor_json("crm", f"/crm/api/v1/customers/{cid}", rm["oid"])
        except SourceError:
            self.audit.emit("tool_call", tool="get_customer_profile", system="crm", customer=cid, outcome="error")
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "The CRM is unavailable.", details={"reason": "SOURCE", "source": "crm"}) from None
        self.audit.emit("tool_call", tool="get_customer_profile", system="crm", customer=cid, outcome="ok")
        return minimise_profile(rec)

    def _embed(self, text: str) -> list[float] | None:
        try:
            r = self.client.request("POST", f"{self.cfg.url('apim')}/openai/deployments/embed/embeddings?api-version=2024-10-21",
                                    json_body={"input": text, "dimensions": 256},
                                    headers={**self.auth_headers("apim"), "x-rm-oid": request_context().data.get("rm_oid", ""), "x-use-case": "uc2"},
                                    timeout=min(1.5, time_left()), peer="apim")
        except TransportError:
            return None
        return r.json()["data"][0]["embedding"] if r.ok else None  # keyword + semantic only if embeddings are unavailable

    def _search(self, r: Request) -> dict:
        rm = self._auth(r)
        body = r.json()
        query, k = str(body.get("query", ""))[:1000], max(1, min(int(body.get("k", 3)), 10))
        ent = self.entitlements(rm["oid"])
        rank = CLEARANCE_RANK.get(ent.get("clearance", "public"), 0)
        vector = self._embed(query)
        req = {"search": query, "top": k, "queryType": "semantic", "semanticConfiguration": "policy-semantic",
               "filter": f"status eq 'current' and clearance_rank le {rank} and effective_from le {self.cfg.settings.as_of}T00:00:00Z",
               "select": "id,doc,ref,text,status,classification,effective_from"}
        if vector:
            req["vectorQueries"] = [{"kind": "vector", "vector": vector, "fields": "contentVector", "k": 20}]
        index = self.cfg.settings.index_name
        try:
            resp = self.client.request("POST", f"{self.cfg.url('search')}/indexes/{index}/docs/search?api-version={SEARCH_API_VERSION}",
                                       json_body=req, headers=self.auth_headers("search"), timeout=min(2.0, time_left()), peer="search")
        except TransportError:
            resp = None
        if resp is None or not resp.ok:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "policy search is unavailable", details={"reason": "SEARCH"})
        hits = [{"id": x["id"], "doc": x["doc"], "ref": x["ref"], "text": x["text"], "status": x["status"], "classification": x["classification"],
                 "effective_from": str(x.get("effective_from", ""))[:10], "score": x.get("@search.rerankerScore", 0.0)} for x in resp.json()["value"]]
        self.audit.emit("tool_call", tool="search_policies", system="search", hits=len(hits), hybrid=bool(vector), index=index)
        return {"value": hits, "index_version": index}

    def _meetings(self, r: Request) -> dict:
        rm = self._auth(r)
        allowed = set(self.entitlements(rm["oid"])["allowed"])
        graph_token = self.tokens.obo_token(request_context().data["rm_token"], "https://graph.microsoft.com/Calendars.ReadBasic")
        try:
            g = self.client.request("GET", f"{self.cfg.url('graph')}/v1.0/me/calendarView?startDateTime={self.cfg.settings.as_of}T00:00:00&endDateTime=2026-12-31T00:00:00",
                                    headers={"authorization": "Bearer " + graph_token}, timeout=min(2.0, time_left()), peer="graph")
        except TransportError:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "calendar unavailable", details={"reason": "GRAPH"}) from None
        if not g.ok:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "calendar unavailable", details={"reason": "GRAPH"})
        out = []
        for e in g.json().get("value", []):
            cid = next((c[4:] for c in e.get("categories", []) if c.startswith("crm:")), None)
            out.append({"subject": e["subject"], "start": e["start"]["dateTime"], "customer_id": cid if cid in allowed else None})
        self.audit.emit("tool_call", tool="list_meetings", system="graph", count=len(out))
        return {"value": out}

    # ------------------------------------------------------------------ write API
    def _reject(self, reason: str) -> HttpError:
        self.m_rejected.inc(reason=reason)
        self.audit.emit("write_blocked", op=APPROVAL_OP, reason=reason)
        if reason == "missing":
            return HttpError(401, "APPROVAL_REQUIRED", "This write needs the RM's approval.")
        return HttpError(403, "APPROVAL_INVALID", "The approval is not valid for this write.", details={"reason": reason})

    def _commit(self, r: Request) -> Response:
        rm, cid = self._auth(r, write=True), r.params["cid"]
        oid = rm["oid"]
        self.require_entitled(oid, cid, "create_call_report")
        body = r.json()
        draft = followup_payload(body.get("draft") or {})
        try:
            claims = self.verifier.verify(body.get("approval_token"), oid=oid, customer_id=cid, draft=draft)
        except ApprovalRejected as e:
            raise self._reject(e.reason) from None
        jti = claims["jti"]
        created = self.cosmos.create("jti", {"id": jti, "oid": oid, "exp": claims["exp"]})
        if created.status == 409:
            self.m_rejected.inc(reason="replayed")
            self.audit.emit("write_blocked", op=APPROVAL_OP, reason="replayed")
            raise HttpError(409, "REPLAYED", "This approval was already used.", details={"jti": jti, "items": self._items(jti, oid)})
        if not created.ok:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "cannot record the approval", details={"reason": "STATE_STORE"})
        self.m_verified.inc()
        approval = {"op": claims["op"], "jti": jti, "kid": claims["kid"].rsplit("/", 1)[-1], "draft_sha256": claims["draft_sha256"][:16], "iat": claims["iat"]}
        self.audit.emit_many([{"event": "approval_issued", "customer": cid, "metadata": {**approval, "issuer": claims["iss"]}},
                              {"event": "approval_verified", "customer": cid, "metadata": {**approval, "verdict": "verified"}}])
        # Persist the verdict with the draft: outbox retries after `exp` rely on it, not on the token.
        self.cosmos.upsert("approvals", {"id": jti, "oid": oid, "customer_id": cid, "draft": draft, "draft_sha256": claims["draft_sha256"],
                                         "verdict": "verified", "verified_at": int(time.time()), "kid": claims["kid"], "op": claims["op"]})
        items = crm_writes.saga_items(jti, cid, oid, draft)
        sp = current_span()
        for it in items:
            it["traceparent"] = sp.context.traceparent() if sp else None
        # Keep ~0.6 s of the deadline to persist outbox items if the CRM does not answer in time.
        attempt_s = max(0.05, min(self.cfg.settings.tool_timeout_s, time_left() - 0.6))
        outcomes = run_parallel({it["id"]: (lambda it=it: crm_writes.post(self, it, attempt_s))
                                 for it in items})
        events = []
        for it in items:
            (status, crm_id), err = outcomes[it["id"]]
            if err is not None:
                status, crm_id = crm_writes.QUEUED, None
            it.update(status=status, crm_id=crm_id, attempts=1, next_attempt_at=time.time() + self.cfg.settings.outbox_backoff_s)
            self.cosmos.upsert("outbox", it)  # every item is tracked; the outbox-worker only acts on `queued`
            self.m_writes.inc(outcome=status)
            events.append({"committed": "write_committed", "queued": "write_queued", "failed": "write_failed"}[status])
        self.audit.emit_many([{"event": ev, "customer": cid, "metadata": {"op": APPROVAL_OP, "item": it["item"], "kind": it["kind"], "jti": jti}}
                              for ev, it in zip(events, items)])
        statuses = {it["status"] for it in items}
        overall = "committed" if statuses == {"committed"} else ("failed" if "failed" in statuses else "queued")
        http = {"committed": 201, "queued": 202, "failed": 422}[overall]
        if sp:
            sp.set(**{"rmc.outcome": overall, "rmc.items": len(items)})
        return Response(http, {"status": overall, "jti": jti, "items": self._public_items(items)})

    @staticmethod
    def _public_items(items: list[dict]) -> list[dict]:
        return [{"item": i["item"], "kind": i["kind"], "status": i["status"], "crm_id": i.get("crm_id"), "attempts": i.get("attempts", 0)}
                for i in sorted(items, key=lambda i: (i["item"] != "report", i["item"]))]

    def _items(self, jti: str, oid: str) -> list[dict]:
        return self._public_items([d for d in self.cosmos.query("outbox", "SELECT * FROM c WHERE c.jti = @jti", jti=jti) if d["oid"] == oid])

    def _write_status(self, r: Request) -> dict:
        rm = self._auth(r)
        items = self._items(r.params["jti"], rm["oid"])
        if not items:
            raise HttpError(404, "NOT_FOUND", "no write with this id for you")
        statuses = {i["status"] for i in items}
        overall = "committed" if statuses == {"committed"} else ("failed" if "failed" in statuses else "queued")
        return {"jti": r.params["jti"], "status": overall, "items": items}

    # ------------------------------------------------------------------ audit relay (LLD §2.3, §6.4)
    def _append_audit(self, r: Request) -> Response:
        """Events from orchestrator (actor = the RM token on the call) and outbox-worker (system on behalf of the RM).
        Content arrives already envelope-encrypted; the gateway cannot read it."""
        caller = self.validator("tool-gateway").validate(r.bearer(), scopes=("Audit.Append",), azp=("orchestrator", "outbox-worker"), app_only=True)
        body = r.json()
        if caller["azp"] == "orchestrator":
            rm = self.validator("tool-gateway").validate(r.bearer("x-rm-authorization"), scopes=("Tools.Read",), app_only=False)
            actor = {"type": "rm", "oid": rm["oid"]}
        else:
            actor = {"type": "system", "oid": caller["oid"], "on_behalf_of": str(body.get("on_behalf_of") or "")[:64] or None}
        events = body.get("events") or []
        if not isinstance(events, list) or not 1 <= len(events) <= 20:
            raise HttpError(400, "VALIDATION_FAILED", "1-20 events", details={"reason": "SCHEMA"})
        clean = []
        for e in events:
            meta = e.get("metadata") or {}
            env = e.get("content_envelope")
            ok = (e.get("event") in AUDIT_EVENTS and isinstance(meta, dict) and len(meta) <= 40
                  and all(isinstance(v, (str, int, float, bool, type(None))) or (isinstance(v, list) and all(isinstance(x, (str, int)) for x in v))
                          for v in meta.values())
                  and all(len(str(v)) <= 300 for v in meta.values())
                  and (env is None or (isinstance(env, dict) and {"alg", "kid", "wrapped_dek", "ciphertext"} <= set(env) and len(str(env)) <= 200_000)))
            if not ok:
                raise HttpError(400, "VALIDATION_FAILED", "audit event does not match schema audit/1", details={"reason": "SCHEMA"})
            clean.append({"event": e["event"], "metadata": meta, "customer": e.get("customer"), "lineage": e.get("lineage") or {}, "content_envelope": env})
        self.audit.emit_many(clean, posted_by=caller["azp"], actor=actor)
        return Response(202, {"accepted": len(clean)})
