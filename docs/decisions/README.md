# Architecture decision records

SME RM Copilot (fictional bank). Source: approved architecture review, section 4.1.

Guiding trade-off: autonomy vs reliability vs efficiency, pick two. We choose **reliability + efficiency**; autonomy is limited on purpose (the copilot assists, people decide and approve).

| # | Decision | Summary | Not chosen |
|---|---|---|---|
| 01 | [Model gateway](01-model-gateway.md) | Model-agnostic gateway with per-step routing, budgets and fallback | Single model |
| 02 | [MCP tool gateway](02-mcp-tool-gateway.md) | Only path to systems of record; OIDC + RBAC + agent + workload identity on every call | Direct API calls |
| 03 | [Hybrid RAG](03-hybrid-rag.md) | BM25 + vector with metadata filters, clause chunks, rerank, retrieval gate | Vector-only, fine-tuning |
| 04 | [Groundedness](04-groundedness.md) | Cited policy answers, numbers from source, output checks before release | Free output |
| 05 | [Human in the loop](05-human-in-the-loop.md) | RM approval on writes, credit and pricing; signed single-use 5-minute token; outbox | Autonomous actions |
| 06 | [Specialised agents](06-specialised-agents.md) | Brief, policy and follow-up agents; own tools; hub and spoke | One general agent |
| 07 | [Layered memory](07-layered-memory.md) | Checkpointer, session 24 h, episodic 90 days, procedural versioned | Stateless |
| 08 | [Plan-and-execute](08-plan-and-execute.md) | Router fast path plus LLM planner in a bounded loop | Fixed workflow |
| 09 | [Regional deployment](09-regional-deployment.md) | UAE North, regional model deployments, private network | Global endpoints |
| 10 | [Evals](10-evals.md) | Runtime gates, online RAGAS sampling, golden-set release gate | Manual testing |
