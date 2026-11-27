"""Degraded modes, end to end (lanes B and D under failure).
Proves a non-critical source outage gives a labelled degraded answer, a critical one gives a decline, a model
outage falls back to the secondary model, and kill switches switch features off without a deploy.
"""
from __future__ import annotations

import pytest

from copilot.contracts import AnswerStatus
from copilot.execution.model_gateway import StructuredOutputError, TokenBudgetExceeded
from copilot.operations.flags import Flags
from copilot.wiring import build_copilot
from helpers import FEE_QUESTION, GULF_STAR, POLICY_QUESTION, ask


def brief(copilot):
    return ask(copilot, "rm-1", action="prepare_brief", customer_id=GULF_STAR)


def test_core_banking_down_degrades_and_labels_the_brief_while_crm_down_declines_it(copilot):
    copilot.core_banking.down = True
    answer = brief(copilot)
    assert answer.status == AnswerStatus.DEGRADED and answer.notices == ["Account data unavailable"]
    assert "### Accounts\n- Account data unavailable (source did not respond)" in answer.text
    assert "AED 2.0m" in answer.text                     # the other sections still carry live figures
    accounts = next(s for s in answer.sources if s.detail == "accounts")
    assert (accounts.available, accounts.as_of) == (False, "unavailable")
    copilot.crm.down = True
    declined = brief(copilot)
    assert declined.status == AnswerStatus.DECLINED and "CRM profile is unavailable" in declined.text


def test_primary_model_down_answers_from_the_fallback_model(copilot):
    copilot.llm.down_models.add("primary-llm")
    answer = ask(copilot, "rm-1", FEE_QUESTION)
    assert answer.status == AnswerStatus.ANSWERED
    assert "fallback-llm" in [s.attrs.get("model") for s in answer.trace.spans]


def test_kill_switches_turn_features_off_without_a_deploy():
    switched_off = build_copilot(flags=Flags(copilot_enabled=False))
    answer = ask(switched_off, "rm-1", FEE_QUESTION)
    assert answer.status == AnswerStatus.DECLINED and switched_off.llm.calls == []
    no_planner = ask(build_copilot(flags=Flags(planner_path=False)), "rm-1", POLICY_QUESTION, customer_id=GULF_STAR)
    assert no_planner.path == "fast" and not [s for s in no_planner.trace.spans if s.name == "plan"]
    assert no_planner.status in (AnswerStatus.ANSWERED, AnswerStatus.DECLINED)


def test_all_models_down_gives_a_safe_decline_that_is_audited_and_traced(copilot):
    copilot.llm.down_models |= {"primary-llm", "fallback-llm", "mini-llm"}
    answer = ask(copilot, "rm-1", FEE_QUESTION)
    assert answer.status == AnswerStatus.DECLINED and "will not guess" in answer.text
    assert answer.notices == ["Model gateway: no model available (primary and fallback failed)"]
    assert ("Model gateway", "safe decline", "declined") in [(s.component.value, s.name, s.status)
                                                             for s in answer.trace.spans]
    assert copilot.audit.records[-1].outcome == "declined" and copilot.tracer.get(answer.trace.trace_id)


@pytest.mark.parametrize("error", [TokenBudgetExceeded("used up"), StructuredOutputError("bad JSON")])
def test_any_model_gateway_error_is_a_safe_decline(copilot, monkeypatch, error):
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(copilot.models, "generate", fail)
    assert ask(copilot, "rm-1", FEE_QUESTION).status == AnswerStatus.DECLINED


def test_prepare_brief_without_a_customer_is_refused_not_crashed(copilot):
    answer = ask(copilot, "rm-1", action="prepare_brief")
    assert answer.status == AnswerStatus.DECLINED and answer.notices == ["customer_id missing"]
