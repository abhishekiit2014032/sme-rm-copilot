"""Diagram box: Model gateway (lane B).
Model-agnostic tiers (planner, primary, mini, judge, embedding), token budgets, retries, fallback, structured output.
Mocked: the LLM behind it (mocks/llm/scripted_llm.py); an untested Azure OpenAI adapter can be selected instead.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from copilot.contracts.context import RunContext
from copilot.contracts.ports import LlmClient, LlmCompletion
from copilot.contracts.trace import Component, Trace
from copilot.operations.resilience import CircuitBreaker, CircuitOpen, RetryPolicy, TransientError, \
    call_with_resilience
from copilot.operations.tracing import cost_usd
from copilot.state.memory.procedural import PromptRegistry

T = TypeVar("T", bound=BaseModel)


class ModelTier(str, Enum):
    PLANNER = "planner"
    PRIMARY = "primary"
    MINI = "mini"
    JUDGE = "judge"
    EMBEDDING = "embedding"


# Tier -> (model, fallback model). Names are logical; each adapter maps them to real deployments.
TIER_MODELS: dict[ModelTier, tuple[str, str | None]] = {
    ModelTier.PLANNER: ("primary-llm", "fallback-llm"),
    ModelTier.PRIMARY: ("primary-llm", "fallback-llm"),
    ModelTier.MINI: ("mini-llm", "fallback-llm"),
    ModelTier.JUDGE: ("judge-llm", None),
    ModelTier.EMBEDDING: ("embedding-model", None),
}

# Low temperature and constrained top-p for every call (hallucination control); prompts may lower them.
DEFAULT_PARAMS: dict[str, Any] = {"temperature": 0.1, "top_p": 0.9, "max_tokens": 800}


class ModelGatewayError(Exception):
    """Base of every model gateway failure; the pipeline turns any of them into a safe decline."""


class ModelUnavailable(ModelGatewayError):
    pass


class TokenBudgetExceeded(ModelGatewayError):
    pass


class StructuredOutputError(ModelGatewayError):
    pass


class ModelGateway:
    def __init__(self, client: LlmClient, prompts: PromptRegistry) -> None:
        self._client = client
        self._prompts = prompts
        self._breakers = {m: CircuitBreaker(m) for pair in TIER_MODELS.values() for m in pair if m}

    def generate(self, tier: ModelTier, prompt_id: str, variables: dict[str, Any], output: type[T],
                 ctx: RunContext) -> T:
        """Render a versioned prompt, call the tier's model (or its fallback) and validate the JSON reply."""
        if ctx.tokens_left <= 0:
            raise TokenBudgetExceeded("request token budget used up")
        prompt, template = self._prompts.render(prompt_id, {**variables, "strict": ctx.strict})
        params = {**DEFAULT_PARAMS, **template.params}
        if ctx.strict:
            params["temperature"] = 0.0
        with ctx.trace.span(Component.MODEL_GATEWAY, f"{tier.value}: {prompt_id}") as span:
            model, completion = self._complete_with_fallback(tier, prompt, params, ctx.trace)
            tokens = completion.tokens_in + completion.tokens_out
            ctx.tokens_left -= tokens
            span.detail = f"{model}, prompt v{template.version}, {tokens} tokens, temperature {params['temperature']}"
            span.attrs.update(model=model, prompt_version=f"{prompt_id}@{template.version}",
                              tokens_in=completion.tokens_in, tokens_out=completion.tokens_out,
                              cost_usd=cost_usd(model, tokens), temperature=params["temperature"],
                              top_p=params["top_p"], tokens_left=ctx.tokens_left)
            return _parse(completion.text, output, span)

    def embed(self, text: str, trace: Trace) -> list[float]:
        model, _ = TIER_MODELS[ModelTier.EMBEDDING]
        vector = self._client.embed(model, text)
        tokens = max(1, len(text.split()))
        trace.event(Component.MODEL_GATEWAY, "embedding", detail=f"{model}, {tokens} tokens",
                    model=model, tokens_in=tokens, cost_usd=cost_usd(model, tokens))
        return vector

    def _complete_with_fallback(self, tier: ModelTier, prompt: Any, params: dict[str, Any],
                                trace: Trace) -> tuple[str, LlmCompletion]:
        primary, fallback = TIER_MODELS[tier]
        for model in [m for m in (primary, fallback) if m]:
            try:
                with trace.span(Component.LLM, model) as llm_span:
                    completion = call_with_resilience(
                        lambda: self._client.complete(model, prompt, params),
                        breaker=self._breakers[model], trace=trace, policy=RetryPolicy(attempts=2))
                    llm_span.detail = f"{completion.tokens_in} in / {completion.tokens_out} out"
                return model, completion
            except (TransientError, CircuitOpen):
                if model == primary and fallback:
                    trace.event(Component.MODEL_GATEWAY, "fallback model", status="retry",
                                detail=f"{primary} failed, switching to {fallback}")
        raise ModelUnavailable(f"no model available for tier {tier.value}")


def _parse(text: str, output: type[T], span: Any) -> T:
    try:
        return output.model_validate_json(text)
    except ValidationError as exc:
        span.status, span.detail = "error", f"reply did not match {output.__name__}"
        raise StructuredOutputError(str(exc)) from exc
