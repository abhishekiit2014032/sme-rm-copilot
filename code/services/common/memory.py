"""Memory helpers (plan §3.8, C26, C27, C29), used by tool-gateway as the memory store.

* `memory_safe`: what may be written to ANY memory (checkpoints, session, episodes). Quarantined or injected text is
  never written: strings that the injection rules flag are withheld, quoted instructions are removed, and the fields
  that carry quarantined text or rejected model output keep only their count.
* `MemoryKeys`: one key per customer and purpose (`memory-ckpt-c-<id>`, `memory-ep-c-<id>`) as Key Vault secrets;
  checkpoints and episodes are sealed with them, so erasure crypto-shreds them (the audit-writer deletes the keys
  and the documents). Content cipher: the same MOCK cipher as the audit content store (envelope.py); production
  uses AES-256-GCM with HSM-wrapped keys in a separate memory key vault.
* `episode_from_draft`: episodic memory is built ONLY from the RM-approved draft (approval-verified commit), with
  PII masked and injected text removed, plus provenance.
* `session_add_turn`: last N turns plus a deterministic rolling summary, kept within a token budget.
"""
from __future__ import annotations

import base64
import json
import math
import re
import secrets
import threading
import time
from typing import TYPE_CHECKING

from rm_copilot import guardrails as g

from . import jose
from .envelope import seal, unseal
from .httpkit import HttpError, TransportError

if TYPE_CHECKING:
    from .service import Service

WITHHELD = "[withheld: flagged as injected text]"
COUNT_ONLY = ("quarantined", "rejected_model_output")  # carry quarantined text / rejected model output: count only
SHARED = "shared"


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / 4)


def _safe_text(s: str) -> str:
    if g.detect_injection(s):
        cleaned, _ = g.quarantine_quotes(s)
        return WITHHELD if g.detect_injection(cleaned) else cleaned
    return s


def memory_safe(obj: object) -> object:
    """A copy of `obj` that may be written to memory (C26)."""
    if isinstance(obj, dict):
        return {k: len(v) if k in COUNT_ONLY and isinstance(v, list) else memory_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [memory_safe(x) for x in obj]
    if isinstance(obj, str):
        return _safe_text(obj)
    return obj


def state_sha256(state: dict) -> str:
    return jose.sha256_hex(jose.canonical_json(state))


def key_name(purpose: str, customer_id: str | None) -> str:
    return f"memory-{purpose}-" + (f"c-{re.sub(r'[^A-Za-z0-9-]', '-', customer_id)[:80]}" if customer_id else SHARED)


def seal_json(key: bytes, obj: object) -> str:
    return base64.b64encode(seal(key, json.dumps(obj, ensure_ascii=False).encode())).decode()


def unseal_json(key: bytes, blob: str) -> object:
    return json.loads(unseal(key, base64.b64decode(blob)))


class MemoryKeys:
    """Per-customer memory keys in Key Vault (secrets), cached briefly. One creator per purpose (this service),
    serialised by a lock, so concurrent first writes cannot race to two different keys."""

    def __init__(self, svc: "Service"):
        self.svc = svc
        self._cache: dict[str, tuple[float, bytes]] = {}
        self._lock = threading.Lock()

    def get(self, purpose: str, customer_id: str | None, create: bool) -> bytes | None:
        name = key_name(purpose, customer_id)
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(name)
            if hit and hit[0] > now:
                return hit[1]
            vault = self.svc.cfg.url("keyvault")
            try:
                r = self.svc.client.request("GET", f"{vault}/secrets/{name}?api-version=7.4", headers=self.svc.auth_headers("vault"),
                                            timeout=2.0, peer="keyvault")
                if r.ok:
                    key = base64.b64decode(r.json()["value"])
                elif r.status == 404 and create:
                    key = secrets.token_bytes(32)
                    w = self.svc.client.request("PUT", f"{vault}/secrets/{name}?api-version=7.4", json_body={"value": base64.b64encode(key).decode()},
                                                headers=self.svc.auth_headers("vault"), timeout=2.0, peer="keyvault")
                    if not w.ok:
                        raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "cannot create the memory key", details={"reason": "KEY_VAULT"})
                elif r.status == 404:
                    return None  # shredded or never created
                else:
                    raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "memory key unavailable", details={"reason": "KEY_VAULT"})
            except TransportError:
                raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "memory key unavailable", details={"reason": "KEY_VAULT"}) from None
            self._cache[name] = (now + self.svc.cfg.settings.memory_key_cache_s, key)
            return key

    def forget(self, customer_id: str) -> None:
        with self._lock:
            for k in [k for k in self._cache if k.endswith(f"-c-{customer_id}")]:
                self._cache.pop(k, None)


