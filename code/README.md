# RM Copilot: sample code and the mock-Azure local stack

Two things live here, both Python 3.13 standard library only, offline, no keys, fictional data ("the Bank"):

1. **`rm_copilot/`**: the in-process reference for the controls (UC1 brief, UC2 policy copilot, UC3 follow-up),
   with the golden-set release gate and the demo.
2. **The local stack** (`azmock/`, `services/`, `stack.py`): the deployable services of the plan (§3.2) as separate
   HTTP servers, talking to **mocks of every Azure dependency that speak the same REST shape** as the real service.
   The same `rm_copilot` workflow code runs inside the `orchestrator` service through small adapters, so the demo,
   the evals and the stack cannot drift apart. Moving a dependency to real Azure is a configuration change.
   Since plan v2.6 the orchestrator is a **fixed multi-agent graph** (supervisor + one specialist per request) whose
   agents call tools only as **MCP servers behind the APIM MCP gateway**, each with **its own Entra identity**, with a
   **checkpointer** and **session, episodic, procedural and profile memory** (see "Agents" and "Memory" below).

```bash
cd code
python3 demo.py                         # in-process walk-through of UC1, UC2, UC3 and the controls
python3 -m evals.run_evals              # release gate on the golden set (passes)
python3 -m evals.run_evals --careless   # same gate with a hallucinating model (fails, exit 1)
python3 -m unittest                     # 122 tests: 9 control + 17 platform unit + 43 stack over HTTP + 28 red-team + 16 agents/MCP + 9 memory
python3 stack.py                        # start the whole stack on 127.0.0.1:8700-8716, console at http://127.0.0.1:8712/
python3 stack.py --latency realistic    # same, with roughly realistic mock latencies
python3 stack.py --port-base 0 --smoke  # ephemeral ports, run UC1/UC2/UC3 once, verify the audit chain, stop
python3 loadtest.py -n 120 -c 16        # mixed load against an in-process stack, p50/p95 vs the plan §5 budgets
python3 loadtest.py --url http://127.0.0.1:8712   # against a stack you started with stack.py
```

