# ADR-013: Audit trail as a hash chain in immutable Blob storage, with crypto-shredded content

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, internal audit (third line), records management, InfoSec, data protection officer |
| Critical issues | C5 (customer data in prompts), C21 (immutability vs erasure), C23 (lineage for MRM) |
| Phase | 1 (ADX for query is a Phase 2 option) |

## Context

Internal audit must be able to reconstruct what the copilot did for any request: who asked, about which customer,
which tools ran, which sources were used, which model and prompt version produced the wording, which checks
passed, who approved which write (governance slide: "replay any answer from the immutable log"). The forces:

- **Tamper evidence.** Records must be provably complete and unaltered, including against a privileged insider.
- **Retention.** Records policy, illustratively 7 years for audit metadata (§7) **[confirm with records
  management]**.
- **Erasure and imperfect masking (C21).** Prompts and outputs are masked (ADR-012) but masking is imperfect, so
  stored content can still contain personal data. A WORM store cannot delete a record when an erasure request
  is valid. Banking record-keeping obligations may override erasure for some data, and not for others **[confirm
  with legal: PDPL banking scope, CBUAE, DIFC/ADGM where relevant, §10 item 8]**.
- **Residency.** Audit data stays in the UAE. GZRS replicates to the paired region (UAE Central), which is
  in-country.
- **RPO.** Zone failure: audit RPO 0. Region loss: audit RPO ≤ 15 min (§5).
- **Lineage (C23).** Every event carries model deployment and version, prompt hash, tool contract versions,
  guardrail version and index version (§4.1 step 10).
- **Durability of the producer path.** Producers wait for the Event Hubs acknowledgement (§3.3).
- **Volume.** Roughly 5,400 requests/day at scale (§5) with about 5 to 15 events per request: tens of thousands of
  small events per day. Modest for every option below.

## Options considered

1. **Event Hubs → `audit-writer` → immutable Blob (GZRS) hash chain** of metadata and content pointers; content
   in a separate store encrypted per customer key (crypto-shredding); metadata also ingested into a restricted
   Log Analytics table for queries.
2. **Log Analytics only**: write audit events straight to a custom Log Analytics table with long retention.
3. **Azure Data Explorer (ADX)** as the audit store, with ingestion from Event Hubs.
4. **Azure Confidential Ledger**: append every event (or its digest) to a managed tamper-proof ledger.
5. **Cosmos DB with change feed**: write events to a Cosmos container, project them elsewhere through the change
   feed.

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 Event Hubs + WORM Blob hash chain | 2 Log Analytics only | 3 ADX | 4 Confidential Ledger | 5 Cosmos + change feed |
|---|---|---|---|---|---|
| Tamper evidence | ++ locked time-based retention prevents change and deletion; the hash chain detects gaps and reordering; seals anchored outside the writer's control | - data is append-only for most users, but a purge API exists for privileged roles and there is no cryptographic chain | - no immutability policy; admin can drop or alter tables | ++ purpose-built, cryptographic receipts | -- documents are mutable by anyone with write access |
| Erasure compatibility (C21) | ++ content separate and crypto-shredded; WORM holds no content | - purge is possible but blunt and itself undermines tamper evidence | 0 purge is possible, same tension | -- ledger entries cannot be removed; content must never go in | + deletable, which is the problem for evidence |
| Residency | ++ UAE North with GZRS to UAE Central | ++ | ++ if the cluster is in UAE North | -- not believed to be offered in UAE regions **[confirm]** | ++ |
| Query for investigations | + via the restricted Log Analytics table (KQL); WORM is the system of record, not the query engine | ++ KQL | ++ strongest analytics | - point lookups and receipts, not analytics | 0 SQL-like queries per partition |
| Region-loss RPO ≤ 15 min | + GZRS asynchronous replication, typically within 15 min, no SLA on the lag **[confirm]** | 0 workspace replication is a separate feature **[confirm availability in UAE North]** | 0 depends on cluster configuration | ? | + with multi-region accounts (not planned) |
| Operability | 0 one custom service (`audit-writer`) plus a verifier job | ++ fully managed | - a cluster to size and run | + managed, but a new service | + managed |
| Cost (qualitative) | + Blob storage is the cheapest long-term tier; Event Hubs Standard is small; LA ingestion only for metadata | - long-retention ingestion of every event, including content if stored there | - a cluster running all the time for modest volume | 0 per-transaction | - RU for writes and long retention |
| Time to pilot | + | ++ | - | -- regional blocker | + |

Option 4 is attractive as an anchor for chain heads (a few writes per hour) rather than as the store; it is
blocked by regional availability today. ADX remains the Phase 2 query option if audit volume or investigation
needs outgrow Log Analytics (§2).

## Decision

Adopt **option 1**.

1. **Producers.** `web-bff`, `orchestrator` and `tool-gateway` emit audit events to Event Hubs (Standard,
   zone-redundant, private endpoint, managed identity, `disableLocalAuth`). Producers wait for the
   acknowledgement. **Writes fail closed**: `tool-gateway` does not call the CRM until `write_intent` is
   acknowledged. **Reads** spool to a bounded Cosmos `audit-spool` container if Event Hubs is unavailable,
   drained by `audit-writer` later, with an alert; if both are unavailable the request fails.
