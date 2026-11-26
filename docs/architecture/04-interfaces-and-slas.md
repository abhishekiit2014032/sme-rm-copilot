# 04 Interfaces and SLAs

Status: v1 for ARB · Workstream B2 · Owner: solution architecture (resilience, security, operations) · Date: 26 Nov 2026

This document is the interface agreement baseline between the SME RM Copilot and every system it depends on. It
follows the contract in `00-plan.md` (service names §3.2, Azure services §3.3, identity chain §3.4, data flows §4,
NFRs and latency budget §5, critical issues §6, retention §7, facts to confirm §10). Everything is fictional and
unbranded ("the Bank"). All volumes are derived from the plan's illustrative baseline; all Microsoft SLA figures are
quoted as "per Microsoft SLA, [confirm] current figure" because the SLA documents change and the Bank must read the
version in force at contract time. Where a number is a design requirement placed on a Bank system, it is labelled
**required** and becomes an OLA to negotiate in Phase 0.

Contents

1. Volumetric baseline and derivation
2. Common conventions (deadlines, resilience patterns, error contract, classification, severity)
3. Bank systems of record and third parties (IF-01 to IF-07)
4. Microsoft identity and AI services (IF-08 to IF-13)
5. Azure platform services (IF-14 to IF-22)
6. Interface Control Document (ICD) template
7. RACI
8. The copilot's own SLOs, error budgets and reporting
9. Composite availability per use case
10. Open points and [confirm] list for this document

---

## 1. Volumetric baseline and derivation

### 1.1 Request volume

Plan §5: 300 RMs at scale, about 5,400 requests per day, peak 0.5 requests/s. Business hours are Mon–Fri 07:00–20:00
GST (13 h = 46,800 s). Pilot: 30 RMs.

