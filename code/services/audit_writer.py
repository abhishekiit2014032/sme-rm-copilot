"""audit-writer: consumes the audit stream and keeps a tamper-evident, erasable audit trail (plan §3.2, C5, C21).

* Metadata (ids, decisions, lineage) goes into an append-only hash chain: each record holds the SHA-256 of the
  previous record, so any edit, insertion or deletion breaks `verify_chain`. Production: immutable Blob storage
  (time-based retention, GZRS) plus a restricted Log Analytics table.
* Content (masked prompts and model outputs) never enters the chain. Producers send it envelope-encrypted for the
  Key Vault key `audit-transport`; this writer unwraps the data key (Key Vault `unwrapKey`), re-encrypts the
  content with a per-customer key held in Key Vault and stores it separately; the chain keeps only a pointer and
  the ciphertext hash.
* Crypto-shredding (`POST /audit/erasure`) deletes the customer's key: the content becomes unreadable for good,
  while the chain still verifies (plan C21).

Local files live under `<run_dir>/audit/`. The content cipher (services/common/envelope.py) is a MOCK built from
the standard library; production uses AES-256-GCM with keys wrapped by Key Vault HSM keys. Differences from the
LLD: one chain (one partition), no hourly signed anchors, customer id in clear in metadata (LLD: HMAC reference).
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
import time
from pathlib import Path

from services.common import jose
from services.common.envelope import seal, unseal
from services.common.config import StackConfig
from services.common.httpkit import HttpError, Request, RequestContext, TransportError, set_request_context
from services.common.service import Service
from services.common.telemetry import parse_traceparent

GENESIS = "0" * 64


# --------------------------------------------------------------------------- hash chain
def record_hash(record: dict) -> str:
    return jose.sha256_hex(jose.canonical_json({k: v for k, v in record.items() if k != "hash"}))


def verify_chain(path: str | Path) -> dict:
    """Recompute every link. Returns {"ok", "records", "first_bad_seq", "reason"}."""
    prev, n = GENESIS, 0
    p = Path(path)
    if not p.exists():
        return {"ok": True, "records": 0, "first_bad_seq": None, "reason": None}
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except ValueError:
            return {"ok": False, "records": n, "first_bad_seq": n, "reason": "unparseable record"}
        if rec.get("seq") != n:
            return {"ok": False, "records": n, "first_bad_seq": n, "reason": "sequence gap"}
        if rec.get("prev_hash") != prev:
            return {"ok": False, "records": n, "first_bad_seq": n, "reason": "broken link"}
        if rec.get("hash") != record_hash(rec):
            return {"ok": False, "records": n, "first_bad_seq": n, "reason": "hash mismatch"}
        prev, n = rec["hash"], n + 1
    return {"ok": True, "records": n, "first_bad_seq": None, "reason": None}


def key_name(customer: str) -> str:
    return "audit-content-" + re.sub(r"[^A-Za-z0-9-]", "-", customer)[:100]


class AuditWriter(Service):
    name = "audit-writer"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self.dir = Path(cfg.settings.run_dir) / "audit"
        (self.dir / "content").mkdir(parents=True, exist_ok=True)
        self.chain_path = self.dir / "audit-chain.jsonl"
        self._next_seq_in = 0      # Event Hubs checkpoint
        self._seq, self._prev = self._head()  # resume after a restart: never restart the chain at seq 0 (false tamper alarm)
        self._keys: dict[str, bytes] = {}
        self._lock = threading.Lock()
        m = self.metrics
        self.m_records = m.counter("audit_records_total", "Records appended to the hash chain", ("event",))
        self.m_content = m.counter("audit_content_sealed_total", "Content blobs encrypted per customer key", ())
        self.m_lag = m.gauge("audit_consumer_lag_events", "Events read but not yet appended", ())
        a = self.app.route
        a("GET", "/audit/verify")(lambda r: verify_chain(self.chain_path))
        a("GET", "/audit/records")(self._records)
        a("POST", "/audit/erasure")(self._erasure)
        a("GET", "/audit/content/{customer}/{event_id}")(self._content)

    def _head(self) -> tuple[int, str]:
        """The chain head (next seq, last hash) of an existing chain file. Production: Cosmos `audit-chain-heads`."""
        if not self.chain_path.exists():
            return 0, GENESIS
        last = None
        for line in self.chain_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                last = line
        if last is None:
            return 0, GENESIS
        rec = json.loads(last)
        return rec["seq"] + 1, rec["hash"]

    def start(self) -> None:
        self.run_loop("consume", self.cfg.settings.audit_poll_s, self.tick)

    # ------------------------------------------------------------------ consume
    def tick(self) -> int:
        set_request_context(RequestContext("audit-consumer"))
        with self._lock:
            start = self._next_seq_in
            r = self.client.request("GET", f"{self.cfg.url('eventhubs')}/audit/consumergroups/audit-writer/partitions/0/events?fromSequenceNumber={start}&maxCount=200",
                                    headers=self.auth_headers("eventhubs"), timeout=2.0, peer="eventhubs")
            events = r.json().get("value", []) if r.ok else []
            for e in events:
                self._append(e)
                self._next_seq_in = e["sequenceNumber"] + 1
            return len(events)

    def _key(self, customer: str, create: bool) -> bytes | None:
        if customer in self._keys:
            return self._keys[customer]
        name = key_name(customer)
        r = self.client.request("GET", f"{self.cfg.url('keyvault')}/secrets/{name}?api-version=7.4", headers=self.auth_headers("vault"), timeout=2.0, peer="keyvault")
        if r.ok:
            key = base64.b64decode(r.json()["value"])
        elif r.status == 404 and create:
            key = secrets.token_bytes(32)
            w = self.client.request("PUT", f"{self.cfg.url('keyvault')}/secrets/{name}?api-version=7.4", json_body={"value": base64.b64encode(key).decode()},
                                    headers=self.auth_headers("vault"), timeout=2.0, peer="keyvault")
            if not w.ok:
                raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "cannot create the content key", details={"reason": "KEY_VAULT"})
        else:
            return None
        self._keys[customer] = key
        return key

    def _open_envelope(self, env: dict) -> bytes:
        kid = env["kid"]
        if not kid.startswith(f"{self.cfg.url('keyvault')}/keys/audit-transport/"):
            raise ValueError("unknown transport key")
        r = self.client.request("POST", f"{kid}/unwrapkey?api-version=7.4", json_body={"alg": "RSA-OAEP-256", "value": env["wrapped_dek"]},
                                headers=self.auth_headers("vault"), timeout=2.0, peer="keyvault")
        if not r.ok:
            raise ValueError("unwrap failed")
        return unseal(jose.b64u_decode(r.json()["value"]), jose.b64u_decode(env["ciphertext"]))

    def _append(self, e: dict) -> None:
        body = dict(e["body"])
        parent = parse_traceparent((e.get("properties") or {}).get("Diagnostic-Id"))
        with self.telemetry.span("audit.append", kind="consumer", parent=parent, **{"rmc.event": str(body.get("event", ""))}):
            env = body.pop("content_envelope", None)
            ref = None
            if env:
                customer = body.get("customer") or "_no-customer"
                try:
                    plaintext = self._open_envelope(env)
                except (ValueError, KeyError, TransportError):
                    body["content_error"] = "undecryptable"  # recorded, never dropped silently
                    plaintext = None
                if plaintext is not None:
                    blob = seal(self._key(customer, create=True), plaintext)
                    folder = self.dir / "content" / key_name(customer)
                    folder.mkdir(exist_ok=True)
                    (folder / f"{body['event_id']}.bin").write_bytes(blob)
                    ref = {"uri": f"content://{key_name(customer)}/{body['event_id']}", "sha256": hashlib.sha256(blob).hexdigest(),
                           "plaintext_sha256": env.get("plaintext_sha256"), "key": key_name(customer)}
                    self.m_content.inc()
            self._write(body, ref)

    def _write(self, body: dict, ref: dict | None) -> None:
        rec = {"seq": self._seq, "ts": round(time.time(), 3), "prev_hash": self._prev, "event": body, "content_ref": ref}
        rec["hash"] = record_hash(rec)
        with self.chain_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        self._seq, self._prev = self._seq + 1, rec["hash"]
        self.m_records.inc(event=str(body.get("event", "")))

    def flush(self, timeout: float = 5.0) -> None:
        """Drain the stream now (tests and shutdown)."""
        end = time.monotonic() + timeout
        while self.tick() and time.monotonic() < end:
            pass

    # ------------------------------------------------------------------ operator APIs (PIM-activated role)
    def _admin(self, r: Request, role: str) -> None:
        self.validator("audit").validate(r.bearer(), scopes=(role,), app_only=True, azp=("ops-admin",))

    def _records(self, r: Request) -> dict:
        self._admin(r, "Audit.ReadContent")
        limit = int(r.query.get("limit", 50))
        lines = self.chain_path.read_text(encoding="utf-8").splitlines() if self.chain_path.exists() else []
        return {"value": [json.loads(x) for x in lines[-limit:]]}

    def _erasure(self, r: Request) -> dict:
        self._admin(r, "Audit.Erase")
        customer = str(r.json().get("customer", ""))
        if not customer:
            raise HttpError(400, "BadRequest", "customer is required")
        with self._lock:
            self._keys.pop(customer, None)
            try:
                d = self.client.request("DELETE", f"{self.cfg.url('keyvault')}/secrets/{key_name(customer)}?api-version=7.4",
                                        headers=self.auth_headers("vault"), timeout=2.0, peer="keyvault")
            except TransportError:
                raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "cannot reach the key store", details={"reason": "KEY_VAULT"}) from None
            if d.status not in (200, 404):
                raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "key deletion failed", details={"reason": "KEY_VAULT"})
            # The erasure itself is recorded in the chain (the writer owns the chain; no round trip through the stream).
            self._write({"schema": "audit/1", "event_id": secrets.token_hex(12), "event": "erasure_executed", "customer": customer,
                         "producer": {"service": self.name}, "metadata": {"key": key_name(customer), "result": "shredded" if d.ok else "no_key"}}, None)
        return {"customer": customer, "key": key_name(customer), "status": "shredded" if d.ok else "no_key"}

    def _content(self, r: Request) -> dict:
        self._admin(r, "Audit.ReadContent")
        customer, event_id = r.params["customer"], r.params["event_id"]
        path = self.dir / "content" / key_name(customer) / f"{re.sub(r'[^a-f0-9]', '', event_id)}.bin"
        if not path.exists():
            raise HttpError(404, "NotFound", "no content for this event")
        with self._lock:
            key = self._key(customer, create=False)
        shredded = HttpError(410, "CONTENT_SHREDDED", "the customer's content key was destroyed; content is unrecoverable")
        if key is None:
            raise shredded
        try:
            plaintext = unseal(key, path.read_bytes())
        except ValueError:
            # Sealed under a key that was shredded; the customer has a newer key for later content.
            raise shredded from None
        return {"event_id": event_id, "content": json.loads(plaintext)}