The console (`services/web/`) signs you in as one of the fictional RMs (Layla, Omar, or Sara who covers one of
Layla's customers on a dated delegation), runs UC1/UC2/UC3 including edit, approve and commit, and shows the span
waterfall and the Azure components the request touched, which agent handled the request and the MCP tools it called,
the RM-approved episodes recalled for a brief, and a profile setting (brief format) kept in profile memory. Plain HTML/CSS/JS, no external assets, light and dark,
usable at 375 px, served under a strict Content-Security-Policy; model output is only ever rendered as text.

## Architecture of the local stack

```
browser console ──► web-bff ──► orchestrator ──► APIM (AI gateway) ──► Azure OpenAI primary ─┐ failover only if
   │  (Entra sign-in)   │       (agent graph)│   ├──► Content Safety, AI Language (PII)       └► secondary  ≥ 4 s left
   │                    │  supervisor ─► one │   ├──► App Configuration (flags, kill switches, transport key, procedural pins)
   │                    │  specialist agent  ├──► APIM (MCP gateway) ──► tool-gateway MCP servers: customer-data,
   ▼                    │                    │       agent token + agent OBO      policy-search, crm-drafts, memory
 Entra ID   Key Vault ◄─┘ (sign)             └──► tool-gateway (write API, memory store: checkpoints, session turns)
 (agent identities,                                │    ├──► APIM (system API facade) ──► CRM, ODS, LOS, KYC, news
  federated with the                               │    ├──► AI Search (hybrid + semantic), APIM embeddings, Graph calendar (OBO)
  orchestrator workload)                           │    ├──► Key Vault (approval public key; per-customer MEMORY keys only)
            Cosmos DB ◄─ sessions, drafts, profile │    ├──► Cosmos DB (jti, approvals, outbox, drafts, checkpoints, sessions, episodes)
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

## Agents, identities and the MCP gateway (plan §3.7, C24, C25, C28)

The orchestrator runs a **fixed graph** (`services/agents.py` on the small runtime `services/graph.py`; definition
`GRAPH_SPEC` in `services/common/procedural.py`, version `graph/2.6.0`):

```
supervisor ──► brief-agent ────┐
 (intent,   ──► policy-agent ──┼──► verify ──► respond                                (UC1, UC2)
  budgets)  ──► followup-agent ┘       └──► await_approval ┄ RM approves ┄► commit ──► respond   (UC3)
```

| Agent (Entra service principal) | Job | MCP tools (app role) |
|---|---|---|
| `supervisor` | intent from the entry point (no router call for CRM-launched requests), picks one specialist, budgets, loads session and profile memory | none |
| `brief-agent` | UC1: runs `Copilot.brief`, recalls this RM's RM-approved episodes | `customer-data/get_brief_inputs` (`Tools.CustomerData.Read`), `customer-data/get_customer_profile` (`Tools.CustomerData.Profile`), `memory/recall_episodes` (`Tools.Memory.Read`) |
| `policy-agent` | UC2: runs `Copilot.ask` | `policy-search/search_policies` (`Tools.PolicySearch`) |
| `followup-agent` | UC3: runs `Copilot.draft_followup`, stores the draft | `customer-data/get_customer_profile`, `crm-drafts/create_followup_draft` (`Tools.CrmDrafts.Write`, drafts only) |

* **At most 2 agent hops** per request (supervisor + one specialist), enforced by the runtime (a third agent node is
  refused with 429 `BUDGET_EXCEEDED` / `AGENT_HOPS`); edges are fixed and chosen by use case, never by model output.
* `verify`, `commit` and `respond` are **deterministic code**, not agents. The numeric, policy and groundedness checks
  inside the workflows are code too. **Commit is not an agent tool**: it runs only through the approval-verified write
  API with the RM's `Tools.Write` token; an agent can never obtain `Tools.Write`.
* **Three identities on every tool call** (`services/common/identities.py`, `azmock/entra.py`): the **user** (the RM's
  token, exchanged by the agent On-Behalf-Of for audience `api://rm-copilot-mcp`; it keeps the RM `oid` and names
  the agent in `act`), the **agent** (its own app token with exactly its tools' roles) and the **workload** (the
  orchestrator pod's assertion, audience `api://AzureADTokenExchange`, subject
  `system:serviceaccount:rm-copilot:orchestrator`). Agents have **no client secret**: Entra issues their tokens only
  for that assertion (federated identity credential), and the tokens carry `xms_fic_sub`.
* **MCP**: tool-gateway exposes `customer-data`, `policy-search`, `crm-drafts` and `memory` as MCP servers
  (JSON-RPC 2.0 over HTTP: `initialize`, `tools/list`, `tools/call`; `services/common/mcp.py`). **APIM is the MCP
  gateway** (`POST /mcp/{server}`, registered servers only): it validates the agent token and the user token (same
  agent, registered workload), applies **per-agent and per-RM limits**, forwards with its own `Mcp.Forward` token
  (tool-gateway accepts MCP only through the gateway) and filters `tools/list` to the agent's roles.
* **tool-gateway decides** (intersection): RM entitled (portfolio or delegation) **and** agent holds the tool's role
  **and** the agent token came from the registered workload **and** data class ≤ RM clearance **and** arguments match
  the pinned schema. The tools reuse the REST tools' code (entitlement first, minimisation, audit).
* **Tool poisoning (C25)**: tool names, versions, descriptions and input schemas are hashed; the hashes are pinned in
  App Configuration `rmc:procedural:pins`. A tool whose definition does not match is neither listed nor callable
  (409 `TOOL_PIN_MISMATCH`), and the orchestrator's MCP client refuses it as well.

## Memory (plan §3.8, C26, C27, C29)

