# Architecture decision records: SME RM Copilot on Azure

This folder holds the architecture decision records (ADRs) for the SME RM Copilot. Each ADR follows the same
structure: status, date, context (forces, constraints and NFRs from `../00-plan.md`), options considered, a trade-off
table, decision, consequences (positive, negative, risks with mitigations), review trigger, and related ADRs and
plan sections. The baseline is `../00-plan.md` v2.4 (ADR-023 to ADR-026 and the ADR-005 amendment follow v2.6; ADR-027, which extends ADR-018, follows v2.7); the high-level design is `../01-hld.md`. ADRs 003, 008, 009, 010, 013, 015,
019 and 020 were amended before ARB approval to match plan v2.1 to v2.4; each records its amendment in the status
table (`07-review-log.md` lists the findings). Once the ARB approves the pack, rule 5 below applies.

Status values: **Accepted** (in force for the pilot), **Proposed** (to be decided at a stated gate), **Amended by**
(still in force, with the changes recorded in the later ADR, which is linked), **Extended by** (in force
unchanged; a later ADR, which is linked, adds to it), **Superseded** (replaced by a later
ADR, which is linked). Items marked **[confirm]** inside an ADR are facts to verify with the
Bank or Microsoft (plan §10); a decision may stand while its [confirm] items are open, but each is tracked to closure
before go-live.

## Index

