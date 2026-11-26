# RM Copilot: sample code and the mock-Azure local stack

Two things live here, both Python 3.13 standard library only, offline, no keys, fictional data ("the Bank"):

1. **`rm_copilot/`**: the in-process reference for the controls (UC1 brief, UC2 policy copilot, UC3 follow-up),
   with the golden-set release gate and the demo.
2. **The local stack** (`azmock/`, `services/`, `stack.py`): the deployable services of the plan (§3.2) as separate
   HTTP servers, talking to **mocks of every Azure dependency that speak the same REST shape** as the real service.
   The same `rm_copilot` workflow code runs inside the `orchestrator` service through small adapters, so the demo,
   the evals and the stack cannot drift apart. Moving a dependency to real Azure is a configuration change.

```bash
cd code
python3 demo.py                         # in-process walk-through of UC1, UC2, UC3 and the controls
python3 -m evals.run_evals              # release gate on the golden set (passes)
python3 -m evals.run_evals --careless   # same gate with a hallucinating model (fails, exit 1)
python3 -m unittest                     # 97 tests: 9 control + 17 platform unit + 43 stack tests over HTTP + 28 red-team tests
python3 stack.py                        # start the whole stack on 127.0.0.1:8700-8716, console at http://127.0.0.1:8712/
python3 stack.py --latency realistic    # same, with roughly realistic mock latencies
python3 stack.py --port-base 0 --smoke  # ephemeral ports, run UC1/UC2/UC3 once, verify the audit chain, stop
python3 loadtest.py -n 120 -c 16        # mixed load against an in-process stack, p50/p95 vs the plan §5 budgets
python3 loadtest.py --url http://127.0.0.1:8712   # against a stack you started with stack.py
```