| Memory | Where in the stack | Scope, retention |
|---|---|---|
| Working | `RunContext` in the orchestrator (raw notes, question, approval token: never checkpointed) | one request |
| Checkpointer | a checkpoint after every graph node, written through the tool-gateway memory store (`/internal/v1/memory/checkpoints`) to Cosmos `checkpoints`, sealed with the customer's memory key | thread = RM `oid` + session; TTL 7 days, **30 days while a UC3 draft waits** |
| Session | Cosmos `sessions` (`turns-<hash(oid, session)>`): last 6 turns + a deterministic rolling summary within 300 tokens; ids and outcomes only | RM + session, 24 h |
| Episodic | Cosmos `episodes`: written **only by tool-gateway from the approval-verified draft** (call report and task titles, PII masked, injected text dropped), with provenance (source, author, time, version, jti); recalled by `brief-agent` for the **same RM and customer**, entitlement re-checked at recall | 90 days |
| Procedural | prompt templates, graph definition and tool definitions with versions and SHA-256 (`services/common/procedural.py`), pinned in App Configuration; a mismatch is refused (503 `PROCEDURAL_PIN_MISMATCH`) | versioned |
| Profile | Cosmos `rm-profile` (language, brief format), set by the RM through web-bff `PUT /api/profile` | until changed |

* **UC3 interrupt and resume**: the run pauses at `await_approval` (checkpoint `interrupted`); on approve web-bff sends
  the run id stored with the draft, and **any** orchestrator process resumes from the checkpoint (`commit`, `respond`),
  also after a restart. A completed run cannot be committed again (409 `ALREADY_COMMITTED`).
* **Replay**: operators read a run's checkpoints with the PIM role `Checkpoints.Replay` (audited as
  `checkpoint_replayed`); `GraphRuntime.replay` re-runs the deterministic nodes and checks they reproduce the state.
* **Nothing quarantined or injected is written to memory** (`memory_safe`): flagged strings are withheld, quoted
  instructions removed, quarantined text and rejected model output are kept as counts only, in the runtime and again
  at the store.
* **Erasure** (`POST /audit/erasure`) also deletes the customer's memory keys (`memory-ckpt-c-<id>`,
  `memory-ep-c-<id>`) and their checkpoints and episodes.
* The orchestrator still holds **no key and no Cosmos DB access**: tool-gateway is the memory store and holds the
  memory keys (Key Vault secrets scoped to the `memory-` prefix).

### Logical service → mock → real Azure

Every dependency is one base URL in `services/common/config.py`; `RMC_URL_<NAME>` overrides it and `stack.py`
then does not start that mock (`NAME` upper case, `-` as `_`, e.g. `RMC_URL_CONTENT_SAFETY`).

