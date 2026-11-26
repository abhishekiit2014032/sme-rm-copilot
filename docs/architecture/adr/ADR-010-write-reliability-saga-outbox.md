# ADR-010: Write reliability with a per-item saga and a Cosmos DB outbox

| Field | Value |
|---|---|
| Status | Accepted (Phase 1); Service Bus Premium for Phase 2 is Proposed; amended 26 Nov 2026 (external reference format and Event Hubs ack before writes, aligned with `02-lld.md` §5) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, CRM integration team, operations |
| Critical issues | C7 (double posting, non-atomic writes), C14 (system of record protection) |
| Phase | 1, revisited at Phase 2 |

## Context

After a verified approval (ADR-009), `tool-gateway` writes one call report and N tasks (typically 1 to 5) to the
CRM through the APIM system API facade (plan §4.2 step 9). Forces:

- **No atomicity across items.** The CRM exposes one REST call per record (assumed); there is no transaction
  spanning the call report and its tasks, and nothing spanning the CRM and our own state.
- **Unknown outcomes.** A timeout or a 5xx after the request reached the CRM leaves the outcome unknown. A blind
  retry can double-post a call report, which is visible to auditors and to the customer's relationship history
  (C7).
- **CRM idempotency support is unconfirmed.** Plan §10 items 11 and 12 list "CRM idempotency / external reference
  support" as a Bank confirmation. This ADR depends on it.
- **Latency.** Commit p95 ≤ 2 s (§5) for the RM to see a per-item status. Tool cap 2 s; per-call timeout =
  min(cap, remaining deadline minus reserve) (§5 deadlines).
- **Token expiry.** Approval tokens live 300 s. A write queued during a CRM outage may run hours later, so retries
  must not depend on the token (C7: verdict persisted).
- **Phase 1 simplification.** No Service Bus in the pilot (§2): the outbox lives in Cosmos DB, delivered by
  `outbox-worker` from the change feed (§3.2).
- **CRM support hours and SLAs** are per system and unknown today (§10 item 12); retries must respect them and must
  not hammer a recovering CRM (C14).

## Options considered

1. **Synchronous only.** Write each item inline; on failure show an error and let the RM press Approve again.
2. **Per-item saga with an external reference, read-before-retry and a Cosmos DB outbox** (Phase 1), moving the
   retry transport to Service Bus Premium in Phase 2 while keeping the Cosmos outbox as the transactional record.
3. **Service Bus Premium from day one.** Publish one message per item; a consumer writes to the CRM with Service
   Bus duplicate detection, dead-lettering and scheduled redelivery.
4. **Distributed transaction.** Two-phase commit (XA or similar) across the CRM and our state store.
5. **CRM composite (all-or-nothing) API**, if the CRM offers a batch endpoint that creates a call report and its
   tasks atomically.

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 Synchronous only | 2 Saga + Cosmos outbox | 3 Service Bus day one | 4 Distributed transaction | 5 CRM composite API |
|---|---|---|---|---|---|
| Duplicate prevention | -- RM re-approve after a timeout double-posts unless the CRM deduplicates | ++ external reference `jti:item` plus read-before-retry | + duplicate detection window on `MessageId`, but only for messages; the CRM call can still time out after success, so read-before-retry is still required | n/a (not available) | + if the endpoint is idempotent; otherwise same as option 1 |
| Atomic hand-off from verify to queue | - nothing to hand off | ++ verdict and outbox items written in one Cosmos transactional batch (same partition key) | - dual write (Cosmos verdict plus Service Bus send) needs its own outbox anyway | n/a | 0 one call, but still unknown outcome on timeout |
| Behaviour during CRM outage | -- RM must come back later and re-approve with a new token | + items queue, status `queued`, retried within CRM support hours | ++ purpose-built: DLQ, scheduled delivery, back-pressure | -- | -- same as option 1 |
| Commit latency (≤ 2 s) | + direct | + first attempt inline; queueing is a local write | 0 adds publish latency; status becomes asynchronous for every item | -- coordinator round trips | ++ one call |
| Operability | ++ nothing to run | + change feed processor, lease container, status UI; poison item handling is ours | + mature tooling, but one more Premium namespace, private endpoint, RBAC, alerts | -- | + if available |
| Cost (qualitative) | ++ none | + Cosmos RU for a low-volume container | - Premium is a fixed-capacity charge regardless of the low pilot volume | n/a | ++ |
| Time to pilot | ++ | + | - more infrastructure before value is shown | -- REST APIs and Azure PaaS do not offer it | ? depends on CRM product |
| Auditability | - failures visible only as errors | ++ per-item status history and audit events | + | n/a | 0 one record of the batch |

Option 4 is rejected outright: neither the CRM REST API nor Cosmos DB participates in two-phase commit, and
building a coordinator would add the exact failure modes this ADR tries to remove. Option 5 is not rejected; it is
unknown. If discovery finds an idempotent composite endpoint, it simplifies the saga to one item but does not
remove the need for read-before-retry on timeouts.

## Decision

Adopt **option 2**.

1. **Atomic hand-off.** After verification, `tool-gateway` writes one Cosmos transactional batch in the
   `approvals` container, partition key = `jti` (ADR-019): the `jti` marker (create, 409 = replay), the verdict
   (ADR-009), the saga document and one item document per write (`state: pending`, external reference `<jti>:cr` for the
   call report and `<jti>:t1` to `<jti>:t10` for tasks, `02-lld.md` §5.1). `approval_verified` must be acknowledged by
   Event Hubs before the first CRM call (ADR-013). Item order is the order in the canonical draft, so references are stable.
