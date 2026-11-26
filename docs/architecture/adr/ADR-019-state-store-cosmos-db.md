# ADR-019: Operational state in Azure Cosmos DB for NoSQL

| Field | Value |
|---|---|
| Status | Accepted; amended 26 Nov 2026 for plan v2.3 and v2.4 (snake_case partition keys as in `02-lld.md` §6.1, entitlement TTL 50 s, `idempotency` and `audit-chain-heads` added, periodic backup) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, platform operations, InfoSec |
| Critical issues | C6 (atomic `jti`), C7 (outbox, saga, persisted verdict), C22 (entitlement cache) |
| Phase | 1 (no Redis in Phase 1, §2) |

## Context

The services are stateless pods on AKS (§3.2) but the platform needs durable, zone-redundant operational state:

| State | Access pattern | Retention (§7) |
|---|---|---|
| Sessions (`web-bff`) | Point read and write per request, by session id | 24 h TTL |
| Drafts (UC3) | Read and update by draft id for one RM; list an RM's open drafts; optimistic concurrency on edit and approve (ADR-009) | 30 days TTL |
| `jti` replay markers | Atomic create; 409 on duplicate | 1 day TTL |
| Approval verdicts and saga state | Written once at verification, updated per item | 30 days after the last item is final |
| Outbox items | Created with the verdict; consumed by `outbox-worker` from the change feed | 30 days after the last item is final |
| Idempotency records | Point read and create by key | 7 days |
| Entitlement cache | Read per tool call, by RM; invalidated on portfolio change events | ≤ 60 s |
| Audit spool (ADR-013) | Append when Event Hubs is unavailable, drained later | Until drained |

Forces: small data volume (300 RMs at most), low write rate, strict correctness for `jti` and outbox, zone
redundancy, CMK, private endpoint, `disableLocalAuth`, managed identity data-plane RBAC, a transactional hand-off
between "approval verified" and "outbox items exist" (ADR-010), TTL-based expiry rather than clean-up jobs, and a
Phase 1 rule of no Redis (§2).

## Options considered

1. **Azure Cosmos DB for NoSQL** (single region, zone-redundant, provisioned autoscale throughput, per-container and
   per-item TTL, transactional batch within a logical partition, change feed).
2. **Azure SQL Database** (zone-redundant General Purpose or Business Critical), with an outbox table and
   transactions.
3. **Azure Managed Redis** for sessions, `jti` and cache, with a durable store elsewhere for drafts and outbox.
4. **Azure Database for PostgreSQL Flexible Server** (zone-redundant HA).

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 Cosmos DB for NoSQL | 2 Azure SQL | 3 Azure Managed Redis | 4 PostgreSQL Flexible |
|---|---|---|---|---|
| Atomic `jti` create | ++ create with `id = jti` returns 409 on conflict | ++ unique constraint | + `SET NX`, but durability depends on persistence configuration | ++ unique constraint |
| Verdict plus outbox atomicity | + transactional batch, only within one logical partition (design for it) | ++ full ACID across tables | -- needs a second store | ++ full ACID |
| Outbox delivery | ++ change feed, processor library with leases | 0 polling or CDC to build | -- | 0 polling or logical decoding |
| TTL expiry | ++ native per container and per item | - scheduled clean-up jobs | ++ native | - scheduled clean-up jobs |
| Zone redundancy and backups | ++ zone-redundant account; continuous or periodic backup | ++ | + zone-redundant tiers **[confirm availability in UAE North, §10 item 5]** | + zone-redundant HA (standby in another zone) |
| Latency | ++ single-digit ms point reads in-region | + | ++ sub-ms | + |
| Security posture | ++ Entra data-plane RBAC, local auth off, CMK, private endpoint | ++ Entra auth, CMK (TDE), private endpoint | + Entra auth support **[confirm]**, private endpoint | + Entra auth, CMK, private endpoint |
| Operability | ++ serverless-like ops, no schema migrations | 0 schema migrations, index tuning | - eviction, persistence and memory sizing to manage | 0 HA failovers, vacuum, migrations |
| Cost at pilot volume (qualitative) | + autoscale minimum throughput per container or shared database throughput | + small vCore or serverless tier | - memory-sized SKU running all the time | + small burstable tier lacks HA; HA doubles compute |
| Bank skills | 0 NoSQL modelling is less familiar | ++ widely known | + | + |

Azure SQL is a credible alternative and would be the choice if the Bank's operations team cannot support Cosmos
DB. Cosmos DB wins on native TTL, change feed for the outbox and zero schema migration for a small, document-shaped
state model. Redis is rejected for Phase 1 by the plan's simplification rule (§2) and because the only state where
it would help (entitlement cache) is already met by in-process caching plus Cosmos at the §5 budget (20 ms cached,
150 ms miss).

## Decision

Adopt **option 1**: one Cosmos DB for NoSQL account per environment, single region UAE North, zone-redundant,
local auth disabled, CMK from Key Vault, private endpoint, Entra data-plane roles scoped per container to the
owning service's workload identity.

Containers and partition keys:

