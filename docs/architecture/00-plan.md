# SME RM Copilot on Azure: delivery plan and architecture baseline

Status: **v2.1 baseline (after ARB pre-review and ADR feedback)** · Owner: solution architecture · Date: 26 Nov 2026 · Presentation: Fri 27 Nov 2026

This file is the single source of truth for every workstream. Docs, deck, prototype and code must use the
names, numbers and decisions below. If a workstream needs to change one, it changes it here first and logs it in
§11. Everything is fictional and unbranded ("the Bank"). Figures are illustrative assumptions in an AED /
UAE-style context; anything marked **[confirm]** must be checked with the Bank or Microsoft before go-live (the
full list is §10).

## 1. Goal and scope

Deliver an architecture-review-grade design for deploying the SME RM Copilot **on Microsoft Azure only**, plus a
runnable local prototype in which every Azure dependency is replaced by a mock that speaks the same REST shape,
so that moving to real Azure is a configuration change, not a rewrite.

In scope: HLD, LLD, data flows, ADRs with trade-offs, interface agreements and SLAs with every external system,
NFRs, failure scenarios, DR and business continuity, security threat model, model risk management, capacity and
cost, deployment and operations, deck slides, the prototype "How it works" tab, and the mock-Azure prototype.

Out of scope now: deploying to a real subscription, real bank data, vendor contracts, applied IaC.

## 2. Use cases and phasing

| Id | Use case | Pattern | Writes? |
|---|---|---|---|
| UC1 | Pre-meeting brief | Fixed workflow: fan-out read tools, model writes wording with `{placeholders}`, system inserts figures | No |
| UC2 | Policy and product copilot | RAG over approved documents, cited, groundedness check, decline when weak | No |
| UC3 | Post-meeting follow-up agent | Extract JSON drafts from notes; RM edits; signed approval; write API verifies | Yes, after approval |

| Phase | Scope | Platform simplifications |
|---|---|---|
| 0 Discovery (4 weeks) | API inventory, residency and outsourcing approvals, abuse-monitoring exemption, model availability, golden set v1 | n/a |
| 1 Pilot (30 RMs) | UC1, UC2, UC3 in the **CRM sidebar** and a **Teams personal tab**; English first, Arabic evaluated | Regional Standard models (no PTU); no bot; no Redis (in-process + Cosmos cache); outbox in Cosmos DB (no Service Bus); audit query in Log Analytics (no ADX); zone redundancy only (no warm DR); evals as a CI job |
| 2 Scale (300 RMs) | All SME RMs, Arabic GA, voice memos | PTU if the measured volume justifies it; Teams bot after residency sign-off; Service Bus Premium; ADX if audit volume needs it; brief prefetch (C9) after risk sign-off |
| 3 Extend | Other segments, more write actions | Re-review |

## 3. Canonical architecture

### 3.1 Region, residency and resilience
- Primary region **UAE North**, **zone-redundant** across 3 availability zones. Customer data, prompts,
  completions, indexes, logs and backups stay in the UAE **[confirm residency policy and outsourcing approval]**.
- **Region loss is a business-continuity event, not hot DR.** UAE Central is a restricted region with a limited
  catalogue and no availability zones **[confirm access and catalogue]**. Plan: GZRS replication for the audit WORM
  store and document store, Cosmos DB backup copies, IaC rebuild of the app tier; best-effort RTO 24 h; RMs fall
  back to existing CRM screens. Phase 2 re-evaluates warm standby if UAE Central offers the needed services.
- Model deployments are **Regional** so inference stays in-region. Models are "the latest GA GPT-class model
  offered as Regional Standard in UAE North at build time" **[confirm model, version, retirement date]**.
- No cross-border model failover. Model unavailability means **degraded mode** (§6, C3).

### 3.2 Deployable services (AKS namespace `rm-copilot`)

