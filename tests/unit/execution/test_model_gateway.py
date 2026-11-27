"""Diagram box: Model gateway (lane B).
Proves tier routing, versioned prompts, the per-request token budget, structured output validation and
fallback to the secondary model when the primary is down.
"""
from __future__ import annotations

import pytest
from pydantic import BaseModel

from copilot.execution.agents.brief import BriefDraft
from copilot.execution.model_gateway import (ModelGateway, ModelTier, ModelUnavailable,
                                             StructuredOutputError, TokenBudgetExceeded)
from copilot.request_path.router import IntentGuess
from copilot.state.memory.procedural import PromptRegistry
from helpers import run_context
from mocks.llm.scripted_llm import ScriptedLlm

ASK = {"text": "What is the fee for a letter of credit amendment?"}


def classify(gateway: ModelGateway, ctx, output: type[BaseModel] = IntentGuess):
    return gateway.generate(ModelTier.MINI, "router.classify_intent", ASK, output, ctx)


def models_used(ctx) -> list[str]:
    return [s.attrs["model"] for s in ctx.trace.spans if s.component.value == "Model gateway" and "model" in s.attrs]


@pytest.fixture
def llm() -> ScriptedLlm:
    return ScriptedLlm()


@pytest.fixture
def gateway(llm) -> ModelGateway:
    return ModelGateway(llm, PromptRegistry())


def summarise(gateway: ModelGateway, ctx) -> BriefDraft:
    return gateway.generate(ModelTier.PRIMARY, "brief.summarise", {"fact_keys": [], "unavailable": []},
                            BriefDraft, ctx)


def test_tiers_route_to_their_own_models(copilot, gateway):
    ctx = run_context(copilot)
    classify(gateway, ctx)
    summarise(gateway, ctx)
    assert models_used(ctx) == ["mini-llm", "primary-llm"]


def test_generate_returns_validated_output_and_records_model_prompt_version_and_tokens(copilot, gateway):
    ctx = run_context(copilot)
    guess = classify(gateway, ctx)
    assert guess.intent == "policy"
    span = next(s for s in ctx.trace.spans if s.component.value == "Model gateway")
    assert span.attrs["model"] == "mini-llm"
    assert span.attrs["prompt_version"] == "router.classify_intent@1.1.0"
    assert span.attrs["tokens_in"] > 0 and span.attrs["cost_usd"] > 0


def test_tokens_are_deducted_from_the_request_budget(copilot, gateway):
    ctx = run_context(copilot)
    classify(gateway, ctx)
    assert ctx.tokens_left < 6000


def test_exhausted_budget_blocks_the_call_before_any_model_is_used(copilot, gateway, llm):
    ctx = run_context(copilot, tokens_left=0)
    with pytest.raises(TokenBudgetExceeded):
        classify(gateway, ctx)
    assert llm.calls == []


def test_primary_down_falls_back_to_the_fallback_model(copilot, gateway, llm):
    llm.down_models.add("mini-llm")
    ctx = run_context(copilot)
    assert classify(gateway, ctx).intent == "policy"
    assert models_used(ctx) == ["fallback-llm"]
    assert any(s.name == "fallback model" for s in ctx.trace.spans)


def test_primary_and_fallback_down_raises_model_unavailable(copilot, gateway, llm):
    llm.down_models |= {"mini-llm", "fallback-llm"}
    with pytest.raises(ModelUnavailable):
        classify(gateway, run_context(copilot))


class WrongShape(BaseModel):
    something_else: int


def test_reply_that_does_not_match_the_schema_is_rejected(copilot, gateway):
    with pytest.raises(StructuredOutputError):
        classify(gateway, run_context(copilot), output=WrongShape)


def test_strict_retry_forces_temperature_zero(copilot, gateway):
    ctx = run_context(copilot, strict=True)
    summarise(gateway, ctx)
    assert next(s for s in ctx.trace.spans if "brief.summarise" in s.name).attrs["temperature"] == 0.0
