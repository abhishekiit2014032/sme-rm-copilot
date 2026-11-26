# SME RM Copilot on Azure: delivery plan and architecture baseline

Status: **v1 baseline, under review** · Owner: solution architecture · Date: 26 Nov 2026 · Presentation: Fri 27 Nov 2026

This file is the single source of truth for every workstream. Docs, deck, prototype and code must use the
names, numbers and decisions below. If a workstream needs to change one, it changes it here first and logs it in
§9 (iteration log). Everything is fictional and unbranded ("the Bank"). Figures are illustrative assumptions in
an AED / UAE-style context; anything marked **[confirm]** must be checked with the Bank or Microsoft before go-live.

## 1. Goal and scope

Deliver an architecture-review-grade design for deploying the SME RM Copilot **on Microsoft Azure only**, plus a
runnable local prototype in which every Azure dependency is replaced by a mock that speaks the same REST shape,
so that moving to real Azure is a configuration change, not a rewrite.

In scope: HLD, LLD, data flows, ADRs with trade-offs, interface agreements and SLAs with every external system,
NFRs, failure scenarios, DR, security threat model, capacity and cost model, deployment and operations, deck
slides for all of the above, the prototype "How it works" tab, and the mock-Azure working prototype with tests.

Out of scope now: deploying to a real Azure subscription, real bank data, real vendor contracts, IaC that has been
applied (the deployment doc lists the modules; no Bicep is compiled in this repo).

## 2. Use cases (unchanged from the deck)

| Id | Use case | Pattern | Writes? |
|---|---|---|---|
| UC1 | Pre-meeting brief | Fixed workflow: fan-out read tools, model writes wording with `{placeholders}`, system inserts figures | No |
| UC2 | Policy and product copilot | RAG over approved documents, cited, groundedness check, decline when weak | No |
| UC3 | Post-meeting follow-up agent | Extract JSON drafts from notes; RM edits; signed approval; write API verifies | Yes, after approval |

## 3. Canonical architecture

### 3.1 Region and residency
- Primary region **UAE North** (3 availability zones). All customer data, prompts, completions, indexes, logs and
  backups stay in the UAE. **[confirm]** the Bank's residency policy and the regulator's cloud outsourcing rules.
- DR region **UAE Central** for data and app tier (cold or warm standby). Azure OpenAI model availability in UAE
  Central is not assumed. **Model DR is graceful degradation, not cross-border failover** (see §6, ADR-015).
- Model deployments are **Regional** (not Global or Data Zone) so inference stays in-region. **[confirm]** that the
  chosen model versions are offered as Regional Standard and Provisioned in UAE North.

### 3.2 Deployable services (Kubernetes workloads, namespace `rm-copilot`)

| Service | Responsibility | Talks to |
|---|---|---|
| `web-bff` | Backend-for-frontend for the CRM sidebar and Teams tab. Validates Entra ID tokens, holds the session, streams answers, **mints approval tokens** (Key Vault `sign`) when the RM clicks Approve | orchestrator, Key Vault, Cosmos DB |
| `bot-adapter` | Teams bot messaging endpoint (Azure Bot Service). Only workload with a public listener; validates Bot Framework JWTs | web-bff |
| `orchestrator` | Intent routing, UC1/UC2/UC3 workflows, step/time/token budgets, guardrail calls, placeholder fill, numeric check, groundedness check | APIM (AI gateway), tool-gateway, Content Safety, AI Language |
| `tool-gateway` | The tool layer and security boundary. Typed read/draft/write tools, **entitlement check first**, field minimisation and masking, policy search, **write API that verifies the approval token** (Key Vault public key), idempotency, outbox, audit events | APIM (system APIs), AI Search, Redis, Cosmos DB, Service Bus, Event Hubs |
| `ingestion-worker` | Policy document pipeline: Blob/SharePoint → Document Intelligence (layout) → clause chunking → embeddings → AI Search | Document Intelligence, APIM (embeddings), AI Search |
| `audit-writer` | Consumes the audit stream, hash-chains events, writes to immutable Blob (WORM) and Azure Data Explorer | Event Hubs, Storage, ADX |
| `eval-runner` | Offline release gate in CI and nightly online sampling with Foundry evaluators | Foundry, APIM |

