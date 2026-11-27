# 01. Model gateway

Status: Accepted · Date: 27 Nov 2026

## Context
Steps differ in difficulty: intent, extraction and summaries need a small model; planning and briefs need the primary model. Provider outages, quota limits and model upgrades must not reach the RM or the code that calls models.

## Decision
- Every model call goes through one model gateway; no agent calls a provider directly.
- Agents ask for a tier (classify, extract, write); the gateway picks the smallest model that can do the step (cascade).
- Primary, mini and fallback LLMs, all pinned versions and in-region.
- Token budgets per request and per RM, retries with backoff, fallback LLM on failure, degraded mode if both fail.
- Low temperature (0 to 0.2) and max-token limits set per step, not per caller.

## Trade-offs
| Gain | Cost | How we contain the cost |
|---|---|---|
| Model-agnostic; swap or upgrade without code change | One more hop and component to run | Managed gateway; latency tracked per step |
| Lower cost per request via routing | Routing errors send hard steps to a small model | Output checks plus one stricter retry on the primary |
| Survives provider outage | Fallback answers may differ in quality | Fallback model covered by the release gate |

## Alternatives considered
- Single model for every step: simpler, but higher cost and one point of failure.

## Where in code
- `src/copilot/execution/model_gateway.py`
- `src/copilot/operations/resilience.py` (retries, circuit breaker), `src/copilot/operations/flags.py` (kill switches)
- Prototype scope: models are named per tier in the gateway; version pinning is deployment configuration

## Azure implementation
Azure API Management as AI gateway (token limits, load balancing, backend failover) over regional Azure OpenAI deployments in UAE North.
