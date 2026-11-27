"""Security: personal data (Input guardrails, Model gateway, Session + memory store, Secure storage, Observability).
Proves personal data is masked before any model or embedding call, never lands in session memory, the audit
log or traces, and is restored only for the RM and for the approved CRM write.
"""
from __future__ import annotations

import pytest

from helpers import ask, draft_follow_up

EMAIL = "cfo.private@gulfstar.example"
PHONE = "+971 50 123 4567"
QUESTION = f"The CFO ({EMAIL}, {PHONE}) asks: what is the fee for a letter of credit amendment?"
NOTES = f"Draft follow-up: owner will send statements to {EMAIL} by 5 Dec"


def test_question_pii_never_reaches_models_memory_audit_or_trace(spied_copilot, spy_llm):
    answer = ask(spied_copilot, "rm-1", QUESTION)
    seen = spy_llm.everything_seen()
    assert spy_llm.prompts and "<EMAIL_1>" in seen and EMAIL not in seen and PHONE not in seen
    assert "<EMAIL_1>" in spied_copilot.pipeline.session.recent("rm-1")[-1].text
    stored = answer.trace.model_dump_json() + str([r.model_dump() for r in spied_copilot.audit.records])
    assert EMAIL not in stored and PHONE not in stored


def test_follow_up_pii_is_masked_for_the_model_and_restored_for_the_rm_and_the_crm(spied_copilot, spy_llm):
    drafted = draft_follow_up(spied_copilot, notes=NOTES)
    assert EMAIL not in spy_llm.everything_seen()
    assert EMAIL in next(d for d in drafted.drafts if d.kind == "note").body
    spied_copilot.pipeline.approve(spied_copilot.token_for("rm-1"), drafted.approval_token)
    written = [w["write"] for w in spied_copilot.crm.committed.values()]
    assert EMAIL in next(w for w in written if w["kind"] == "note")["body"]
    assert "<EMAIL_1>" not in str(written)


@pytest.mark.xfail(strict=True, reason="BUG request_path/pipeline.py RequestPipeline._release unmasks draft "
                                       "bodies but not draft titles, so the RM sees placeholders such as "
                                       "<EMAIL_1> in task titles")
def test_draft_titles_shown_to_the_rm_have_pii_restored(copilot):
    answer = draft_follow_up(copilot, notes=NOTES)
    assert not [d.title for d in answer.drafts if "<EMAIL_" in d.title]