### 3.3 Azure services (one row per service; this list is canonical)

| Layer | Azure service | Role | Key settings |
|---|---|---|---|
| Identity | Microsoft Entra ID | SSO, MFA, Conditional Access, app roles, On-Behalf-Of (OBO) | RM identity propagated end to end; workload identity for pods |
| Edge | Application Gateway WAF v2 | Internal listener for CRM sidebar and Teams tab; separate public listener only for `/api/messages` (bot) | OWASP CRS, restricted to `AzureBotService` service tag on the public listener |
| Channel | Azure Bot Service | Teams channel for the copilot | Single-tenant app registration |
| Compute | Azure Kubernetes Service (private cluster) | Runs all services | 3 zones, Azure CNI Overlay + Cilium network policy, workload identity, KEDA, Uptime SLA tier |
| Gateway | Azure API Management (Premium, internal VNet, zone-redundant) | **AI gateway** in front of Azure OpenAI and **system API facade** in front of the Bank's systems of record | `llm-token-limit`, `llm-emit-token-metric`, backend pool with priority + circuit breaker, `validate-jwt`, per-user rate limits; semantic cache **off** for customer data |
| Models | Azure AI Foundry (Foundry resource + project) with Azure OpenAI deployments | `gen-large` (frontier GPT-class, e.g. GPT-4.1), `gen-small` (e.g. GPT-4.1-mini) for routing and extraction, `embed` (text-embedding-3-large) | Regional deployments; Provisioned (PTU) primary + Standard spillover in production; auto-upgrade off, versions pinned |
| Safety | Azure AI Content Safety | Prompt Shields (user prompt and document attacks), harm categories, groundedness detection where available, protected material | Plus Azure OpenAI built-in content filters; thresholds tuned on the golden set |
| PII | Azure AI Language (PII detection) | Masks PII before model calls | Custom regex for Emirates ID, IBAN, trade licence; Arabic coverage **[confirm]** |
| Retrieval | Azure AI Search (Standard, 3 replicas) | Hybrid BM25 + vector + semantic ranker; security-trimming filters on clearance, status and effective date | Private endpoint; 3 replicas for the read/write SLA |
| Ingestion | Azure AI Document Intelligence (layout model) | Parses tables, headings, Arabic and scans | In-region |
| Voice | Azure AI Speech (speech to text) | UC3 voice memos | Audio deleted after transcription |
| State | Azure Cosmos DB for NoSQL | Sessions, drafts, idempotency and token-replay (`jti`) store, prefetched briefs | Zone-redundant, TTLs, CMK |
| Cache | Azure Managed Redis | Entitlement cache (5 min), per-request tool-result cache, rate counters | Private endpoint |
| Messaging | Azure Service Bus Premium | Write outbox and retry queue for CRM writes; async jobs | Duplicate detection on (idempotency key) |
| Audit stream | Azure Event Hubs | Audit event stream | Capture off; consumed by `audit-writer` |
| Audit store | Azure Blob Storage (immutable, time-based retention) + Azure Data Explorer | Tamper-evident audit trail and query | Retention per records policy (e.g. 7 years) **[confirm]**; legal hold supported |
| Keys | Azure Key Vault Premium (HSM-backed keys) | Approval-token signing key (EC P-256, ES256), CMKs, secrets for non-Entra APIs | `web-bff` has `sign` only; `tool-gateway` has public key only; orchestrator has neither |
| Telemetry | Azure Monitor: Application Insights (OpenTelemetry), Log Analytics | Distributed traces, logs, dependency maps | **Metadata only**; no prompt or customer content in telemetry |
| Metrics | Azure Monitor managed service for Prometheus + Azure Managed Grafana | Service and AI metrics (latency, tokens, cost, guardrail hits, declines) | Scraped from `/metrics` in AKS; recording rules and alerts |
| Security ops | Microsoft Defender for Cloud (Containers, AI threat protection), Microsoft Sentinel, Microsoft Purview | Posture, runtime threats, SIEM on audit and security events, data classification and DSPM for AI | Sentinel analytics for misuse (bulk briefs, repeated denials) |
| Platform | Azure Container Registry Premium, Azure Policy, Azure Firewall Premium (hub egress), Private DNS zones, ExpressRoute | Supply chain and network | No public network access on any PaaS; all private endpoints |
| Delivery | GitHub Actions or Azure DevOps with OIDC federation; Bicep with Azure Verified Modules | CI/CD and IaC | Environments dev, test, pre-prod, prod |
| M365 | Microsoft Graph | Calendar (meetings to brief), Outlook drafts (never send) | OBO, delegated `Calendars.Read`, `Mail.ReadWrite` for drafts only |

