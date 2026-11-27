"""Diagram box: Secure storage (lane C), the write-once audit log.
Proves records are hash-chained so any edit or deletion is detected, that the log exposes no way to change a
record, and that every request leaves an audit record without payload content.
"""
from __future__ import annotations

from copilot.state.audit_log import GENESIS, AuditLog
from helpers import FEE_QUESTION, GULF_STAR, ask


def filled_log() -> AuditLog:
    log = AuditLog()
    log.append("rm-1", "ask", "-", "answered", "rq-1")
    log.append("rm-1", "tool customer-data.get_facilities", GULF_STAR, "ok", "rq-2")
    log.append("rm-2", "prepare_brief", GULF_STAR, "denied", "rq-3")
    return log


def test_hash_chain_detects_edits_rehashing_and_deletion():
    assert filled_log().records[0].prev_hash == GENESIS and filled_log().verify_chain()
    edited = filled_log()
    edited._records[1].outcome = "denied"                       # simulate tampering with storage
    rehashed = filled_log()
    rehashed._records[0].actor = "someone-else"
    rehashed._records[0].hash = rehashed._records[0].compute_hash()
    deleted = filled_log()
    del deleted._records[1]
    assert not edited.verify_chain() and not rehashed.verify_chain() and not deleted.verify_chain()


def test_log_is_append_only_and_requests_are_audited_without_their_text(copilot):
    log = filled_log()
    log.records[0].outcome = "changed"                          # records are handed out as copies
    assert log.records[0].outcome == "answered"
    assert not [n for n in dir(log) if n.startswith(("update", "delete", "remove", "edit"))]
    answer = ask(copilot, "rm-1", FEE_QUESTION)
    last = copilot.audit.records[-1]
    assert (last.actor, last.action, last.outcome, last.request_id) == ("rm-1", "ask", "answered", answer.request_id)
    assert "letter of credit" not in str([r.model_dump() for r in copilot.audit.records])
    assert copilot.audit.verify_chain()
