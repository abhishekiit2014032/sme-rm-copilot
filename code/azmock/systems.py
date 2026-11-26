"""Systems of record mock: the Bank's CRM, core banking ODS, LOS, KYC, news feed, and Microsoft Graph calendar.

All are fictional REST APIs (the Bank's real contracts are a Phase 0 deliverable); one server hosts them under
per-system prefixes so each has its own base URL in the stack config (`crm`, `ods`, `los`, `kyc`, `news`, `graph`):

  CRM   GET  /crm/api/v1/rms/{oid}/entitlements          portfolio + active delegations + clearance (plan C22)
        GET  /crm/api/v1/customers/{id}                   full record (incl. national ID: the tool-gateway minimises)
        POST /crm/api/v1/customers/{id}/call-reports      {externalReference, text, aiAssisted, approvedBy}
        POST /crm/api/v1/customers/{id}/tasks             {externalReference, title, dueDate}
        GET  /crm/api/v1/call-reports?externalReference=  read-before-retry lookups (plan C7)
        GET  /crm/api/v1/tasks?externalReference=
  ODS   GET  /ods/api/v1/customers/{id}/transaction-signals   aggregated signals only
  LOS   GET  /los/api/v1/customers/{id}/facilities
  KYC   GET  /kyc/api/v1/customers/{id}
  News  GET  /news/api/v1/companies/{id}/headlines
  Graph GET  /graph/v1.0/me/calendarView                  delegated `Calendars.ReadBasic` (OBO token); there is no
                                                          mail API here: the copilot never writes to a mailbox (C20)

The CRM deliberately does NOT de-duplicate writes: duplicate protection is the caller's read-before-retry by
external reference. CRM writes must carry the RM `oid` in a header signed by APIM (plan §3.4 step 5).
Fault injection targets: crm, ods, los, kyc, news, graph. `calls` records (system, method, route, customer) so
tests can prove a denied request reached no system of record.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

from rm_copilot import data
from services.common.config import AUD, StackConfig, user_oid
from services.common.httpkit import HttpError, Request, Response

from ._base import Mock

MEETINGS = {  # fictional calendar: (rm, customer, start, subject)
    "layla": [("harbourline", "2026-11-27T10:00:00", "Annual review"), ("crescent", "2026-11-30T14:30:00", "Payroll proposal")],
    "omar": [("gulfstar", "2026-11-27T11:00:00", "Quarterly catch-up")],
    "sara": [("harbourline", "2026-12-02T09:00:00", "Cover visit")],
}


def sign_rm_header(secret: str, oid: str, method: str, path: str, ts: int) -> str:
    return hmac.new(secret.encode(), f"{oid}|{method}|{path}|{ts}".encode(), hashlib.sha256).hexdigest()


class SystemsOfRecordMock(Mock):
    name = "systems"
    targets = ("crm", "ods", "los", "kyc", "news", "graph")

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self.calls: list[dict] = []
        self.call_reports: list[dict] = []
        self.tasks: list[dict] = []
        self._lock = threading.Lock()
        self._by_oid = {user_oid(f"{rm}@bank.example"): rm for rm in data.RMS}
        a = self.app.route
        a("GET", "/crm/api/v1/rms/{oid}/entitlements")(self._entitlements)
        a("GET", "/crm/api/v1/customers/{cid}")(lambda r: self._read(r, "crm", self._customer))
        a("POST", "/crm/api/v1/customers/{cid}/call-reports")(lambda r: self._write(r, "call-report"))
        a("POST", "/crm/api/v1/customers/{cid}/tasks")(lambda r: self._write(r, "task"))
        a("GET", "/crm/api/v1/call-reports")(lambda r: self._lookup(r, self.call_reports))
        a("GET", "/crm/api/v1/tasks")(lambda r: self._lookup(r, self.tasks))
        a("GET", "/ods/api/v1/customers/{cid}/transaction-signals")(lambda r: self._read(r, "ods", lambda c: {"value": data.CUSTOMERS[c]["signals"], "aggregated": True}))
        a("GET", "/los/api/v1/customers/{cid}/facilities")(lambda r: self._read(r, "los", lambda c: {"value": data.CUSTOMERS[c]["facilities"]}))
        a("GET", "/kyc/api/v1/customers/{cid}")(lambda r: self._read(r, "kyc", lambda c: data.CUSTOMERS[c]["kyc"]))
        a("GET", "/news/api/v1/companies/{cid}/headlines")(lambda r: self._read(r, "news", lambda c: {"value": data.NEWS.get(c, [])}))
        a("GET", "/graph/v1.0/me/calendarView")(self._calendar)
        a("GET", "/admin/calls")(lambda r: {"value": list(self.calls)})

    def _record(self, system: str, r: Request, customer: str | None) -> None:
        with self._lock:
            self.calls.append({"system": system, "method": r.method, "route": r.route, "customer": customer, "ts": time.time()})

    def _read(self, r: Request, system: str, fn) -> dict:
        cid = r.params["cid"]
        self._record(system, r, cid)
        self.simulate(system)
        if cid not in data.CUSTOMERS:
            raise HttpError(404, "NotFound", "customer not found")
        out = fn(cid)
        self.faults.after(system)
        return out

    @staticmethod
    def _customer(cid: str) -> dict:
        c = data.CUSTOMERS[cid]
        return {"id": cid, **{k: c[k] for k in ("crm_id", "name", "sector", "since", "turnover_aed", "revenue_yoy_pct", "contact", "emirates_id")}}

    def _entitlements(self, r: Request) -> dict:
        self._record("crm", r, None)
        self.simulate("crm")
        rm = self._by_oid.get(r.params["oid"])
        if rm is None:
            raise HttpError(404, "NotFound", "no RM with this oid")
        today = self.cfg.settings.as_of
        delegated = [{"customer": c, "owner": d["owner"], "expires": d["expires"]}
                     for d in data.DELEGATIONS if d["delegate"] == rm and d["expires"] >= today for c in d["customers"]]
        return {"rm": rm, "clearance": data.RMS[rm]["clearance"], "customers": sorted(data.RMS[rm]["portfolio"]),
                "delegations": delegated, "asOf": today}

    def _verify_rm_header(self, r: Request) -> str:
        oid, ts, sig = r.header("x-apim-rm-oid"), r.header("x-apim-rm-ts"), r.header("x-apim-rm-sig")
        secret = self.cfg.secrets.get("apim-backend-signing", "")
        if not (oid and ts and sig and secret) or abs(time.time() - int(ts)) > 300 or \
                not hmac.compare_digest(sig, sign_rm_header(secret, oid, r.method, r.path, int(ts))):
            raise HttpError(401, "Unauthorized", "missing or invalid APIM-signed RM header")
        return oid

    def _write(self, r: Request, kind: str) -> Response:
        cid = r.params["cid"]
        self._record("crm", r, cid)
        self.simulate("crm")
        oid = self._verify_rm_header(r)
        if cid not in data.CUSTOMERS:
            raise HttpError(404, "NotFound", "customer not found")
        body = r.json()
        ref = body.get("externalReference")
        if not ref:
            raise HttpError(400, "BadRequest", "externalReference is required")
        rec = {"id": f"{'CR' if kind == 'call-report' else 'TK'}-{secrets.token_hex(4)}", "customerId": cid, "externalReference": ref,
               "approvedByOid": oid, "aiAssisted": bool(body.get("aiAssisted")), "createdAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if kind == "call-report":
            rec["text"] = body.get("text", "")
        else:
            rec.update({"title": body.get("title", ""), "dueDate": body.get("dueDate")})
        with self._lock:
            (self.call_reports if kind == "call-report" else self.tasks).append(rec)
        self.faults.after("crm")  # a slow response AFTER the write: the double-post hazard
        return Response(201, rec)

    def _lookup(self, r: Request, store: list[dict]) -> dict:
        self._record("crm", r, None)
        self.simulate("crm")
        ref = r.query.get("externalReference")
        with self._lock:
            return {"value": [x for x in store if x["externalReference"] == ref]}

    def _calendar(self, r: Request) -> dict:
        self._record("graph", r, None)
        self.simulate("graph")
        claims = self.caller(r, AUD["graph"], scopes=("Calendars.ReadBasic",), app_only=False)
        rm = self._by_oid.get(claims.get("oid", ""))
        events = []
        for i, (cid, start, subject) in enumerate(MEETINGS.get(rm or "", [])):
            events.append({"id": f"AAMk-{rm}-{i}", "subject": f"{subject}: {data.CUSTOMERS[cid]['name']}",
                           "start": {"dateTime": start, "timeZone": "Arabian Standard Time"},
                           "end": {"dateTime": start[:11] + f"{int(start[11:13]) + 1:02d}" + start[13:], "timeZone": "Arabian Standard Time"},
                           "categories": [f"crm:{cid}"]})
        return {"@odata.context": "https://graph.microsoft.com/v1.0/$metadata#users('me')/calendarView", "value": events}
