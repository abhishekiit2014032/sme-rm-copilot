# ADR-025: Supervisor-led multi-agent orchestration on a fixed graph

| Field | Value |
|---|---|
| Status | Accepted (graph framework choice Proposed until the Phase 0 spike, see Decision step 6) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, engineering lead, model risk management, information security |
| Plan references | §2 (patterns), §3.2 `orchestrator`, §3.7 (agents), §3.8 (checkpointer), §4.1 to §4.3, §5 (latency, budgets), §6 C3, C10, C18, C23, C24, C28, C29 |
| Amends | ADR-005 (thin custom orchestrator with deterministic workflows) |

## Context

ADR-005 chose a thin custom orchestrator: three deterministic workflow functions, one service identity, no agent
framework. Plan v2.6 keeps the deterministic principles but asks for agents, for three reasons:

- **Least privilege per task.** In one orchestrator, every workflow runs with the same identity and could call
  every tool. UC2 reads policy documents (untrusted text) in a process that can also read customer data. Splitting
  work into agents with their own identities (ADR-024) and tool sets (ADR-023) makes that separation enforceable at
  `tool-gateway`.
- **Human-in-the-loop as a first-class pause.** UC3 stops for the RM's approval and resumes minutes or days later.
  ADR-005 handles this with the `drafts` container and a separate commit call; a graph with a checkpointer makes the
  pause, resume and replay explicit and testable.
- **Growth.** Phase 2 and 3 add use cases (other segments, more write actions). A supervisor that routes to bounded
  specialists scales better than one growing workflow module.

Forces that do not change: hard deadlines and budgets (≤ 6 tool steps, ≤ 12k input and 1.5k output tokens,
plan §5); verify-then-release (C18); explainability to model risk (C23); `store=false` and no hosted state at
Microsoft (§3.5); latency targets UC1 ≤ 8 s, UC2 ≤ 7 s, UC3 draft ≤ 10 s.

**Compounding error.** Each model-mediated step can misread intent, drop context or call the wrong tool. If each
step is right with probability p and errors are independent (a simplification; real errors correlate), the chance
that a chain of n steps is right is pⁿ:

| Model-mediated hops n | p = 0.95 | p = 0.98 |
|---|---|---|
| 1 | 0.950 | 0.980 |
| 2 | 0.903 | 0.960 |
| 3 | 0.857 | 0.941 |
| 4 | 0.815 | 0.922 |
| 6 | 0.735 | 0.886 |
| 10 | 0.599 | 0.817 |

Deterministic verification (numeric, policy and groundedness checks, ADR-011) catches wrong numbers and forbidden
promises, but not "answered a different question" or "skipped a section". So the number of model-mediated handoffs
must be small and fixed. This is C28.

## Options considered

1. **Supervisor-led fixed graph**: `supervisor`, `brief-agent`, `policy-agent`, `followup-agent` (plan §3.7);
   verification in code; commit is not an agent tool; at most 2 agent hops per request; run in a graph framework
   with a checkpointer, in code, on AKS.
2. **Single orchestrator (ADR-005 unchanged)**: one service, one identity, three workflow functions.
3. **Autonomous peer-to-peer multi-agent (group chat)**: agents converse and decide among themselves who speaks next
   and when the task is done.
4. **Foundry Agent Service connected agents**: a main agent in Azure AI Foundry Agent Service delegates to connected
   sub-agents, with tools attached in the service **[confirm connected agents and MCP tool support, private
   networking, thread storage location and availability in UAE North]**.
5. **One agent per tool**: a micro-agent for each tool (profile, facilities, transactions, KYC, news, policy search,
   drafts, memory), coordinated by a planner.

## Trade-off analysis

