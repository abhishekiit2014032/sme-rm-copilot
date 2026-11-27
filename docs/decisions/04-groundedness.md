# 04. Groundedness

Status: Accepted · Date: 27 Nov 2026

## Context
A wrong limit, fee or policy statement given to an SME customer is a conduct and credit risk. Target: RAGAS faithfulness of at least 0.90 and 100% numeric fidelity.

## Decision
- Every policy answer cites its clause; an uncited claim is not released.
- Numbers are filled from the source (system of record or clause), never generated; every figure carries provenance and an as-of timestamp.
- Structured output validated against Pydantic schemas.
- Output guardrails before release: faithfulness and relevancy, citation validation, number match, policy and never-say check, PII and entitlement check.
- On a failed check: one stricter retry, otherwise a safe decline. Answers are buffered and verified, not streamed.

## Trade-offs
| Gain | Cost | How we contain the cost |
|---|---|---|
| Nothing unverified reaches the RM | Higher perceived latency (no streaming) | p95 SLO of 8 s; checks run per step in parallel |
| Every claim traceable to a clause | Judge model cost on every answer | Judge calls inside the model gateway token budget |
| No invented figures | Less fluent, more templated answers | Prompt design and few-shot examples |

## Alternatives considered
- Free output with disclaimers: fast and fluent, but no control over invented numbers or policy.

## Where in code
- `src/copilot/request_path/output_guardrails.py`
- `src/copilot/execution/agents/` (structured outputs, citations)
- `src/copilot/operations/evals/` (runtime faithfulness and relevancy scoring)

## Azure implementation
Azure AI Content Safety groundedness detection and Azure AI Language PII, plus a judge model on Azure OpenAI.