2. **First attempt inline.** Items are written in parallel (bounded to 4 concurrent calls), except where the CRM
   requires a task to reference the call report id; then the saga runs report first, tasks after. Each call
   carries the external reference (CRM field or idempotency header, whichever the CRM supports) and a timeout
   derived from the remaining commit deadline.
3. **Outcome classes.** 2xx → `committed` with the CRM record id. Timeout, 5xx, 429 or connection error →
   `queued` (outcome unknown). 4xx validation error → `failed` (not retryable) with the CRM reason shown to the RM.
   The RM sees per-item status within the 2 s commit budget.
4. **Outbox delivery.** `outbox-worker` runs a change feed processor over the `approvals` container (lease
   container, one owner per partition range) and handles `queued` items. **Before every retry it reads the CRM by
   external reference.** Found → mark `committed` with the found id, no post. Not found → post. Item updates use
   Cosmos optimistic concurrency (ETag), so two workers cannot both post the same item. Backoff is exponential
   with jitter, paused outside the CRM's support hours, with a maximum of 8 attempts or 24 h (illustrative,
   tuned against the CRM SLA in `04-interfaces-and-slas.md`). After that the item is `failed`, an alert fires and
   the RM is notified in the sidebar.
5. **No automatic compensation.** Committed items are never deleted automatically when a sibling fails. Deleting
   CRM records is itself an auditable, risky action. Recovery is forward only: retry, or the RM completes the
   failed item manually from the status view. "Partially committed" is a first-class state in the UI and in
   audit events.
6. **Retries never need the token.** The worker acts on the persisted verdict; it re-checks only that the write
   kill switch is off (ADR-020) and that the RM's entitlement to the customer still exists (a revoked
   entitlement turns a queued item into `failed: entitlement_revoked`).
7. **Audit.** `write_committed`, `write_queued`, `write_failed`, with `ext_ref`, attempt number and lineage
   (ADR-013).
8. **Gate.** If the CRM supports neither a queryable external reference nor an idempotency key, UC3 writes do not
   go live. The fallback (a nightly reconciliation report plus manual de-duplication) needs an explicit risk
   acceptance by the business owner.
9. **Phase 2.** When the Phase 2 trigger below fires, an outbox relay publishes `queued` items to a Service Bus
   Premium queue (zone-redundant, private endpoint, `MessageId` = `ext_ref` for duplicate detection, dead-letter
   queue). The Cosmos outbox stays the system of record for item state; read-before-retry stays.

## Consequences

Positive

- No double posting as long as the CRM honours external references; the residual risk is reduced to the CRM's own
  behaviour, which is testable in discovery.
- A CRM outage does not lose approved work and does not force the RM to re-approve.
- No new messaging platform in the pilot.

Negative

- Partial commits are possible and visible. RMs need a short explanation of `queued` and `failed`, and the CRM
  team must accept that a call report can exist for a while without all its tasks.
- The change feed processor, lease container and poison-item handling are custom code that must be tested under
  fault injection (workstream C fault injection covers CRM timeouts and 5xx).
- Read-before-retry doubles CRM calls on the retry path (one read, possibly one write). Volume is low, but it
  counts against the CRM quota in APIM (C14).
- Change feed delivery latency is seconds, not milliseconds **[confirm typical change feed lag for the chosen
  throughput mode]**; acceptable for a retry path.

Risks and mitigations

| Risk | Mitigation |
|---|---|
| CRM ignores or truncates the external reference | Discovery test: post twice with the same reference, expect one record; contract test in CI against the CRM sandbox |
| Read-after-write lag in the CRM makes the read miss a just-created record | Minimum first-retry delay (for example 60 s, tuned to the CRM's indexing lag); read by reference uses the CRM's primary store if it has one **[confirm with CRM team]** |
| Poison item loops forever | Attempt cap, `failed` terminal state, alert, runbook in `06-deployment-and-operations.md` |
| Worker outage leaves items `queued` | KEDA scales `outbox-worker` on backlog; alert on oldest `queued` age > 15 min in business hours |
| Write kill switch flipped while items are queued | Worker holds items (status unchanged) until the switch is cleared; nothing is dropped |

## Review trigger

- Phase 2 start, or any of: more than one write target system, more than one consumer of write events, sustained
  queued backlog above an agreed threshold, or an operations request for DLQ tooling. Then implement step 9.
- The CRM team confirms a composite idempotent endpoint (re-assess option 5).
- Discovery shows the CRM has no idempotency mechanism (gate in step 8).

## Related

- Plan: §2 (Phase 1 simplifications), §3.2 (`tool-gateway`, `outbox-worker`), §4.2 steps 8 to 10, §5 (commit
  ≤ 2 s, deadlines), §6 C7, C14, §10 items 11 and 12.
- ADR-009 (verdict that the saga relies on), ADR-013 (audit events), ADR-015 (circuit breakers and deadlines),
  ADR-019 (`approvals` container, partition key, per-item TTL), ADR-020 (write kill switch), ADR-003 (APIM system API facade and per-consumer quotas), ADR-008 (entitlement
  re-check on retry).
- Code: `code/rm_copilot/tools.py` (`create_call_report`, `CRM_WRITES` stands in for the CRM).
