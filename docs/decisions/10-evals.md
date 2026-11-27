# 10. Evals

Status: Accepted · Date: 27 Nov 2026

## Context
No evals, no production. Every prompt, model or tool change can shift quality silently, and live answers drift as policies and data change. Targets: faithfulness at least 0.90, numeric fidelity 100%.

## Decision
- Runtime, every request: retrieved chunks scored against the relevance threshold before generation; answer scored for faithfulness and relevancy after; below threshold means decline or retry once.
- Online: RAGAS scoring (faithfulness, answer relevancy, context precision) on a 5% sample of live traces plus RM feedback; alert on a downward trend.
- Release gate: RAGAS benchmark runs on a golden set of real RM questions with grading criteria, rubric plus LLM-as-judge, spot-checked by humans; no pass, no deploy. Nightly runs as well.
- Canary rollout after the gate, one-click rollback; bad production traces become new golden-set cases.

## Trade-offs
| Gain | Cost | How we contain the cost |
|---|---|---|
| Quality measured, not assumed | Judge model cost | Online scoring on a 5% sample only |
| Regressions blocked before release | Slower release cycle | Automated gate in the pipeline |
| Golden set grows with real failures | Owner effort to keep it current | Named golden-set owner |

## Alternatives considered
- Manual testing: cannot cover every change, not repeatable, no runtime protection.

## Where in code
- `src/copilot/operations/evals/`
- `src/copilot/request_path/output_guardrails.py`, `src/copilot/execution/retrieval.py` (runtime gates)
- `src/copilot/operations/tracing.py` (traces scored by the runtime evals)
- Prototype scope: the judge is a lexical mock; 5% online sampling and nightly runs are production jobs

## Azure implementation
RAGAS with Azure AI Foundry evaluation, Langfuse and Azure Monitor for traces and alerts, gate in the release pipeline.
