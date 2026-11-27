# 07. Layered memory

Status: Accepted · Date: 27 Nov 2026

## Context
Follow-up actions pause for approval and resume; RMs ask follow-up questions during one meeting prep; briefs need continuity across meetings; prompts and tools must behave consistently and be auditable.

## Decision
- Checkpointer: state after every step, so a flow can pause for RM approval and resume.
- Session: recent turns, 24 hours (production adds a rolling summary).
- Episodic: RM-approved meeting summaries per customer, 90 days.
- Procedural: versioned prompts, tool schemas and the intent table, pinned and released through the gate.
- All memory is per RM, encrypted, never shared across RMs. Semantic memory is the policy index (see 03); RM profile memory is out of scope for now.

## Trade-offs
| Gain | Cost | How we contain the cost |
|---|---|---|
| Resumable approvals and follow-ups | Store to run and secure | Managed store, per-RM partition |
| Briefs remember past meetings | Personal data retained | Only RM-approved summaries; 90-day TTL |
| Auditable, consistent behaviour | Version discipline on prompts | Change control and golden-set gate |

## Alternatives considered
- Stateless: simplest, but no pause and resume for approvals and no continuity between meetings.

## Where in code
- `src/copilot/state/memory/` (checkpointer, session, episodic, procedural)
- `src/copilot/execution/mcp_servers/` (memory)

## Azure implementation
Cosmos DB with per-RM partitions, TTL (24 h, 90 days) and customer-managed keys; App Configuration pins procedural versions.