### 3.4 Systems of record (Bank-owned, reached over ExpressRoute through APIM)
CRM, core banking (via ODS read replica), lending and trade finance (LOS), KYC/CDD, policy DMS, M365 (Graph),
news and company registry (licensed external feed, reached through Azure Firewall egress allow-list).

## 4. Data flows (the deck and the prototype must match these numbers)

### 4.1 Read path: UC1 pre-meeting brief
1. RM opens the brief in the CRM sidebar or Teams. Entra ID SSO token attached.
2. Application Gateway WAF → `web-bff` validates the token, creates a request id and W3C `traceparent`.
3. `web-bff` → `orchestrator`: intent `customer_brief` (fixed workflow, so no router call is needed when launched from a CRM record).
4. `orchestrator` → `tool-gateway`: read tools with the RM's identity. **Entitlement check first** (Redis cache, source: CRM portfolio assignments). Denied → 403 `NOT_ENTITLED`, no data moves.
5. `tool-gateway` → APIM (system API facade) → CRM, core banking ODS, LOS, KYC, news, in parallel, each with its own timeout and circuit breaker.
6. `tool-gateway` minimises and masks fields (no national ID; transactions aggregated), returns **facts**, and writes one audit event per call to Event Hubs.
7. `orchestrator` → Content Safety Prompt Shields (document attack check on fetched free text) and PII masking.
8. `orchestrator` → APIM AI gateway → Azure OpenAI `gen-large`: masked facts in, wording with `{placeholders}` out.
9. `orchestrator` fills placeholders from tool output and runs the **numeric check** (any free number → line rejected).
10. Brief streamed back via `web-bff` to the RM with sources. Audit event `brief_built`.

Private network boundary: everything from Application Gateway inwards, including Azure OpenAI and every PaaS
service on private endpoints. Only the bot listener and the outbound news-feed call cross the boundary.

### 4.2 Write path: UC3 follow-up agent
1. RM pastes notes or records a voice memo (Speech to text). 
2. `orchestrator` guardrails: **quarantine quoted instructions** (Prompt Shields document attack + rules), **mask PII** (AI Language + regex), resolve dates deterministically (GST).
3. `orchestrator` → APIM → `gen-small` (or `gen-large`) extracts **JSON drafts**: call report, tasks, signals. Schema and policy validation (no approvals, pricing or limit commitments).
4. `tool-gateway` `draft_email` creates an Outlook draft (Graph); it **cannot send**.
5. Drafts shown to the RM; the RM edits them.
6. RM clicks Approve → `web-bff` computes the SHA-256 hash of the final draft and asks Key Vault to **sign** an approval token: `{sub: RM, draft_sha256, aud: tool-gateway, iat, exp: iat+300s, jti}` (ES256).
7. `orchestrator` calls `tool-gateway` commit with the draft and the token. It cannot mint or alter a token.
8. `tool-gateway` write API **verifies** signature (Key Vault public key), `aud`, `exp` (30 s skew), `sub` equals the caller's RM, hash equals the submitted draft, and `jti` not seen before (Cosmos DB).
9. `tool-gateway` → APIM → CRM (call report) and tasks, with idempotency key = `jti`. On timeout or 5xx: enqueue to Service Bus outbox, return `202 queued`.
10. Audit events `approval_issued`, `write_committed` or `write_queued`; the email stays a draft the RM sends.

