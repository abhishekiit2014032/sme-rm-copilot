# ADR-025: Multi-agent orchestration: deterministic orchestrator instead of an LLM supervisor

| Field | Value |
|---|---|
| Status | Accepted (graph framework choice Proposed until the Phase 0 spike, see Decision step 7; Supervisor (LLM planner) recorded as a Phase 2 option) |
| Date | 26 Nov 2026; revised 27 Nov 2026 (plan v2.9 names and waves rule) |
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
- **Growth.** Phase 2 and 3 add use cases (other segments, more write actions). An orchestrator that hands tasks to
  bounded specialists scales better than one growing workflow module.
- **Naming (plan v2.9).** Reviewers read "supervisor" as a model that plans and loops, and "router" as code. Earlier
  drafts used them the other way round (a model "router", a code "supervisor"). This ADR uses the usual meanings:
  the **intent classifier** understands the request, the **orchestrator** is code that controls execution, the
  **specialist agents** reason, and a **Supervisor (LLM planner)** is a model that plans, which we do not adopt for
  the pilot.

Forces that do not change: hard deadlines and budgets (≤ 6 tool steps, ≤ 12k input and 1.5k output tokens,
plan §5); verify-then-release (C18); explainability to model risk (C23); `store=false` and no hosted state at
Microsoft (§3.5); latency targets UC1 ≤ 8 s, UC2 ≤ 7 s, UC3 draft ≤ 10 s.

**Compounding error.** Each model-mediated step can misread intent, drop context or call the wrong tool. If each
step is right with probability p and errors are independent (a simplification; real errors correlate), the chance
that a chain of n steps is right is pⁿ:

| Model-mediated steps in a chain n | p = 0.95 | p = 0.98 |
|---|---|---|
| 1 | 0.950 | 0.980 |
| 2 | 0.903 | 0.960 |
| 3 | 0.857 | 0.941 |
| 4 | 0.815 | 0.922 |
| 6 | 0.735 | 0.886 |
| 10 | 0.599 | 0.817 |

Deterministic verification (numeric, policy and groundedness checks, ADR-011) catches wrong numbers and forbidden
promises, but not "answered a different question" or "skipped a section". So the number of model-mediated steps in
any chain must be small and fixed, and the control flow between them should not itself be a model step. This is C28.

## Options considered

Two questions are decided together: how the work is split into agents (A), and who controls execution once the
request is understood (B).

**A. Agent structure**
1. **Orchestrator with specialist agents on fixed graphs**: intent classifier, deterministic orchestrator in code,
   `brief-agent`, `policy-agent`, `followup-agent` (plan §3.7); verification in code; commit is not an agent tool;
   at most 2 waves per request; run in a graph framework with a checkpointer, in code, on AKS.
2. **Single orchestrator (ADR-005 unchanged)**: one service, one identity, three workflow functions.
3. **Autonomous peer-to-peer multi-agent (group chat)**: agents converse and decide among themselves who speaks next
   and when the task is done.
4. **Foundry Agent Service connected agents**: a main agent in Azure AI Foundry Agent Service delegates to connected
   sub-agents, with tools attached in the service **[confirm connected agents and MCP tool support, private
   networking, thread storage location and availability in UAE North]**.
5. **One agent per tool**: a micro-agent for each tool (profile, facilities, transactions, KYC, news, policy search,
   drafts, memory), coordinated by a planner.

**B. Who controls execution**
- **B1. Fixed intent table in code (deterministic orchestrator).** The intent classifier returns tasks; code plans
  them from a versioned intent table (read or write, dependencies, budget share) into at most 2 waves.
- **B2. Model plans, code validates (Supervisor as LLM planner).** A model proposes the plan as JSON; the orchestrator
  validates it against the intent table and falls back to the fixed table if it is invalid.
- **B3. Free ReAct loop (LLM supervisor).** A model decides at every step which agent or tool to call next, and when
  to stop.

## Trade-off analysis

**A. Agent structure**