| Criterion | 1 Supervisor-led fixed graph | 2 Single orchestrator (ADR-005) | 3 Peer-to-peer group chat | 4 Foundry connected agents | 5 One agent per tool |
|---|---|---|---|---|---|
| Compounding error (C28) | **Bounded.** ≤ 2 model-mediated hops; p² ≈ 0.90 at p = 0.95 before verification | **Best.** One model step per use case | **Unbounded.** Turn count chosen by the models; p falls with every turn | Depth chosen by the main agent's model; bounded only by service limits **[confirm]** | **Worst.** UC1 alone would be 6+ hops (p⁶ ≈ 0.74) |
| Least privilege and injection containment | **Strong.** Each agent has its own identity and 1 to 2 tool roles (ADR-024) | Weak: one identity for all tools | Weak: agents share context; an injected agent can address others | Medium: per-agent tools in the service; mapping to our identities unclear **[confirm]** | Strong per agent, but the planner sees everything |
| Determinism, testability, MRM (C23) | **Good.** Fixed edges in code; one routing decision; each node testable with a mock model | **Best.** Plain functions | **Poor.** Emergent control flow; hard to validate or reproduce | Poor to medium: control flow inside a managed service | Medium: many nodes, planner is a model |
| Latency | **+0 to about 600 ms** p95: routing by rule when the entry point carries the intent, one `gen-small` call otherwise; async checkpoints | **Lowest** | High and variable | Service round trips per delegation; per-step deadlines not ours **[confirm]** | **High**: a model call per tool |
| Cost (calls and tokens) | Small increase: one routing call for free text, agent system prompts, checkpoint writes | **Lowest** | High: every turn re-reads the conversation | Medium: per-agent runs and stored threads | High: a model call per tool plus planning |
| Human-in-the-loop pause and resume | **Native.** Interrupt and resume from a checkpoint | Works through `drafts` and a separate commit call | Awkward: no single point to pause | Supported through threads; state held in the service **[confirm]** | Possible, more state |
| Residency and data handling | **Ours.** State in our Cosmos DB, encrypted (ADR-026) | Ours | Ours if self-hosted | Thread storage in the service or Bank resources **[confirm]** | Ours |
| Extensibility (Phase 2 and 3) | **Good.** New specialist = new node, identity and tool roles | Poor: one module grows | Easy to add, hard to control | Good inside Foundry | Good per tool, poor overall |
| Operational and framework risk | Medium: a graph framework dependency and a checkpointer to run | **Low** | High | Medium to high: managed service lock-in | High |

## Decision

Adopt **option 1**, on the same deterministic principles as ADR-005.

1. **Agents** (plan §3.7):

   | Agent | Job | Tools (MCP, ADR-023) | Model |
   |---|---|---|---|
   | `supervisor` | Classify intent, pick one specialist (or decline), enforce step, time and token budgets, assemble the reply | none | `gen-small`, or no model call when the entry point carries the intent |
   | `brief-agent` | UC1 customer brief | `customer-data` (read), `memory` (recall) | `gen-large` |
   | `policy-agent` | UC2 cited policy answer | `policy-search` only | `gen-small` rewrite, `gen-large` answer |
   | `followup-agent` | UC3 notes to drafts | `customer-data` (profile), `crm-drafts` (create, no commit) | `gen-small` |

2. **Fixed graph, at most 2 agent hops.** An agent hop is one agent node that makes a model call. A request runs
   `supervisor` → one specialist → deterministic verification → `supervisor` assembly (code, no model call). The
   supervisor hands off **once**; specialists never call each other or talk freely; edges are defined in code, and
   the supervisor's only model-chosen edge is a choice among an enumerated set (`brief`, `policy`, `followup`,
   `decline`). A request with two intents is answered for the first and offers the second as a follow-up turn.
3. **Inside each specialist, the plan §4 workflows are unchanged**: UC1 one fan-out call (`get_brief_inputs`), wording
   with `{placeholders}`, placeholder fill; UC2 rewrite, retrieval, answer, one conditional retry; UC3 extraction
   and validation. A specialist may choose further calls only among its own allow-listed tools and within the ≤ 6
   tool step budget.
4. **Verification is code, not an agent.** Numeric, policy and groundedness checks (ADR-011) run as deterministic
   nodes after the specialist; failing lines are dropped or the answer declines; nothing unverified is released
   (ADR-022).
5. **Commit is not an agent tool.** UC3 pauses at an `await_approval` interrupt (checkpointed, ADR-026). After the RM
   approves, `web-bff` mints the approval token (ADR-009) and resumes the thread; a deterministic commit node calls
   the write API. No model sees or calls the write API.
