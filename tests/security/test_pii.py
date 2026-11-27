"""Security: personal data (Input guardrails, Model gateway, Session + memory store, Secure storage, Observability).
Proves personal data is masked before any model or embedding call, never lands in session memory, the audit
log or traces, and is restored only for the RM and for the approved CRM write.
"""
from __future__ import annotations

import pytest

from helpers import GULF_STAR, ask, draft_follow_up

EMAIL = "cfo.private@gulfstar.example"
PHONE = "+971 50 123 4567"
QUESTION = f"The CFO ({EMAIL}, {PHONE}) asks: what is the fee for a letter of credit amendment?"
NOTES = f"Draft follow-up: owner will send statements to {EMAIL} by 5 Dec"


def test_question_pii_never_reaches_the_model_or_embeddings(spied_copilot, spy_llm):
    ask(spied_copilot, "rm-1", QUESTION)
    seen = spy_llm.everything_seen()
    assert spy_llm.prompts and EMAIL not in seen and PHONE not in seen
    assert "<EMAIL_1>" in seen


def test_follow_up_pii_is_masked_for_the_model_and_restored_in_the_crm_write(spied_copilot, spy_llm):
    token = draft_follow_up(spied_copilot, notes=NOTES).approval_token
    assert EMAIL not in spy_llm.everything_seen()
    spied_copilot.pipeline.approve(spied_copilot.token_for("rm-1"), token)
    written = [w["write"] for w in spied_copilot.crm.committed.values()]
    note = next(w for w in written if w["kind"] == "note")
    assert EMAIL in note["body"] and "<EMAIL_1>" not in str(written)


def test_answer_text_shows_the_rm_their_own_data_unmasked(copilot):
    answer = draft_follow_up(copilot, notes=NOTES)
    note = next(d for d in answer.drafts if d.kind == "note")
    assert EMAIL in note.body


@pytest.mark.xfail(strict=True, reason="BUG request_path/pipeline.py:98: _release unmasks draft bodies but not "
                                       "draft titles, so the RM sees placeholders such as <EMAIL_1> in task titles")
def test_draft_titles_shown_to_the_rm_have_pii_restored(copilot):
    answer = draft_follow_up(copilot, notes=NOTES)
    assert not [d.title for d in answer.drafts if "<EMAIL_" in d.title]


def test_session_memory_keeps_only_masked_text(copilot):
    ask(copilot, "rm-1", QUESTION)
    turns = copilot.pipeline.session.recent("rm-1")
    assert EMAIL not in turns[-1].text and "<EMAIL_1>" in turns[-1].text


def test_audit_log_and_trace_hold_no_personal_data(copilot):
    answer = ask(copilot, "rm-1", QUESTION)
    assert EMAIL not in answer.trace.model_dump_json() and PHONE not in answer.trace.model_dump_json()
    assert EMAIL not in str([r.model_dump() for r in copilot.audit.records])


def test_customer_contact_details_stay_out_of_the_brief_trace(copilot):
    trace = ask(copilot, "rm-1", action="prepare_brief", customer_id=GULF_STAR).trace
    assert "faisal.rahman@gulfstar.example" not in trace.model_dump_json()
