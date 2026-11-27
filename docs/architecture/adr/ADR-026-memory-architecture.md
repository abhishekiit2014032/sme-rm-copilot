# ADR-026: Memory architecture: seven typed memories, partitioned by RM, no customer facts memorised

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, information security, data protection officer, records management, model risk management |
| Plan references | §3.3 (Cosmos DB, Key Vault, App Configuration), §3.5, §3.8 (memory), §4.1 to §4.3, §5 (token budget), §6 C5, C21, C26, C27, C29, §7 (retention) |
| Phase | 1 (pilot); episodic recall in UC1 behind a feature flag until the memory red-team cases pass |

## Context

The agents of ADR-025 need state at several time scales: within one request (graph state), across a pause for the
RM's approval (UC3), across a conversation, and across meetings with the same customer ("last time we agreed two
follow-up tasks"). They also need Bank knowledge and the "how to do the job" instructions. Each of these is a kind
of memory with a different owner, lifetime and risk.

Forces:

- **Freshness.** Customer data is read live as the RM (ADR-007). A memorised balance or KYC status is stale by
  definition and would bypass the numeric check's "figures only from tool output" rule.
- **Memory poisoning (C26).** Text an agent reads (news, policy documents, RM notes with quoted instructions) can
  carry injected instructions. If it is saved and recalled later, it returns as trusted context, possibly for a
  different request or customer.
- **Leakage across RMs or customers (C27).** Memory is a new path around `tool-gateway`'s entitlement check unless
  recall goes through it.
- **Checkpoints hold customer data (C29).** Graph state includes masked tool results.
- **Residency and Microsoft data handling** (§3.1, §3.5): `store=false`; no Assistants or stored-response features.
- **Erasure** (C21): customer erasure must be provable without breaking the audit hash chain; crypto-shredding is the
  established pattern (ADR-013).
- **Context window budget** (plan §5): ≤ 12k input and 1.5k output tokens per request.
- **Model risk (C23):** what the model is told must be versioned and evaluated; the model must not rewrite its own
  instructions.

## Options considered

1. **Typed, partitioned memory per plan §3.8**: seven memory types, each with its own store, scope, retention and
   writer; no customer facts memorised; recall through `tool-gateway` with an entitlement re-check.
2. **No memory (stateless)**: keep only the 24 h session of ADR-019; no checkpoints, episodes or profile.
3. **One vector store of all conversations**: embed every turn and tool result; semantic recall filtered by RM.
4. **Vendor-managed memory**: Foundry Agent Service threads (and any managed memory feature) hold conversation and
   agent state **[confirm features, storage location and availability in UAE North]**.
5. **Model-written long-term memory**: give agents a "remember" tool so the model decides what to store and recall.

## Trade-off analysis

| Criterion | 1 Typed, partitioned | 2 Stateless | 3 One vector store | 4 Vendor-managed | 5 Model writes memory |
|---|---|---|---|---|---|
| Usefulness and continuity | **Good.** Pause and resume, session summaries, customer episodes, preferences | **Poor.** Every request starts cold; UC3 resume relies on drafts only | High recall, low precision: old turns resurface out of context | Good within threads | High, unpredictable |
| Poisoning resistance (C26) | **Strong.** Only RM-approved or RM-authored content; quarantine before write; provenance | **Strongest**: nothing persists | **Weak.** Injected text is embedded and recalled by similarity | Medium: whatever is in the thread persists **[confirm controls]** | **Weakest.** An injection can instruct the model to save itself |
| Leakage across RMs and customers (C27) | **Strong.** Partition key = RM `oid` from the token; entitlement re-checked at recall | Strong | **Weak.** One index; a filter bug leaks across RMs | Medium: thread scoping is ours to get right | Medium |
| Freshness (no stale facts) | **Strong.** Pointers, not figures; facts re-read live | Strong | **Weak.** Old figures recalled as if current | Weak: thread history holds old tool results | Weak |
| Residency and Microsoft data handling | **Ours**, Cosmos DB in UAE North, CMK plus per-customer keys | Ours | Ours (AI Search in UAE North) | Conflicts with `store=false` and §3.5 unless held in Bank resources **[confirm]** | Ours |
| Erasure (C21) | **Crypto-shred** with the customer key plus TTLs | Trivial | **Hard.** Vectors and chunks per customer must be found and deleted; no per-customer encryption in the index | Through the service API **[confirm]** | Hard: free-form items |
| Auditability and provenance | **Every item** carries source, author, time and version | n/a | Weak: embeddings lose provenance unless added | Service-defined | Weak |
| Latency | Small: point reads by partition; recall is recency-ordered, no vector search | **None** | Embedding plus vector query per request | Service round trips | Model calls to decide what to store |
| Operational effort | Medium: four Cosmos containers, key handling, tests | **Lowest** | Medium | Low to run, high to evidence | Medium |
| Testability (MRM) | **Good.** Deterministic writers and recall | n/a | Medium | Low | **Poor** |