| Service | Responsibility | Talks to |
|---|---|---|
| `web-bff` | Backend-for-frontend for the CRM sidebar and the Teams tab (Teams SSO). Validates the Entra ID token, gets an **OBO token for the `tool-gateway` audience**, holds the session, enforces Origin/CSRF checks, requires a **fresh authentication** (authentication context `approve-write`, 10 min, step-up otherwise; §3.4) to approve, **mints approval tokens** with Key Vault `sign` | orchestrator, Key Vault, Cosmos DB, Entra ID |
| `orchestrator` | Intent routing, UC1/UC2/UC3 workflows, deadlines and budgets, guardrails, placeholder fill, numeric and policy checks, groundedness check, degraded mode. Forwards the RM's OBO token unchanged and adds its own **workload identity token** | APIM AI gateway, tool-gateway, Content Safety, AI Language, App Configuration |
| `tool-gateway` | The tool layer and security boundary. Validates the RM token (`iss`, `aud`, `tid`, `scp`; RM = `oid`) and the caller's workload token; **entitlement check first**; field minimisation and masking; policy search; **write API that verifies the approval token** (public key only); jti replay store; outbox and saga; audit events | APIM system API facade, APIM AI gateway (query embeddings), AI Search, Cosmos DB, Event Hubs |
| `outbox-worker` | Delivers queued CRM writes from the Cosmos outbox (change feed), read-before-retry using the external reference | tool-gateway write adapters, Cosmos DB |
| `ingestion-worker` | Policy pipeline: document store → Document Intelligence (layout) → PII scan → clause chunking → embeddings → AI Search; keeps chunks and embeddings for index rebuild | Document Intelligence, AI Language, APIM AI gateway, AI Search, Blob |
| `audit-writer` | Consumes the audit stream; hash-chains metadata into immutable Blob (WORM); content to a separate per-customer-key encrypted store; metadata to a restricted Log Analytics table | Event Hubs, Storage, Key Vault, Log Analytics |
| `eval-runner` | CI job: release gate on the golden set; nightly regression; online sampling only with an in-region judge model | APIM AI gateway, Foundry project |
| `bot-adapter` (Phase 2) | Teams bot via Azure Bot Service and the Microsoft 365 Agents SDK, only after residency sign-off (C1) | web-bff |

### 3.3 Azure services (canonical list)

