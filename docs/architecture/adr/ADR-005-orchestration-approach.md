# ADR-005: Thin custom orchestrator with deterministic workflows

| Field | Value |
|---|---|
| Status | Amended by ADR-025 (supervisor-led agents on the same deterministic principles) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, engineering lead, model risk management |
| Plan references | §2 (patterns), §3.2 `orchestrator`, §4.1 to §4.3, §5 (deadlines, budgets), §6 C3, C10, C18, C23 |

## Context

The `orchestrator` runs intent routing, the UC1/UC2/UC3 workflows, deadlines and budgets, guardrails, placeholder
fill, numeric and policy checks, the groundedness check and degraded mode (plan §3.2). The three use cases are
**known shapes**, not open-ended tasks:

- UC1 is a fixed fan-out of read tools, one generation call, then deterministic fill and checks (§4.1).
- UC2 is a fixed RAG pipeline with one conditional retry (§4.3).
- UC3 is one extraction call, deterministic validation, a human approval and a deterministic write saga (§4.2).

Forces:

- **Explainability to audit and model risk** (C23): every step must be traceable and its behaviour testable without
  a model in the loop.
- **Hard latency deadlines** with hop timeouts derived from the remaining deadline and a "failover only if ≥ 4 s
  remain" rule (plan §5); budgets of ≤ 6 tool steps and token caps per request (C10).
- **Verify-then-release** (C18): output is buffered, checked and only then returned; no streaming of unverified text.
- **No stored conversation state at Microsoft** (`store=false`, plan §3.5); session state in our Cosmos DB (§7).
- **Prototype parity**: the existing Python prototype (`code/rm_copilot/orchestrator.py`) already implements the
  three workflows as plain functions over a typed tool catalogue with a mock LLM; the mock-Azure prototype must
  run the same logic (plan §8 WS-C).
- **Team**: a small Python team; the Bank's model risk team must be able to read the control flow.

## Options considered

1. **Thin custom orchestrator**: explicit async workflow functions per use case, a typed tool client for
   `tool-gateway`, a model client for the APIM AI gateway, a deadline and budget object passed through every call,
   guardrails as plain functions; no agent framework.
2. **Microsoft Agent Framework** (the successor that brings together Semantic Kernel and AutoGen) or **Semantic
   Kernel** as the orchestration library inside our service **[confirm Agent Framework GA status and Python
   parity]**.
3. **Azure AI Foundry Agent Service**: hosted agents with service-managed threads, tool calling and tracing.
4. **LangGraph**: graph-based state machine with nodes and checkpoints, inside our service.

## Trade-off analysis

| Criterion | 1 Thin custom | 2 Agent Framework / Semantic Kernel | 3 Foundry Agent Service | 4 LangGraph |
|---|---|---|---|---|
| Fit for fixed workflows | **Exact.** Workflows are code; no planner where none is needed | Good: has workflow constructs, but agent abstractions are the default path | **Weak.** Designed for model-driven tool selection; fixed workflows fight the abstraction | Good: explicit graphs map to the flows |
| Deadline and budget control | **Full.** One deadline object, per-hop timeouts, "≥ 4 s" rule as code | Possible via filters/middleware; must verify every model and tool call honours it | **Limited.** Run-level timeouts; per-hop control and the retry rule are inside the service **[confirm]** | Possible; per-node timeouts by our code |
| Verify-then-release, placeholder fill | Plain code after the model call | Plain code around framework calls | Post-processing outside the agent run; partial loss of the benefit | Nodes |
| Residency and data handling | Only our calls; `store=false` | Only our calls | **Concern.** Threads and messages stored by the service (or in our own resources with the standard setup **[confirm]**); regional availability in UAE North **[confirm]** | Only our calls; avoid hosted platform add-ons |
| Testability and explainability (MRM) | **Strongest.** Unit tests per step without a model; small code surface | Good, larger surface; framework behaviour to validate | **Weakest.** Control flow partly inside a managed service | Good; graph is inspectable, framework behaviour to validate |
| Observability | OTel spans we define (GenAI semantic conventions, content off) | Built-in OTel; must verify content capture can be disabled | Foundry tracing; content recording must be off (plan §3.3) | OTel via integrations; hosted tracing products not allowed |
| Lock-in | Low | Moderate (Microsoft library) | **High** (managed service API) | Moderate (library) |
| Dependency churn | None beyond SDKs | Moderate: fast-moving, recent consolidation | Moderate: service API versions | **High**: frequent releases |
| Team skills | Python only | Learn the framework | Learn the service | Learn the framework |
| Time to pilot | **Fast**: prototype logic already exists | Moderate | Fast for a demo, slower to meet deadlines, residency and audit requirements | Moderate |
| Future open-ended agents (Phase 3) | Must build planning loops ourselves | **Strong** | Strong | Strong |