| Logical service | Mock module (port) | Real endpoint and REST surface used | Config key |
|---|---|---|---|
| Microsoft Entra ID | `azmock/entra.py` (8700) | `https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token` (client credentials, OBO), `/discovery/v2.0/keys`, OpenID metadata | `RMC_URL_ENTRA`, `RMC_TENANT_ID` |
| Azure Key Vault (Premium) | `azmock/keyvault.py` (8701) | `{vault}/keys/{name}[/{version}]`, `/sign` (ES256), `/verify`, `/unwrapkey` (RSA-OAEP-256), `/secrets/{name}` (7.4) | `RMC_URL_KEYVAULT` |
| Azure App Configuration | `azmock/appconfig.py` (8702) | `{store}/kv/{key}`, feature flags `.appconfig.featureflag/{uc1,uc2,uc3,writes}`, procedural pins `rmc:procedural:pins` | `RMC_URL_APPCONFIG` |
| Azure Cosmos DB for NoSQL | `azmock/cosmos.py` (8703) | `/dbs/rmcopilot/colls/{sessions,drafts,jti,approvals,outbox,leases,checkpoints,episodes,rm-profile}/docs`, change feed (`A-IM: Incremental feed`) | `RMC_URL_COSMOS` |
| Azure Event Hubs | `azmock/eventhubs.py` (8704) | `POST {namespace}/{audit,crm-portfolio-events}/messages` (batch) | `RMC_URL_EVENTHUBS` |
| Azure OpenAI (Foundry), primary / secondary | `azmock/aoai.py` (8705, 8706) | `/openai/deployments/{gen-large,gen-small,embed}/chat/completions`, `/embeddings` (2024-10-21) | `RMC_URL_AOAI_PRIMARY`, `RMC_URL_AOAI_SECONDARY` |
| Azure API Management | `azmock/apim.py` (8707) | AI gateway (same paths as Azure OpenAI), system API facade `/{crm,ods,los,kyc,news}/api/...`, MCP gateway `/mcp/{customer-data,policy-search,crm-drafts,memory}` | `RMC_URL_APIM` |
| Azure AI Content Safety | `azmock/content_safety.py` (8708) | `/contentsafety/text:shieldPrompt`, `text:analyze`, `text:detectGroundedness` | `RMC_URL_CONTENT_SAFETY` |
| Azure AI Language | `azmock/language.py` (8709) | `/language/:analyze-text` (PiiEntityRecognition) | `RMC_URL_LANGUAGE` |
| Azure AI Search | `azmock/search.py` (8710) | `/indexes/{index}` (PUT), `/docs/index`, `/docs/search` (filter, semantic, vector) | `RMC_URL_SEARCH`, `RMC_INDEX_NAME` |
| Systems of record (CRM, ODS, LOS, KYC, news) | `azmock/systems.py` (8711) | the Bank's APIs behind APIM (fictional contracts) | `RMC_URL_CRM`, `RMC_URL_ODS`, `RMC_URL_LOS`, `RMC_URL_KYC`, `RMC_URL_NEWS` |
| Microsoft Graph (calendar) | `azmock/systems.py` (8711, `/graph`) | `https://graph.microsoft.com/v1.0/me/calendarView` (`Calendars.ReadBasic`, OBO) | `RMC_URL_GRAPH` |
| Azure Monitor (App Insights, managed Prometheus) | `services/common/telemetry.py` | OpenTelemetry exporters; PodMonitor scraping `/metrics` | n/a (see below) |

| Service (plan §3.2) | Module (port) | Notes |
|---|---|---|
| `web-bff` | `services/web_bff.py` (8712) | token validation, OBO, session + CSRF, Origin check, step-up (`auth_time` ≤ 10 min), deadlines, approval minting via Key Vault, CSP, console, `/api/trace/{request_id}` |
| `orchestrator` | `services/orchestrator.py` + `agents.py` + `graph.py` + `orchestrator_adapters.py` (8713) | supervisor-led agent graph (≤ 2 hops) running `rm_copilot.Copilot` per specialist with MCP and HTTP adapters; agent identities via the workload's federated credential; checkpoints, interrupt/resume, procedural pins; budgets, degraded mode, feature flags, guardrail floor, audit via tool-gateway with content envelope-encrypted |
| `tool-gateway` | `services/tool_gateway.py` (8714) | both tokens, entitlement first (cache ≤ 60 s, delegations, evicted by `crm-portfolio-events`), `writes` kill switch, key-version allow-list, parallel fan-out, minimisation, approval verification (`approval_verifier.py`), saga, sole Event Hubs producer; MCP servers and the intersection rule; memory store (checkpoints, session turns, episodes, per-customer memory keys) |
| `outbox-worker` | `services/outbox_worker.py` (8715) | Cosmos change feed, read-before-retry, backoff, per-item status |
| `audit-writer` | `services/audit_writer.py` (8716) | hash chain (`verify_chain`), per-customer content keys in Key Vault, crypto-shred (audit content, checkpoints, episodes), operator APIs |
| `ingestion-worker` | `services/ingestion_worker.py` (job at start-up) | PII scan, embeddings via APIM, AI Search index |