## Decision

Adopt **option 1**.

### The seven memory types

| Memory | Holds | Store | Scope and key | Retention | Written by | Read by |
|---|---|---|---|---|---|---|
| Working | Current request's graph state: messages, tool results, plan | In process, then checkpointed | One request | Request | Graph runtime | The running graph |
| Checkpointer | Graph state after every node; interrupt for approval and resume; replay | Cosmos DB `checkpoints`, encrypted | Hierarchical key `/rm_oid`, `/thread_id` (thread = RM session); one item per node | 7 days; 30 days while a UC3 draft is open | Graph runtime (`orchestrator` workload identity) | Graph runtime; PIM-gated replay tool |
| Session | Conversation turns and a rolling summary | Cosmos DB `sessions` (ADR-019) | RM `oid` + session | 24 h | `web-bff` persists; the summary is authored by the session summariser (after release, `gen-small`) and returned with the reply | `web-bff`, sent with each request |
| Episodic | What happened before with this customer: approved call reports, open tasks, briefs viewed, RM notes | CRM is the source of truth (read via tools); copilot episode log in Cosmos DB `episodes` | Hierarchical key `/rm_oid`, `/customer_id` | Episode log 90 days | `tool-gateway` only, on approved or RM-authored events | `brief-agent` via `memory.recall_episodes` |
| Semantic | Policies and products | AI Search, versioned and effective-dated (ADR-006) | Clearance filter | Policy lifecycle | Ingestion pipeline | `policy-agent` via `policy-search` |
| Procedural | Prompts, graph definitions, tool schemas and descriptions, few-shot examples, guardrail rules | Git and the prompt registry, pinned in App Configuration | Global, versioned | Versions kept indefinitely | Engineers, through the ADR-018 eval gate only | Graph runtime (read-only) |
| Profile | RM preferences: language, brief format, default customers | Cosmos DB `rm-profile` | `/rm_oid` | Until changed or the RM leaves | The RM, through `web-bff` | `web-bff`, sent with each request |

### Rules

1. **No customer facts are memorised.** Balances, limits, facilities, transactions, KYC status, ratings and news
   content are never written to session summaries, episodes or profile. Episodes hold **pointers** (CRM record ids,
   approval `jti`, brief id, audit event id), dates, controlled-vocabulary topics and task ids. Facts are re-read
   live through `customer-data` at the time of use; the numeric check accepts figures only from current tool output.
2. **No memory crosses RMs.** Every memory container is partitioned by the RM `oid` taken from the validated token
   (never from a body). There is no shared or team memory. A delegate (holiday cover) sees the customer's CRM
   history through the tools, not the primary RM's episode log.
3. **Provenance on every item**: `{source, author (RM oid or service), created_at, graph and prompt version, tool
   definition hash, approval jti if any}`.
4. **Approved-only episodic writes.** Episodes are written by `tool-gateway`, never by an agent: on
   `write_committed` (approved call report and tasks, pointers only), on a brief being delivered (brief id, date,
   sections; no content), and when the RM explicitly saves a short note in the UI (RM-authored, ≤ 500 characters).
   No MCP tool writes episodes; `memory` exposes `recall_episodes` only (ADR-023).