| ADR | Title | Status | File |
|---|---|---|---|
| ADR-001 | Azure only, UAE North, Regional deployments, zone redundancy, no cross-border failover | Accepted | [ADR-001-azure-uae-north-regional-residency.md](ADR-001-azure-uae-north-regional-residency.md) |
| ADR-002 | AKS private cluster for compute | Accepted | [ADR-002-aks-compute.md](ADR-002-aks-compute.md) |
| ADR-003 | API Management as AI gateway and system API facade | Accepted | [ADR-003-apim-ai-gateway.md](ADR-003-apim-ai-gateway.md) |
| ADR-004 | Model hosting and tiering on Azure OpenAI in Azure AI Foundry | Accepted (PTU gate Proposed) | [ADR-004-model-hosting-and-tiering.md](ADR-004-model-hosting-and-tiering.md) |
| ADR-005 | Thin custom orchestrator with deterministic workflows | Amended by ADR-025 (supervisor-led agents on the same deterministic principles) | [ADR-005-orchestration-approach.md](ADR-005-orchestration-approach.md) |
| ADR-006 | Retrieval on Azure AI Search with hybrid query and semantic ranker | Accepted | [ADR-006-retrieval-azure-ai-search.md](ADR-006-retrieval-azure-ai-search.md) |
| ADR-007 | Customer data is read live as the RM, not embedded or indexed | Accepted | [ADR-007-customer-data-live-not-indexed.md](ADR-007-customer-data-live-not-indexed.md) |
| ADR-008 | Entra OBO plus workload identity; entitlements enforced in tool-gateway | Accepted | [ADR-008-identity-obo-and-entitlements.md](ADR-008-identity-obo-and-entitlements.md) |
| ADR-009 | Approval token signed with ES256 in Key Vault HSM | Accepted | [ADR-009-approval-token-es256-key-vault.md](ADR-009-approval-token-es256-key-vault.md) |
| ADR-010 | Write reliability with a per-item saga and a Cosmos DB outbox | Accepted (Phase 1); Service Bus Premium for Phase 2 Proposed | [ADR-010-write-reliability-saga-outbox.md](ADR-010-write-reliability-saga-outbox.md) |
| ADR-011 | Layered guardrails and verified release | Accepted | [ADR-011-guardrails-and-verified-release.md](ADR-011-guardrails-and-verified-release.md) |
| ADR-012 | PII masking before every model call | Accepted (Arabic coverage open) | [ADR-012-pii-masking.md](ADR-012-pii-masking.md) |
| ADR-013 | Audit trail as a hash chain in immutable Blob storage, with crypto-shredded content | Accepted | [ADR-013-audit-worm-hash-chain.md](ADR-013-audit-worm-hash-chain.md) |
| ADR-014 | Observability with OpenTelemetry, Application Insights and managed Prometheus, metadata only | Accepted | [ADR-014-observability-otel-prometheus.md](ADR-014-observability-otel-prometheus.md) |
| ADR-015 | Resilience through zone redundancy, degraded mode and region loss as BCP | Accepted (Phase 1); warm standby re-evaluation Proposed for Phase 2 | [ADR-015-resilience-zone-redundancy-degraded-mode.md](ADR-015-resilience-zone-redundancy-degraded-mode.md) |
| ADR-016 | Channels: CRM sidebar and Teams personal tab first, Teams bot after residency sign-off | Accepted (Phase 1); Teams bot Proposed for Phase 2, gated on C1 | [ADR-016-channels-crm-sidebar-teams-tab.md](ADR-016-channels-crm-sidebar-teams-tab.md) |
| ADR-017 | Network private by default | Accepted | [ADR-017-network-private-by-default.md](ADR-017-network-private-by-default.md) |
| ADR-018 | Evaluation, release gates and model risk management | Accepted; Extended by ADR-027 | [ADR-018-evaluation-and-model-risk.md](ADR-018-evaluation-and-model-risk.md) |
| ADR-019 | Operational state in Azure Cosmos DB for NoSQL | Accepted | [ADR-019-state-store-cosmos-db.md](ADR-019-state-store-cosmos-db.md) |
| ADR-020 | Feature flags and kill switches in Azure App Configuration | Accepted | [ADR-020-feature-flags-kill-switch.md](ADR-020-feature-flags-kill-switch.md) |
| ADR-021 | Infrastructure as code with Bicep and Azure Verified Modules, CI/CD with OIDC | Accepted (CI platform follows the Bank standard [confirm]) | [ADR-021-iac-and-cicd.md](ADR-021-iac-and-cicd.md) |
| ADR-022 | No unverified streaming: buffer, verify, release | Accepted | [ADR-022-no-unverified-streaming.md](ADR-022-no-unverified-streaming.md) |
| ADR-023 | APIM as the MCP gateway in front of tool-gateway MCP servers | Accepted (APIM-native MCP features used where confirmed) | [ADR-023-mcp-gateway-and-tool-access.md](ADR-023-mcp-gateway-and-tool-access.md) |
| ADR-024 | Three identities on every tool call: user, agent and workload | Accepted (Entra Agent ID used where available) | [ADR-024-agent-identity-three-way.md](ADR-024-agent-identity-three-way.md) |
| ADR-025 | Supervisor-led multi-agent orchestration on a fixed graph | Accepted (graph framework choice Proposed until the Phase 0 spike) | [ADR-025-multi-agent-orchestration.md](ADR-025-multi-agent-orchestration.md) |
| ADR-026 | Memory architecture: seven typed memories, partitioned by RM, no customer facts memorised | Accepted | [ADR-026-memory-architecture.md](ADR-026-memory-architecture.md) |
| ADR-027 | Evaluation architecture in four tiers: runtime gates, online sampling, scheduled evals, release gate | Accepted (RAGAS library offline only, subject to the Bank's open-source approval) | [ADR-027-evaluation-architecture.md](ADR-027-evaluation-architecture.md) |

Titles and statuses above are taken from each ADR file.

## Map to the plan's critical issues (plan §6)

| Critical issue | ADR(s) |
|---|---|
| C1 Teams bot residency | ADR-016 |
| C2 Model and feature availability in UAE North | ADR-001, ADR-004, ADR-011 |
| C3 No in-country model DR | ADR-001, ADR-015 |
| C4 Tool-gateway must know the RM | ADR-008 |
| C5 Customer data in telemetry | ADR-014, ADR-013 |
| C6 Approval token forgery and binding | ADR-009 |
| C7 CRM write retries and atomicity | ADR-010 |
| C8 APIM semantic cache | ADR-003 |
| C9 Brief prefetch without the RM | ADR-008 (Phase 2) |
| C10 Token and cost runaway | ADR-003, ADR-004, ADR-005, ADR-025, ADR-027 (tokens per request, cost per request, alerts) |
| C11 Compute platform | ADR-002 |
| C12 Model version retirement | ADR-004, ADR-018, ADR-027 (release gate thresholds) |
| C13 Content filter false positives | ADR-004, ADR-011, ADR-027 (false-positive and correct-decline gates, retrieval-gate decline rate) |
| C14 Core banking load | ADR-007, ADR-003 |
| C15 Insider misuse | ADR-008, ADR-020 |
| C16 Dates and weekends | ADR-011 |
| C17 Microsoft abuse monitoring | ADR-001, ADR-004 |
| C18 Streaming unverified text | ADR-022, ADR-011 |
| C19 Output-side exfiltration | ADR-011 |
| C20 Graph scopes | ADR-016, ADR-008 |
| C21 Audit immutability vs erasure | ADR-013, ADR-012 |
| C22 Entitlement staleness and delegation | ADR-008 |
| C23 Model risk management | ADR-018, ADR-027 |
| C24 Agent identity sprawl and over-privilege | ADR-024, ADR-023 |
| C25 MCP tool poisoning or rogue MCP servers | ADR-023 |
| C26 Memory poisoning | ADR-026, ADR-011 |
| C27 Memory leaking across RMs or customers | ADR-026, ADR-023, ADR-008 |
| C28 Multi-agent error compounding, cost and latency | ADR-025, ADR-027 (per-stage p95, budget guards) |
| C29 Checkpoints hold customer data | ADR-026, ADR-025 |

## Writing a new ADR

1. Take the next number; never reuse a number. File name `ADR-NNN-short-slug.md`.
2. Change `../00-plan.md` first if the decision changes a canonical name, number or phase, and log it in plan §11.
3. Include at least three options and a trade-off table with a short justification in every cell.
4. State a review trigger that is observable (a metric, a product availability, a regulatory change).
5. To change a decision, write a new ADR and mark the old one **Superseded** with a link; do not rewrite history.