| Criterion | 1 Orchestrator with specialist agents | 2 Single orchestrator (ADR-005) | 3 Peer-to-peer group chat | 4 Foundry connected agents | 5 One agent per tool |
|---|---|---|---|---|---|
| Compounding error (C28) | **Bounded.** The orchestrator adds no model step; a chain has at most the intent classifier (free text only) and one specialist per wave, and the pilot runs a single wave (p² ≈ 0.90 at p = 0.95 before verification) | **Best.** One model step per use case | **Unbounded.** Turn count chosen by the models; p falls with every turn | Depth chosen by the main agent's model; bounded only by service limits **[confirm]** | **Worst.** UC1 alone would be 6+ model steps (p⁶ ≈ 0.74) |
| Least privilege and injection containment | **Strong.** Each specialist has its own identity and 1 to 2 tool roles (ADR-024); the orchestrator has none | Weak: one identity for all tools | Weak: agents share context; an injected agent can address others | Medium: per-agent tools in the service; mapping to our identities unclear **[confirm]** | Strong per agent, but the planner sees everything |
| Determinism, testability, MRM (C23) | **Good.** Fixed edges in code; one classification decision; each node testable with a mock model | **Best.** Plain functions | **Poor.** Emergent control flow; hard to validate or reproduce | Poor to medium: control flow inside a managed service | Medium: many nodes, planner is a model |
| Latency | **+0 to about 600 ms** p95: classification by rule when the entry point carries the intent, one `gen-small` call otherwise; independent reads run concurrently; async checkpoints | **Lowest** | High and variable | Service round trips per delegation; per-step deadlines not ours **[confirm]** | **High**: a model call per tool |
| Cost (calls and tokens) | Small increase: one classification call for free text, agent system prompts, checkpoint writes | **Lowest** | High: every turn re-reads the conversation | Medium: per-agent runs and stored threads | High: a model call per tool plus planning |
| Human-in-the-loop pause and resume | **Native.** Interrupt and resume from a checkpoint; writes run last and alone | Works through `drafts` and a separate commit call | Awkward: no single point to pause | Supported through threads; state held in the service **[confirm]** | Possible, more state |
| Residency and data handling | **Ours.** State in our Cosmos DB, encrypted (ADR-026) | Ours | Ours if self-hosted | Thread storage in the service or Bank resources **[confirm]** | Ours |
| Extensibility (Phase 2 and 3) | **Good.** New specialist = new node, identity, tool roles and intent table row | Poor: one module grows | Easy to add, hard to control | Good inside Foundry | Good per tool, poor overall |
| Operational and framework risk | Medium: a graph framework dependency and a checkpointer to run | **Low** | High | Medium to high: managed service lock-in | High |

**B. Who controls execution**

| Criterion | B1 Fixed intent table in code | B2 Model plans, code validates (Phase 2) | B3 Free ReAct loop |
|---|---|---|---|
| Ordering and writes | **Guaranteed.** Reads before writes, writes last and alone, after approval | Guaranteed by validation; an invalid plan falls back to B1 | Not guaranteed: the model may write early or twice |
| Budgets (12k tokens, the request deadline, ≤ 6 tool steps) | **Split and enforced by code** (60/40 share between two tasks) | Enforced by code; the plan only proposes | Checked per step at best; the loop decides when to stop |
| Model calls added | **None** beyond the intent classifier | One planning call per multi-task request | One per step, unbounded without a cap |
| Compounding error (C28) | **Lowest:** control flow is not a model step | Low: one planning step, validated | High: every step is model-mediated |
| Audit and MRM (C23) | **Simple:** the plan is a function of the task list and the table version | Medium: the proposed and validated plans are both logged | Hard: control flow differs per run |
| Flexibility for new or unusual requests | Limited to rows in the table; anything else is clarified or offered as a follow-up | **Good** within the table's limits | Highest |
| Latency | **Lowest** | + one `gen-small` call (about 600 ms p95) | High and variable |

## Decision