| Quantity | Derivation | Scale | Pilot (30 RMs) |
|---|---|---|---|
| Requests per RM per day | 5,400 / 300 | 18 | 18 |
| Requests per day | plan §5 | 5,400 | 540 |
| Average rate in business hours | 5,400 / 46,800 s | 0.115 req/s | 0.012 req/s |
| Peak rate | plan §5 (peak-to-average factor 4.3) | 0.5 req/s | 0.1 req/s (design value; small populations are burstier, so we use 8× average, not 4.3×) |
| Concurrent requests at peak (Little's law) | 0.5 req/s × ~6 s mean latency | ≈ 3 | ≈ 1 |
| Business minutes per month | 13 h × 60 × 22 working days | 17,160 | 17,160 |

### 1.2 Request mix (assumption, to be replaced by pilot telemetry)

| Use case | Per RM per day | Requests per day (scale) | Share | Peak req/s (share × 0.5) |
|---|---|---|---|---|
| UC1 pre-meeting brief | 5 (2 to 3 meetings, plus re-opens) | 1,500 | 27.8% | 0.139 |
| UC2 policy question | 8 | 2,400 | 44.4% | 0.222 |
| UC3 draft (extraction) | 3 | 900 | 16.7% | 0.083 |
| UC3 commit (approve) | 2 | 600 | 11.1% | 0.056 |
| Total | 18 | 5,400 | 100% | 0.5 |

### 1.3 Downstream calls per request (design assumption)

| Use case | Downstream calls per request |
|---|---|
| UC1 | Entitlement 1 (cache ≤ 60 s, plan §4.1 step 4; worst case counted as a miss); CRM 2 (profile and contacts; pipeline and call history); core ODS 2 (balances; aggregated transactions); LOS 2 (lending; trade); KYC 1; news and registry 1; Graph 1 (meeting context); AI Language 2; Content Safety 1 (documents); `gen-large` 1; audit events about 10 |
| UC2 | `gen-small` 1 (rewrite); `embed` 1; AI Search 1; `gen-large` 1.1 (10% stricter retry, plan §4.3); groundedness check 1.1 (local, on `gen-small`, assumption); Content Safety 1 (user prompt, plus groundedness if offered in-region); AI Language 1; audit events about 5 |
| UC3 draft | Entitlement 1; CRM 1 (profile); AI Language 2; Content Safety 1 (user prompt); `gen-small` 1; audit events about 4 |
| UC3 commit | Entitlement 1; Key Vault `sign` 1 (`web-bff`); Cosmos jti create 1; CRM writes 3 (one call report plus on average two tasks, plan §4.2 step 9); audit events about 6 |

The per-interface volumetrics in sections 3 to 5 multiply this table by section 1.2. "Contracted peak" is the derived
peak × 3 headroom (bursts, retries, growth), rounded up; it is what we ask each provider to sustain.

---

## 2. Common conventions

### 2.1 Deadlines and timeouts (plan §5, LLD `02-lld.md` §1.6)

- `web-bff` sets the request deadline (UC1 8 s, UC2 7 s, UC3 draft 10 s, commit 2 s) and propagates the **remaining
  budget** in the relative header `x-deadline-ms` together with W3C `traceparent` (02 §1.3). Each receiver computes
  `deadline_at = receive_time + value`, so pod clock skew does not matter.
- Every hop computes `timeout = min(own cap, remaining deadline − reserve)`; if the result is below the dependency's
  floor the call is skipped and treated as a timeout (fail fast).
- Caps used in this document are the LLD caps (02 §1.6): tool call 2,000 ms (plan); CRM, ODS and LOS 1,500 ms; KYC
  1,200 ms; news 1,000 ms; Graph 800 ms; AI Search 600 ms; query embedding 300 ms; `gen-small` rewrite 1,000 ms;
  Prompt Shields and AI Language 500 ms; Key Vault `sign` 400 ms; Cosmos point operations 300 ms; Event Hubs send
  with ack 300 ms. Model generation cap = remaining deadline. Failover to the second model backend only if
  ≥ 4,000 ms remain, else degraded mode (C3).

### 2.2 Standard resilience patterns (LLD defaults, 02 §1.7)

| Pattern | Standard setting | Note |
|---|---|---|
| Timeout | Deadline-derived, capped per dependency (§2.1) | Never infinite; connect timeout short on private links |
| Retry (reads) | At most 1, only on connection reset or 503 before any response bytes, only if `remaining ≥ 2 × p95(dependency)` and the breaker is closed. **Never retry a timeout** (it amplifies load). Model calls: no client retry (APIM owns failover) | Retries count towards the ≤ 6 tool-step budget (plan §5) |
| Retry (writes) | **None inline.** A CRM write that times out or returns 5xx goes to the Cosmos outbox; `outbox-worker` reads before retrying by external reference `jti:item` (C7) | Backoff schedule in 02 §5.3 |
| Circuit breaker | Per pod, per dependency. Opens on ≥ 50% failures in a 30 s window with ≥ 10 calls, or 5 consecutive failures (timeouts, connection errors, 5xx, 429). Open 30 s, then half-open with 2 trial calls. CRM: 60 s open; CRM writes: 3 consecutive failures | At pilot volume (≈ 0.03 req/s per system) the window rule rarely fires, so the consecutive rule and synthetic probes (§8.6) drive it |
| Bulkhead | Max in-flight per pod: ODS 10, CRM 10, LOS 10, KYC 5, news 5, Graph 5, AI Search 10, AI gateway 20; plus APIM `rate-limit-by-key` / `quota-by-key` per consumer and per backend (C14) | A slow system cannot exhaust shared connections |
| Cache | Entitlement ≤ 60 s total (Cosmos 50 s + in-process 10 s, event invalidation, C22); tool results request scope only (plan §7); JWKS 24 h and approval public keys 10 min, both refreshed on unknown `kid`; App Configuration last-known values | No cross-request cache of customer data |
| Degraded behaviour | Per interface; flagged in the UI and counted as `degraded` or `partial` (C3) | A degraded answer counts as available (plan §5) |

### 2.3 Error contract

**Copilot side.** All copilot services use the single error envelope and error-code catalogue defined in the LLD
(02 §1.4): `{"error": {"code", "reason", "message", "request_id", "retryable", "retry_after_ms", "details"}}`, codes in
UPPER_SNAKE_CASE, `message` safe to show to an RM. Degraded and partial results are **not errors**: they are `200`
with `"mode": "degraded" | "partial"` and `warnings` (for example `DEGRADED/MODEL_UNAVAILABLE`,
`SOURCE_UNAVAILABLE` with `section: "kyc"`); a UC2 decline is `200` with `"status": "declined"`. Downstream error bodies
are mapped at the adapter, never passed through, and never logged (C5).

**Downstream to copilot mapping** (applies to every interface below unless the interface states otherwise):

| Downstream condition | Critical dependency for this request | Non-critical (degradable) dependency |
|---|---|---|
| 400 / schema-invalid response | `500 INTERNAL` (contract defect, alert) | Section dropped, warning `SOURCE_UNAVAILABLE` |
| 401 / 403 from the downstream (our identity rejected) | `503 DEPENDENCY_UNAVAILABLE` + configuration or security alert | Same warning + alert |
| 404 for a customer the RM is entitled to | Empty section (not an error) | Empty section |
| 409 duplicate `external_reference` on a CRM write | Treated as success: read the existing item (C7) | n/a |
| 429 with `Retry-After` | `429 RATE_LIMITED` if the deadline cannot absorb it, else wait and call once | Warning `SOURCE_UNAVAILABLE` |
| 5xx, connection error, breaker open | `503 DEPENDENCY_UNAVAILABLE` | Warning `SOURCE_UNAVAILABLE`, `mode: partial` |
| Timeout | `504 DEPENDENCY_TIMEOUT` | Warning `SOURCE_UNAVAILABLE`, `mode: partial` |
| Model 429 / 5xx / breaker open on all backends | n/a (model is always degradable) | `200`, `mode: degraded`, warning `DEGRADED/MODEL_UNAVAILABLE` (or `MODEL_DEADLINE`) |
| Model content filter on input or output | Input attack in the RM's own text: `422 GUARDRAIL_BLOCKED/PROMPT_ATTACK` or `CONTENT_FILTER` | Otherwise `DEGRADED/CONTENT_FILTER` |
| Audit event not acknowledged by Event Hubs | `503 DEPENDENCY_UNAVAILABLE` (**fail closed**, nothing released or written) | n/a (always critical) |
| Kill switch or feature flag off | `503 FEATURE_DISABLED` with `details.flag` | n/a |

Copilot-side codes used in this document and their meaning to the RM: `NOT_ENTITLED` (not in your portfolio; a
correct outcome), `STEP_UP_REQUIRED` (re-authenticate to approve), `APPROVAL_INVALID` (with reasons such as
`DRAFT_HASH_MISMATCH`, `EXPIRED`, `UNKNOWN_KID`: approve again), `REPLAYED` (token already used), `BUDGET_EXCEEDED`
(per-RM daily token budget, resets at midnight GST), `FEATURE_DISABLED`, `DEPENDENCY_UNAVAILABLE`,
`DEPENDENCY_TIMEOUT`, `DEADLINE_EXCEEDED`. The full catalogue and reasons are in 02 §1.4 and §4.

### 2.4 Data classification (Bank scheme, labels to [confirm] with the CISO office)

| Class | Examples in this solution | Handling |
|---|---|---|
| Restricted | Customer identifiers, balances, transactions, facilities, KYC status and risk rating, call notes, drafts, audit content | Private network only; minimised and masked in `tool-gateway`; PII masked before any model call; encrypted with CMK; content never in telemetry |
| Confidential | Internal policies, product guides, authority matrix, RM calendar metadata, prompts and prompt templates | Private network; clearance filter in AI Search |
| Internal | Metrics, traces without content, configuration | Standard controls |
| Public | Licensed news is commercially restricted but not customer-confidential; treated as Confidential because the query reveals a customer relationship | Query terms are customer names: outbound call through the firewall allow-list only |

### 2.5 Incident severity (used in all OLAs)

| Severity | Definition (copilot view) | Response | Restore target | Communication |
|---|---|---|---|---|
| P1 | Copilot unavailable for all RMs, or any confirmed entitlement leak, data exposure, audit chain break or harmful output in production | 15 min | 4 h (workaround or kill switch within 30 min) | Every 30 min; incident bridge |
| P2 | A use case unavailable or degraded for > 25% of RMs, or SLO burn rate alert firing | 30 min | 8 business hours | Every 2 h |
| P3 | Partial degradation (one non-critical source, single RM issue) | 4 business hours | 3 business days | Daily |
| P4 | Cosmetic, question, minor defect | 1 business day | Next release | On closure |

These targets apply in business hours. Outside business hours only P1 security incidents are worked (SOC 24×7);
all other incidents are picked up at 07:00 GST.

---

## 3. Bank systems of record and third parties

All Bank systems are reached over ExpressRoute through the APIM **system API facade** (internal VNet), never
directly from pods. APIM authenticates to each backend with `tool-gateway`'s managed identity or a certificate held in
Key Vault, and forwards the RM's `oid` in a header signed by APIM when the backend does not accept Entra tokens
(plan §3.4 step 5). Entitlements are always enforced in `tool-gateway`, regardless of what the backend does.

The SLA/OLA tables use these columns: **Required** (what the copilot design needs), **Current** (what the system
offers today: unknown until the Phase 0 API inventory, so every cell is [confirm]), and **Gap action**.

### IF-01 CRM (customer, pipeline, call reports, tasks, portfolio and delegation records)

| Attribute | Agreement |
|---|---|
| Purpose | Customer profile, contacts, pipeline, call history (UC1, UC3 context); **source of portfolio assignment and delegation records used for entitlements** (C22); target of approved writes (UC3) |
| Direction and operations | Read: `GET /customers/{id}`, `GET /customers/{id}/contacts`, `GET /customers/{id}/opportunities`, `GET /customers/{id}/activities?since=`, `GET /rms/{oid}/portfolio`, `GET /rms/{oid}/delegations`. Write: `POST /activities` (call report), `POST /tasks`, each with `external_reference = jti:item`; `GET /activities?external_reference=` (read-before-retry). Events: `portfolio.changed`, `delegation.changed` published to the Bank's event backbone, consumed to invalidate the entitlement cache |
| Protocol and auth | HTTPS REST over ExpressRoute via APIM; TLS 1.2+; OBO token (audience CRM API) if the CRM accepts Entra tokens, else managed identity plus APIM-signed `x-rm-oid` header **[confirm CRM capability]** |
| Data classification and fields | Restricted. Read fields after minimisation: customer id, legal name, segment, sector, RM, contacts (name, role; no personal phone or national ID), open opportunities (stage, product, indicative size band), last 10 activities (date, type, summary text). Write fields: call report (subject, date, attendees, summary, next steps, "AI-assisted, RM-approved" marker, model lineage reference), tasks (title, due date, owner) |
| Volumetrics | Reads/day: UC1 2 × 1,500 + UC3 draft 1 × 900 + entitlement 1 × (1,500 + 900 + 600) = **6,900**; peak 0.139 × 2 + 0.083 + 0.278 = **0.64 req/s**; contracted peak **2 req/s**. Writes/day: 3 × 600 = **1,800**; peak 3 × 0.056 = **0.17 req/s**; contracted **0.5 req/s**. Pilot: one tenth |
| Availability (business hours) | **Required: ≥ 99.8% pilot, ≥ 99.9% scale** (derived in §9: CRM is on the critical path of UC1 and UC3; ≥ 99.95% recommended for scale headroom). Current: [confirm] |
| Latency p95 | Required: reads ≤ 400 ms, portfolio/delegation ≤ 150 ms, writes ≤ 1 s. Current: [confirm] |
| Throughput | Contracted peak above, enforced by APIM `rate-limit-by-key` on the copilot subscription |
| Support hours | Required: business hours 07:00–20:00 GST Mon–Fri, P1 response 15 min, restore 4 h; P2 response 30 min, restore 8 business hours |
| Change notice | Breaking change ≥ 60 days with a parallel-run version; non-breaking ≥ 10 business days; contract test suite run by the copilot CI against the CRM test environment |
| Maintenance windows | Outside business hours only (for example Sat 22:00 to Sun 06:00 GST) **[confirm]**; announced ≥ 5 business days ahead |
| Deprecation policy | Versioned API (`/v1`, `/v2`); N-1 supported ≥ 6 months after N is live |
| Data retention by provider | CRM keeps written call reports and tasks per the Bank's CRM records policy; access logs per CRM standard; the copilot's identity on each write is recorded (managed identity plus RM `oid`) |
| Idempotency | **Required:** the CRM stores and enforces uniqueness on `external_reference` and supports lookup by it (C7). If it cannot, the outbox keeps the guarantee but double-post risk on CRM-side timeouts remains: [confirm in Phase 0, plan §10.8] |
| Client-side protections | Reads and entitlements: timeout min(1,500 ms, remaining − 100 ms), 1 retry only on connection reset or 503 before response bytes (§2.2), breaker open 60 s; writes: timeout min(1,000 ms, remaining − 250 ms), no inline retry, breaker after 3 consecutive failures, item goes to the outbox (C7); bulkhead 10 in-flight per pod; entitlement cache ≤ 60 s (Cosmos 50 s + in-process 10 s) invalidated by `portfolio.changed` / `delegation.changed` events (02 §2.3) |
| Degraded behaviour | Entitlement source down and cache cold: **deny** with `503 DEPENDENCY_UNAVAILABLE` (never allow on failure), no customer data. Profile down: UC1 and UC3 unavailable (CRM is also the host of the sidebar). Writes down: items `queued`, RM told "saved, will post when CRM is back" |
| Error contract | Per §2.3 (critical dependency). Specific: 401/403 from CRM → `503 DEPENDENCY_UNAVAILABLE` plus security alert (our identity was rejected, not the RM); 404 for a customer id → `NOT_ENTITLED` only if the entitlement check already denied, otherwise `404 NOT_FOUND` without revealing existence; 409 duplicate `external_reference` → success (read the existing item) |
| Owner / contact | CRM platform owner (Bank IT, CRM product team); resolver group "CRM L2"; escalation CRM service manager |
| Monitoring and alert | `dependency_latency_seconds{dep="crm",op}`, error ratio, breaker state, outbox depth and age; alerts: breaker open > 2 min (P2), outbox oldest item > 30 min (P2), write error ratio > 5% over 15 min (P2), portfolio event lag > 5 min (P3), 401/403 from CRM (P1 security triage) |

### IF-02 Core banking ODS (read replica)

| Attribute | Agreement |
|---|---|
| Purpose | Balances, aggregated transactions, product holdings for UC1 (plan §3.6, C14: the copilot never calls the core system directly) |
| Direction and operations | Read only: `GET /customers/{id}/accounts` (balances, currency, status), `GET /customers/{id}/transactions/summary?months=6` (monthly inflow/outflow aggregates, top counterparties as categories, no individual transaction lines) |
| Protocol and auth | HTTPS REST via APIM; managed identity of `tool-gateway` to APIM, APIM to ODS with client certificate from Key Vault or managed identity **[confirm]**; APIM-signed `x-rm-oid` |
| Data classification and fields | Restricted. Account number masked to last 4 digits, IBAN never returned, balances in AED with currency, aggregates only (plan §4.1 step 6) |
| Volumetrics | 2 × 1,500 = **3,000/day**; peak 2 × 0.139 = **0.28 req/s**; contracted **1 req/s** |
| Availability (business hours) | Required ≥ 99.5% (degradable: the transaction-signals section is flagged when ODS fails, 02 §2.3). Note: `get_facilities` also reads balances from the ODS (02 §3.4); the LLD treats facilities as critical, so ODS balance reads inherit the LOS requirement below unless facilities without balances are allowed [confirm with product owner] |
| Latency p95 | Required ≤ 800 ms |
| Freshness | Intraday; replication lag ≤ 15 min **[confirm]**; the response carries `as_of` which the brief shows |
| Support hours | Business hours; P1 response 30 min; restore 8 business hours |
| Change notice / maintenance / deprecation | As IF-01 |
| Data retention by provider | No copilot data stored; access logs per data platform standard |
| Client-side protections | Timeout min(1,500 ms, remaining − 100 ms); 1 read retry per §2.2; breaker; bulkhead 10 in-flight per pod; APIM quota per consumer protects the replica (C14) |
| Degraded behaviour | Partial brief: "Transaction activity unavailable (source not reachable at 10:42)"; `mode: partial` |
| Error contract | As IF-01, plus 404 account → empty section, not an error |
| Owner / contact | Data platform owner (ODS); resolver group "Data Platform L2" |
| Monitoring and alert | Latency, errors, breaker; `as_of` lag > 60 min (P3); breaker open > 5 min (P3) |

### IF-03 Lending and trade finance (LOS)

| Attribute | Agreement |
|---|---|
| Purpose | Facilities, limits, utilisation, covenants, upcoming reviews, LCs and guarantees for UC1 |
| Direction and operations | Read only: `GET /customers/{id}/facilities`, `GET /customers/{id}/trade-instruments?status=open` |
| Protocol and auth | HTTPS REST via APIM; as IF-02 |
| Data classification and fields | Restricted. Facility type, limit, utilisation, expiry and review date, covenant names and status (no covenant test workings), LC/guarantee counts and amounts by status |
| Volumetrics | 2 × 1,500 = **3,000/day**; peak **0.28 req/s**; contracted **1 req/s** |
| Availability (business hours) | **Required ≥ 99.8% pilot, ≥ 99.9% scale**: `get_facilities` is **critical for UC1** in the LLD (02 §3.4, §2.3), so LOS is on the UC1 critical path (§9). If the product owner accepts a brief without facilities (flagged), LOS becomes degradable and ≥ 99.5% suffices |
| Latency p95 | Required ≤ 800 ms |
| Freshness | Daily (end-of-day batch), shown as `as_of` |
| Support hours | Business hours; P1 targets as IF-01 (facilities are critical for UC1) |
| Change notice / maintenance / deprecation | As IF-01 |
| Client-side protections | Timeout min(1,500 ms, remaining − 100 ms); 1 read retry per §2.2; breaker; bulkhead 10; separate breakers for lending and trade so one does not hide the other |
| Degraded behaviour | Facilities (critical in the LLD): UC1 returns `503 DEPENDENCY_UNAVAILABLE`. Trade instruments only: partial brief, section flagged |
| Error contract | As IF-02 |
| Owner / contact | Lending systems owner and trade finance systems owner (two resolver groups) |
| Monitoring and alert | As IF-02 |

### IF-04 KYC / CDD

| Attribute | Agreement |
|---|---|
| Purpose | KYC status, next review date, risk rating band for UC1 |
| Direction and operations | Read only: `GET /parties/{id}/kyc-summary` |
| Protocol and auth | HTTPS REST via APIM; as IF-02 |
| Data classification and fields | Restricted. Status, review due date, risk band (low/medium/high), open document requests count. **No** national ID, passport, UBO identity documents or screening hit details (minimisation) |
| Volumetrics | **1,500/day**; peak **0.14 req/s**; contracted **0.5 req/s** |
| Availability (business hours) | Required ≥ 99.0% (non-critical source, plan §4.1 step 5) |
| Latency p95 | Required ≤ 500 ms |
| Support hours | Business hours; P3 targets for copilot-only impact |
| Client-side protections | Timeout min(1,200 ms, remaining − 100 ms); retry per §2.2; breaker; bulkhead 5 |
| Degraded behaviour | Partial brief: "KYC status unavailable, check the KYC system before the meeting" (warning `SOURCE_UNAVAILABLE`, section `kyc`) |
| Error contract | As IF-02 |
| Owner / contact | Financial crime operations technology owner |
| Monitoring and alert | Error ratio > 20% over 30 min (P3) |

### IF-05 Policy document management system (DMS)

| Attribute | Agreement |
|---|---|
| Purpose | Source of approved policies, product guides, circulars and authority matrix for the UC2 index (ingestion only; RMs open the cited document in the DMS through a deep link) |
| Direction and operations | Pull: `GET /documents?status=approved&changed_since=` (delta), `GET /documents/{id}/versions/{v}/content`, `GET /documents/{id}/metadata`. Push: `document.published`, `document.superseded`, `document.withdrawn` events to trigger ingestion |
| Protocol and auth | HTTPS REST via APIM; managed identity of `ingestion-worker`; read-only service principal scoped to approved libraries |
| Data classification and fields | Confidential. Content plus metadata: document id, version, title, owner, status, `effective_from`, `effective_to`, `superseded_by`, clearance level, language, product tags |
| Volumetrics | Corpus assumption 1,500 documents, about 45,000 pages. Delta: about 20 changed documents/day (≈ 600 pages); delta poll every 15 min (52/day) plus events; weekly reconciliation listing. Peak irrelevant (asynchronous) |
| Availability | Required ≥ 99.0% business hours; not on any user critical path |
| Freshness OLA | Required: a published or withdrawn document is reflected in the index within **4 business hours** (target 30 min). Withdrawal and supersession are priority events: target 15 min, because a superseded policy still cited is a conduct risk (see 05 failure modes) |
| Metadata quality OLA | Required: every document has owner, status, `effective_from`, clearance; documents missing them are quarantined, not indexed |
| Support hours | Business hours; DMS owner P2 for missing withdrawal events |
| Change notice | As IF-01 |
| Data retention by provider | DMS is the system of record; the copilot keeps chunks and embeddings for index rebuild (plan §3.2) and deletes them when the source version is withdrawn plus 30 days |
| Client-side protections | Timeout 10 s per content fetch; retries with backoff (idempotent, no user deadline); breaker; ingestion bulkhead separate from user traffic |
| Degraded behaviour | Index stays at last good state; UC2 continues; staleness banner if last successful sync > 1 business day |
| Error contract | 404 on a listed document → quarantine and alert the owner; 403 → configuration alert |
| Owner / contact | Policy office (content owner) and ECM platform team (system owner) |
| Monitoring and alert | `ingestion_lag_seconds`, quarantined documents count, last successful sync; alerts: withdrawal event not applied within 30 min (P2), sync failing > 4 h (P3) |

### IF-06 Microsoft Graph (calendar)

| Attribute | Agreement |
|---|---|
| Purpose | The RM's upcoming meetings to pick a customer for UC1 (attendee domains matched to CRM contacts) |
| Direction and operations | Read only: `GET /me/calendarView?startDateTime=&endDateTime=` with `$select=subject,start,end,attendees,location` |
| Protocol and auth | HTTPS to `graph.microsoft.com` over the Microsoft backbone (plan §4.1 note); delegated OBO token with **`Calendars.ReadBasic` only** (C20); no mail scope; email drafts open in Outlook via a compose deeplink |
| Data classification and fields | Confidential (RM's calendar). Subject, time, attendee email domains; body not requested (`ReadBasic` excludes it) |
| Volumetrics | 1 per UC1 (1,500) plus about 4 calendar refreshes per RM per day (1,200) = **2,700/day**; peak about **0.3 req/s** across the tenant; contracted 1 req/s |
| Availability | Graph has no standalone SLA; Microsoft 365 service SLAs apply (Exchange Online 99.9% per Microsoft SLA, [confirm] current figure) |
| Latency p95 | Required ≤ 800 ms; observed values to be measured |
| Throttling | Graph service and mailbox throttling limits apply per app and per mailbox **[confirm current limits]**; our load is far below them |
| Support hours | Microsoft support via the Bank's support plan; M365 platform team owns tenant configuration |
| Change notice / deprecation | Microsoft Graph versioning (v1.0 only, no beta in production); Microsoft 365 message center monitored by the M365 team |
| Data retention by provider | No copilot data stored in Graph; read only |
| Client-side protections | Timeout min(800 ms, remaining − 100 ms); no retry; breaker; bulkhead 5; calendar result request scope |
| Degraded behaviour | RM picks the customer from the CRM record instead (UC1 launched from CRM does not need Graph) |
| Error contract | Per §2.3 (non-critical). 401/403 → consent or Conditional Access issue, configuration alert; 429 → empty meeting list with warning |
| Owner / contact | M365 platform team (Bank); Microsoft via support plan |
| Monitoring and alert | Error ratio, 429 count; alert on any 403 spike (P3) |

### IF-07 News and company registry feed (licensed third party)

| Attribute | Agreement |
|---|---|
| Purpose | Adverse news headlines and trade licence status for UC1 |
| Direction and operations | Read only: `GET /entities/search?name=&licence_no=`, `GET /entities/{id}/news?since=30d`, `GET /entities/{id}/registry` |
| Protocol and auth | HTTPS over the internet through **Azure Firewall Premium egress allow-list** (FQDN rule, TLS inspection per Bank policy [confirm]); API key or OAuth client credentials stored in Key Vault, injected by APIM (the key never reaches pods) |
| Data classification and fields | Outbound query: company name and trade licence number (Confidential: reveals the relationship). Inbound: headline, source, date, URL (URL shown as text only, never rendered as a link, C19), registry status. **Inbound text is untrusted**: Prompt Shields document check and quoted-instruction quarantine before any model call |
| Volumetrics | **1,500/day** (one per brief, request scope only, plan §7); peak **0.14 req/s**; contracted 0.5 req/s. A per-customer daily cache would cut this by about a third but needs a §7 change (proposal, see §10) |
| Availability | Required ≥ 99.0% business hours (non-critical); contract SLA [confirm with vendor] |
| Latency p95 | Required ≤ 1,000 ms |
| Support hours | Vendor business hours overlapping 07:00–20:00 GST [confirm vendor time zone]; P1 response 1 h for total outage |
| Change notice | Contractual ≥ 90 days for breaking changes; ≥ 30 days for licence or pricing terms |
| Data retention by provider | **Contractual:** the vendor must not retain or use the Bank's query terms beyond billing and security logs (≤ 90 days), must process in an approved location, and must not resell query data [confirm with procurement and outsourcing] |
| Client-side protections | Timeout min(1,000 ms, remaining − 100 ms); no retry; breaker; bulkhead 5; per-day call quota in APIM (cost control); flag `news.enabled` (02 §2) disables the source |
| Degraded behaviour | Partial brief: "News unavailable" |
| Error contract | Per §2.3 (non-critical). Malformed, schema-invalid or oversize payload → rejected, warning `SOURCE_UNAVAILABLE`; any item flagged by Prompt Shields is dropped (`NEWS_ITEM_DROPPED`, 02 §9.2) |
| Owner / contact | Vendor manager (Bank procurement) and vendor support; technical owner copilot product team |
| Monitoring and alert | Error ratio, cost counter (calls per day vs contract), injection detections from news (security metric); alerts: daily calls > 120% of plan (P3), Prompt Shields indirect-attack hits on news > 0 (security review) |

---

## 4. Microsoft identity and AI services

For Azure services the "Required" column states what the copilot needs; "Published" quotes Microsoft's SLA. Azure
SLAs are monthly 24×7 financially backed commitments, not predictions; they are used as conservative planning floors
in §9.

### IF-08 Microsoft Entra ID

| Attribute | Agreement |
|---|---|
| Purpose | RM sign-in (MSAL in the CRM sidebar, Teams SSO in the Teams tab), MFA and Conditional Access, step-up for approval (authentication context), OBO for `tool-gateway` and Graph, workload identity for pods, app roles (RM, team lead read-only, operator) |
| Direction and operations | Token issuance (`/oauth2/v2.0/token` incl. OBO grant, federated credential exchange for workload identity), OIDC discovery and JWKS (`/discovery/v2.0/keys`), Conditional Access evaluation |
| Protocol and auth | OIDC / OAuth 2.0 over HTTPS (Microsoft backbone, outbound allowed, plan §4.1); token validation is local (`iss`, `aud`, `tid`, `scp`, `oid`, `auth_time`) against cached JWKS |
| Data classification | Confidential (identity claims) |
| Volumetrics | Interactive sign-ins about 600/day (300 RMs × 2); OBO exchanges about 1,200/day (cached per session); step-up about 600/day (one per approve session at most); workload identity token refresh per pod hourly; JWKS fetch every 15 min per pod. Peak well below 1 token request/s |
| Availability | Required ≥ 99.99%; Published **99.99% per Microsoft SLA, [confirm] current figure** |
| Latency | Token endpoint p95 ≤ 500 ms (cached OBO tokens remove it from most requests; plan latency table "Edge + web-bff + OBO (cached) 150 ms") |
| Support | Microsoft via support plan; Bank identity team owns app registrations, Conditional Access, PIM |
| Change notice | Microsoft Entra change announcements; MSAL library updates tracked monthly |
| Data retention by provider | Sign-in and audit logs per Entra retention (exported to Log Analytics and Sentinel) |
| Client-side protections | OBO token cache per session (until 5 min before expiry); OBO call on cache miss timeout 1,000 ms, 1 retry on connection error; JWKS cached 24 h and refreshed on unknown `kid`; if Entra is unreachable, existing valid tokens keep working until expiry (no new sessions, no step-up so no approvals) |
| Degraded behaviour | New sign-ins and approvals impossible; read requests with valid cached tokens continue |
| Error contract | `invalid_grant` / `interaction_required` → `STEP_UP_REQUIRED` or re-auth; `AADSTS` throttling → retry once |
| Owner / contact | Bank identity and access management team |
| Monitoring and alert | Token acquisition failures, step-up failure ratio, sign-in risk events in Sentinel; alert OBO failure ratio > 5% over 10 min (P2) |

### IF-09 Azure OpenAI via Azure AI Foundry (through the APIM AI gateway)

| Attribute | Agreement |
|---|---|
| Purpose | `gen-large` (UC1 wording, UC2 answer), `gen-small` (UC2 rewrite, UC3 extraction, local groundedness check), `embed` (UC2 query embeddings, ingestion) |
| Direction and operations | `POST /openai/deployments/{deployment}/chat/completions` (or the Responses API with `store=false` [confirm API choice]), `POST /openai/deployments/embed/embeddings`. Always via APIM (`validate-jwt`, `llm-token-limit`, `llm-emit-token-metric`, backend pool with priority and circuit breaker, `retry`) |
| Protocol and auth | HTTPS over private endpoint; APIM uses its managed identity (Entra, `disableLocalAuth` on the Foundry resource, no API keys); pods authenticate to APIM with workload identity tokens |
| Data classification | Restricted input (masked facts, masked notes, policy passages), Restricted output; `store=false`; content filters with Prompt Shields on; abuse monitoring modified (gate C17) |
| Volumetrics | `gen-large`: 1,500 + 2,640 = **4,140 calls/day**, peak **0.38 calls/s (23/min)**, about **22.9 M tokens/day**, peak ≈ **127k TPM**. `gen-small`: 2,400 + 2,640 + 900 = **5,940 calls/day**, peak **0.55 calls/s (33/min)**, about **10.6 M tokens/day**, peak ≈ **59k TPM**. `embed`: 2,400 queries/day (≈ 0.12 M tokens) plus ingestion bursts. Derivation in 05 §2 |
| Availability | Required: none on the availability SLO (model loss = degraded mode, C3), but **non-degraded rate ≥ 99.5%** monthly. Published **99.9% per Microsoft SLA, [confirm] current figure** for Standard deployments; provisioned deployments additionally carry a latency commitment **[confirm]** |
| Latency p95 | Required: TTFT ≤ 1,000 ms (UC1, UC2), ≤ 700 ms (`gen-small`), output rate sufficient for 200 tokens in ≤ 3.3 s (plan latency table). No latency SLA on Standard; this is a monitored objective |
| Throughput | TPM quota per deployment: `gen-large` ≥ 250k TPM, `gen-small` ≥ 120k TPM, `embed` ≥ 350k TPM (2× peak; see 05 §2.4) **[confirm regional quota availability]** |
| Support | Microsoft via support plan (Sev A for total outage) |
| Change notice / deprecation | Model versions retire on a published schedule (about yearly, C12); deployment auto-upgrade **off**; Microsoft retirement notices monitored; 90-day upgrade runbook (06 §9.5) |
| Maintenance | None scheduled by contract; regional incidents published on Azure Service Health |
| Data retention by provider | `store=false`: no stored completions. Abuse monitoring: up to 30 days with possible human review **unless modified abuse monitoring is approved** (plan §3.5, C17) [confirm eligibility and review location]. No training on customer data per Azure OpenAI data terms |
| Client-side protections | Deadline: model cap = remaining deadline − 300 ms; APIM backend pool: priority 1 = primary deployment, priority 2 = secondary deployment in UAE North (separate resource and quota, Regional only, plan §3.1); APIM breaker trips on 429/5xx; failover only if ≥ 4 s remain; `max_tokens` set per use case (UC1 400, UC2 300, UC3 800) because the rate limiter counts `max_tokens` into the estimate; APIM `llm-token-limit` per RM (400k/day) and per use case; bulkhead: separate APIM products for user traffic, ingestion and evals |
| Degraded behaviour | UC1 data-only brief, UC2 ranked passages without a generated answer, UC3 manual call report (C3); flagged and counted as `degraded` |
| Error contract | 429 (`retry-after-ms`, `retry-after`): APIM retries on the other backend only if ≥ 4,000 ms remain, else `200 mode: degraded` (`DEGRADED/MODEL_UNAVAILABLE` or `MODEL_DEADLINE`); 400 with `code=content_filter` or `finish_reason=content_filter` → `DEGRADED/CONTENT_FILTER` (or `422 GUARDRAIL_BLOCKED/CONTENT_FILTER` when the RM's own input triggered it), with the filter category recorded as metadata, never the text; 408/500/503 → pool failover, then degraded; 401/403 → configuration P1 |
| Owner / contact | Copilot product team (deployments, prompts); cloud platform team (Foundry resource, quota requests); Microsoft |
| Monitoring and alert | APIM token metrics (`llm-emit-token-metric`: prompt/completion tokens by deployment, use case, pseudonymous RM), 429 ratio, TTFT and total latency, content filter block rate, degraded rate; alerts: 429 ratio > 5% over 10 min (P2), degraded rate > 5% over 30 min (P2), TPM utilisation > 70% of quota at peak (capacity ticket), content filter block rate > 2% daily (C13 review) |

### IF-10 Azure AI Content Safety

| Attribute | Agreement |
|---|---|
| Purpose | Prompt Shields (user prompt attacks; document attacks on fetched text such as CRM notes and news), harm categories; groundedness detection only if offered in UAE North (C2) |
| Direction and operations | `POST /contentsafety/text:shieldPrompt`, `POST /contentsafety/text:analyze`, optionally `POST /contentsafety/text:detectGroundedness` **[confirm API versions and regional availability, plan §10.5]** |
| Protocol and auth | HTTPS over private endpoint; managed identity (`disableLocalAuth`) |
| Data classification | Restricted (masked text) |
| Volumetrics | Shield calls: UC1 1,500 + UC2 2,400 + UC3 900 = **4,800/day**, peak **0.44 req/s**; groundedness (if used) 2,640/day. Input size limits per call apply (user prompt and documents character limits) **[confirm]**; long inputs are split |
| Availability | Required: non-critical (see degraded behaviour). Published **99.9% per Microsoft SLA, [confirm] current figure** |
| Latency p95 | Required ≤ 300 ms (runs in parallel with the fan-out, plan latency table) |
| Data retention by provider | Input processed and not stored **[confirm]** |
| Client-side protections | Timeout min(500 ms, remaining − reserve), runs in parallel with the fan-out; retry per §2.2; breaker |
| Degraded behaviour | Soft dependency (02 §9): the Azure OpenAI deployment's integrated content filter with Prompt Shields stays on, and local injection and quote-quarantine rules always run (plan §3.3 "primary injection defence"). If the standalone service is down, UC1 and UC2 continue on local rules, UC3 continues with a visible notice (the RM reviews every draft and nothing commits without approval); audit records `guardrail_degraded`. InfoSec may require a stricter posture (fail into degraded mode) [decision to confirm] |
| Error contract | 400 (input too long) → split and retry; 429/5xx → as above |
| Owner / contact | Copilot product team; Microsoft |
| Monitoring and alert | Attack detections by source (user, CRM notes, news, policy docs), latency, error ratio; alert any `documentAttack` detection in policy documents (P2, possible poisoned document) |

### IF-11 Azure AI Language (PII detection)

| Attribute | Agreement |
|---|---|
| Purpose | Mask PII before any model call; PII scan of policy documents in ingestion |
| Direction and operations | `POST /language/:analyze-text` with `kind=PiiEntityRecognition` (synchronous); categories restricted to the Bank's list **[confirm API version and Arabic coverage, plan §10.5]** |
| Protocol and auth | HTTPS over private endpoint; managed identity |
| Data classification | Restricted |
| Volumetrics | UC1 2 × 1,500 + UC2 2,400 + UC3 2 × 900 = **7,200 calls/day** (each call up to the per-document character limit and documents-per-request limit **[confirm]**); peak about **0.65 req/s**; ingestion about 600 pages/day off-peak |
| Availability | Required: non-critical for availability, critical for model calls. Published **99.9% per Microsoft SLA, [confirm] current figure** |
| Latency p95 | Required ≤ 300 ms per call |
| Data retention by provider | Synchronous requests not stored; asynchronous job results retained for a limited period then deleted **[confirm]** |
| Client-side protections | Regex masking for Emirates ID, IBAN, card, UAE phone, email, trade licence **always on** (plan §3.3, 02 §9.1); timeout min(500 ms, remaining − reserve), parallel with the fan-out; retry per §2.2; breaker |
| Degraded behaviour | Soft dependency (02 §9): regex masking continues; in UC1 customer and contact names are already replaced by placeholders before the model call (02 §9.2 step 3), and known names from tool output are masked; UC2 and UC3 continue on regex masking with `pii_degraded` recorded in audit. DPO to confirm this posture, in particular for UC3 free-text notes [confirm] |
| Error contract | 400 too long → split; 429/5xx → as above |
| Owner / contact | Copilot product team; Microsoft |
| Monitoring and alert | Entities masked per category (counts only), latency, errors; alert error ratio > 10% over 10 min (P2) |

### IF-12 Azure AI Search

| Attribute | Agreement |
|---|---|
| Purpose | Hybrid retrieval (BM25 + vector + semantic ranker) over policy chunks for UC2, with filters `status = current`, `effective_from <= today`, `clearance <= RM clearance` |
| Direction and operations | Query: `POST /indexes/policies-v{n}/docs/search` (alias `policies` points at the active index). Ingestion: `POST /indexes/{i}/docs/index` (merge or upload, delete on withdrawal). Index management: create, alias swap |
| Protocol and auth | HTTPS over private endpoint; Entra RBAC (`Search Index Data Reader` for `tool-gateway`, `Search Index Data Contributor` for `ingestion-worker`); API keys disabled |
| Data classification | Confidential (policy content); no customer data in the index (deck "data" principle) |
| Volumetrics | **2,400 queries/day**, peak **0.22 QPS**; semantic ranker calls 2,400/day (about 53k/month, billed per 1,000 [confirm]); ingestion about 1,200 chunk upserts/day; full rebuild about 60k chunks (05 §2.5) |
| Availability | Required ≥ 99.9% (critical for UC2). Published: **99.9% for read with ≥ 2 replicas, 99.9% for read-write with ≥ 3 replicas, per Microsoft SLA, [confirm] current figures**. Pilot 2 replicas (read SLA), production 3 (plan §3.3); zone redundancy across replicas **[confirm in UAE North, plan §10.5]** |
| Latency p95 | Required ≤ 400 ms including semantic ranker (plan budget: embed + search + rerank 700 ms) |
| Data retention by provider | Customer-controlled; index content deleted with the index |
| Client-side protections | Timeout min(600 ms, remaining − 100 ms); no retry; breaker; if the query embedding fails (300 ms cap), keyword-only query (BM25 + semantic ranker), flagged `retrieval_mode=keyword` |
| Degraded behaviour | Search down: UC2 unavailable with a clear message and routing to the product or credit desk (counted unavailable). Semantic ranker down or throttled: hybrid without rerank, stricter decline threshold |
| Error contract | 503 (replica busy) → retry once; 207 partial on indexing → re-queue failed keys; 403 → config P1 |
| Owner / contact | Copilot product team; Microsoft |
| Monitoring and alert | Query latency, throttled queries, index document count vs expected, alias target, decline rate; alerts: throttled queries > 1% (P3), document count drop > 2% (P2, possible accidental delete) |

### IF-13 Azure AI Document Intelligence (layout)

| Attribute | Agreement |
|---|---|
| Purpose | Parse policy PDFs and scans (tables, headings, Arabic) in `ingestion-worker` |
| Direction and operations | `POST /documentintelligence/documentModels/prebuilt-layout:analyze` (asynchronous), `GET` result, delete result after reading where the API supports it **[confirm API version and result-retention behaviour]** |
| Protocol and auth | HTTPS over private endpoint; managed identity; documents passed as Blob URLs reachable by the service's managed identity (trusted service access) or as bytes |
| Data classification | Confidential (policies; PII scan follows) |
| Volumetrics | About 600 pages/day delta; one-off initial load about 45,000 pages; full re-parse only if chunks are lost |
| Availability | Required ≥ 99.0% (off critical path). Published **99.9% per Microsoft SLA, [confirm] current figure** |
| Latency | Asynchronous; target ≤ 2 min per 50-page document |
| Data retention by provider | Analysis results kept for a limited period (about 24 h) then deleted, or deleted on request **[confirm]**; plan §3.5 "results deleted after processing" |
| Client-side protections | Polling with backoff; per-document retries (3) then quarantine; concurrency limit to respect transactions-per-second limit **[confirm]** |
| Degraded behaviour | New or changed documents wait; index stays at last good state; withdrawals still applied (they do not need parsing) |
| Owner / contact | Copilot product team; Microsoft |
| Monitoring and alert | Ingestion lag, quarantined documents; alert lag > 4 business hours (P3) |

---

## 5. Azure platform services

### IF-14 Azure Key Vault Premium (HSM-backed keys)

| Attribute | Agreement |
|---|---|
| Purpose | Approval-signing key `approval-signing` (EC-HSM P-256, ES256, `kid` = key version) in vault `kv-rmc-sign-<env>` (`web-bff` sign only, `tool-gateway` public key only); per-customer content keys `cust-<customer_ref>` (RSA-HSM 3072) and the `audit-anchor` signing key in `kv-rmc-cust-<env>`; the `audit-transport` key whose public part producers use to envelope-encrypt audit content (02 §6.6); CMKs for Cosmos DB, Storage, Event Hubs; secrets for non-Entra APIs (news feed, legacy backends) used by APIM |
| Direction and operations | `keys/sign` (`web-bff`, approval key only), `keys/read` public part (`tool-gateway`, cached), `unwrapKey` on `audit-transport` and `wrapKey`/`unwrapKey`/`create` on per-customer keys (`audit-writer`), `wrapKey`/`unwrapKey` (services for CMK), `secrets/get` (APIM named values), key create/rotate (rotation policy and pipeline identity under PIM) |
| Protocol and auth | HTTPS over private endpoint; Entra RBAC data-plane roles per identity; purge protection on; public network off |
| Data classification | Restricted (key material never leaves the HSM) |
| Volumetrics | `sign`: **600/day** (one per approval), peak 0.06/s; public key refresh every 10 min per `tool-gateway` pod; `audit-writer` about 2 key operations per content event (unwrap transport DEK, wrap with the customer key), about 10k to 20k/day; well below vault transaction limits **[confirm current limits for HSM keys and the per-vault key count for one key per customer; LLD suggests Managed HSM at scale]** |
| Availability | Required ≥ 99.99% for UC3 commit (critical). Published **99.99% per Microsoft SLA, [confirm] current figure**; Key Vault is zone-resilient in regions with zones and replicates to the paired region for read-only failover **[confirm behaviour for UAE North / UAE Central]** |
| Latency p95 | Required `sign` ≤ 150 ms |
| Data retention | Soft delete 90 days, purge protection; key versions retained while any approval token or content item may need them |
| Client-side protections | `sign`: timeout min(400 ms, remaining − 150 ms), 1 retry on 429/503 only if ≥ 800 ms remain; public keys cached per `kid` for 10 min and refreshed on unknown `kid` (verification keeps working during a Key Vault outage for known `kid`s); accepted `kid`s are pinned in `rmc:tool-gateway:approval:allowed_kids` (02 §4.7); CMK services cache keys per service behaviour |
| Degraded behaviour | `sign` unavailable → approvals blocked with "Approve unavailable, drafts are saved" (UC3 commit unavailable; reads unaffected). CMK unavailable for an extended period → dependent services lose access (Cosmos, Storage) per their CMK revocation behaviour [confirm timings]: P1 |
| Error contract | 403 → configuration or RBAC drift, P1; 429 → retry per above; 5xx → `503 DEPENDENCY_UNAVAILABLE`, no token minted |
| Owner / contact | Cloud platform team (vault), copilot product team (key usage), InfoSec crypto custodian (key ceremonies, rotation approval) |
| Monitoring and alert | Diagnostic logs (AuditEvent) to Log Analytics and Sentinel; alerts: any `sign` by an identity other than `web-bff` (P1), key near expiry 30 days (P3), 403 spikes (P1) |

### IF-15 Azure Cosmos DB for NoSQL

| Attribute | Agreement |
|---|---|
| Purpose | Sessions (24 h TTL), drafts and approval verdicts (30 days), jti replay store (1 day, atomic create), outbox, entitlement cache (≤ 60 s), saga state (plan §3.3, §7) |
| Direction and operations | Point reads, upserts, `create` with unique `id` for jti (409 on duplicate), change feed for the outbox |
| Protocol and auth | HTTPS over private endpoint; Entra RBAC data plane, `disableLocalAuth`; CMK |
| Data classification | Restricted (drafts, notes) |
| Volumetrics | About 17 RU/s at peak (05 §2.6); about 60k operations/day |
| Availability | Required ≥ 99.99% (critical for all use cases: sessions; commit: jti). Published **99.99% for a single-region account, higher with availability zones (99.995% per Microsoft SLA), [confirm] current figures** |
| Latency p95 | Required point read ≤ 10 ms, write ≤ 15 ms (in-region); published latency SLA for point operations exists **[confirm]** |
| Data retention | TTLs per plan §7; outbox items 30 days after terminal state (proposed addition to §7); continuous backup (7 or 30 days) **[confirm tier]** |
| Client-side protections | Point operations timeout 300 ms; SDK retries on 429 with `x-ms-retry-after-ms` within the deadline; autoscale throughput; separate containers per purpose (`sessions`, `drafts`, `approvals`, `jti`, `saga`, `outbox`, `entitlements`, 02 §6.1) so a hot container cannot starve the jti store |
| Degraded behaviour | Sessions: none (critical). Entitlement cache unavailable: go to the source each time (slower, still correct) |
| Error contract | 409 on jti create → same RM and same draft hash: return the existing saga status; otherwise `409 REPLAYED` (02 §4.5); 412 (etag) → `412 DRAFT_CONFLICT`; 429 → retry within deadline; 503 → `503 DEPENDENCY_UNAVAILABLE` |
| Owner / contact | Copilot product team; cloud platform team |
| Monitoring and alert | Normalised RU consumption, 429 count, server-side latency, availability; alerts: normalised RU > 80% for 10 min (P3), any 5xx burst (P2) |

### IF-16 Azure Event Hubs (audit stream)

| Attribute | Agreement |
|---|---|
| Purpose | Audit event stream; producers wait for the ack (**fail closed**: no answer is released and no write is performed without an acked audit event) |
| Direction and operations | Produce (AMQP) to hubs `audit-events` (metadata, partition key `request_id`) and `audit-content` (envelope-encrypted content) from `tool-gateway`, which also relays `orchestrator` events (the orchestrator holds no Event Hubs rights, 02 §2.2); consume by `audit-writer` (checkpoints in Blob); `crm-portfolio-events` hub in the same namespace carries entitlement invalidation events (02 §2.3) |
| Protocol and auth | AMQP over TLS on private endpoint; Entra RBAC (`Event Hubs Data Sender` / `Receiver`), `disableLocalAuth`; CMK **[confirm CMK support in Standard tier; it may require Premium or Dedicated]** |
| Data classification | Restricted. Metadata in clear inside the private namespace (CMK at rest); content envelope-encrypted by the producer with the `audit-transport` public key, so Event Hubs never holds readable content (02 §6.6) |
| Volumetrics | UC1 10 × 1,500 + UC2 5 × 2,400 + UC3 draft 4 × 900 + commit 6 × 600 = **34,200 events/day**; peak **3.2 events/s**; average event about 10 KB (metadata 2 KB, masked content up to about 50 KB) → about 32 KB/s peak, far below one throughput unit |
| Availability | Required ≥ 99.95% (critical, fail closed). Published: **Standard 99.95%; Premium and Dedicated 99.99%, per Microsoft SLA, [confirm] current figures**. Recommendation: evaluate Premium at scale (§9.4) |
| Latency p95 | Required send-with-ack ≤ 50 ms (not in the plan latency table; it runs once per step and is budgeted inside each step) |
| Data retention | `audit-events` 3 days, `audit-content` 1 day (02 §2.6) (Standard maximum 7 days [confirm]); the WORM store is the record |
| Client-side protections | Send with ack: timeout 300 ms, 1 retry within the deadline; producer client reused per pod; no local disk buffering (would weaken fail-closed semantics) |
| Degraded behaviour | **None. Fail closed** (`503 DEPENDENCY_UNAVAILABLE`): a tool result is not released without its audit event (02 §2.3). UC1/UC2 show "temporarily unavailable"; UC3 drafts remain in Cosmos but commit is blocked |
| Error contract | `ServerBusy` → retry once; `QuotaExceeded` → P2 (raise TUs/auto-inflate) |
| Owner / contact | Copilot product team; cloud platform team |
| Monitoring and alert | Incoming/outgoing messages, throttled requests, consumer lag (`audit-writer`, alert at 5 min, 02 §2.6), producer ack failures; alerts: any audit-caused `DEPENDENCY_UNAVAILABLE` (P1 if > 1 min), consumer lag > 5 min (P2) |

### IF-17 Azure Blob Storage (immutable audit store, content store, document store)

| Attribute | Agreement |
|---|---|
| Purpose | WORM audit metadata hash chain (time-based retention 7 years); audit content encrypted per customer key (1 year); policy document staging and stored chunks/embeddings for index rebuild; Event Hubs checkpoints |
| Direction and operations | Append/put blobs (`audit-writer`), immutability policy (version-level, locked), reads for audit replay and index rebuild |
| Protocol and auth | HTTPS over private endpoints (blob); Entra RBAC; shared key access disabled; CMK; **GZRS** (secondary in the paired region UAE Central) |
| Data classification | Restricted (audit), Confidential (documents) |
| Volumetrics | Audit metadata about 70 MB/day (about 25 GB/year, about 175 GB over 7 years); content about 160 MB/day (about 60 GB rolling for 1 year); documents and chunks about 30 GB |
| Availability | Required ≥ 99.9% for writes from `audit-writer` (asynchronous after the Event Hubs ack, so not on the user path). Published **GZRS hot tier 99.9% (99.99% for reads with RA-GZRS), per Microsoft SLA, [confirm] current figures**; durability per Microsoft documentation **[confirm]** |
| RPO | Geo-replication is asynchronous; Microsoft states a typical RPO of under 15 minutes without an SLA **[confirm]**; plan §5 audit RPO ≤ 15 min on region loss |
| Data retention | WORM 7 years; content 1 year with crypto-shredding on erasure; documents per DMS |
| Client-side protections | `audit-writer` checkpoints only after the hash-chain append succeeds; retries with backoff (Event Hubs retains events) |
| Degraded behaviour | Blob write outage → consumer lag grows; no user impact until Event Hubs retention is at risk (P1 at 12 h lag) |
| Owner / contact | Copilot product team; records management (retention policy owner) |
| Monitoring and alert | Availability, success E2E latency, last chain append time, nightly chain verification job; alerts: chain verification failure (P1), no append for 15 min in business hours (P2) |

### IF-18 Azure App Configuration

| Attribute | Agreement |
|---|---|
| Purpose | Feature flags, **kill switches** per use case and for all writes, prompt and model version pins, cohort targeting for progressive rollout |
| Direction and operations | Read with sentinel key `rmc:sentinel` polled every 15 s (02 §2); writes only via the pipeline or the Sentinel kill-switch playbook |
| Protocol and auth | HTTPS over private endpoint; Entra RBAC (`App Configuration Data Reader` for pods); `disableLocalAuth` |
| Volumetrics | About 15 pods × 240 refreshes/h = 3,600 requests/h, inside the Standard tier request quota **[confirm current quota]** |
| Availability | Required ≥ 99.9%; not on the request path (last-known-good). Published **Standard 99.9% per Microsoft SLA, [confirm] current figure**; geo-replicas raise it **[confirm]** |
| Kill switch propagation OLA (ours) | Effective in all pods ≤ 30 s after the flag change (15 s refresh, 02 §2) |
| Client-side protections | Sentinel key `rmc:sentinel` polled every 15 s (02 §2); last loaded values stay in force if the store becomes unreachable, **except the write kill switch, which fails closed**: if `writes.enabled` has not been refreshed for > 5 min, writes are treated as disabled. A pod that cannot load configuration at start is not ready |
| Owner / contact | Copilot product team; change via CAB-approved pipeline; kill switch by the on-call lead or Sentinel playbook |
| Monitoring and alert | Config refresh age per pod (alert > 5 min, P2); every change audited (Activity log and audit event `config_changed`) |

### IF-19 Azure API Management (Premium, internal VNet)

| Attribute | Agreement |
|---|---|
| Purpose | AI gateway in front of Azure OpenAI; system API facade in front of the systems of record |
| Direction and operations | Inbound from `orchestrator`, `tool-gateway`, `ingestion-worker`, `eval-runner`; outbound to Foundry/Azure OpenAI (private endpoint), Bank systems (ExpressRoute), news feed (firewall) |
| Protocol and auth | HTTPS; `validate-jwt` on workload identity tokens; managed identity to backends; client certificates for legacy backends; request/response body logging **off** (body bytes 0), semantic cache **off** (C8) |
| Volumetrics | System facade about 17,700 calls/day, AI gateway about 12,500 calls/day; total about 30k/day, peak about 3 req/s; far below one unit's capacity |
| Availability | Required ≥ 99.95% (critical for everything). Published **Premium 99.95% single region, 99.99% multi-region, per Microsoft SLA, [confirm] current figures**. Pilot 1 unit, production ≥ 2 units across zones (plan §3.3) **[confirm zone-redundancy unit rules, plan §10.6]**; see §10 on the pilot single unit |
| Latency | Gateway overhead p95 ≤ 20 ms |
| Change notice | Microsoft APIM release notes and platform updates; our policies versioned in Git (APIOps) |
| Client-side protections | Client timeouts derived from deadlines; APIM is in front of every external call, so it is a single point: mitigated by zone-redundant units at scale |
| Degraded behaviour | None (critical). Requests fail fast with `503 DEPENDENCY_UNAVAILABLE` |
| Error contract | APIM-generated 401 (JWT), 429 (`rate-limit`, `llm-token-limit` with `Retry-After`), 503 (all backends broken) are distinguished from backend errors by the `x-apim-error-source` header we set in `on-error` |
| Owner / contact | Cloud platform team (instance), copilot product team (APIs and policies) |
| Monitoring and alert | Gateway requests, capacity metric, backend latency by API, 4xx/5xx by source; alerts: capacity > 70% for 30 min (P3), gateway 5xx ratio > 2% for 5 min (P1) |

### IF-20 Azure Kubernetes Service (private cluster)

| Attribute | Agreement |
|---|---|
| Purpose | Runs all copilot services in namespace `rm-copilot` |
| Operations | Kubernetes API (CI/CD via GitOps or private runners only), workloads, KEDA scaling, workload identity |
| Protocol and auth | Private API server; Entra integration with Kubernetes RBAC, local accounts off; PIM for cluster admin |
| Volumetrics | About 3 concurrent requests at peak; sizing driven by HA (05 §2.7) |
| Availability | Required ≥ 99.95% for the data plane. Published: **Standard tier uptime SLA for the API server 99.95% with availability zones (99.9% without), per Microsoft SLA, [confirm]**; nodes are VMs: **99.99% for VMs across two or more zones per Microsoft SLA, [confirm]**. The API server SLA does not cover running workloads; workload availability is our design (≥ 3 replicas per service, topology spread across 3 zones, PodDisruptionBudgets) |
| Maintenance | Planned maintenance windows configured outside business hours (Sat 22:00 to Sun 06:00 GST) for node image and Kubernetes upgrades; auto-upgrade channel `patch` for node images [confirm Bank standard] |
| Deprecation | Kubernetes minor versions supported for a limited window (N-2 policy) **[confirm]**; upgrade at least twice a year |
| Owner / contact | Cloud platform team (cluster), copilot product team (workloads) |
| Monitoring and alert | Container Insights, managed Prometheus, node readiness, pod restarts, pending pods; alerts: any service below 2 ready replicas (P1), node NotReady > 5 min (P2) |

### IF-21 Azure Application Gateway WAF v2 (internal)

| Attribute | Agreement |
|---|---|
| Purpose | Only ingress for the CRM sidebar and the Teams tab from managed devices on the corporate network or Entra Private Access |
| Operations | TLS termination (certificate from Key Vault), WAF (OWASP ruleset), routing to `web-bff` |
| Volumetrics | About 5,400 API requests/day plus static assets; peak about 5 req/s including assets |
| Availability | Required ≥ 99.95%. Published **99.95% per Microsoft SLA, [confirm] current figure**; deployed across 3 zones with autoscale minimum 2 instances |
| Latency | ≤ 20 ms overhead p95 |
| Client-side protections | n/a (edge); health probes on `web-bff` `/healthz/ready` |
| Owner / contact | Cloud platform team (network); copilot product team (WAF exclusions, reviewed by InfoSec) |
| Monitoring and alert | Unhealthy backend count, WAF blocks (false positives tracked), 5xx; alerts: unhealthy hosts > 0 for 5 min (P2), WAF block spike (security triage) |

### IF-22 Azure Monitor (Application Insights, Log Analytics, managed Prometheus, Managed Grafana)

| Attribute | Agreement |
|---|---|
| Purpose | Traces, logs, metrics, alerts, dashboards; restricted audit metadata table for audit queries in the pilot |
| Operations | OpenTelemetry export (Azure Monitor exporter) via AMPLS and data collection endpoints; PodMonitor scraping of `/metrics`; alert rules; KQL queries |
| Protocol and auth | HTTPS via Azure Monitor Private Link Scope; Entra auth for ingestion (local auth off on App Insights) **[confirm]** |
| Data classification | Internal (**metadata only**, C5) except the restricted audit metadata table (Confidential: RM `oid`, customer id hash, action) |
| Volumetrics | Estimate 3 to 6 GB/day at scale (traces ≈ 0.3 GB, container and platform logs the rest) [estimate]; metrics about 50k active series |
| Availability | Required ≥ 99.9% (not on the user path; telemetry is asynchronous and dropped rather than blocking). Published **Log Analytics and Application Insights 99.9% per Microsoft SLA, [confirm] current figures**; managed Prometheus and Managed Grafana per their SLAs **[confirm]** |
| Data retention | Telemetry 90 days; metrics 18 months (plan §7); restricted audit table retention [confirm, see §10] |
| Client-side protections | Batch export with bounded in-memory queue; drop on overflow (never block a request); sampling 100% for errors, adaptive for success |
| Owner / contact | Cloud platform team (workspace), copilot product team (instrumentation), SOC (Sentinel) |
| Monitoring and alert | Ingestion heartbeat (alert if no data for 10 min in business hours, P2), daily cap warnings, content-capture policy compliance |

---

## 6. Interface Control Document (ICD) template

Each interface gets an ICD in the Bank's architecture repository, version-controlled, signed off by both owners. The
tables in §3 to §5 are the first draft of each ICD.

```markdown
# ICD-<nn> <System name> ↔ SME RM Copilot            Version x.y · Status draft/agreed · Review date

## 1. Parties and owners
| Role | Name / team | Contact | Deputy |
| Provider system owner | | | |
| Consumer owner (copilot product owner) | | | |
| Technical contacts (both sides) | | | |
| Escalation (service managers) | | | |

## 2. Purpose and scope
Business purpose, use cases served (UC1/UC2/UC3), in scope / out of scope operations.

## 3. Operations
| Operation | Method / path / event | Direction | Idempotent? | Request schema ref | Response schema ref | Contract version |

## 4. Transport and security
Network path (ExpressRoute / private endpoint / firewall egress), TLS version and cipher policy, authentication
(OBO / managed identity / mTLS / API key in Key Vault), authorisation model, signed RM identity header, IP or private
endpoint allow-lists, secret and certificate rotation owner and schedule.

## 5. Data
Classification, field list with purpose (minimisation justification), masking rules, residency of processing,
provider-side retention and logging of consumer data, deletion on request, lawful basis reference.

## 6. Volumetrics
Average and peak calls/s, daily calls, payload sizes, growth forecast (pilot, scale), contracted peak, quota
enforcement (APIM product and limits).

## 7. Service levels
| Measure | Required | Provider commitment | Measurement method and source |
| Availability (business hours Mon–Fri 07:00–20:00 GST) | | | |
| Latency p95 / p99 per operation | | | |
| Throughput | | | |
| Freshness / replication lag | | | |
| Support hours | | | |
| Incident response / restore by severity P1–P4 | | | |
| Maintenance windows and notice | | | |
| Change notice (breaking / non-breaking) | | | |
| Deprecation policy (versions supported, overlap) | | | |
| Planned capacity review cadence | | | |

## 8. Error contract
Status codes and error bodies the provider returns; mapping to copilot codes; retry-ability; rate-limit headers.

## 9. Consumer-side protections
Timeouts, retries, circuit breaker, bulkhead, cache, degraded behaviour shown to the RM.

## 10. Monitoring, reporting and testing
Metrics each side exposes, synthetic probes, alerts and who receives them, monthly service report, contract tests in
CI, joint DR or failover tests and their frequency.

## 11. Change and incident procedure
How changes are notified (CAB references), how incidents are raised (resolver groups, bridge), post-incident review
obligations.

## 12. Sign-off
Provider owner · Consumer owner · InfoSec · Data protection officer · Architecture (date, version)
```

---

## 7. RACI

R = responsible, A = accountable, C = consulted, I = informed. Roles: **PO** copilot product owner (SME banking);
**CT** copilot engineering and SRE team; **CP** cloud platform team (landing zone, AKS, APIM instance, network);
**SO** system-of-record owner (CRM, ODS, LOS, KYC, DMS, M365); **IS** InfoSec and SOC; **DPO** data protection
officer; **MR** model risk management (second line); **VM** vendor management and procurement; **MS** Microsoft
(via the support plan).

| Activity | PO | CT | CP | SO | IS | DPO | MR | VM | MS |
|---|---|---|---|---|---|---|---|---|---|
| Define interface need, fields and minimisation | A | R | C | C | C | C | I | I | |
| Publish and version the provider API | I | C | C | A/R | C | | | | |
| Negotiate OLA / SLA, sign ICD | A | R | C | R | C | C | I | R (third parties) | |
| Capacity and quota (APIM limits, model TPM) | I | R | A | C | | | | | C |
| Network path, private endpoints, firewall rules | I | C | A/R | C | C | | | | |
| Identity: app registrations, OBO, workload identity | I | R | C | C | A | | | | |
| Secret and certificate rotation | I | R | R | R | A | | | | |
| Contract tests in CI | I | A/R | | C | | | | | |
| Monitoring and alerting of the interface | I | A/R | R | C | I | | | | |
| Incident response on interface failure | I | R | R | R | C | | | | C/R |
| Breaking change notice and joint testing | I | R | C | A/R | I | | | | |
| Data protection impact assessment | C | R | | C | C | A | C | | |
| Third-party (news vendor) due diligence and contract | C | C | | | C | C | | A/R | |
| Model deployment, version pin, retirement plan | C | R | C | | C | | A (approval) | | I |
| Kill switch decision (AI incident) | A | R | I | I | C | C | C | | |
| SLO reporting to the business | A | R | C | I | I | | I | | |

---

## 8. The copilot's own SLOs, error budgets and reporting

### 8.1 Service level indicators and objectives

All SLOs are measured in **business hours** (Mon–Fri 07:00–20:00 GST, UAE public holidays excluded [confirm]) over a
rolling 4-week window and reported per calendar month. Measurement source: `web-bff` request events (server side)
plus synthetic probes (§8.6). Plan §5 is the source of the targets.

| SLO | SLI definition | Pilot | Scale |
|---|---|---|---|
| **Availability** | Good = request returns `200` with `mode` `ok`, `partial` or `degraded` with a flagged, verified response within the use-case deadline; or a correct business refusal (`NOT_ENTITLED`, `BUDGET_EXCEEDED`, a UC2 decline with routing). Bad = 5xx (including audit-caused `DEPENDENCY_UNAVAILABLE`), `DEADLINE_EXCEEDED` without a usable response, client-visible error page, or synthetic probe failure. Excluded from the denominator: requests rejected for invalid client input (4xx caused by the client), pre-announced maintenance outside business hours | **99.0%** | **99.5%** |
| Kill switch time | Minutes a use case is off by an operator-set kill switch in business hours | **Not counted** against the availability SLO (`FEATURE_DISABLED`, 02 §1.4) so that no one hesitates to pull it; reported separately as "risk-controlled downtime" with its own monthly review; a kill switch held for > 1 business day is escalated to the steering committee | Same |
| Latency UC1 | p95 of end-to-end verified output time for good requests | ≤ 8 s | ≤ 8 s |
| Latency UC2 | as above | ≤ 7 s | ≤ 7 s |
| Latency UC3 draft | as above | ≤ 10 s | ≤ 10 s |
| Latency commit | as above | ≤ 2 s | ≤ 2 s |
| Degraded rate | Share of good requests that were `degraded` or `partial` | ≤ 5% | ≤ 3% |
| Write delivery | Share of approved items `committed` within 15 min; `queued` items delivered within 30 min of CRM recovery; no item silently lost | 99% within 15 min; 100% resolved to `committed` or `failed` (visible) within 24 h | same |
| Quality: groundedness | Golden-set groundedness and citation accuracy (release gate, plan §5) and online sampled groundedness | ≥ 0.95 (lower 95% confidence bound) | ≥ 0.95 |
| Quality: correct decline | UC2 decline when evidence is weak (golden-set items labelled "should decline") | ≥ 90% (deck metrics slide) | ≥ 90% |
| Quality: zero-tolerance | Entitlement leaks, injection success, numeric mismatches in released output | 0 | 0 |
| Quality: false positive blocks | Content filter or guardrail blocks of legitimate banking language (golden set and sampled) | ≤ 2% | ≤ 2% |
| Kill switch effectiveness | Time from flag change to all pods enforcing | ≤ 30 s | ≤ 30 s |

Notes. A degraded answer counts as available because it is honest and useful (plan §5); the degraded-rate SLO stops
"permanently degraded" being reported as healthy. Zero-tolerance quality metrics are not budgeted: a single
occurrence is a P1 incident.

### 8.2 Error budgets

| | Pilot (99.0%) | Scale (99.5%) |
|---|---|---|
| Time-based budget per month (17,160 business minutes) | 172 min | 86 min |
| Request-based budget per month (pilot 540 × 22 = 11,880; scale 5,400 × 22 = 118,800 requests) | 119 bad requests | 594 bad requests |
| Latency budget | 5% of good requests may exceed the p95 target | same |

The time-based and request-based budgets are both tracked; the request-based one governs at scale, the time-based
one (driven by synthetic probes) governs in the pilot where traffic is too sparse for stable ratios.

### 8.3 Burn-rate alerting (multi-window, business hours only)

| Alert | Condition | Severity |
|---|---|---|
| Fast burn | Burn rate ≥ 14.4 over 1 h **and** ≥ 14.4 over 5 min (≈ 2% of the monthly budget in 1 h) | P1 page |
| Medium burn | Burn rate ≥ 6 over 6 h and ≥ 6 over 30 min | P2 page |
| Slow burn | Burn rate ≥ 1 over 3 business days | Ticket |
| Pilot low-traffic guard | ≥ 3 consecutive synthetic probe failures for any use case, or ≥ 5 bad requests in 10 min | P2 page |

### 8.4 Error budget policy

| Budget consumed in the window | Action |
|---|---|
| < 50% | Normal releases, progressive rollout |
| 50% to 100% | Only low-risk changes; every release needs SRE sign-off; reliability items prioritised in the next sprint |
| > 100% | Change freeze except reliability and security fixes; post-incident review for each contributing incident; report to the product owner and the steering committee |
| Any zero-tolerance quality event | Kill switch for the affected use case (AI incident process, 06 §10), regardless of budget |
| Quality SLO breach (groundedness, decline, false positives) | Rollout paused (deck governance slide: "a drop there pauses rollout regardless of adoption"), rollback to last good prompt/model pin |

### 8.5 Incident and SLO breach reporting

| Report | Audience | Frequency | Content |
|---|---|---|---|
| Incident notification | On-call, product owner, affected SO, SOC (security), MRM (AI incidents) | Per incident (P1 within 15 min, P2 within 30 min) | Impact, use cases, degraded or down, workaround, next update |
| Post-incident review | Product owner, SRE, SO, InfoSec, MRM as relevant | Within 5 business days of P1/P2 | Timeline, root cause, error budget consumed, actions with owners |
| Monthly service report | Business sponsor, SME banking head, IT service management | Monthly | SLO attainment per use case, budget consumed, dependency-attributed unavailability per system of record (from §9 attribution), degraded rate by cause, latency, quality metrics, cost per RM per use case, top incidents |
| AI governance pack | AI governance board, MRM | Quarterly | Quality and drift metrics, overrides and edits on UC3 drafts, false-positive blocks, incidents, model changes, validation findings |
| Regulatory notification | Compliance (decides) | Per Bank policy | Material outsourcing or operational incidents per CBUAE requirements **[confirm with compliance]** |

**Attribution.** Every bad request carries a `cause` (copilot, platform service name, or system of record). The
report shows SLO attainment both "as experienced by RMs" (the SLO) and "copilot-attributable", so that a CRM outage
is visible as such and drives the CRM OLA conversation rather than hiding inside the copilot number.

### 8.6 Synthetic probes

A probe runs every 5 minutes in business hours per use case in production against a **designated synthetic test
customer** that exists in production systems of record and is excluded from MI and business reporting **[confirm the
Bank allows a synthetic customer in production]**. UC3 probe drafts only and commits to the synthetic customer's
CRM record with a `synthetic` marker once per hour. Probe identities are dedicated service accounts with a portfolio of
one synthetic customer; their audit events are tagged `synthetic=true`.

---

## 9. Composite availability per use case

### 9.1 Method

- Availability of a serial critical path = product of the availabilities of its **hard** dependencies (independence
  assumed; correlated failures are discussed in §9.5).
- A dependency is **hard** if its loss makes the request bad under the §8.1 definition. A dependency is **soft** if its
  loss produces a flagged `partial` or `degraded` answer, which counts as available.
- Azure components use the **published SLA as a planning floor** (24×7 monthly commitment, [confirm] each figure).
  Bank systems use the **required OLA** from §3. The copilot application tier (our code, configuration and
  deployments) uses an internal target.
- Downtime is expressed in business minutes per month (17,160).

### 9.2 Component planning values

Hard and soft classification follows the LLD failure behaviour (02 §2.3, §9): `get_customer_profile` (CRM) and
`get_facilities` (LOS, with ODS balances) are critical for UC1; transaction signals (ODS), KYC and news are
degradable; AI Language and Content Safety fall back to regex masking and local rules; the model falls back to
degraded mode.

| Component | Planning value | Basis | UC1 | UC2 | UC3 draft | UC3 commit |
|---|---|---|---|---|---|---|
| Microsoft Entra ID | 99.99% | Microsoft SLA [confirm] | Hard | Hard | Hard | Hard |
| Application Gateway WAF v2 | 99.95% | Microsoft SLA [confirm] | Hard | Hard | Hard | Hard |
| ExpressRoute (redundant circuits, Bank network) | 99.95% | Microsoft SLA [confirm] | Hard | Hard | Hard | Hard |
| AKS node VMs across 3 zones | 99.99% | VM SLA [confirm] | Hard | Hard | Hard | Hard |
| Private Link (private endpoints) | 99.99% | Microsoft SLA [confirm] | Hard | Hard | Hard | Hard |
| Cosmos DB (sessions, entitlements, jti) | 99.99% | Microsoft SLA, AZ uplift ignored [confirm] | Hard | Hard | Hard | Hard |
| APIM Premium | 99.95% | Microsoft SLA [confirm] | Hard | Hard | Hard | Hard |
| Event Hubs Standard (**audit, fail closed**) | 99.95% | Microsoft SLA [confirm] | Hard | Hard | Hard | Hard |
| AI Search (≥ 2 replicas) | 99.9% | Microsoft SLA [confirm] | n/a | Hard | n/a | n/a |
| Key Vault (`sign`) | 99.99% | Microsoft SLA [confirm] | n/a | n/a | n/a | Hard |
| CRM incl. entitlement source | 99.8% pilot / 99.9% scale | Required OLA (IF-01) | Hard | n/a (clearance from app roles) | Hard | Hard (entitlement; the write itself is soft: queued) |
| LOS facilities (with ODS balances) | 99.8% pilot / 99.9% scale | Required OLA (IF-03) | Hard (02 §3.4) | n/a | n/a | n/a |
| Copilot application tier | 99.7% pilot / 99.85% scale | Internal target | Hard | Hard | Hard | Hard |
| Azure OpenAI (`gen-large`, `gen-small`, `embed`) | 99.9% | Microsoft SLA [confirm] | Soft (facts-only brief) | Soft (passages; keyword search if `embed` down) | Soft (manual form) | n/a |
| AI Language PII | 99.9% | Microsoft SLA [confirm] | Soft (regex) | Soft (regex) | Soft (regex) | n/a |
| Content Safety | 99.9% | Microsoft SLA [confirm] | Soft (local rules) | Soft | Soft (notice) | n/a |
| Core ODS transaction signals | 99.5% | Required OLA (IF-02) | Soft (partial) | n/a | n/a | n/a |
| KYC, news and registry | 99.0% each | Required OLA / vendor | Soft (partial) | n/a | n/a | n/a |
| Graph | 99.9% | M365 SLA [confirm] | Soft (launch from CRM) | n/a | n/a | n/a |
| App Configuration, Blob, Azure Monitor, Document Intelligence | various | Off the request path (cached or asynchronous) | none | none | none | none |

### 9.3 Results

Azure platform common path (Entra × App Gateway × ExpressRoute × AKS nodes × Private Link × Cosmos × APIM × Event
Hubs):

0.9999 × 0.9995 × 0.9995 × 0.9999 × 0.9999 × 0.9999 × 0.9995 × 0.9995 = 0.9999⁴ × 0.9995⁴ = 0.99960 × 0.99800 =
**0.99760** (≈ 41 business minutes/month).

| Path | Product | Pilot values | Pilot result | Scale values | Scale result |
|---|---|---|---|---|---|
| UC1 brief | Common × CRM × LOS × App | 0.99760 × 0.998 × 0.998 × 0.997 | **99.06%** (161 min/month) | 0.99760 × 0.999 × 0.999 × 0.9985 | **99.41%** (101 min/month) |
| UC2 policy | Common × AI Search × App | 0.99760 × 0.999 × 0.997 | **99.36%** (110 min/month) | 0.99760 × 0.999 × 0.9985 | **99.51%** (84 min/month) |
| UC3 draft | Common × CRM × App | 0.99760 × 0.998 × 0.997 | **99.26%** (127 min/month) | 0.99760 × 0.999 × 0.9985 | **99.51%** (84 min/month) |
| UC3 commit | Common × CRM × App × Key Vault | × 0.9999 | **99.25%** (128 min/month) | × 0.9999 | **99.50%** (86 min/month) |
| Target | | | 99.0% (172 min) | | 99.5% (86 min) |

**Sensitivity.** If the CRM and LOS deliver only 99.5% and the application tier only 99.5%, UC1 falls to
0.99760 × 0.995³ = **98.27%** and misses the pilot target; UC2 (no system-of-record dependency) stays at 99.16%.
If the product owner accepts a brief without facilities (LOS made soft, flagged), UC1 becomes identical to UC3 draft:
**99.26% pilot, 99.51% scale**.

**Effect of degraded mode.** Without degraded mode the model, guardrail services and the degradable sources would be
hard dependencies. For UC1 that adds Azure OpenAI × AI Language × Content Safety × ODS signals × KYC × news =
0.999³ × 0.995 × 0.99² = **97.23%**, pulling UC1 at scale from 99.41% to about **96.7%**: the SLO would be unachievable.
Degraded mode converts these into soft dependencies. The same product is the expected share of time a UC1 brief is
**complete** under floor values (about 2.8% of business time with at least one section missing or the wording
degraded), which is why the separate degraded-rate SLO (≤ 5% pilot, ≤ 3% scale) and the ODS, KYC and news OLAs
matter for usefulness even though they do not count against availability. For UC2 and UC3 the model and guardrail
services contribute 0.999³ = 99.70%, so about 0.3% of the time (≈ 51 business minutes a month) answers would be
degraded or on fallback guardrails at floor values.

**Audit fail-closed.** Event Hubs is deliberately hard on every path: no answer is released and no write performed
without an acknowledged audit event (02 §2.3). It costs at most 0.05% (≈ 9 business minutes/month) at floor value on
Standard. Event Hubs Premium (published 99.99% [confirm]) would reduce this to 0.01%.

### 9.4 Conclusion

1. **Pilot 99.0% is supported** for every use case at floor values: UC1 99.06% (11 minutes of headroom), UC3 99.25%
   to 99.26%, UC2 99.36%. It rests on three conditions: CRM and LOS OLAs ≥ 99.8% in business hours, and an
   application tier held to ≥ 99.7% (about 50 business minutes a month) through zone-spread replicas, progressive
   rollout and fast rollback (06). UC1's margin is thin; making facilities degradable (product decision) raises it
   to 99.26%.
2. **Scale 99.5% is met at the floor by UC2 and UC3 (99.50% to 99.51%) but not by UC1 (99.41%)** with OLAs of
   99.9%. It becomes achievable with any of these levers: Event Hubs Premium (+0.04 points), CRM and LOS OLAs of
   99.95% (+0.05 points each), application tier 99.9%, or facilities made degradable. With Event Hubs Premium and
   99.95% CRM and LOS OLAs, UC1 at scale is 0.99800 × 0.9995 × 0.9995 × 0.9985 = **99.55%**; with the application tier
   at 99.9% as well, **99.60%**.
3. Published SLAs are floors, not expected values; measured availability of zone-redundant services is normally
   higher, which must be evidenced with pilot telemetry before the scale commitment.
4. **Recommendation:** commit 99.0% for the pilot as planned; make the 99.5% scale commitment conditional on (i) three
   months of pilot measurements showing the platform path ≥ 99.85% in business hours, (ii) CRM and LOS OLAs ≥ 99.95%,
   or facilities made degradable, and (iii) the Event Hubs Premium decision. Report both "as experienced" and
   "copilot-attributable" availability (§8.5).

### 9.5 Caveats

- **Business hours vs 24×7.** Microsoft SLAs are measured over the whole month. Business-hours experience can be
  better (Bank maintenance is outside business hours) or worse (an incident concentrated in a working day). We
  therefore measure ourselves rather than rely on SLA arithmetic.
- **Correlation.** A zone outage affects several components at once; with zone redundancy throughout (plan §3.1) this
  is absorbed (RTO minutes). A region outage takes everything down together and is a BCP event (05 §5), outside the
  availability SLO by definition [confirm with the business sponsor that region loss is excluded, as plan §3.1
  implies].
- **Shared dependencies** such as DNS, the hub firewall (only on the news path) and Bank network are folded into the
  ExpressRoute and Private Link terms; the Bank network team should confirm its own internal availability figure.
- **Pilot APIM single unit.** One unit is not spread across zones; a zone loss affecting it is a full outage until
  Microsoft restores it. See §10.

---

## 10. Open points and [confirm] list for this document

| # | Item | Owner | Needed by |
|---|---|---|---|
| 1 | Every Microsoft SLA figure quoted in §4, §5 and §9 against the SLA document in force | Cloud platform team | ARB sign-off |
| 2 | CRM OLA ≥ 99.8% (pilot) / ≥ 99.9% (scale) in business hours; external reference idempotency | CRM owner | End of Phase 0 |
| 3 | ODS, LOS, KYC, DMS OLAs and APIs (Phase 0 API inventory) | System owners | End of Phase 0 |
| 4 | News vendor contract: retention of query terms, processing location, SLA, support hours | Vendor management | Before UC1 pilot |
| 5 | APIM pilot with 1 unit conflicts with "zone redundancy only" resilience (plan §2, §3.1, §5 zone RTO minutes). Proposal: 2 units from the pilot, or Premium v2 with zone redundancy where available [confirm] | Architecture | Plan v3 |
| 6 | Event Hubs Standard vs Premium for the fail-closed audit stream (SLA and CMK support) | Architecture | Before scale |
| 7 | Restricted audit table in Log Analytics: retention not in plan §7; Event Hubs retention and outbox TTL not in plan §7 | Records management | Plan v3 |
| 8 | Synthetic test customer allowed in production systems of record (§8.6) | Bank operations, compliance | Before pilot |
| 9 | Plan §4.1 names only news and KYC as non-critical; the LLD makes ODS transaction signals degradable and facilities (LOS) critical. Decide whether a brief without facilities is acceptable (flagged): it lifts UC1 from 99.41% to 99.51% at scale (§9.3) | Product owner | Plan v3 |
| 10 | Per-customer daily cache for the news feed (cost) would need a plan §7 change | Product owner, DPO | Phase 2 |
| 11 | Posture when AI Language or Content Safety is down: this document follows the LLD (continue on regex masking and local rules, flagged); InfoSec and DPO may require failing into degraded mode instead | InfoSec, DPO | Before pilot |
| 12 | Region loss excluded from the availability SLO | Business sponsor | ARB sign-off |