6. **Framework.** The graph runs in code inside the `orchestrator` deployment on AKS, using a graph-based framework
   with a checkpointer: **Microsoft Agent Framework workflows** if it passes the Phase 0 spike, else **LangGraph**
   **[confirm Agent Framework GA status and Python parity]**. Pass criteria: statically defined edges; per-node
   timeouts driven by our `RequestContext` deadline; interrupt and resume; a custom checkpointer on Cosmos DB with
   our encryption (ADR-026); OpenTelemetry spans with content capture off; no hosted service dependency. Graph
   definitions sit behind our own thin interface so the framework can be swapped without changing the graphs'
   behaviour or tests.
7. **Checkpoint durability.** Synchronous checkpoint at interrupts and at the end of a request; asynchronous
   elsewhere, so a lost intermediate checkpoint costs replay detail, not correctness.
8. **ADR-005 principles carried over**: `RequestContext` with deadline, budgets, request id and `traceparent`
   through every node; hop timeout = min(own cap, remaining deadline − reserve); degraded mode (C3); the runtime
   holds no signing key; `store=false` on every model call.

**Latency and cost impact.**
- UC1 from the CRM record and UC2 from the policy entry point: the intent is known, the supervisor routes by rule,
  no added model call. Budget unchanged (UC1 ≈ 6,500 ms, UC2 ≈ 5,900 ms p95, plan §5).
- Free-text requests: one `gen-small` routing call, up to about 600 ms p95 (the same order as the plan's router
  line). For UC2 this brings the p95 near 6,500 ms against the 7,000 ms target: thin but inside. Plan §5's latency
  table needs a "supervisor routing (free text)" row **[plan owner to add]**.
- Checkpoint writes: a few small Cosmos point writes per request, mostly asynchronous; agent tokens cached.
- Cost: one short extra model call for free text, a few hundred extra prompt tokens per agent system prompt, Cosmos
  request units for checkpoints. No change to the per-RM daily token limit (plan §5).

## Consequences

**Positive**
- Least privilege per agent, enforced at `tool-gateway`: an injected policy document cannot reach customer data;
  news text read by `brief-agent` cannot create drafts; nothing can commit.
- Error compounding is bounded by design (≤ 2 model hops) and residual errors meet deterministic checks.
- Approval pauses and resumes are explicit, checkpointed and replayable for incident analysis (PIM-gated, C29).
- New use cases arrive as new specialists with their own identity, tool roles, eval set and kill switch.

**Negative**
- More moving parts than ADR-005: a framework, a checkpointer, four identities, routing evaluation.
- A framework now executes our control flow, which ADR-005 avoided; its behaviour must be validated by MRM and
  pinned like any dependency.
- Free-text requests pay a routing call; composite questions need two turns.
- Checkpoints store customer data at rest (C29), a new data store to protect, retain and erase (ADR-026).
- Phase 1 runs all agents in one pod, so the workload layer does not separate agents (ADR-024).

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| Supervisor misroutes | Routing eval set in the golden set (ADR-018) with a release gate; rule routing for entry points; `decline` with a menu when confidence is low |
| Graph grows hops over time | Hop count asserted in CI per graph; plan rule "at most 2 agent hops"; a third hop needs a new ADR |
| Framework churn or defect | Pinned version; thin interface; graph tests run against a mock model; fallback to ADR-005 style functions behind the same interface |
| Checkpointer adds latency | Async writes except at interrupts; p95 per node in Grafana; budget alert at 50 ms per request |
| Resume with a stale graph version | Checkpoints carry the graph version; incompatible resumes restart from the draft of record in `drafts` |
| Agents drift into free conversation | No agent-to-agent channel exists in the graph; MRM review of every new edge |

## Review trigger

- Measured UC2 free-text p95 above 6,800 ms in the pilot (merge routing into the specialist's first call or route
  more by rule).
- Routing accuracy below the gate on the golden set, or composite-question rate above an agreed threshold.
- A use case that genuinely needs more than 2 hops or open-ended planning (Phase 3): new ADR.
- Foundry Agent Service connected agents available in UAE North with Bank-owned storage, per-step deadlines and our
  identities **[confirm]**.
- Agent Framework fails the Phase 0 spike or LangGraph's licence or release cadence becomes unacceptable.

## Related

- ADR-005 (amended by this ADR); ADR-011 guardrails; ADR-018 evaluation (routing gate); ADR-020 kill switch per
  agent; ADR-022 no unverified streaming; ADR-023 MCP gateway; ADR-024 identities; ADR-026 memory and checkpoints.
- Plan §3.7, §3.8, §5, §6 C28, C29.