The console (`services/web/`) signs you in as one of the fictional RMs (Layla, Omar, or Sara who covers one of
Layla's customers on a dated delegation), runs UC1/UC2/UC3 including edit, approve and commit, and shows the span
waterfall and the Azure components the request touched. Plain HTML/CSS/JS, no external assets, light and dark,
usable at 375 px, served under a strict Content-Security-Policy; model output is only ever rendered as text.

## Architecture of the local stack

```
browser console ──► web-bff ──► orchestrator ──► APIM (AI gateway) ──► Azure OpenAI primary ─┐ failover only if
   │  (Entra sign-in)   │            │   ├──► Content Safety, AI Language (PII)               └► secondary  ≥ 4 s left
   │                    │            │   ├──► App Configuration (flags, kill switches, transport key)
   ▼                    │            └──► tool-gateway ──► APIM (system API facade) ──► CRM, ODS, LOS, KYC, news
 Entra ID   Key Vault ◄─┘ (sign)          │    ├──► AI Search (hybrid + semantic), APIM embeddings, Graph calendar (OBO)
            Cosmos DB ◄─ sessions, drafts │    ├──► Key Vault (approval public key only), Cosmos DB (jti, approvals, outbox)
                                          │    └──► Event Hubs (sole audit producer) ──► audit-writer ──► hash chain + encrypted content
                                outbox-worker ◄── Cosmos change feed (read-before-retry to the CRM via APIM)
```

Each box is its own `ThreadingHTTPServer` on its own port with its own telemetry, as separate pods would be. All run
in one Python process for convenience (`stack.py`); nothing is shared between them except HTTP.

Identity chain (plan §3.4): the RM signs in (aud `web-bff`); web-bff exchanges it On-Behalf-Of for a token with
audience `tool-gateway` (`Tools.Read`, plus `Tools.Write` only on approve) and sends it in `X-RM-Authorization`,
authenticating itself with its own workload token. The orchestrator forwards the RM token unchanged with its own
workload token; tool-gateway validates both and takes the RM from the user token's `oid`, never from a body or
header. Approvals are ES256 tokens signed by Key Vault for web-bff
(`{iss, aud, sub, customer_id, op, draft_sha256, schema, iat, exp=iat+300, jti, kid}`, hash over the canonical
approved draft: call report, tasks with type and due date, and the email text); tool-gateway verifies with the
public key only, accepts only key versions listed in App Configuration `rmc:tool-gateway:approval:allowed_kids`
(rotation with overlap, emergency revocation; else `APPROVAL_INVALID` / `kid_not_allowed`), audits every rejection as
`approval_rejected` (reason, jti, kid, token hash; never the draft), creates the `jti` atomically in Cosmos DB
(409 = replay), persists the verdict with the draft and runs the write saga (one CRM write per item, external
reference `jti:item`, Cosmos outbox on timeout/5xx). Entitlements are cached at most 60 s and evicted at once when
the CRM publishes a portfolio or delegation change (`POST /crm-events/v1/portfolio` on APIM, identity
`crm-integration`, role `CrmEvents.Publish` -> Event Hub `crm-portfolio-events` -> tool-gateway consumer).

### Logical service → mock → real Azure

Every dependency is one base URL in `services/common/config.py`; `RMC_URL_<NAME>` overrides it and `stack.py`
then does not start that mock (`NAME` upper case, `-` as `_`, e.g. `RMC_URL_CONTENT_SAFETY`).

| Logical service | Mock module (port) | Real endpoint and REST surface used | Config key |
|---|---|---|---|
| Microsoft Entra ID | `azmock/entra.py` (8700) | `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token` (client credentials, OBO), `/discovery/v2.0/keys`, OpenID metadata | `RMC_URL_ENTRA`, `RMC_TENANT_ID` |
| Azure Key Vault (Premium) | `azmock/keyvault.py` (8701) | `{vault}/keys/{name}[/{version}]`, `/sign` (ES256), `/verify`, `/unwrapkey` (RSA-OAEP-256), `/secrets/{name}` (7.4) | `RMC_URL_KEYVAULT` |
| Azure App Configuration | `azmock/appconfig.py` (8702) | `{store}/kv/{key}`, feature flags `.appconfig.featureflag/{uc1,uc2,uc3,writes}` | `RMC_URL_APPCONFIG` |
| Azure Cosmos DB for NoSQL | `azmock/cosmos.py` (8703) | `/dbs/rmcopilot/colls/{sessions,drafts,jti,approvals,outbox,leases}/docs`, change feed (`A-IM: Incremental feed`) | `RMC_URL_COSMOS` |
| Azure Event Hubs | `azmock/eventhubs.py` (8704) | `POST {namespace}/{audit,crm-portfolio-events}/messages` (batch) | `RMC_URL_EVENTHUBS` |
| Azure OpenAI (Foundry), primary / secondary | `azmock/aoai.py` (8705, 8706) | `/openai/deployments/{gen-large,gen-small,embed}/chat/completions`, `/embeddings` (2024-10-21) | `RMC_URL_AOAI_PRIMARY`, `RMC_URL_AOAI_SECONDARY` |
| Azure API Management | `azmock/apim.py` (8707) | AI gateway (same paths as Azure OpenAI) and system API facade `/{crm,ods,los,kyc,news}/api/...` | `RMC_URL_APIM` |
| Azure AI Content Safety | `azmock/content_safety.py` (8708) | `/contentsafety/text:shieldPrompt`, `text:analyze`, `text:detectGroundedness` | `RMC_URL_CONTENT_SAFETY` |
| Azure AI Language | `azmock/language.py` (8709) | `/language/:analyze-text` (PiiEntityRecognition) | `RMC_URL_LANGUAGE` |
| Azure AI Search | `azmock/search.py` (8710) | `/indexes/{index}` (PUT), `/docs/index`, `/docs/search` (filter, semantic, vector) | `RMC_URL_SEARCH`, `RMC_INDEX_NAME` |
| Systems of record (CRM, ODS, LOS, KYC, news) | `azmock/systems.py` (8711) | the Bank's APIs behind APIM (fictional contracts) | `RMC_URL_CRM`, `RMC_URL_ODS`, `RMC_URL_LOS`, `RMC_URL_KYC`, `RMC_URL_NEWS` |
| Microsoft Graph (calendar) | `azmock/systems.py` (8711, `/graph`) | `https://graph.microsoft.com/v1.0/me/calendarView` (`Calendars.ReadBasic`, OBO) | `RMC_URL_GRAPH` |
| Azure Monitor (App Insights, managed Prometheus) | `services/common/telemetry.py` | OpenTelemetry exporters; PodMonitor scraping `/metrics` | n/a (see below) |

| Service (plan §3.2) | Module (port) | Notes |
|---|---|---|
| `web-bff` | `services/web_bff.py` (8712) | token validation, OBO, session + CSRF, Origin check, step-up (`auth_time` ≤ 10 min), deadlines, approval minting via Key Vault, CSP, console, `/api/trace/{request_id}` |
| `orchestrator` | `services/orchestrator.py` + `orchestrator_adapters.py` (8713) | runs `rm_copilot.Copilot` with HTTP adapters; budgets, degraded mode, feature flags, guardrail floor, audit via tool-gateway with content envelope-encrypted |
| `tool-gateway` | `services/tool_gateway.py` (8714) | both tokens, entitlement first (cache ≤ 60 s, delegations, evicted by `crm-portfolio-events`), `writes` kill switch, key-version allow-list, parallel fan-out, minimisation, approval verification (`approval_verifier.py`), saga, sole Event Hubs producer |
| `outbox-worker` | `services/outbox_worker.py` (8715) | Cosmos change feed, read-before-retry, backoff, per-item status |
| `audit-writer` | `services/audit_writer.py` (8716) | hash chain (`verify_chain`), per-customer content keys in Key Vault, crypto-shred, operator APIs |
| `ingestion-worker` | `services/ingestion_worker.py` (job at start-up) | PII scan, embeddings via APIM, AI Search index |

Telemetry is metadata only by construction: span attributes and log fields pass an allow-list
(`telemetry.SAFE_KEYS`), exceptions are recorded by type, metric labels use route templates. Every service exposes
`/metrics` (Prometheus text: `http_requests_total`, `http_request_duration_seconds`, `llm_tokens_total`,
`guardrail_events_total`, `entitlement_denials_total`, `declines_total`, `degraded_responses_total`,
`circuit_breaker_state`, `approval_tokens_issued_total` / `_verified_total` / `_rejected_total`, outbox and audit
counters) and `/telemetry/spans?trace_id=` (the local stand-in for an App Insights query). A test runs all three
use cases and asserts no customer name, notes text, prompt text or PII appears in any span, log or metric.

Errors use one envelope everywhere in our services: `{"error": {"code", "message", "request_id", "retryable",
"reason"?, ...}}` with the LLD §1.4 codes (`NOT_ENTITLED`, `APPROVAL_INVALID` + reason, `REPLAYED`,
`STEP_UP_REQUIRED`, `RATE_LIMITED`, `BUDGET_EXCEEDED`, `FEATURE_DISABLED`, `DEPENDENCY_UNAVAILABLE` + reason, ...).
Degraded and partial answers are `200` with `"mode": "degraded" | "partial"` and `warnings`.

### Fault injection

Every mock has `POST /admin/faults {"target", "latency_ms", "error_rate", "outage", "status", "retry_after_ms",
"slow_ms"}` and `DELETE /admin/faults`, or at start-up `AZMOCK_FAULTS="crm:outage=1;aoai-primary:status=429,error_rate=1,retry_after_ms=2000"`.
Targets: `crm ods los kyc news graph` (systems), `aoai-primary`, `aoai-secondary`, `content-safety`, `language`, and
one per other mock. `slow_ms` delays the response after a write has been processed (the lost-response case).
APIM policies: `GET|POST /admin/policies` (tokens per minute, daily quota, calls per minute, min failover seconds),
`POST /admin/reset`. Kill switches: App Configuration `PUT /kv/.appconfig.featureflag%2Fwrites` as `ops-admin`.

## What the mocks do NOT emulate (honest list)

- **Security perimeter**: no TLS, no private endpoints, no WAF, no network policy; everything is on 127.0.0.1.
  Workloads authenticate with client secrets generated at start-up (in memory) instead of AKS workload identity.
- **Entra ID**: RM sign-in is ROPC with a fixed mock password (production: MSAL auth code + PKCE or Teams SSO, MFA,
  Conditional Access, authentication context); `auth_time` is placed in the access token; no consent, revocation,
  group overage or token binding.
- **Azure OpenAI**: answers come from the deterministic `MockLLM`; token counts are characters / 4; embeddings are
  hashed bag-of-words vectors; no streaming, tools, real content filters (a rule stands in for Prompt Shields);
  quotas and regional capacity are not modelled.
- **APIM**: counters live in one process (real APIM counters are per unit/region, plan §10 item 6); policy error
  bodies use APIM's `{"statusCode","message"}` shape; the per-RM quota key is an `x-rm-oid` header honoured only
  from the orchestrator and tool-gateway (LLD: `oid` from a validated RM token); chat deployments are limited to the
  orchestrator by caller identity rather than separate `AiGateway.Generate`/`Embed` roles; the RM header to legacy backends is an HMAC
  over (oid, method, path, ts), not the LLD `x-rm-assertion`; one App ID URI for both APIM APIs.
- **AI Search**: the semantic reranker score is BM25 × query coverage, not the calibrated 0-4 scale, so
  `MIN_RETRIEVAL_SCORE` must be re-tuned with the golden set on real Azure; OData filters support `eq ne lt le gt
  ge` joined by `and` only; no analyzers (no `ar.microsoft`), scoring profiles, captions or index aliases.
- **Content Safety / AI Language**: rules and regexes, English only; no ML classifiers, no Arabic PII model (Arabic-Indic
  and Eastern Arabic-Indic digits are normalised to ASCII before matching, ADR-012, so structured IDs and phone numbers
  in those scripts are masked; Arabic names are not);
  groundedness wraps the local claim-by-claim check.
- **Cosmos DB**: single partition, equality-only SQL, change feed starts at the beginning when no continuation is
  given, bearer auth instead of the SDK's AAD signature scheme, in memory (lost on restart).
- **Event Hubs**: one partition, two hubs (`audit`, with the LLD's `audit-events` / `audit-content` split not modelled,
  and `crm-portfolio-events`), per-hub sender/receiver roles, and a mock-only REST receive API standing in for the AMQP
  consumer; no checkpoint store (consumers keep an in-memory checkpoint).
- **Key Vault**: keys in memory, no HSM, no soft-delete/purge protection (DELETE purges), no rotation policy.
- **Audit store**: a local JSON-lines file, one chain, no WORM storage, no hourly signed anchors, customer id in
  clear in metadata (LLD: HMAC reference). The content cipher is a **mock** (HMAC-SHA256 counter mode,
  encrypt-then-MAC); the DEK wrap is real RSA-OAEP-256 (checked against OpenSSL). Production: AES-256-GCM.
- **Crypto**: ES256 (RFC 6979, verified against the RFC test vector) and RS256/RSA-OAEP (verified against OpenSSL)
  are pure Python and not constant-time. Fine for a mock; never for real keys.
- **Not built**: Document Intelligence (the corpus is already clause-segmented), Speech, the Teams tab / bot,
  Application Gateway, Purview, Defender, Sentinel, the eval-runner service (the golden-set gate runs as
  `evals/run_evals.py`), operator outbox APIs (retry/resolve/cancel), `approval_unused` sweep, the Cosmos
  `entitlements` / `idempotency` / `audit-chain-heads` containers (entitlement cache is in-process).
- **Performance**: all services share one interpreter; `loadtest.py` measures orchestration overhead against mock
  latencies that are illustrative p50s, not Azure benchmarks.

## Switching a dependency to real Azure

1. Set the base URL: `RMC_URL_<NAME>=https://...` (table above). `stack.py` stops starting that mock.
2. Identity: point `RMC_URL_ENTRA` at `https://login.microsoftonline.com` and `RMC_TENANT_ID` at the tenant;
   register the app IDs and roles listed in `azmock/entra.py` (`APP_ROLES`, `DELEGATED`); in AKS replace the
   client secrets in `TokenProvider` with workload identity (federated token as `client_assertion`).
3. Per service, what changes beyond the URL:
   - **Azure OpenAI / APIM**: import the APIM policies from LLD §7; deployments `gen-large`, `gen-small`, `embed`.
     The orchestrator already sends `store: false`, `max_tokens`, `x-use-case`; APIM must take the quota key from
     the RM token.
   - **AI Search**: create the index from `services/ingestion_worker.py` `INDEX_FIELDS` plus a vector profile and
     semantic configuration `policy-semantic`; re-tune `MIN_RETRIEVAL_SCORE` on the golden set.
   - **Content Safety / Language**: same paths and bodies; check regional availability (plan §10) and keep the
     local floor (it always runs).
   - **Key Vault**: create `approval-signing` (EC P-256) and `audit-transport` (RSA, `unwrapKey`), assign RBAC as in
     `azmock/keyvault.py` `RBAC`; publish the transport public JWK to App Configuration key
     `audit/transport-public-key` and the accepted approval key versions to `rmc:tool-gateway:approval:allowed_kids`
     (JSON list of versions; both done by `stack.py` locally). Without that key every commit fails closed (503).
   - **Cosmos DB**: swap `services/common/cosmos_client.py` for the `azure-cosmos` SDK (real auth header and
     partition keys); containers and TTLs as in `azmock/cosmos.py` `CONTAINERS`.
   - **Event Hubs**: keep the REST send; replace the mock receive calls (audit-writer on `audit`, tool-gateway on
     `crm-portfolio-events`) with the AMQP consumer and a checkpoint store. The CRM publishes to APIM
     `POST /crm-events/v1/portfolio` with its own Entra identity (`CrmEvents.Publish`).
   - **Systems of record and Graph**: the Bank's contracts (LLD §3) behind APIM; Graph needs only `Calendars.ReadBasic`.
4. Run `python3 -m evals.run_evals` against the real model, then the stack tests, before any release.

## What each `rm_copilot` file shows

| File | Deck slide | What it demonstrates |
|---|---|---|
| `rm_copilot/tools.py` | Agents, Security | Typed tool catalogue; entitlements enforced in code per call; field minimisation (national ID never leaves the tool); write tools that need a signed, draft-bound, expiring RM approval token (HMAC here; the stack uses ES256 via Key Vault); audit log |
| `rm_copilot/retrieval.py` | RAG | Filter first (current version, effective date, clearance), then BM25 recall, then reranking. The AI Search mock reuses it |
| `rm_copilot/llm.py` | AI components | One `LLMClient` interface; the real system prompts; `MockLLM` (also inside the Azure OpenAI mock); `CarelessLLM` to prove the checks work; `ModelUnavailable` for degraded mode |
| `rm_copilot/guardrails.py` | Hallucination, Security | Injection detection and quote quarantine; PII masking (Emirates ID, IBAN, card, trade licence, email, phone); claim-by-claim groundedness; no free numbers (digits and English/Arabic number words); no approval/pricing/limit promises |
| `rm_copilot/ports.py` | Architecture | The two interfaces the workflows depend on (`ToolPort`, `GuardPort`) and their in-process adapters |
| `rm_copilot/orchestrator.py` | Architecture | UC1 fixed workflow with `{placeholders}`, numeric and policy checks, partial and degraded briefs; UC2 rewrite, decline threshold, verify, retry once only with ≥ 4 s left, passages-only degraded mode; UC3 drafts with deterministic dates and manual degraded path |
| `evals/` | Evaluation | Golden set and release gate with thresholds |
| `tests/` | Governance | `test_controls.py` (in-process controls), `test_platform_units.py` (crypto, JOSE, canonical hash, filters, telemetry), `test_stack_e2e.py` and `test_stack_writes_resilience.py` (the stack over HTTP), `test_redteam.py` and `test_redteam_followups.py` (attacks on the controls) |