| Layer | Azure service | Role | Key settings |
|---|---|---|---|
| Identity | Microsoft Entra ID | SSO, MFA, Conditional Access, app roles, OBO, workload identity | Approval needs authentication context / fresh sign-in; PIM for all admin roles |
| Edge | Application Gateway WAF v2 (internal) | Only ingress for the sidebar and the Teams tab, reached from managed devices on the corporate network or Entra Private Access | OWASP rules, TLS 1.2+, CSP headers set by web-bff |
| Compute | Azure Kubernetes Service (private cluster) | Runs all services | 3 zones, Azure CNI Overlay + Cilium network policy, workload identity, KEDA, local accounts off, Standard tier (uptime SLA) |
| Gateway | Azure API Management (Premium, internal VNet) | **AI gateway** in front of Azure OpenAI and **system API facade** in front of the systems of record | `validate-jwt`, `llm-token-limit`, `llm-emit-token-metric`, backend pool with priority + circuit breaker + `retry`; request/response body logging **off**; semantic cache **off**. Per-RM limits keyed on an `x-rm-oid` header that APIM accepts only from the orchestrator's workload identity, plus a per-caller ceiling. Pilot 1 unit (single-zone dependency, an accepted pilot risk counted in the availability maths), production ≥ 2 units across zones **[confirm]** |
| Models | Azure AI Foundry (Foundry resource + project) with Azure OpenAI deployments | `gen-large` (frontier GPT-class), `gen-small` (small GPT-class: routing, extraction), `embed` (text-embedding-3-large) | Regional Standard in pilot; PTU is a post-pilot gate on measured tokens; versions pinned, auto-upgrade off; `store=false`; content filters with Prompt Shields (jailbreak + indirect attack) on |
| Safety | Azure AI Content Safety | Prompt Shields for user prompts and fetched documents, harm categories; groundedness detection only if offered in-region | Primary injection defence is the Azure OpenAI content filter + our own rules; standalone features used where available in UAE North **[confirm]** |
| PII | Azure AI Language (PII detection) | Masks PII before any model call | Plus regex for Emirates ID, IBAN, trade licence, phone; Arabic coverage **[confirm]**, regex fallback always on |
| Retrieval | Azure AI Search (Standard) | Hybrid BM25 + vector + semantic ranker; filters on status, effective date, clearance | 2 replicas (read SLA) in pilot, 3 in production; `ar.microsoft` analyzer for Arabic; rebuild from stored chunks |
| Ingestion | Azure AI Document Intelligence (layout) | Parses tables, headings, Arabic, scans | Results deleted after processing **[confirm retention]** |
| Voice | Azure AI Speech (speech to text) | UC3 voice memos (Phase 2) | Audio deleted after transcription |
| State | Azure Cosmos DB for NoSQL | Sessions, drafts, jti replay store (atomic create), outbox, entitlement cache, saga state | Zone-redundant; TTLs in §7; CMK; `disableLocalAuth`; periodic backup with geo-redundant backup storage (continuous backup does not survive region loss) **[confirm]**; containers per ADR-019 incl. `approvals` (jti, verdict, saga, outbox) and `audit-spool` (buffer if Event Hubs is unavailable) |
| Config | Azure App Configuration | Feature flags and **kill switches** per use case and for all writes; prompt and model version pins | Changes audited; Sentinel playbook can flip a kill switch; turning off is one person, turning on is four-eyes; **writes are treated as disabled if App Configuration has been unreachable for 15 min** |
| Audit stream | Azure Event Hubs (Standard, zone-redundant) | Audit event stream; producers wait for the ack | Private endpoint |
| Audit store | Azure Blob Storage, immutable (time-based retention), GZRS | Tamper-evident hash chain (one per Event Hubs partition) of metadata + content pointers; 5-minute seals copied to a seal store in a separately administered subscription | Retention per records policy (e.g. 7 years) **[confirm]** |
| Audit content store | Separate Azure Blob Storage account, GZRS, not immutable | Masked prompts and outputs, each encrypted with a data key wrapped by a per-customer Key Vault key | Erasure = delete the customer key (crypto-shredding); completes after the Key Vault soft-delete/purge-protection window (≥ 7 days) |
| Keys | Azure Key Vault Premium (HSM-backed keys); Managed HSM if the Bank's crypto standard requires single-tenant HSMs | Approval-signing key (EC P-256, ES256, `kid` versioned), CMKs, per-customer content keys, secrets for non-Entra APIs | `web-bff`: sign only; `tool-gateway`: public key only; orchestrator: none; rotation every 90 days with overlap |
| Telemetry | Azure Monitor: Application Insights (OpenTelemetry), Log Analytics | Traces, logs, dependency maps | **Metadata only**: GenAI content capture off, Foundry tracing content recording off, APIM body bytes 0, exception messages scrubbed; enforced by Azure Policy and a CI test |
| Metrics | Azure Monitor managed service for Prometheus + Azure Managed Grafana | Service and AI metrics (latency, tokens, cost, guardrail hits, declines, breaker state) | PodMonitor scraping of `/metrics`; private cluster uses AMPLS + data collection endpoints **[confirm]**; APIM token metrics land in App Insights and are shown in Grafana via the Azure Monitor data source |
| Security ops | Defender for Cloud (Containers; AI threat protection with prompt evidence **off** until residency confirmed), Microsoft Sentinel, Microsoft Purview | Posture, runtime threats, SIEM, data classification, DSPM for AI (scoped) | Sentinel analytics for misuse (C15) |
| Platform | ACR Premium, Azure Policy, Azure Firewall Premium (hub egress allow-list), Private DNS zones + Azure DNS Private Resolver (or the Bank's forwarders), ExpressRoute, Azure Bastion, in-network CI runners (Managed DevOps Pools or self-hosted) for deployments to private resources | Supply chain, network, admin access | No public network access on PaaS; `disableLocalAuth` everywhere; image signing (Notation) and admission policy |
| Delivery | GitHub Actions or Azure DevOps with OIDC federation; Bicep with Azure Verified Modules | CI/CD and IaC | dev, test, pre-prod, prod; **synthetic data only outside prod**; separate Azure OpenAI per environment |
| M365 | Microsoft Graph | Calendar for meetings to brief (`Calendars.ReadBasic`, OBO) | **No mail scope**: the approved email opens in Outlook via a compose deeplink; the copilot never writes to the mailbox |

### 3.4 Identity chain (C4, review finding 1)
1. RM signs in (CRM sidebar: MSAL; Teams tab: Teams SSO). Token audience = `web-bff`.
2. `web-bff` validates it and exchanges it (OBO) for a token with audience `tool-gateway`, scope `Tools.Read` (and
   `Tools.Write` only on approve). Approve requires the Conditional Access **authentication context** `approve-write`
   (sign-in frequency 10 min): `web-bff` checks the `acrs` claim and issues a claims challenge if absent; the ID
   token's `auth_time` is the fallback **[confirm]**.
3. `orchestrator` forwards that token unchanged and authenticates itself with its workload identity token.
4. `tool-gateway` validates both; the RM is the `oid` of the user token. It never trusts an RM id in a body or a header.
5. Systems of record that accept Entra tokens get an OBO token; legacy ones are called with `tool-gateway`'s
   managed identity through APIM with the RM's `oid` in a header signed by APIM. Entitlements are always enforced
   in `tool-gateway`, regardless of the downstream.

### 3.5 Microsoft data handling (review finding 3)
- Azure OpenAI abuse monitoring stores prompts and completions for up to 30 days with possible human review by
  default. **Gate before real data: the Bank's application for modified abuse monitoring is approved** **[confirm
  eligibility and review location]**.