### 4.3 UC2 policy question
RM question → Prompt Shields + PII → `gen-small` intent and query rewrite → `tool-gateway.search_policies` →
AI Search hybrid query filtered on `status = current`, `effective_from <= today`, `clearance <= RM clearance` →
semantic rerank → threshold (decline when weak) → `gen-large` answers only from passages with citations →
groundedness check (Content Safety groundedness where available, plus the local claim-by-claim check) → retry
once with a stricter prompt → else decline and offer routing to the credit or product desk.

## 5. Non-functional baseline

| Item | Target (illustrative) |
|---|---|
| Users | 300 SME RMs (pilot 30); peak 0.5 requests/s; ~5,400 requests/day |
| Availability SLO | 99.5% in business hours (Mon–Fri 07:00–20:00 GST); 99.0% overall |
| Latency p95 | UC1 brief ≤ 8 s cold; UC2 answer ≤ 6 s (first token ≤ 2.5 s); UC3 draft ≤ 10 s; commit ≤ 2 s |
| Tool calls | 2 s timeout per call, one retry for idempotent reads, circuit breaker after 5 failures in 30 s |
| Model calls | 20 s timeout; APIM fails over PTU → Standard on 429/5xx; orchestrator deadline propagation |
| RPO / RTO | Audit RPO 0 (Event Hubs zone-redundant, ack before success); sessions RPO 15 min; RTO 4 h for region loss |
| Quality gates | Groundedness ≥ 0.95, citation accuracy ≥ 0.95, entitlement leaks = 0, injection success = 0, numeric mismatches = 0 |
| Token budget | Per request: ≤ 6 tool steps, ≤ 12k input and 1.5k output tokens; per RM per day: 400k tokens (APIM `llm-token-limit`) |

## 6. Critical issues found while planning, and resolutions (summary; ADRs hold the detail)