Adopt **A1 with B1**, on the same deterministic principles as ADR-005. **We deliberately use a deterministic
orchestrator instead of an LLM supervisor: the model understands the request (intent classifier) and the specialist
agents reason, but code controls execution, so ordering, budgets and approvals are guaranteed and auditable. An LLM
supervisor (planner) is a Phase 2 option, with the orchestrator still validating every plan.**

1. **Components** (plan §3.7):

   | Component | Job | Tools (MCP, ADR-023) | Identity | Model |
   |---|---|---|---|---|
   | Intent classifier | Rules first, then `gen-small` structured output `{tasks: [{id, intent, query, depends_on}], confidence}` for free text; ≥ 0.8 proceeds, 0.5 to 0.8 clarifies, < 0.5 or `out_of_scope` declines; decides cache eligibility (public policy intents only); one decision, no loop | none | the `orchestrator` workload identity (AI gateway only) | `gen-small`, or no model call when the entry point carries the intent |
   | Orchestrator | Plan from the intent table, run the waves, split and enforce step, time and token budgets, read typed results (`ok`, `partial`, `declined`, `needs_approval`, `error`) and act by fixed rules, checkpoint, pause for approval, combine results | none | the `orchestrator` workload identity; **no agent identity**, cannot obtain agent or tool tokens | none: never calls or picks a model, never writes to the CRM |
   | `brief-agent` | UC1 customer brief | `customer-data` (read), `memory` (recall) | `rmc-agent-brief` | `gen-large` |
   | `policy-agent` | UC2 cited policy answer | `policy-search` only | `rmc-agent-policy` | `gen-small` rewrite, `gen-large` answer |
   | `followup-agent` | UC3 notes to drafts | `customer-data` (profile), `crm-drafts` (create, no commit) | `rmc-agent-followup` | `gen-small` |

2. **Waves, not hops.** At most **2 waves** per request; independent read tasks run concurrently in wave 1 (tasks for
   the same agent are bundled into one call); dependent tasks (conditional, for example only if the first answer is
   "no") and write tasks run in wave 2; a write runs last and alone, with RM approval. Agents never call each other:
   each reports a typed result back to the orchestrator; edges are defined in code. The intent classifier's only
   model-chosen output is a task list over an enumerated set of intents, validated against the intent table before
   anything runs. Detail (schema, intent table, wave planning, result-status table): `02-lld.md` §15.1. **Pilot:** the
   prototype runs one task per request, a single wave (the orchestrator plus one specialist); multi-task waves are the
   production design.
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
6. **Phase 2 option: Supervisor (LLM planner), B2.** Not in the pilot. A model may propose the plan as JSON; the
   orchestrator validates it against the intent table (known agents, ≤ 2 waves, writes last, valid dependencies,
   budget shares) and falls back to the fixed table if it is invalid. Both the proposed and the executed plan are
   logged. Adoption needs the review trigger evidence below, its own eval gate and MRM sign-off; B3 (a free ReAct
   loop) is rejected.
7. **Framework.** The graph runs in code inside the `orchestrator` deployment on AKS, using a graph-based framework
   with a checkpointer: **Microsoft Agent Framework workflows** if it passes the Phase 0 spike, else **LangGraph**
   **[confirm Agent Framework GA status and Python parity]**. Pass criteria: statically defined edges; per-node
   timeouts driven by our `RequestContext` deadline; interrupt and resume; a custom checkpointer on Cosmos DB with
   our encryption (ADR-026); OpenTelemetry spans with content capture off; no hosted service dependency. Graph
   definitions sit behind our own thin interface so the framework can be swapped without changing the graphs'
   behaviour or tests.
8. **Checkpoint durability.** Synchronous checkpoint at interrupts and at the end of a request; asynchronous
   elsewhere, so a lost intermediate checkpoint costs replay detail, not correctness.
9. **ADR-005 principles carried over**: `RequestContext` with deadline, budgets, request id and `traceparent`
   through every node; per-call timeout = min(own cap, remaining deadline − reserve); degraded mode (C3); the runtime
   holds no signing key; `store=false` on every model call.