5. **Quarantine before any memory write.** Text that reaches a session summary or an RM note first passes Prompt
   Shields and the quoted-instruction quarantine rules and PII masking (ADR-011, ADR-012). Quarantined or
   flagged text is never written to memory (C26).
6. **Procedural memory changes only through the eval gate.** Agent pods have read-only access to App Configuration;
   there is no tool or code path by which a model changes prompts, graphs, tool descriptions or rules at runtime.
7. **Profile has no free text.** Language and format are enumerations; default customers are ids, re-checked for
   entitlement when used. The profile is therefore not an injection channel.
8. **Crypto-shred on erasure.** Checkpoint and episode payloads are encrypted (AES-GCM) with data keys wrapped by a
   per-customer key in a dedicated memory-keys Key Vault (separate from the audit content keys of ADR-013);
   checkpoints without a customer (UC2) use an environment key and, by construction, contain no customer data
   (`policy-agent` has no customer tools; the question is masked). Erasure deletes the customer's keys in both
   vaults; data becomes unreadable once the soft-delete and purge-protection window (≥ 7 days) ends, and TTLs remove
   the ciphertext anyway. Cosmos DB CMK applies at rest underneath.

### Context-window management

- **Budget per request** (plan §5: ≤ 12k input tokens), illustrative split: procedural prompt and tool definitions
  about 2.5k, tool facts about 5k, recalled episodes ≤ 1k (≤ 10 items), session summary ≤ 500 plus the last 4 turns
  ≤ 2k, remainder as headroom. The graph's state reducer decides what enters the prompt; the checkpoint holds more
  than the model ever sees.
- **Rolling summary.** When a session passes 6 turns or 3k tokens, the session summariser (a post-release job in the orchestrator service, not part of orchestration) summarises older
  turns with `gen-small` after the reply is released into a structured summary (topics, open questions, customer ids referenced, preferences stated),
  with figures replaced by references ("see brief b-123"). The summary passes the quarantine rules, carries
  provenance, and is never a source for figures or for policy citations.
- **Tool results** are dropped from the prompt after the node that used them, keeping source references.

### Recall with entitlement re-check

`brief-agent` calls `memory.recall_episodes(customer_id)`. `tool-gateway` applies ADR-023 steps 5 to 13, reads only
the partition of the token's `oid`, **re-checks entitlement now** (a customer moved to another RM since the episode
was written returns `NOT_ENTITLED`), returns at most 10 items by recency within 90 days, with provenance. The agent
runtime wraps recalled items in a delimited, untrusted-data block; open task status is re-read from the CRM. No
vector search: per RM and customer there are tens of items, and recency is explainable.

### Checkpoint-based human-in-the-loop

1. `followup-agent` produces drafts; the graph reaches `await_approval` and **interrupts**; the checkpoint is written
   synchronously, the thread is marked `awaiting_approval` and its TTL extends to 30 days. The draft of record stays
   in `drafts` (ADR-019); the checkpoint holds its id and ETag, not a second copy to keep in sync.
2. The RM edits and approves in the UI; `web-bff` enforces step-up and mints the approval token (ADR-009).
3. `web-bff` calls resume with the thread id, draft id, ETag and approval token, plus the RM's tool token.
4. The runtime loads the checkpoint and checks: thread `rm_oid` equals the `oid` of the token on the resume call;
   AES-GCM authentication passes (tamper evident); graph version compatible. Any failure: restart from the draft.
5. The deterministic commit node calls the write API; `tool-gateway` verifies the approval token independently. The
   checkpoint is never trusted for authorization.
6. Abandoned threads expire by TTL; unused approvals follow the ADR-019 sweep.

**Replay for debugging (C29)**: an engineer gets unwrap rights on the memory keys only through PIM (time-bound,
approved, with an incident reference); replay runs in a production-only tool inside the cluster, re-executes
deterministic nodes and takes model outputs from the checkpoint (no new model calls); checkpoints are never copied
to lower environments (synthetic data only outside prod, plan §3.3); each replay emits an audit event.