- `store=false` on every model call; no Assistants or stored-response features.
- Document Intelligence and Speech results deleted after processing; Defender prompt evidence off until confirmed.
- Microsoft does not train on customer prompts (Azure OpenAI data terms); still recorded in the outsourcing file.

### 3.6 Systems of record (Bank-owned, over ExpressRoute through APIM)
CRM, core banking (via an ODS read replica), lending and trade finance (LOS), KYC/CDD, policy DMS, Microsoft 365
(Graph), news and company registry (licensed external feed through the Azure Firewall egress allow-list).

## 4. Data flows (the deck and the prototype must match these numbers)

### 4.1 Read path: UC1 pre-meeting brief
1. RM opens the brief from the CRM sidebar or the Teams tab; Entra ID SSO token attached.
2. Application Gateway (WAF) → `web-bff` validates the token, gets the OBO token for `tool-gateway`, creates the request id and W3C `traceparent`, sets the request deadline (8 s).
3. `web-bff` → `orchestrator`: intent `customer_brief` (fixed workflow; no router call when launched from a CRM record).
4. `orchestrator` → `tool-gateway`: read tools with the RM's token. **Entitlement check first** (cache ≤ 60 s, invalidated by a CRM portfolio-change webhook through APIM). Denied → 403 `NOT_ENTITLED`, no data moves.
5. `tool-gateway` → APIM system API facade → CRM, core banking ODS, LOS, KYC, news, in parallel, each with a deadline-derived timeout and a circuit breaker. A failed non-critical source (news, KYC) yields a **partial brief** with that section flagged.
6. `tool-gateway` minimises and masks fields (no national ID; transactions aggregated), returns **facts** with source references, and writes one audit event per call to Event Hubs.
7. `orchestrator` masks PII and runs Prompt Shields on fetched free text (documents), in parallel with the slowest tool calls.
8. `orchestrator` → APIM AI gateway → Azure OpenAI `gen-large`: masked facts in, wording with `{placeholders}` out.
9. `orchestrator` fills placeholders from tool output and runs the **numeric check** (digits and number words, English and Arabic) and the **policy check** (no promises of approval, pricing or limits). Failing lines are dropped.
10. The **verified** brief returns via `web-bff` to the RM as plain text with sources (nothing unverified is streamed). "Facts first": sections built only from tool output (snapshot, watch list) are released as soon as step 6 completes (about 1.7 s); the generated talking points follow after step 9. Audit event `brief_built` with lineage (model version, prompt hash, tool contract versions, guardrail version).

