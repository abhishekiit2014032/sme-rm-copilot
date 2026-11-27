# 08. Plan-and-execute

Status: Accepted · Date: 27 Nov 2026

## Context
Most asks are specific ("Prepare brief", "Draft follow-up"). Some are multi-step: "Brief me on Gulf Star; can we go to 3.5m unsecured? If not, what collateral?" Cost and behaviour must stay predictable either way.

## Decision
- Router fast path: a specific ask (UI action or one clear intent) goes straight to one agent.
- Planner path: for multi-step or open asks the primary LLM plans typed tasks with dependencies; plans are validated against a schema.
- The orchestrator runs the plan step by step; a step can depend on an earlier answer ("if not, ...").
- Bounded loop: step limit, token and time budgets; early stop returns what is ready.
- Checkpoint at every pause for human approval.
- Prototype scope: steps run in sequence; production can run independent steps in parallel.

## Trade-offs
| Gain | Cost | How we contain the cost |
|---|---|---|
| Handles open, multi-step asks | Planner call adds latency and cost | Fast path skips the planner for most asks |
| Predictable cost and behaviour | Some complex asks cut short | Partial answer returned; RM can ask again |
| Auditable execution order | Planner errors possible | Schema validation; code enforces order, budgets and approvals |

## Alternatives considered
- Fixed workflow per button: predictable, but cannot handle combined or conditional asks.

## Where in code
- `src/copilot/request_path/router.py`, `src/copilot/request_path/orchestrator.py`
- `src/copilot/execution/agents/`

## Azure implementation
LangGraph on private AKS with the primary LLM (planner) and a mini model or rules (intent) on Azure OpenAI via APIM.