### Memory poisoning

| Path | Control |
|---|---|
| Injected news or document text lands in a session summary | Quarantine and Prompt Shields before the summary is written; summary has provenance; 24 h TTL; never a source for figures or citations |
| RM notes with quoted instructions saved as an episode | Only explicit RM-saved notes; same quarantine; masked; ≤ 500 characters; shown back to the RM as "your note of <date>" |
| A poisoned episode steers a later brief | Recalled items wrapped as untrusted data; `brief-agent` has no write or draft tool; verified release (ADR-011) |
| Insider changes procedural memory (a prompt or tool description) | Git four-eyes review, ADR-018 eval gate, pinned hashes (ADR-023), MRM notified of changes |
| Model tries to save its own memory | No such tool exists; `memory` exposes recall only |

The red-team suite (plan §5, ≥ 1,000 attacks) includes multi-turn and memory-poisoning cases, and a cross-RM recall
test runs in CI (C27). The RM can view and delete their own episodes and notes in the UI.

## Consequences

**Positive**
- Continuity across meetings without memorising facts that go stale or bypass the numeric check.
- One rule for leakage: every memory read is partitioned by the token's `oid` and, for customer memory, passes the
  same entitlement check as live data.
- UC3 pause and resume is explicit, tamper-evident and replayable under control.
- Erasure stays a key deletion, consistent with the audit design.

**Negative**
- Three new Cosmos containers (`checkpoints`, `episodes`, `rm-profile`), a summary field in `sessions`, and a
  memory-keys vault to run, size and back up. ADR-019's container table and its note that "the
  orchestrator holds no state" need updating **[ADR-019 owner]**.
- The `orchestrator` and `tool-gateway` workload identities need `wrapKey` / `unwrapKey` on the memory keys; plan
  §3.3's Key Vault row ("orchestrator: none") must be read as "no approval-signing role" **[plan owner to update]**.
- Episodes are deliberately thin: the copilot "remembers that" a call report was approved, not "what" it said, so
  recall costs a live CRM read.
- No cross-RM memory means holiday cover gets no copilot continuity beyond the CRM.
- RM-saved notes are free text and can still carry facts or opinions; they are labelled as notes, not facts.

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| Checkpoints grow large (tool results) and slow writes | State reducer stores masked, minimised facts only; item size alarm well below the Cosmos 2 MB item limit; async writes except at interrupts |
| Key Vault throttling on wrap and unwrap | One data key per thread and customer, cached in process for the thread's life; Key Vault limits checked in the load test **[confirm limits]** |
| A summary or note smuggles customer facts into memory | Summary prompt forbids figures; numeric scan replaces numbers with references; sampled review by MRM |
| Episode written before an approval is final | Writes keyed on `write_committed`, not on draft or approval events |
| Cross-RM recall through a bug | Partition key from the token only; CI test that a second RM recalls nothing; Sentinel alert on any cross-partition query |
| Stale graph version resumes an old checkpoint | Version check on resume; restart from the draft of record |

## Review trigger

- Records management or the data protection officer changes retention or erasure rules for any memory type.
- A use case needs team or shared memory (for example a team-lead view): new ADR, explicit consent and entitlement
  model.
- Foundry Agent Service memory or threads become available in UAE North with Bank-owned storage and per-customer
  keys **[confirm]**.
- A memory-poisoning red-team case succeeds, or the cross-RM test fails once.
- Episode volumes per RM and customer grow past what recency recall handles well (consider structured search, not a
  shared vector store).

## Related

- ADR-006 retrieval (semantic memory); ADR-007 customer data live; ADR-011 guardrails and quarantine; ADR-012 PII
  masking; ADR-013 audit and crypto-shredding; ADR-018 eval gate (procedural memory); ADR-019 Cosmos DB; ADR-020
  feature flags; ADR-023 MCP gateway (`memory` server); ADR-024 identities; ADR-025 agents and checkpointer.
- Plan §3.8, §6 C26, C27, C29, §7.