Private network boundary: Application Gateway inwards, including Azure OpenAI and every PaaS service on private
endpoints. Outbound only: Entra ID and Graph (Microsoft backbone), Azure Monitor ingestion via AMPLS, and the news
feed through the firewall allow-list.

### 4.2 Write path: UC3 follow-up agent
1. RM pastes notes (Phase 2: voice memo via Speech to text).
2. `orchestrator` guardrails: Prompt Shields **user prompt** check plus rules that **quarantine quoted instructions**; **mask PII** (AI Language + regex); deterministic date resolution in GST (UAE week Mon–Fri).
3. `orchestrator` → APIM → `gen-small` extracts **JSON drafts** (call report, tasks, signals); schema and policy validation (no approvals, pricing or limit commitments).
4. Drafts returned to the RM, who edits them. The email text is a draft shown in the UI; nothing is written anywhere yet.
5. RM clicks Approve. `web-bff` checks Origin and CSRF token and requires a fresh authentication (authentication context `approve-write`, 10 min; §3.4), else step-up.
6. `web-bff` canonicalises the final draft (RFC 8785 JSON), hashes it (SHA-256) and asks Key Vault to **sign** an approval token (ES256): `{iss: web-bff, aud: tool-gateway, sub: RM oid, customer_id, op: "crm.commit_followup", draft_sha256, schema: "followup/1", iat, exp: iat+300, jti, kid}`.
7. `orchestrator` passes draft + token to `tool-gateway` commit. It cannot mint or alter a token.
8. `tool-gateway` verifies: signature with the public key for `kid`, `aud`, `exp` (30 s skew), `sub` = the `oid` of the RM token on the call, `customer_id` and `op` match the request, hash of the canonical submitted draft matches, then an **atomic create** of `jti` in Cosmos DB (409 = replay). The verdict is persisted with the draft.
9. Saga: call report and each task written to CRM through APIM with the external reference `jti:item`; on timeout or 5xx the item goes to the **Cosmos outbox**; `outbox-worker` reads before retrying (by external reference) so nothing is posted twice. Status per item shown to the RM (`committed`, `queued`, `failed`).
10. Audit events `approval_issued`, `write_committed` / `write_queued` / `write_failed` with lineage. The approved email opens in Outlook via a compose deeplink for the RM to send. Call reports are marked "AI-assisted, RM-approved" in the CRM.

### 4.3 UC2 policy question
1. RM asks in the sidebar or Teams tab; `web-bff` sets the 7 s deadline and forwards with the RM's OBO token.
2. `orchestrator`: Prompt Shields (user prompt) and PII masking.
3. APIM AI gateway → `gen-small`: intent and query rewrite.
4. `tool-gateway.search_policies` with the RM's token; query embedding via APIM (`embed`).
5. AI Search hybrid query (BM25 + vector) filtered on `status = current`, `effective_from <= today`, `clearance <= RM clearance`.
6. Semantic rerank; ranked passages with document and clause references back to the orchestrator.
7. Best score below threshold → decline and offer routing to the credit or product desk.
8. APIM → `gen-large` answers only from the passages, one citation per claim.
9. Groundedness check: local claim-by-claim check always; Content Safety groundedness detection if offered in-region.
10. Fails and ≥ 4 s of the deadline remain → retry once with a stricter prompt; otherwise decline. At p95 the first attempt ends near 5.8 s, so a failed check usually declines: we accept a higher decline rate over a slower answer.
11. The answer is released only after it passes.

## 5. Non-functional baseline