| # | Issue | Resolution |
|---|---|---|
| C1 | Teams bot needs a public endpoint; the rest is private | Separate public listener on Application Gateway for `/api/messages` only, `AzureBotService` service tag, Bot Framework JWT validation in `bot-adapter`; no other route reachable |
| C2 | Newest models and some Content Safety features (e.g. groundedness detection) may not exist in UAE North | Design does not depend on them: local groundedness check always runs; model choice is gated by regional availability **[confirm]** |
| C3 | No in-country model DR | Degraded mode: UC1 returns a data-only brief (facts, no wording), UC2 returns ranked passages with no generated answer, UC3 lets the RM type the call report. Clearly flagged in the UI |
| C4 | Legacy systems of record do not accept Entra OBO tokens | `tool-gateway` calls them with its managed identity through APIM, passes the RM id in a signed header, and enforces entitlements itself; downstream logs carry the RM id |
| C5 | Prompts and completions contain customer data; telemetry is broadly readable | App Insights gets metadata only (ids, timings, token counts, decisions). Content goes only to the immutable audit store, masked, readable via PIM-approved roles |
| C6 | HMAC approval key shared by signer and verifier means either side can forge | Asymmetric ES256 key in Key Vault HSM: `web-bff` can sign, `tool-gateway` holds only the public key; plus `jti` replay store and draft hash binding |
| C7 | CRM write times out: retry may double-post | Idempotency key = token `jti`; Service Bus duplicate detection; outbox with status shown to the RM |
| C8 | APIM semantic cache could return one RM's answer to another | Semantic cache off for UC1/UC3; for UC2 only if keyed on clearance + index version, decided after pilot |
| C9 | Pre-generating briefs at 07:00 runs without the RM present (no user token) | Phase 2 only, with risk sign-off: prefetch job uses its own identity, `tool-gateway` enforces the RM's portfolio, output stored encrypted and readable only by that RM, audit marks "system on behalf of" |
| C10 | Token and cost runaway (loops, abuse) | Orchestrator step/token budgets, APIM per-user and per-use-case token limits, FinOps alerts at 80% of monthly budget |
| C11 | Prometheus is native on AKS, not on Container Apps | AKS chosen (ADR-002); Container Apps documented as the alternative if the Bank has no AKS platform team |
| C12 | Model versions retire roughly yearly | Versions pinned, auto-upgrade off, model registry, eval gate before any upgrade, 90-day upgrade runbook |
| C13 | Content filters may block normal banking language ("exposure", "default", "kill switch") | Thresholds tuned on the golden set; false-positive tests in the eval suite; custom blocklists only where needed |
| C14 | Core banking cannot take copilot read load | ODS read replica, per-request result cache, APIM quotas per consumer, bulkhead per backend |
| C15 | Insider misuse (RM pulls many briefs outside normal pattern) | Sentinel analytics on audit stream: N briefs/hour, repeated `NOT_ENTITLED`, after-hours bulk use |
| C16 | Dates and weekends | All date logic in GST; UAE working week Mon–Fri |

## 7. Workstreams and acceptance criteria

| WS | Output | Owner files | Done when |
|---|---|---|---|
| A | HLD + ADRs | `docs/architecture/01-hld.md`, `docs/architecture/adr/*.md` | Context, container, deployment, network and identity views as Mermaid; ≥ 16 ADRs each with options, trade-off table, decision, consequences, review trigger |
| B | LLD, interfaces and SLAs, NFR and scenarios, deployment and operations | `02-lld.md`, `04-interfaces-and-slas.md`, `05-nfr-scenarios-and-dr.md`, `06-deployment-and-operations.md` | API contracts (OpenAPI-style), sequence diagrams, per-system interface agreements (SLA/OLA, timeouts, retries, error contract, data class, owner, support hours), composite availability maths, failure-mode catalogue, STRIDE, capacity and cost, runbooks |
| C | Mock-Azure working prototype | `code/azmock/`, `code/services/`, `code/stack.py`, new tests | Each Azure dependency mocked behind its real REST shape; services talk HTTP with trace propagation; `/metrics` in Prometheus format; fault injection; all old and new tests and evals green |
| D | Deck | `deck/project/slides/*`, `deck/project/deck.json`, `README.md` | Slides `flow-read`, `flow-write`, `component-map`, `azure-deploy`, `slas`; `stack` and `decisions` updated to Azure; every slide within the 808 px budget, fonts ≥ 24 px, ≤ 24 pinned children per host, measured in headless Chrome |
| E | Prototype "How it works" tab | `prototype/rm-copilot.html` | Fourth tab with the Azure component diagram highlighting what the last request touched; offline, light and dark, 375 px wide without horizontal scroll |
| F | Review and iteration | `docs/architecture/07-review-log.md` | Independent architecture review of all outputs, findings fixed or recorded with rationale |

## 8. Execution order
1. Plan review (independent critic) → revise this file.
2. Workstreams A–E in parallel, disjoint files; no agent commits; the lead commits per item.
3. Architecture review board pass over everything (F) → fixes.
4. Final verification: tests, evals, stack smoke run, slide layout measurement, prototype phone-width check.
5. Publish deck and prototype artifacts; push.

## 9. Iteration log
- v1 (26 Nov): baseline written from the existing deck, code and the owner's Azure-only direction.
