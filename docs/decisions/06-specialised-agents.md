# 06. Specialised agents

Status: Accepted · Date: 27 Nov 2026

## Context
Three use cases need different data, tools and models: customer brief, policy and product Q&A, follow-up actions. A single agent with every tool widens the blast radius of injection or error.

## Decision
- One agent per use case: brief, policy and follow-up.
- Brief agent: customer data (read only), approved meeting history, product rules.
- Policy agent: policy search only.
- Follow-up agent: customer data (read), CRM drafts (create drafts only, no commit tool).
- Hub and spoke: agents talk only through the orchestrator, never to each other; each has its own identity and tool allow-list.
- Each agent runs a fixed sequence of typed tool calls within the orchestrator's step limit (production: a bounded tool loop per agent).

## Trade-offs
| Gain | Cost | How we contain the cost |
|---|---|---|
| Least privilege per agent | More prompts, identities and tests | Procedural memory versions them together |
| Smaller prompts, cheaper models per task | Orchestration code needed | Shared orchestrator (see 08) |
| Failures isolated to one agent | Cross-agent context passed explicitly | Orchestrator passes only what each task needs |

## Alternatives considered
- One general agent with all tools: simpler, but no tool boundaries, larger prompts and harder evaluation.

## Where in code
- `src/copilot/execution/agents/` (brief, policy, follow-up)
- `src/copilot/request_path/orchestrator.py`
- `src/copilot/execution/tool_gateway.py` (per-agent allow-lists)

## Azure implementation
LangGraph agents on private AKS, each with its own Entra service principal and APIM MCP gateway tool filter.