Telemetry is metadata only by construction: span attributes and log fields pass an allow-list
(`telemetry.SAFE_KEYS`), exceptions are recorded by type, metric labels use route templates. Every service exposes
`/metrics` (Prometheus text: `http_requests_total`, `http_request_duration_seconds`, `llm_tokens_total`,
`guardrail_events_total`, `entitlement_denials_total`, `declines_total`, `degraded_responses_total`,
`circuit_breaker_state`, `approval_tokens_issued_total` / `_verified_total` / `_rejected_total`, outbox and audit
counters; agents: `agent_hops` (histogram per use case), `mcp_calls_total{agent,tool,decision}` (tool-gateway),
`mcp_gateway_requests_total{agent,server,outcome}` (APIM), `memory_writes_total{type}` (checkpoint, session, episode,
profile), `checkpoint_resumes_total`) and `/telemetry/spans?trace_id=` (the local stand-in for an App Insights query). A test runs all three
use cases and asserts no customer name, notes text, prompt text or PII appears in any span, log or metric; spans
exist per agent node (`graph.node <node>`: `rmc.node`, `rmc.agent`, `rmc.hop`) and per MCP call
(`mcp tools/call <server>`: `rmc.agent`, `rmc.tool`, `rmc.decision`), metadata only; another test checks that no
metric carries a customer name, id, RM `oid` or UPN.

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
- **Entra Agent ID**: not emulated. Each agent is a plain service principal in the Entra mock with app roles and one
  federated identity credential; the "workload assertion" is a token the Entra mock signs for the orchestrator's client
  credentials (standing in for the AKS-projected service-account token from the cluster's OIDC issuer). The
  `xms_fic_sub` / `act.xms_fic_sub` claims are this mock's way to show the federation; real agent tokens (agent
  identity blueprints, agent user flows) carry different claims [confirm with Microsoft].
- **MCP**: no MCP SDK. A hand-written JSON-RPC 2.0 subset (`initialize`, `notifications/initialized`, `ping`,
  `tools/list`, `tools/call`) over plain HTTP POST: no SSE streaming, no `Mcp-Session-Id`, no resources, prompts,
  sampling, elicitation or `listChanged` notifications, no OAuth protected-resource metadata; authorization denials
  are JSON-RPC errors with the HTTP status mirrored. APIM's MCP server support (GA, UAE North) is a [confirm] item;
  here the APIM mock forwards JSON-RPC and filters `tools/list`.
- **Agent framework**: `services/graph.py` is a small fixed-graph runtime standing in for Microsoft Agent Framework
  workflows or LangGraph; the supervisor does not call `gen-small` (all entry points carry their intent), so its
  identity holds no roles and makes no calls.
- **Memory**: checkpoints and episodes use the same MOCK cipher as audit content; memory keys are Key Vault secrets
  restricted by name prefix (production: a separate memory key vault, AES-256-GCM); tool-gateway caches memory keys
  for 60 s, so another process can still decrypt for up to that long after an erasure (the documents are deleted at
  once); the session summary is a deterministic count of earlier turns, not a model summary; session documents share
  the `sessions` container with web-bff's CSRF sessions (container-level RBAC in the mock).
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
   - **Agents and MCP**: register one agent identity per agent (Entra Agent ID or app registration) with the app roles
     in `services/common/identities.py` on an API `api://rm-copilot-mcp`, and a federated identity credential trusting
     the AKS OIDC issuer and `system:serviceaccount:rm-copilot:orchestrator`; publish `pins()` from
     `services/common/procedural.py` to App Configuration `rmc:procedural:pins` in the pipeline (after the eval gate).
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
| `tests/` | Governance | `test_controls.py` (in-process controls), `test_platform_units.py` (crypto, JOSE, canonical hash, filters, telemetry), `test_stack_e2e.py` and `test_stack_writes_resilience.py` (the stack over HTTP), `test_redteam.py` and `test_redteam_followups.py` (attacks on the controls), `test_agents_mcp.py` (agent identities, MCP intersection rule, tool poisoning, hops, procedural pins, agent telemetry), `test_memory.py` (checkpoints, interrupt/resume incl. restart, replay, session budget, episodic recall rules, memory poisoning, erasure) |
| `services/agents.py`, `services/graph.py` | Agents | Supervisor-led graph with checkpointer, interrupt/resume and replay; specialists reuse `rm_copilot.Copilot` |
| `services/common/mcp.py`, `identities.py`, `procedural.py`, `memory.py` | Agents, Security | MCP tool registry and JSON-RPC; agent identities and roles; procedural pins; memory sealing, `memory_safe`, episodes, session budget |