| Container | Partition key | Documents | TTL | Writers | Readers |
|---|---|---|---|---|---|
| `sessions` | `/id` (hash of the session cookie) | Session state | 24 h (container default) | `web-bff` | `web-bff` |
| `drafts` | `/rm_oid` | Draft, versions (ETag), reference to the approval `jti` | 30 days | `web-bff` (the orchestrator holds no state, `02-lld.md` §2.2) | `web-bff`; `outbox-worker` reads metadata for the unused-approval sweep |
| `approvals` | `/jti` | `jti` marker (per-item TTL 1 day), approval document with verdict, approved snapshot and saga state, outbox items (TTL -1 while open, 30 days once the last item is final) | Per item | `tool-gateway`, `outbox-worker` | `tool-gateway`, `outbox-worker` |
| `idempotency` | `/id` (hash of `rm_oid:key`) | Stored responses for `Idempotency-Key` | 7 days | `web-bff`, `tool-gateway` | same |
| `leases` | `/id` | Change feed processor leases | none | `outbox-worker` | `outbox-worker` |
| `entitlements` | `/id` (`rm_oid`) | Cached portfolio and delegation records (C22) | 50 s (+ 10 s in-process = 60 s) | `tool-gateway` | `tool-gateway` |
| `audit-spool` | `/producer_pod` | Audit events spooled during an Event Hubs outage | none (deleted when drained) | `tool-gateway` (the only producer) | `audit-writer` (drains into chain `s0`) |
| `audit-chain-heads` | `/id` (`chain_id`) | Hash-chain heads, ETag-guarded | none | `audit-writer` | `audit-writer` |

Rationale for the keys:

- **`/rm_oid` for drafts**: every access is by an authenticated RM whose `oid` is known from the token, so reads are
  point reads, listing is a single-partition query, and a bug cannot return another RM's draft without also
  using the wrong partition key. Logical partitions stay tiny (an RM has tens of drafts at most), far below the
  20 GB logical partition limit.
- **`/jti` for approvals**: the `jti` marker, verdict, saga and outbox items share one logical partition, so a
  single **transactional batch** creates all of them or none (ADR-009 step 5, ADR-010 step 1). A replay fails
  the batch on the marker's 409 and writes nothing. Per-item TTL keeps the marker for 1 day and the rest for 30
  days, matching §7, in one container. This is an LLD refinement of the plan's "jti replay store" and
  "outbox" rows: same retention, one container.
- **`/id`** for sessions and the entitlement cache spreads load evenly across a few hundred keys; volume is too
  small for hot partitions to matter.

Throughput: autoscale with the lowest maximum per container (or shared database throughput for the small
containers), sized from the load test in workstream C. Consistency: Session (default) for the account; the `jti`
check does not depend on read consistency because it uses a conditional create. Backups: **periodic backup mode
with geo-redundant backup storage** so copies survive region loss (plan §3.3; continuous backup restores only within
the account's regions, and an account uses one mode) (ADR-015) **[confirm interval, retention, copy location in the
UAE and whether serverless supports zone redundancy; this ADR assumes provisioned throughput; plan §10 item 13]**.

In-process caches in `tool-gateway` (entitlements ≤ 60 s, request-scoped tool results) sit in front of Cosmos;
portfolio change events invalidate both.

## Consequences

Positive

- The approval-to-outbox hand-off is atomic without a relational database.
- TTLs implement the retention table (§7) declaratively; no clean-up jobs.
- One managed, zone-redundant store for all operational state; no Redis to run in the pilot.

Negative

- Transactional batch works only within one logical partition (and has a per-batch operation limit **[confirm
  current limit]**); a draft with an unusually large number of items must be rejected by schema (cap tasks per
  draft, for example 20).
- NoSQL modelling and RU sizing are less familiar to many bank DBAs; RU throttling (429) must be handled with
  retries inside deadlines.
- Cross-container queries (for example "all approvals by an RM") need either the `rm_oid` duplicated in approval
  documents or the audit store; operational reporting should come from audit, not from Cosmos.

Risks and mitigations

| Risk | Mitigation |
|---|---|
| RU throttling during load peaks | Autoscale, retry with backoff within the deadline, alert on 429 rate |
| A service writes to a container it should not | Per-container data-plane role assignments per workload identity |
| TTL misconfiguration deletes data early | TTL values in Bicep, asserted by a deployment test |
| Change feed processor falls behind | Lag metric and alert (ADR-010), KEDA scaling |

## Review trigger

- Phase 2 latency or cost data show a cache tier is needed (re-assess Azure Managed Redis, subject to UAE North
  availability).
- The Bank's operations team cannot support Cosmos DB (switch to Azure SQL, with polling for the outbox).
- State volume or write rate grows by an order of magnitude (Phase 3 segments).

## Related

- Plan: §2 (no Redis in Phase 1), §3.2, §3.3 (State row), §4.2 steps 8 and 9, §5 latency table, §6 C6, C7, C22,
  §7, §10 item 5.
- ADR-009 (atomic `jti`, verdict), ADR-010 (outbox and saga), ADR-013 (`audit-spool`), ADR-015 (backups, zone
  redundancy), ADR-021 (containers and TTLs in Bicep), ADR-008 (entitlement cache and delegation
  records), ADR-002 (stateless services on AKS).
- Code: `code/rm_copilot/tools.py` (in-memory `AUDIT_LOG`, `CRM_WRITES` and the stateless HMAC verify that this
  replaces).