2. **Event schema.** Metadata only: `seq`, `ts`, `event`, `request_id`, `trace_id`, actor (`oid`), customer
   reference (keyed hash of the CRM id, HMAC key in Key Vault), use case, tool names and contract versions,
   lineage (model deployment and version, prompt hash, guardrail version, index version), outcome and reason
   codes, approval `jti` and `kid`, and a **content pointer** `{store, blob, sha256_of_ciphertext, key_ref}`.
   No prompt or output text in the event.
3. **Hash chain.** `audit-writer` keeps one chain per Event Hubs partition: `hash = SHA-256(prev_hash ||
   JCS(record))`, written to append blobs (one per partition per hour) in a container with a **locked
   time-based retention policy** (7 years, §7) and protected append writes allowed **[confirm feature
   combination with GZRS and append blobs]**. Every 5 minutes a **seal** record with all partition heads and the
   previous seal hash is written to the same container and copied to an independently administered location
   (a separate subscription's immutable container whose administrators are not the platform team, and Sentinel
   as a secondary copy). A nightly verifier job recomputes the chains and seals and alerts on any break.
4. **Content store.** Masked prompts, tool facts and outputs go to a separate Blob account (GZRS, private
   endpoint, not immutable, lifecycle delete at 1 year, §7). Each object is encrypted client-side with
   AES-256-GCM using a **per-customer data key**, wrapped by an HSM key encryption key in a dedicated Key Vault
   (C21). Erasure = delete the customer's wrapped data key and append a `content_shredded` event to the chain.
   With purge protection on, the deleted key is unrecoverable only after the soft-delete retention period
   (minimum 7 days), so the erasure statement to the customer reflects that delay.
5. **Query copy.** `audit-writer` also sends the metadata to a custom table in a dedicated Log Analytics workspace
   via the Logs Ingestion API, with table-level RBAC limited to the audit and investigations role, retention per
   records policy (interactive retention plus long-term retention) **[confirm]**. The WORM chain is the system
   of record; Log Analytics is a convenience copy that may be purged under policy without affecting evidence.
6. **Replay.** For any request, audit can join the chain record (who, what, lineage) with the content object
   (what was said) while the content exists (1 year), and verify the content hash against the pointer. After 1
   year, or after shredding, replay shows metadata and lineage only.
7. **Keys and identities.** `audit-writer` is the only identity with write access to the audit containers; no
   human has data-plane write access; storage account keys disabled (`allowSharedKeyAccess = false`).

## Consequences

Positive

- Strong, explainable tamper evidence that internal audit can verify independently with a script and the
  public chain rules.
- Erasure requests can be honoured for content without breaking the chain (C21).
- Long retention on the cheapest storage tier; analytics cost limited to metadata.

Negative

- `audit-writer` and the verifier are custom code on the evidence path; they need their own tests, threat model
  and change control.
- A locked retention policy is irreversible: anything written into the WORM container stays for 7 years and the
  account cannot be deleted while it holds protected blobs. A schema mistake that writes content into metadata
  cannot be undone; a CI test asserts no free-text fields in the event schema.
- Event Hubs becomes a hard dependency for writes and a soft one for reads.
- Replay of full content is limited to the 1-year content retention. The governance slide's "replay any answer"
  holds for one year, then becomes "replay the lineage". Records management must confirm this is acceptable.
- Pseudonymous identifiers (RM `oid`, keyed customer hash) remain in the WORM store for 7 years; legal must
  confirm this is consistent with erasure rules.

Risks and mitigations

| Risk | Mitigation |
|---|---|
| `audit-writer` lag exceeds Event Hubs retention | Lag alert at 15 min; Standard tier retention covers days **[confirm current maximum]**; KEDA scaling on lag |
| Insider rewrites the unsealed tail | Seals every 5 min to an independently administered store; nightly verification; PIM for storage roles |
| Region loss loses events not yet replicated | GZRS asynchronous replication (RPO ≤ 15 min target); CRM holds the business record of writes; accepted residual |
| Per-customer key sprawl in Key Vault | Data keys are stored wrapped (one secret per customer), unwrapped keys cached in memory for minutes; Key Vault operation limits reviewed **[confirm]** |
| Logs Ingestion API or workspace outage | Query copy is best effort; WORM path unaffected; backfill from the chain |

## Review trigger

- Azure Confidential Ledger (or another managed ledger) becomes available in UAE North: evaluate it as the seal
  anchor.
- Investigation queries exceed Log Analytics practicality, or audit volume grows by an order of magnitude
  (Phase 2 ADX option, §2).
- Legal or records management changes retention or erasure rules.

## Related

- Plan: §3.2 (`audit-writer`), §3.3 (Audit stream, Audit store, Keys rows), §4.1 step 10, §4.2 step 10, §5 RPO,
  §6 C5, C21, C23, §7 retention, §10 item 8.
- ADR-009 (approval events), ADR-010 (write events), ADR-012 (masked content), ADR-014 (telemetry carries no
  content), ADR-015 (GZRS and region loss), ADR-018 (lineage for MRM), ADR-019 (`audit-spool` container), ADR-001 (in-country GZRS secondary).
- Code: `code/rm_copilot/tools.py` (`audit`, `AUDIT_LOG`, an in-memory list in the prototype).