# --------------------------------------------------------------------------- episodic memory
_SENTENCE = re.compile(r"(?<=[.!?])\s+")


def episode_from_draft(draft: dict) -> tuple[str, int]:
    """Text of an episode from the RM-approved draft: call report and task titles; quoted instructions and sentences
    the injection rules flag are dropped (C26); PII is masked. Returns (text, number of fragments removed)."""
    report, removed = g.quarantine_quotes(str(draft.get("call_report", "")))
    kept, dropped = [], len(removed)
    for sentence in _SENTENCE.split(report):
        if sentence.strip() and g.detect_injection(sentence):
            dropped += 1
        elif sentence.strip():
            kept.append(sentence.strip())
    tasks = []
    for t in draft.get("tasks", []):
        title = str(t.get("title", ""))
        if g.detect_injection(title):
            dropped += 1
        elif title:
            tasks.append(f"{title} (due {t.get('due') or 'n/a'})")
    text = " ".join(kept) + (" Follow-ups: " + "; ".join(tasks) + "." if tasks else "")
    text, _ = g.mask_pii(text.strip())
    return text, dropped


# --------------------------------------------------------------------------- session memory
def _summarise(turns: list[dict], previous: dict) -> dict:
    """Deterministic rolling summary: counts per use case and the customers referred to (ids only, no facts)."""
    counts = dict(previous.get("counts", {}))
    customers = list(previous.get("customers", []))
    for t in turns:
        uc = str(t.get("use_case", "other"))
        counts[uc] = counts.get(uc, 0) + 1
        cid = t.get("customer_id")
        if cid and cid not in customers:
            customers.append(cid)
    return {"counts": counts, "customers": customers[-8:]}


def summary_text(summary: dict) -> str:
    if not summary.get("counts"):
        return ""
    names = {"uc1": "brief", "uc2": "policy question", "uc3": "follow-up draft", "commit": "commit"}
    parts = [f"{n} {names.get(uc, uc)}{'s' if n != 1 else ''}" for uc, n in sorted(summary["counts"].items())]
    text = "Earlier in this session: " + ", ".join(parts)
    if summary.get("customers"):
        text += " (customers: " + ", ".join(summary["customers"]) + ")"
    return text + "."


def session_tokens(doc: dict) -> int:
    return estimate_tokens(doc.get("summary", "")) + sum(estimate_tokens(json.dumps(t, sort_keys=True)) for t in doc.get("turns", []))


def session_add_turn(doc: dict, turn: dict, budget_tokens: int, max_turns: int) -> dict:
    """Append a (memory-safe) turn; fold the oldest turns into the rolling summary until the session fits both the
    turn limit and the token budget. The summary itself is cut to the budget as a last resort."""
    turns = list(doc.get("turns", [])) + [memory_safe(turn)]
    summary = dict(doc.get("summary_state", {}))
    out = {**doc, "turns": turns, "summary_state": summary, "summary": summary_text(summary)}
    while out["turns"] and (len(out["turns"]) > max_turns or session_tokens(out) > budget_tokens):
        folded, out["turns"] = out["turns"][0], out["turns"][1:]
        out["summary_state"] = _summarise([folded], out["summary_state"])
        out["summary"] = summary_text(out["summary_state"])
    if session_tokens(out) > budget_tokens:
        out["summary"] = out["summary"][: max(0, budget_tokens * 4 - 4)]
    out["tokens"] = session_tokens(out)
    return out