## Decision

Adopt **option 1: a thin custom orchestrator**.

- Each use case is an explicit, deterministic workflow function (UC1 fan-out, UC2 RAG with one conditional retry,
  UC3 extract-validate-approve-commit), matching plan §4 step for step.
- The model is used only where language is needed (UC1 wording with placeholders, UC2 rewrite and answer, UC3
  extraction). There is no model-driven planner in Phase 1 or 2; the "≤ 6 tool steps" budget remains as a guard
  for future open questions.
- A `RequestContext` object carries the deadline, budgets, request id, `traceparent` and the RM token through every
  call; every hop timeout is min(own cap, remaining deadline − reserve).
- The orchestrator holds no keys and never mints or edits approval tokens (ADR-009).
- Frameworks may be used as **libraries** for narrow functions (for example a schema validator or a tokenizer) but
  do not own control flow.

## Consequences

**Positive**
- The control flow that model risk and audit review is the code that runs; each guardrail and branch is
  unit-testable with a mock model, as in the existing prototype.
- Precise deadline, budget and degraded-mode behaviour, which the latency targets in plan §5 depend on.
- No framework or service lock-in; no hidden state outside our Cosmos DB.

**Negative**
- We own more code: retries, timeouts, tool schemas, prompt templates and tracing conventions.
- We do not get framework features for free (memory, multi-agent patterns, built-in evaluators, tool schema
  generation).
- Hiring and onboarding cannot rely on a well-known framework's conventions.

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| Custom code grows into an ad-hoc framework | Size limit and architecture review on the orchestrator package; reuse SDK primitives (Azure OpenAI client, OTel) rather than re-implementing |
| Inconsistent tracing across steps | One span helper implementing GenAI semantic conventions with content capture off; CI test on span attributes (plan §3.3 Telemetry) |
| Phase 3 needs genuine multi-step planning | Review trigger below; an Agent Framework workflow can be introduced behind the same tool contracts and eval gate |

## Review trigger

- A use case requires open-ended, model-driven planning over more than a handful of tools (Phase 3).
- Microsoft Agent Framework (or successor) is GA with Python parity, deterministic workflow support, per-step
  timeouts and content-free tracing, and the team has capacity to adopt it.
- Foundry Agent Service is available in UAE North with storage in Bank-owned resources and per-step deadline
  control **[confirm]**.

## Related

- ADR-003 APIM AI gateway; ADR-008 identity (token forwarding); ADR-009 approval token; ADR-011 guardrails and
  verified release; ADR-015 degraded mode; ADR-022 no unverified streaming.
- `01-hld.md` §5, §8; `02-lld.md` (workflow and context contracts); plan §3.2, §4, §5.

## Amendment (26 Nov 2026, ADR-025)

Plan v2.6 replaces the single orchestrator with a supervisor-led fixed graph of agents (`supervisor`,
`brief-agent`, `policy-agent`, `followup-agent`) run by a graph framework with a Cosmos DB checkpointer inside the
same `orchestrator` deployment. What changes: each agent has its own identity and tool roles (ADR-024) and reaches
tools through the MCP gateway (ADR-023); a framework, not our own functions, executes the graph; and graph state is
checkpointed (ADR-026) instead of the orchestrator holding no state. What stays: the UC1 to UC3 workflows inside each
agent, the deadline and budget object, verification in code, verify-then-release, no planner beyond one routing
decision (at most 2 agent hops), and no keys in the orchestrator for approvals.
