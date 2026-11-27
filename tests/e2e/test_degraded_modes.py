"""Degraded modes, end to end (lanes B and D under failure).
Proves a non-critical source outage gives a labelled degraded answer, a critical one gives a decline, a model
outage falls back to the secondary model, and kill switches switch features off without a deploy.
"""
from __future__ import annotations

import pytest

from copilot.contracts import AnswerStatus
from copilot.execution.model_gateway import ModelUnavailable
from copilot.operations.flags import Flags
from copilot.wiring import build_copilot
from helpers import FEE_QUESTION, GULF_STAR, POLICY_QUESTION, ask


def brief(copilot):
    return ask(copilot, "rm-1", action="prepare_brief", customer_id=GULF_STAR)


def test_core_banking_down_gives_a_degraded_brief_labelled_account_data_unavailable(copilot):
    copilot.core_banking.down = True
    answer = brief(copilot)
    assert answer.status == AnswerStatus.DEGRADED
    assert answer.notices == ["Account data unavailable"]
    assert "### Accounts\n- Account data unavailable (source did not respond)" in answer.text
    assert "AED 2.0m" in answer.text      # the other sections still carry live figures


def test_degraded_source_is_marked_unavailable_in_sources(copilot):
    copilot.core_banking.down = True
    accounts = next(s for s in brief(copilot).sources if s.detail == "accounts")
    assert accounts.available is False and accounts.as_of == "unavailable"


def test_two_sources_down_are_both_labelled(copilot):
    copilot.core_banking.down = copilot.trade.down = True
    assert brief(copilot).notices == ["Account data unavailable", "Trade finance data unavailable"]


def test_crm_down_declines_the_brief_instead_of_guessing(copilot):
    copilot.crm.down = True
    answer = brief(copilot)
    assert answer.status == AnswerStatus.DECLINED and "CRM profile is unavailable" in answer.text


def test_primary_model_down_answers_from_the_fallback_model(copilot):
    copilot.llm.down_models.add("primary-llm")
    answer = ask(copilot, "rm-1", FEE_QUESTION)
    assert answer.status == AnswerStatus.ANSWERED
    assert "fallback-llm" in [s.attrs.get("model") for s in answer.trace.spans]


def test_global_kill_switch_declines_before_any_work():
    copilot = build_copilot(flags=Flags(copilot_enabled=False))
    answer = ask(copilot, "rm-1", FEE_QUESTION)
    assert answer.status == AnswerStatus.DECLINED and "switched off" in answer.text
    assert copilot.llm.calls == []


def test_planner_kill_switch_falls_back_to_a_safe_fast_path_answer():
    copilot = build_copilot(flags=Flags(planner_path=False))
    answer = ask(copilot, "rm-1", POLICY_QUESTION, customer_id=GULF_STAR)
    assert answer.path == "fast" and answer.status in (AnswerStatus.ANSWERED, AnswerStatus.DECLINED)
    assert not [s for s in answer.trace.spans if s.name == "plan"]


@pytest.mark.xfail(strict=True, raises=ModelUnavailable,
                   reason="BUG request_path/pipeline.py:54-63: ModelUnavailable (and TokenBudgetExceeded, "
                          "StructuredOutputError) are not caught, so a full model outage is an HTTP 500, not a "
                          "safe decline")
def test_all_models_down_gives_a_safe_decline(copilot):
    copilot.llm.down_models |= {"primary-llm", "fallback-llm", "mini-llm"}
    assert ask(copilot, "rm-1", FEE_QUESTION).status == AnswerStatus.DECLINED


@pytest.mark.xfail(strict=True, raises=KeyError,
                   reason="BUG execution/tool_gateway.py:91-92 skips the entitlement check when customer_id is "
                          "empty, and nothing validates it, so Prepare brief without a customer crashes")
def test_prepare_brief_without_a_customer_is_refused_not_crashed(copilot):
    answer = ask(copilot, "rm-1", action="prepare_brief")
    assert answer.status in (AnswerStatus.DENIED, AnswerStatus.DECLINED)