| Item | Target (illustrative) |
|---|---|
| Users | Pilot 30 RMs; scale 300 RMs; peak 0.5 requests/s; ~5,400 requests/day at scale |
| Availability SLO | **Pilot 99.0% in business hours** (Mon–Fri 07:00–20:00 GST); scale 99.5%. "Available" = a correct answer **or** a clearly flagged degraded answer. Composite maths in `05-nfr-scenarios-and-dr.md` |
| Latency p95 (verified output, end to end) | UC1 brief ≤ 8 s; UC2 answer ≤ 7 s; UC3 draft ≤ 10 s; commit ≤ 2 s |
| Deadlines | Set at `web-bff`, propagated as a header; every hop's timeout = min(own cap, remaining deadline − reserve). Tool cap 2 s; model cap = remaining deadline; failover to a second backend only if ≥ 4 s remain, else degraded mode |
| RPO / RTO | Zone failure: audit RPO 0, RTO minutes (automatic). Region loss: audit RPO target ≤ 15 min (GZRS has no RPO guarantee unless geo priority replication is used **[confirm]**), app RTO best effort 24 h (BCP, only if UAE Central's catalogue allows the rebuild) |
| Quality gates | Groundedness ≥ 0.95 and citation accuracy ≥ 0.95 as the **95% Wilson lower bound** on a ≥ 300-item golden set (needs about ≥ 293/300 observed); entitlement leaks = 0; numeric mismatches = 0; injection success = 0 on a red-team suite of ≥ 1,000 attacks (zero failures in 300 items only bounds the rate below about 1%); false-positive block rate ≤ 2% |
| Token budget | Per request ≤ 6 tool steps, ≤ 12k input and 1.5k output tokens; per RM per day 400k tokens (APIM `llm-token-limit`) |

Latency budget per use case (p95, ms):

| Step | UC1 | UC2 | UC3 draft |
|---|---|---|---|
| Edge + web-bff + OBO (cached) | 150 | 150 | 150 |
| Entitlement (cached / miss) | 20 / 150 | n/a | 20 / 150 |
| Tool fan-out or retrieval (embed + search + rerank) | 1,500 | 700 | 300 (profile only) |
| PII + Prompt Shields (parallel with fan-out where possible) | 300 | 400 | 400 |
| Router / rewrite (`gen-small`) | 0 | 600 | 0 |
| Generation (time to first token + output) | 1,000 + 3,300 (≈200 tokens) | 1,000 + 2,500 (≈150 tokens) | 700 + 3,300 (≈400 tokens on `gen-small`) |
| Checks (numeric, policy, groundedness) | 100 | 400 | 100 |
| Total p95 | ≈ 6,500 (target 8,000) | ≈ 5,900 (target 7,000) | ≈ 5,100 (target 10,000) |

## 6. Critical issues and resolutions (ADRs hold the detail)

| # | Issue | Resolution |
|---|---|---|
| C1 | Teams bot traffic is processed by the global Bot Framework connector, which conflicts with UAE residency | Phase 1 uses a **Teams personal tab** calling `web-bff` directly (no bot, no public listener). Bot only in Phase 2 after a residency sign-off |
| C2 | Newest models and some Content Safety features may not exist in UAE North | No hard dependency: local groundedness and injection rules always run; model choice gated by regional availability |
| C3 | No in-country model DR | Degraded mode: UC1 data-only brief (facts, no wording), UC2 ranked passages without a generated answer, UC3 manual call report; flagged in the UI and counted as degraded |
| C4 | Tool-gateway must know the RM without trusting the orchestrator | OBO token for the `tool-gateway` audience + orchestrator workload identity (§3.4) |
| C5 | Prompts and completions contain customer data | Telemetry carries metadata only (explicit settings in §3.3, Azure Policy, CI test); content only in the encrypted audit content store |
| C6 | A shared HMAC key lets either side forge approvals; token under-bound | ES256 in Key Vault HSM (sign vs public key), claims bind RM, customer, operation, schema, draft hash; RFC 8785 canonical JSON; atomic jti create; step-up on approve |
| C7 | CRM write retries may double-post; two writes are not atomic | External reference per item + read-before-retry, saga with per-item status, Cosmos outbox; verdict persisted so retries after `exp` are valid |
| C8 | APIM semantic cache could leak across RMs | Off |
| C9 | Pre-generating briefs runs without the RM present | Phase 2 only, with risk sign-off (system identity, RM's portfolio enforced, output readable only by that RM, audit "system on behalf of") |
| C10 | Token and cost runaway | Step and token budgets; APIM per-RM and per-use-case limits; Azure budgets per environment with alerts at 80% |
| C11 | Compute platform | AKS because the Bank standardises on it (network policy, workload identity, PodMonitor scraping into managed Prometheus, signer/verifier pod isolation); Container Apps is the documented alternative if no AKS platform team exists |
| C12 | Model versions retire about yearly | Pins, auto-upgrade off, model inventory, eval gate before upgrade, 90-day upgrade runbook |
| C13 | Content filters may block normal banking language | Thresholds tuned on the golden set; false-positive rate is a release gate |
| C14 | Core banking cannot take copilot load | ODS replica, per-request cache, APIM quotas per consumer, bulkheads |
| C15 | Insider misuse | Sentinel analytics: briefs per hour, repeated `NOT_ENTITLED`, after-hours bulk use; kill switch playbook |
| C16 | Dates and weekends | All date logic in GST; UAE working week Mon–Fri |
| C17 | Microsoft abuse monitoring stores prompts for up to 30 days | Modified abuse monitoring approval is a go-live gate; `store=false` (§3.5) |
| C18 | Streaming would show unverified text | Buffer, verify, then release; progress steps shown while waiting |
| C19 | Output-side exfiltration (Markdown images, links) and number-free injected claims | Plain-text rendering, strict CSP, no images or external links; policy check on UC1 and UC3; number words in the numeric check |
| C20 | Graph scopes too broad | `Calendars.ReadBasic` only; no mail scope (compose deeplink) |
| C21 | Audit immutability vs. erasure and imperfect masking | WORM holds hashes, metadata and pointers; content encrypted per customer key, crypto-shredded on erasure without breaking the chain |
| C22 | Entitlement staleness and delegation | Cache ≤ 60 s + event invalidation; delegation (holiday cover) from CRM delegation records with expiry; team lead read-only role |
| C23 | No model risk management | MRM section in `05`: inventory, independent validation (including evaluators), lineage in every audit event, monitoring, AI-assisted marking in CRM |

## 7. Data retention (defaults, [confirm] with records management)

| Data | Store | Retention |
|---|---|---|
| Session state | Cosmos DB | 24 h TTL |
| Drafts and approval verdicts | Cosmos DB | 30 days TTL (committed items live in CRM) |
| jti replay store | Cosmos DB | 1 day TTL (> token life) |
| Entitlement and tool-result cache | Cosmos DB / in-process | ≤ 60 s / request scope |
| Audit metadata hash chain | Immutable Blob (GZRS) | 7 years |
| Audit content (masked prompts, outputs) | Encrypted store, per-customer key | 1 year, crypto-shred on erasure |
| Telemetry (no content) | App Insights / Log Analytics | 90 days |
| Metrics | Managed Prometheus | 18 months (platform default) |

## 8. Workstreams and acceptance criteria

| WS | Output | Owner files | Done when |
|---|---|---|---|
| A | HLD + ADRs | `docs/architecture/01-hld.md`, `docs/architecture/adr/*.md` | Context, container, deployment, network, identity and data-flow views as Mermaid; ≥ 18 ADRs each with context, options, trade-off table, decision, consequences, review trigger |
| B | LLD, interfaces and SLAs, NFR/scenarios/DR/MRM, deployment and operations | `02-lld.md`, `04-interfaces-and-slas.md`, `05-nfr-scenarios-and-dr.md`, `06-deployment-and-operations.md` | API contracts, sequence diagrams, per-system interface agreements (SLA/OLA, timeouts, retries, error contract, data class, owner, support hours), composite availability maths, failure-mode catalogue, STRIDE, MRM, capacity and cost, runbooks |
| C | Mock-Azure working prototype | `code/azmock/`, `code/services/`, `code/stack.py`, tests | Each Azure dependency mocked behind its REST shape; HTTP between services; trace propagation; `/metrics`; fault injection; all tests and evals green |
| D | Deck | `deck/project/slides/*`, `deck/project/deck.json`, `README.md` | `flow-read`, `flow-write`, `component-map`, `azure-deploy`, `slas` added; `stack`, `decisions` and other affected slides updated to Azure; layout measured |
| E | Prototype "How it works" tab | `prototype/rm-copilot.html` | Fourth tab, Azure diagram, highlights from trace steps; offline, light/dark, 375 px |
| F | Review and iteration | `docs/architecture/07-review-log.md` | Independent ARB review of all outputs; findings fixed or recorded with rationale |

## 9. Execution order
1. Plan review → this v2. 2. Workstreams A–E in parallel (lead commits per item). 3. ARB review of all outputs →
fixes. 4. Verification: tests, evals, stack smoke and load run, slide layout, prototype phone-width check.
5. Publish deck and prototype; push.

## 10. Facts to confirm with Microsoft or the Bank
1. Current GPT and embedding models offered as Regional Standard and Regional Provisioned in UAE North; retirement dates; minimum regional PTU and quota lead times.
2. Modified abuse monitoring eligibility, and where abuse data is stored and reviewed for UAE resources.
3. Whether Bot Service / Teams bot traffic can be processed in the UAE.
4. UAE Central access and service catalogue (AKS, APIM Premium, AI Search, Cosmos DB, Event Hubs).
5. In UAE North: Content Safety Prompt Shields and groundedness detection, semantic ranker, AI Search zones, AI Language PII for Arabic, Managed Redis.
6. APIM `llm-token-limit` daily quota behaviour across units; zone-redundant unit count.
7. Regions where Foundry risk-and-safety evaluators run.
8. Authentication freshness: whether `acrs` (authentication context) is present on OBO access tokens; `auth_time` on ID tokens.
9. CRM: iframe embedding and extension model for the sidebar; Teams tab on mobile through Entra Private Access.
10. Networking for the Sentinel playbook (Logic Apps) and in-network CI runners; DNS forwarding (Private Resolver or the Bank's forwarders).
11. **Go-live gate for UC3 writes:** the CRM supports an external-reference lookup or an idempotency key (ADR-010). Without it, UC3 stays draft-only.
12. Bank: residency and outsourcing approvals, records retention, erasure rules (PDPL banking scope, CBUAE, DIFC/ADGM where relevant), MRM standard, CRM idempotency / external reference support, SLAs and support hours for each system of record.

## 11. Iteration log
- v1 (26 Nov): baseline from the existing deck, code and the owner's Azure-only direction.
- v2 (26 Nov): ARB pre-review, 20 findings. Changes: identity chain (§3.4); Teams tab instead of bot in Phase 1
  (C1); Microsoft data handling (§3.5, C17); approval token binding, canonical JSON, atomic jti, step-up (C6);
  buffer-then-release instead of streaming (C18); SLO restated to 99.0% pilot with degraded counted as available;
  region loss treated as BCP with zone redundancy as the resilience design; telemetry settings explicit; output
  rendering and number-word checks (C19); Graph scopes reduced (C20); model named by availability, PTU after pilot;
  MRM added (C23); saga and read-before-retry (C7); crypto-shredding (C21); entitlement cache ≤ 60 s and delegation
  (C22); latency budget table; Phase 1 cuts (no bot, PTU, Redis, ADX, Service Bus; evals as CI job).
- v2.1 (26 Nov): ADR feedback. Per-RM token limits via an orchestrator-only `x-rm-oid` header; pilot single APIM
  unit recorded as an accepted risk; entitlement invalidation by CRM webhook; step-up via Conditional Access
  authentication context; UC2 numbered 1–11 (matches HLD §8.3) and the retry rule's p95 effect stated; "facts
  first" release for UC1; Wilson-bound gates and a ≥ 1,000-attack red-team suite; Cosmos periodic geo backup;
  audit content store, seal store and audit spool named; crypto-shred timing; write kill switch fail-closed after
  15 min; DNS Private Resolver, in-network CI runners, Managed HSM option; CRM idempotency as a UC3 go-live gate.
