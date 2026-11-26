# ADR-007: Customer data is read live as the RM, not embedded or indexed

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, data protection officer, information security, data owners (CRM, core banking, lending, KYC) |
| Plan references | §2 UC1, UC3, §3.2 `tool-gateway`, §3.6, §4.1 steps 4 to 6, §5 (UC1 1,500 ms fan-out), §6 C14, C21, C22, §7 |

## Context

UC1 needs a customer's profile, facilities and utilisation, transaction aggregates, KYC status and news; UC3 needs the
customer profile and writes call reports and tasks. A common RAG pattern copies such data into a vector store so the
model can retrieve it. Forces:

- **Access control**: the RM may see only customers in their portfolio, delegated portfolios with expiry, or (team
  lead) read-only views (C22). These rules live in the CRM and change daily.
- **Freshness**: utilisation, KYC expiry and call history must be current at meeting time; a stale figure presented
  confidently is worse than none.
- **Privacy and residency**: every copy of identifiable customer data is another store to secure, classify, retain,
  erase and evidence. Erasure and records rules apply (plan §7, §10 item 12; C21).
- **Load on core systems**: core banking cannot take copilot load directly (C14); the ODS read replica exists for
  this purpose.
- **Latency**: the UC1 fan-out budget is 1,500 ms p95 with a 2 s per-tool cap (plan §5).
- **Data shape**: the UC1 data is structured (amounts, dates, statuses), so semantic similarity search adds little;
  the questions are "this customer's facts", not "find similar text".

## Options considered

1. **Live tool calls as the RM**: `tool-gateway` calls systems of record through APIM at request time, after an
   entitlement check, and returns minimised, masked facts with source references.
2. **Embed customer data** (profiles, transactions summaries, call notes) into a vector index with per-document
   security trimming, retrieved by the orchestrator.
3. **Replicated customer-360 store** (a nightly or near-real-time copy in an Azure data store) queried by tools
   instead of the systems of record.
4. **Hybrid**: live structured data (option 1) plus an index of unstructured customer content (historic call
   reports, meeting notes) with per-document ACLs.

## Trade-off analysis

| Criterion | 1 Live tools as RM | 2 Embed customer data | 3 Customer-360 replica | 4 Hybrid live + notes index |
|---|---|---|---|---|
| Access control | **Strongest.** Entitlement check in `tool-gateway` on each call; source-system rules apply | **Weak to moderate.** ACLs copied into the index go stale; delegation and portfolio moves lag | Moderate: entitlements must be replicated and kept in sync | Moderate: index ACL staleness for notes |
| Freshness | **Real time** (ODS intraday for core banking) | Batch lag | Lag of the replication schedule | Mixed |
| Privacy and residency footprint | **Minimal.** No second copy of identifiable data; only request-scope caches (≤ 60 s) | **Large**: a new PII store, embeddings that can leak content, erasure must reach vectors | Large: a full copy to secure and erase | Moderate |
| Erasure and retention | Handled by source systems | Hard: re-embedding and index purge per erasure | Must be implemented | Partial |
| Load on systems of record | Per request; protected by ODS replica, quotas and bulkheads (C14) | Low at query time; batch extract load | Low at query time | Per request plus batch |
| Latency | Bounded by slowest source; parallel fan-out, partial brief if a non-critical source fails | Fast single query | Fast | Mixed |
| Fit to the data | **Good**: structured facts, fetched by key | Poor: similarity search over numbers is the wrong tool | Good | Good for notes, if needed |
| Quality (hallucination risk) | **Lowest**: facts with source references; figures inserted by the system | Higher: retrieved chunks may be outdated or partial | Low | Mixed |
| Integration effort | **High**: an API per system, interface agreements (`04`) | Moderate: extracts | **Highest**: a data platform build | High |
| Cost | Per-call APIM and system cost; no extra store | Index storage plus embedding cost | Data platform cost | Both |
| Time to pilot | Depends on API inventory (discovery) | Moderate | Slowest | Slowest |

## Decision

Adopt **option 1**. Customer data is **never embedded, indexed or copied** into a copilot store. It is read at
request time by `tool-gateway`, through the APIM system API facade, **as the RM** (OBO or APIM-signed RM header,
ADR-008), after the **entitlement check first** (cache ≤ 60 s, event invalidation). Core banking is read through the
**ODS read replica** (C14). `tool-gateway` minimises and masks fields (no national ID; transactions aggregated) and
returns facts with source references (plan §4.1 step 6). The only persisted customer content is: drafts and
verdicts in Cosmos DB (30-day TTL), the audit content store (masked, per-customer key, 1 year, crypto-shreddable),
and the records the RM approves into the CRM (plan §7). The AI Search index holds approved, non-customer documents
only (ADR-006).

## Consequences

**Positive**
- Access rules, delegation and freshness come from the systems of record; no entitlement replication to drift.
- No new PII store, no embeddings of customer data to protect or erase; smaller residency and DPIA scope.
- Facts arrive structured with source references, which enables placeholder fill and the numeric check (ADR-011).

**Negative**
- Integration effort is the long pole: every system needs a usable API with an interface agreement, timeouts and an
  error contract (`04-interfaces-and-slas.md`).
- The UC1 latency and availability depend on five sources; the slowest source sets the brief time.
- Questions spanning a customer's history in free text ("what did we discuss last spring?") are limited to what
  the CRM API returns.

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| A system of record lacks a usable read API | Discovery API inventory (plan §2 Phase 0); ODS or a thin read service built by the owning team |
| Copilot load degrades a system of record | ODS replica, APIM per-consumer quotas, per-request cache, bulkheads and circuit breakers (C14) |
| A non-critical source is slow or down | Deadline-derived timeouts; partial brief with the section flagged (plan §4.1 step 5) |
| Request-scope caches persist longer than intended | Cache TTLs enforced in code and tested; no customer data in telemetry (C5) |
| Pressure to add a notes index for history search | Review trigger below; any index of customer content needs a new ADR and DPIA |

## Review trigger

- A use case needs semantic search over customer free text (for example call note history) that the CRM API cannot
  serve.
- Systems of record cannot meet the latency or availability in `04-interfaces-and-slas.md` after the pilot.
- The Bank delivers a governed customer-360 platform with entitlement enforcement that the copilot could consume as
  a system of record.

## Related

- ADR-006 retrieval (documents only); ADR-008 identity and entitlements; ADR-012 PII masking; ADR-013 audit content
  store; ADR-019 state store.
- `01-hld.md` §8.1, §9.3; `04-interfaces-and-slas.md`; plan §3.6, §4.1, C14, C22.
