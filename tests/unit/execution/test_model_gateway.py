"""Diagram box: Model gateway (lane B).
Proves tier routing, versioned prompts, the per-request token budget, structured output validation and
fallback to the secondary model when the primary is down. Also proves the scripted model behind it derives its
policy answers from the clauses it is given, not from text written for the demo questions.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from copilot.execution.agents.brief import BriefDraft
from copilot.execution.model_gateway import (ModelGateway, ModelTier, ModelUnavailable, StructuredOutputError,
                                             TokenBudgetExceeded)
from copilot.request_path.router import IntentGuess
from copilot.state.memory.procedural import PromptRegistry
from helpers import run_context
from mocks.llm.scripted_llm import ScriptedLlm

ASK = {"text": "What is the fee for a letter of credit amendment?"}


@pytest.fixture
def llm() -> ScriptedLlm:
    return ScriptedLlm()


@pytest.fixture
def gateway(llm) -> ModelGateway:
    return ModelGateway(llm, PromptRegistry())


def classify(gateway: ModelGateway, ctx, output: type[BaseModel] = IntentGuess):
    return gateway.generate(ModelTier.MINI, "router.classify_intent", ASK, output, ctx)


def summarise(gateway: ModelGateway, ctx) -> BriefDraft:
    return gateway.generate(ModelTier.PRIMARY, "brief.summarise", {"fact_keys": [], "unavailable": []},
                            BriefDraft, ctx)


def model_spans(ctx):
    return [s for s in ctx.trace.spans if s.component.value == "Model gateway" and "model" in s.attrs]


def test_tiers_route_to_their_models_and_spans_record_prompt_version_tokens_and_cost(copilot, gateway):
    ctx = run_context(copilot)
    assert classify(gateway, ctx).intent == "policy"
    summarise(gateway, ctx)
    mini, primary = model_spans(ctx)
    assert (mini.attrs["model"], primary.attrs["model"]) == ("mini-llm", "primary-llm")
    assert mini.attrs["prompt_version"] == "router.classify_intent@1.1.0"
    assert mini.attrs["tokens_in"] > 0 and mini.attrs["cost_usd"] > 0
    assert ctx.tokens_left == 6000 - sum(s.attrs["tokens_in"] + s.attrs["tokens_out"] for s in (mini, primary))


def test_exhausted_budget_blocks_the_call_before_any_model_is_used(copilot, gateway, llm):
    with pytest.raises(TokenBudgetExceeded):
        classify(gateway, run_context(copilot, tokens_left=0))
    assert llm.calls == []


def test_primary_down_falls_back_and_both_down_raises(copilot, gateway, llm):
    llm.down_models.add("mini-llm")
    ctx = run_context(copilot)
    assert classify(gateway, ctx).intent == "policy"
    assert [s.attrs["model"] for s in model_spans(ctx)] == ["fallback-llm"]
    llm.down_models.add("fallback-llm")
    with pytest.raises(ModelUnavailable):
        classify(gateway, run_context(copilot))


class WrongShape(BaseModel):
    something_else: int


def test_reply_that_does_not_match_the_schema_is_rejected_and_strict_mode_uses_temperature_zero(copilot, gateway):
    with pytest.raises(StructuredOutputError):
        classify(gateway, run_context(copilot), output=WrongShape)
    strict = run_context(copilot, strict=True)
    summarise(gateway, strict)
    assert model_spans(strict)[0].attrs["temperature"] == 0.0


def test_scripted_policy_answer_is_derived_from_the_clauses_it_is_given(llm):
    cap = {"ref": "lending@1#2.1", "ref_label": "§2.1", "heading": "Cap",
           "text": "A trade line must not exceed AED 4m. It needs a signed agreement."}
    above = {"ref": "lending@1#2.2", "ref_label": "§2.2", "heading": "Above the cap",
             "text": "Any amount above §2.1 needs board approval."}

    def answer(question: str) -> dict:
        return llm._policy_answer({"question": question, "clauses": [cap, above]})

    assert answer("Can we go to AED 3.5m?") == {
        "outcome": "yes", "cited_refs": ["lending@1#2.1"],
        "answer": "Yes. AED 3.5m is within the limit: a trade line must not exceed AED 4m (§2.1). "
                  "It needs a signed agreement."}
    assert answer("Can we go to AED 5m?")["answer"] == (
        "No. AED 5m is above the limit: a trade line must not exceed AED 4m (§2.1). "
        "Any amount above §2.1 needs board approval (§2.2).")
    assert len({llm._classify_intent({"text": t})["confidence"] for t in ("fees?", "fee for an LC limit?")}) == 2