**Latency and cost impact.**
- UC1 from the CRM record and UC2 from the policy entry point: the intent is known, the intent classifier uses rules,
  no added model call. Budget unchanged (UC1 ≈ 6,500 ms, UC2 ≈ 5,900 ms p95, plan §5).
- Free-text requests: one `gen-small` classification call, up to about 600 ms p95 (the plan §5 row "intent
  classification and query rewrite"). For UC2 this brings the p95 near 6,500 ms against the 7,000 ms target: thin
  but inside.
- Multi-task requests (production design): wave 1 tasks run concurrently, so a second independent read adds little
  wall-clock time; a dependent or write task in wave 2 runs within the same request deadline (60/40 split).
- Checkpoint writes: a few small Cosmos point writes per request, mostly asynchronous; agent tokens cached.
- Cost: one short extra model call for free text, a few hundred extra prompt tokens per agent system prompt, Cosmos
  request units for checkpoints. No change to the per-RM daily token limit (plan §5).

## Consequences

**Positive**
- Least privilege per agent, enforced at `tool-gateway`: an injected policy document cannot reach customer data;
  news text read by `brief-agent` cannot create drafts; nothing can commit.
- Error compounding is bounded by design (the orchestrator adds no model step; at most 2 waves) and residual errors
  meet deterministic checks.
- Ordering, budgets and approvals are enforced by code and reproducible from the task list and the intent table
  version, which is what model risk and audit need.
- Approval pauses and resumes are explicit, checkpointed and replayable for incident analysis (PIM-gated, C29).
- New use cases arrive as new specialists with their own identity, tool roles, eval set and kill switch.

**Negative**
- More moving parts than ADR-005: a framework, a checkpointer, three agent identities, intent classification
  evaluation.
- A framework now executes our control flow, which ADR-005 avoided; its behaviour must be validated by MRM and
  pinned like any dependency.
- Free-text requests pay a classification call; requests outside the intent table, or with more than 2 tasks, are
  clarified or offered as a follow-up turn rather than planned freely.
- Checkpoints store customer data at rest (C29), a new data store to protect, retain and erase (ADR-026).
- Phase 1 runs all agents in one pod, so the workload layer does not separate agents (ADR-024).

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| Intent classifier misclassifies or splits tasks wrongly | Classification eval set in the golden set (ADR-018) with a release gate; rules for entry points; clarify between 0.5 and 0.8, decline below 0.5 |
| Plans grow over time | Wave count and the no agent-to-agent rule asserted in CI per graph; plan rule "at most 2 waves per request"; a third wave needs a new ADR |
| Framework churn or defect | Pinned version; thin interface; graph tests run against a mock model; fallback to ADR-005 style functions behind the same interface |
| Checkpointer adds latency | Async writes except at interrupts; p95 per node in Grafana; budget alert at 50 ms per request |
| Resume with a stale graph version | Checkpoints carry the graph version; incompatible resumes restart from the draft of record in `drafts` |
| Agents drift into free conversation | No agent-to-agent channel exists in the graph; MRM review of every new edge |

## Review trigger

- Measured UC2 free-text p95 above 6,800 ms in the pilot (merge routing into the specialist's first call or route
  more by rule).
- Intent classification accuracy below the gate on the golden set, or the clarify and follow-up rate for
  multi-task requests above an agreed threshold (evidence for the Phase 2 Supervisor (LLM planner), B2).
- A use case that genuinely needs more than 2 waves or open-ended planning (Phase 3): new ADR.
- Foundry Agent Service connected agents available in UAE North with Bank-owned storage, per-step deadlines and our
  identities **[confirm]**.
- Agent Framework fails the Phase 0 spike or LangGraph's licence or release cadence becomes unacceptable.

## Related

- ADR-005 (amended by this ADR); ADR-011 guardrails; ADR-018 evaluation (intent classification gate); ADR-020 kill switch per
  agent; ADR-022 no unverified streaming; ADR-023 MCP gateway; ADR-024 identities; ADR-026 memory and checkpoints.
- Plan §3.7, §3.8, §5, §6 C28, C29.
