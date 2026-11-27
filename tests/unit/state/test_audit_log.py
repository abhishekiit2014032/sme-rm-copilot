"""Diagram box: Secure storage (lane C), the write-once audit log.
Proves records are hash-chained so any edit, deletion or reordering is detected, that the log exposes no
way to change a record, and that every request leaves an audit record without payload content.
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


def test_records_are_chained_from_genesis():
    records = filled_log().records
    assert records[0].prev_hash == GENESIS
    assert [r.prev_hash for r in records[1:]] == [r.hash for r in records[:-1]]
    assert filled_log().verify_chain()


def test_editing_a_record_breaks_the_chain():
    log = filled_log()
    log._records[1].outcome = "denied"          # simulate tampering with storage
    assert not log.verify_chain()


def test_deleting_a_record_breaks_the_chain():
    log = filled_log()
    del log._records[1]
    assert not log.verify_chain()


def test_recomputing_the_hash_of_an_edited_record_is_still_detected():
    log = filled_log()
    log._records[0].actor = "someone-else"
    log._records[0].hash = log._records[0].compute_hash()
    assert not log.verify_chain()


def test_records_returned_are_copies_and_there_is_no_update_or_delete_api():
    log = filled_log()
    log.records[0].outcome = "changed"
    assert log.records[0].outcome == "answered" and log.verify_chain()
    assert not [name for name in dir(log) if name.startswith(("update", "delete", "remove", "edit"))]


def test_every_request_is_audited_without_question_text(copilot):
    answer = ask(copilot, "rm-1", FEE_QUESTION)
    last = copilot.audit.records[-1]
    assert (last.actor, last.action, last.outcome, last.request_id) == ("rm-1", "ask", "answered", answer.request_id)
    assert "letter of credit" not in copilot.audit.records[-1].model_dump_json()
    assert copilot.audit.verify_chain()
