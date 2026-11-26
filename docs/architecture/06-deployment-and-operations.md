# 06 Deployment and operations

Status: v1 for ARB · Workstream B2 · Owner: solution architecture (resilience, security, operations) · Date: 26 Nov 2026

Contract: `00-plan.md` ("plan §n"). Interfaces and SLOs: `04-interfaces-and-slas.md` ("04 §n"). Failure modes,
capacity and MRM: `05-nfr-scenarios-and-dr.md` ("05 §n", "FM-nn"). Everything is fictional ("the Bank"). Azure facts we
are not certain of are marked **[confirm]**. No real subscription is deployed now (plan §1); this is the target design
the IaC will implement.

Contents

1. Landing zone and subscription layout
2. Environments
3. Resource inventory per environment
4. IaC modules (Bicep, Azure Verified Modules)
5. CI/CD pipeline and release gates
6. Secrets and key management
7. Azure Policy assignments
8. Operations model
9. Runbooks
10. AI incident management
11. FinOps
12. Open points

---

## 1. Landing zone and subscription layout

The copilot is an **application landing zone** in the Bank's Azure landing zone (Cloud Adoption Framework
architecture), under the **Corp** management group (private, connected to the hub, no internet ingress).

```
Tenant root group
└── mg-bank
    ├── mg-platform
    │   ├── mg-identity        sub-identity          (domain services if any, Entra Connect [Bank standard])
    │   ├── mg-management      sub-management        (central Log Analytics for platform, Sentinel, Automation)
    │   └── mg-connectivity    sub-connectivity-uaen (hub VNet: Azure Firewall Premium, ExpressRoute gateway,
    │                                                  Bastion, DNS Private Resolver, central Private DNS zones)
    ├── mg-landingzones
    │   ├── mg-corp
    │   │   ├── mg-corp-prod     sub-rmcilot-prod
    │   │   └── mg-corp-nonprod  sub-rmcilot-preprod, sub-rmcilot-test, sub-rmcilot-dev
    │   └── mg-online           (not used by the copilot)
    ├── mg-sandbox              (no connectivity, no Bank data)
    └── mg-decommissioned
```

| Layer | Owner | Contents relevant to the copilot |
|---|---|---|
| Platform: connectivity | Cloud platform (network) | Hub VNet in UAE North peered to each spoke; Azure Firewall Premium (egress allow-list incl. the news feed FQDN, plan §3.6); ExpressRoute gateway (zone-redundant SKU) to the Bank data centres; Private DNS zones for every `privatelink.*` namespace; DNS Private Resolver for on-premises resolution |
| Platform: management | Cloud platform (operations), SOC | Sentinel workspace (security logs, Entra sign-in logs, Key Vault audit, Defender alerts, restricted audit metadata copy for C15 analytics); Defender for Cloud plans |
| Platform: identity | Identity team | Entra ID tenant configuration, PIM, Conditional Access, app registrations for `web-bff` and `tool-gateway` APIs |
| Application landing zone | Copilot team (with platform guardrails) | One subscription per environment; spoke VNet; all copilot resources |

**Spoke network (illustrative address plan per environment, /22 per spoke):**

| Subnet | Size | Purpose | Notes |
|---|---|---|---|
| `snet-appgw` | /26 | Application Gateway WAF v2 (internal frontend IP only) | Dedicated subnet required |
| `snet-aks-nodes` | /24 | AKS nodes (Azure CNI Overlay: pods use a separate overlay CIDR) | UDR 0.0.0.0/0 to the hub firewall |
| `snet-apim` | /27 | APIM Premium internal VNet injection | NSG per APIM requirements [confirm for the chosen APIM tier] |
| `snet-pe` | /25 | Private endpoints for all PaaS | Network policies enabled for private endpoints |
| `snet-runners` | /27 | Self-hosted CI runners or deployment agents (only if GitOps is not used for a step) | No inbound |
| `snet-ingestion` | (shared with AKS) | n/a | |

Traffic rules: RM devices → corporate network / ExpressRoute or Entra Private Access → `snet-appgw` → AKS internal load
balancer. Egress from AKS only through the firewall (FQDN allow-list: Entra, Graph, Azure Monitor via AMPLS where
private is not possible, the news feed, MCR/ACR via private endpoint). No public IPs in the spoke (policy, §7).

---

## 2. Environments

| Aspect | dev | test | pre-prod | prod |
|---|---|---|---|---|
| Purpose | Developer integration, feature work | Automated integration and contract tests, SoR test endpoints | Prod-like: release rehearsal, eval gate, load and DR drills, security testing | Pilot (30 RMs) then scale (300) |
| Data | **Synthetic only** | **Synthetic only** | **Synthetic only** (prod-shaped volumes) | Real customer data after go-live gates (C17, residency approval) |
| Systems of record | Mocks (mock-Azure prototype `code/azmock/` style stubs) | SoR **test** environments via APIM | SoR **UAT** environments (synthetic customers) [confirm availability] | Production SoR |
| Azure OpenAI | Own Foundry resource, small quota | Own resource | Own resource, eval-sized quota (05 §2.4) | Own primary + secondary resources (priority pool) |
| Zone redundancy | No (cost) | No | **Yes** (same topology as prod, to test zone drills) | Yes |
| SKUs | Minimum (APIM Developer [no SLA], AI Search Basic, AKS Free tier) | Small | Same SKUs as prod, smaller counts | Per §3 |
| Network | Spoke, private endpoints (same pattern; no public exceptions) | Same | Same | Same |
| Access | Developers contributor via PIM (time-boxed) | Pipeline only; read for devs | Pipeline only; break-glass via PIM | Pipeline only; PIM with approval; no standing access |
| Operating hours | Auto-stop AKS and scale-down out of hours | Auto-stop out of hours | Business hours + drills | Business hours SLO; platform 24×7 |
| Monitoring | Basic | Basic | Full (alerts to the team channel, not paging) | Full, paging per §8 |
| Kill switches | Present | Present | Present, drilled monthly | Present, drilled quarterly |

**Why a separate Azure OpenAI per environment:** isolates quota (an eval run cannot throttle production), isolates
content (no prod data in non-prod resources), allows a model version to be trialled in pre-prod while prod stays
pinned (C12), and keeps diagnostic and abuse-monitoring settings per environment.

---

## 3. Resource inventory per environment (prod; pre-prod mirrors it at smaller counts)

Naming: `<type>-rmc-<env>-uaen-<nn>` (storage and similar: `st<purpose>rmc<env>uaen`). All resources tagged
`env`, `app=rm-copilot`, `owner`, `cost-centre`, `data-class` (policy-enforced).

| Resource | Name (prod) | SKU pilot | SKU scale | Zone redundancy | Private endpoint | Diagnostic settings |
|---|---|---|---|---|---|---|
| Resource groups | `rg-rmc-prod-{net,app,data,ai,ops}` | n/a | n/a | n/a | n/a | n/a |
| Spoke VNet, NSGs, UDR | `vnet-rmc-prod-uaen-01` | n/a | n/a | Regional | n/a | NSG flow logs / VNet flow logs to platform workspace [confirm] |
| Application Gateway WAF v2 + WAF policy | `agw-rmc-prod-uaen-01` | WAF_v2, autoscale 2 to 4 | WAF_v2, 2 to 6 | Zones 1,2,3 | Internal frontend only | All logs + metrics → app workspace |
| AKS (private, Standard tier) | `aks-rmc-prod-uaen-01` | Pools `system`, `signer`, `verifier`, `core`, each 3 nodes across zones (05 §2.7) | `signer` 3 to 6 × D2ds_v5; `verifier` 3 to 6 × D4ds_v5; `core` 3 to 9 × D4ds_v5 | Zones 1,2,3 | Private API server | Control-plane logs (audit-admin, guard) + Container Insights + managed Prometheus |
| Container Registry | `crrmcprod` | Premium | Premium | Zone-redundant | Yes | Logs → workspace |
| API Management | `apim-rmc-prod-uaen-01` | Premium, 1 unit (gap: 04 §10 item 5; recommend 2) | Premium, 2 to 3 units | Scale: zones (units spread) [confirm unit rules] | Internal VNet mode | Gateway logs with **body bytes 0** (C5), metrics, token metrics to App Insights |
| Foundry resource (primary) + project | `aif-rmc-prod-uaen-01` | AIServices S0; deployments `gen-large`, `gen-small`, `embed` Regional Standard | Same; PTU only after the Phase 2 gate (05 §3.3) | Microsoft-managed [confirm] | Yes | Audit + RequestResponse **metadata** logs [confirm no content in these categories] |
| Foundry resource (secondary, priority 2) | `aif-rmc-prod-uaen-02` | AIServices S0; same deployments | Same | As above | Yes | As above |
| Content Safety | `cs-rmc-prod-uaen-01` | S0 | S0 | [confirm] | Yes | Audit, metrics |
| AI Language | `lang-rmc-prod-uaen-01` | S | S | [confirm] | Yes | Audit, metrics |
| Document Intelligence | `di-rmc-prod-uaen-01` | S0 | S0 | [confirm] | Yes | Audit, metrics |
| AI Search | `srch-rmc-prod-uaen-01` | Standard S1, 2 replicas, 1 partition | S1, 3 replicas, 1 partition | Replicas across zones [confirm UAE North] | Yes; shared private link for indexer access not needed (push model) | OperationLogs (no query text [confirm category content]), metrics |
| Cosmos DB for NoSQL | `cosmos-rmc-prod-uaen-01` | Autoscale max 1,000 RU/s (shared) + 1,000 (jti/outbox) | Same, raise on evidence | Zone-redundant | Yes | ControlPlaneRequests, DataPlaneRequests **without query text** [confirm], metrics |
| Event Hubs namespace (hubs `audit-events`, `audit-content`, `crm-portfolio-events`) | `evhns-rmc-prod-uaen-01` | Standard, 2 TU, auto-inflate 4; 4 partitions on `audit-events` | Standard or **Premium** (04 §9.4) | Zone-redundant | Yes | Operational, runtime audit logs, metrics |
| Storage: audit WORM | `stauditrmcprod` | StorageV2, **GZRS**, version-level immutability, locked time-based policy 7 years | Same | ZRS primary + geo | Yes (blob) | Read/write/delete logs → workspace and Sentinel |
| Storage: audit content | `stcontentrmcprod` | GZRS, per-customer key encryption (application-level envelope) + CMK at rest | Same | Yes | Yes | As above |
| Storage: documents and chunks | `stdocsrmcprod` | GZRS | Same | Yes | Yes | Logs |
| Storage: checkpoints | `stckptrmcprod` | ZRS | ZRS | Yes | Yes | Metrics |
| Key Vault (approval signing) | `kv-rmc-sign-prod` | Premium, key `approval-signing` EC-HSM P-256, RBAC, purge protection | Same | Platform zone resilience [confirm] | Yes | AuditEvent → workspace + Sentinel |
| Key Vault (customer content keys) | `kv-rmc-cust-prod` | Premium, keys `cust-<customer_ref>` RSA-HSM 3072, `audit-transport`, `audit-anchor`; soft-delete retention 7 days (02 §6.6) | Same, or Managed HSM if key count requires [confirm] | Same | Yes | AuditEvent → workspace + Sentinel |
| Key Vault (CMKs) | `kv-rmc-cmk-prod` | Premium, RSA-HSM 3072 CMKs | Same | Same | Yes | AuditEvent |
| Key Vault (secrets, certificates) | `kv-rmc-sec-prod` | Standard or Premium | Same | Same | Yes | AuditEvent |
| App Configuration | `appcs-rmc-prod-uaen-01` | Standard | Standard (+ replica in UAE Central if available [confirm]) | Platform [confirm] | Yes | HttpRequest, Audit |
| Log Analytics workspace (app) | `log-rmc-prod-uaen-01` | Pay-as-you-go; restricted table for audit metadata (table-level RBAC) | Commitment tier if ≥ threshold [confirm] | n/a | Via AMPLS | n/a (is the sink) |
| Application Insights (workspace-based) | `appi-rmc-prod-uaen-01` | n/a | n/a | n/a | Via AMPLS; local auth off | n/a |
| Azure Monitor workspace (Prometheus) | `amw-rmc-prod-uaen-01` | n/a | n/a | n/a | Via DCE private link [confirm] | n/a |
| Managed Grafana | `amg-rmc-prod-uaen-01` | Standard | Standard | [confirm] | Private endpoint, public access off | n/a |
| AMPLS, DCE, DCR | `ampls-…`, `dce-…`, `dcr-…` | n/a | n/a | n/a | Yes | n/a |
| User-assigned managed identities | `id-rmc-{web-bff,orchestrator,tool-gateway,outbox-worker,ingestion-worker,audit-writer,eval-runner,apim}` (02 §1.5), one set per environment subscription | n/a | n/a | n/a | n/a | Sign-in logs (workload identities) |
| Action groups, alert rules | `ag-rmc-prod-*`, `alert-*` | n/a | n/a | n/a | n/a | n/a |
| Budgets | `budget-rmc-prod` | n/a | n/a | n/a | n/a | n/a |
| Speech (Phase 2) | `spch-rmc-prod-uaen-01` | n/a | S0 | [confirm] | Yes | Audit |
| Service Bus (Phase 2) | `sb-rmc-prod-uaen-01` | n/a | Premium | Zone-redundant | Yes | Logs |
| Bot Service (Phase 2, after C1 sign-off) | `bot-rmc-prod` | n/a | [confirm regional option] | n/a | [confirm] | Logs |

Pre-prod: identical resources and zone redundancy, smaller counts (AKS user pool 3 nodes, APIM 1 unit, AI Search 2
replicas, eval-sized OpenAI quota). Test and dev: no zone redundancy, minimum SKUs, same private networking.

---

## 4. IaC modules (Bicep, Azure Verified Modules)

Bicep with Azure Verified Modules from the public Bicep registry (`br/public:avm/...`), pinned to exact versions and
mirrored to the Bank's ACR [confirm Bank registry policy]. Module names below are the AVM resource-module paths we are
confident of; others are marked [confirm]. A thin set of **copilot composition modules** (`infra/modules/*.bicep`)
wraps AVM modules with Bank defaults (private endpoint, diagnostic settings, CMK, tags, `disableLocalAuth`).

| Resource | AVM module | Notes |
|---|---|---|
| Resource group | `avm/res/resources/resource-group` | |
| Virtual network and subnets | `avm/res/network/virtual-network` | Peering to hub via the module's peering parameters |
| NSG | `avm/res/network/network-security-group` | |
| Route table | `avm/res/network/route-table` | |
| Private endpoint | `avm/res/network/private-endpoint` | Most resource modules also accept `privateEndpoints` parameters directly |
| Private DNS zone (hub) | `avm/res/network/private-dns-zone` | Owned by the platform team |
| Application Gateway | `avm/res/network/application-gateway` | |
| WAF policy | `avm/res/network/application-gateway-web-application-firewall-policy` | |
| AKS | `avm/res/container-service/managed-cluster` | Private cluster, Overlay + Cilium, workload identity, local accounts off |
| Flux extension and configuration | `avm/res/kubernetes-configuration/extension`, `avm/res/kubernetes-configuration/flux-configuration` | If GitOps (§5.2) |
| Container Registry | `avm/res/container-registry/registry` | |
| API Management | `avm/res/api-management/service` | APIs and policies deployed by APIOps from Git |
| Foundry / Azure OpenAI / Content Safety / AI Language / Document Intelligence accounts | `avm/res/cognitive-services/account` | `kind` = AIServices, ContentSafety, TextAnalytics, FormRecognizer; deployments via the module's `deployments` parameter |
| Foundry project | `avm/res/cognitive-services/account` child or `avm/ptn/ai-ml/ai-foundry` | [confirm module support for Foundry projects] |
| AI Search | `avm/res/search/search-service` | |
| Cosmos DB | `avm/res/document-db/database-account` | |
| Event Hubs | `avm/res/event-hub/namespace` | |
| Storage accounts | `avm/res/storage/storage-account` | Immutability policy configuration [confirm module coverage of version-level immutability; else a small native Bicep resource] |
| Key Vault | `avm/res/key-vault/vault` | Keys with rotation policy via module `keys` parameter [confirm rotation policy support] |
| App Configuration | `avm/res/app-configuration/configuration-store` | |
| User-assigned identity + federated credentials | `avm/res/managed-identity/user-assigned-identity` | Federated credentials for AKS service accounts and for CI (OIDC) |
| Log Analytics workspace | `avm/res/operational-insights/workspace` | |
| Application Insights | `avm/res/insights/component` | |
| Azure Monitor Private Link Scope | `avm/res/insights/private-link-scope` | |
| Data collection endpoint / rule | `avm/res/insights/data-collection-endpoint`, `avm/res/insights/data-collection-rule` | |
| Action group, metric alert, log alert | `avm/res/insights/action-group`, `avm/res/insights/metric-alert`, `avm/res/insights/scheduled-query-rule` | |
| Diagnostic settings (where not in the resource module) | `avm/res/insights/diagnostic-setting` [confirm] | Most AVM resource modules expose `diagnosticSettings` |
| Azure Monitor workspace | `avm/res/monitor/account` [confirm] | |
| Managed Grafana | `avm/res/dashboard/grafana` [confirm] | |
| Budgets | `avm/res/consumption/budget` [confirm] | |
| Policy and role assignments | `avm/ptn/authorization/policy-assignment`, `avm/ptn/authorization/role-assignment` [confirm] | Platform team owns most assignments at management group scope |
| Hub firewall, Bastion | `avm/res/network/azure-firewall`, `avm/res/network/firewall-policy`, `avm/res/network/bastion-host` | Platform team |

Repository layout: `infra/main.bicep` (subscription scope) → `infra/env/{dev,test,preprod,prod}.bicepparam`;
`infra/modules/` composition; `apim/` (APIOps); `deploy/k8s/` (Helm charts or Kustomize overlays per environment);
`config/appconfig/` (flags and pins as code).

---

## 5. CI/CD pipeline and release gates

GitHub Actions or Azure DevOps (plan §3.3) with **OIDC workload identity federation**: no client secrets stored in the
CI system. Deployments into private resources run either from **self-hosted runners in `snet-runners`** or by **GitOps
pull** (Flux in the cluster), so the control plane is never exposed publicly.

### 5.1 Stages

| # | Stage | What runs | Gate (blocking) |
|---|---|---|---|
| 1 | Pull request | Lint, format, unit tests, type checks; **SAST** (CodeQL or equivalent); **secret scanning** with push protection; **dependency scan** (SCA, licence policy); IaC lint (`bicep build`, PSRule for Azure, Checkov); Helm/Kustomize lint and policy test (Gatekeeper constraints with `gator` or equivalent) | All green; coverage ≥ 80% overall and ≥ 90% for entitlement, approval verification and masking (MAINT-03); no new high/critical SAST findings; two reviewers incl. a CODEOWNER for `tool-gateway` and `web-bff` security paths |
| 2 | Build | Reproducible container build (minimal base); **SBOM** (SPDX or CycloneDX); **container scan**; **image signing with Notation** (signing key in Key Vault, pipeline identity has `sign` on that key only); push to non-prod ACR by digest | No critical CVEs; high CVEs with fix available block unless exception approved (≤ 15 days) |
| 3 | Component and contract tests | Contract tests against SoR stubs and SoR test APIs; approval-token negative tests; **telemetry canary test** (seeded canary strings must not appear in exported spans, logs, APIM logs: C5); prompt-injection regression suite; numeric check corpus | 100% pass |
| 4 | **Eval gate** (`eval-runner` on the pre-prod Foundry resource) | Golden set (≥ 300) + holdout (run by MRM pipeline on model/prompt changes) + adversarial set (≥ 100) | Groundedness ≥ 0.95 and citation accuracy ≥ 0.95 (lower 95% confidence bound); entitlement leaks 0; injection success 0; numeric mismatches 0; false-positive block rate ≤ 2%; correct decline ≥ 90%; Arabic slice parity within 5 points (Phase 2 GA); no metric regresses > 2 points vs the current production release |
| 5 | IaC what-if | `az deployment sub what-if` per environment; Azure Policy compliance pre-check | Any `Delete` or `Modify` on data resources (Cosmos, Storage, Key Vault, AI Search) requires a second approver; no policy violations |
| 6 | Deploy test | IaC apply; app deploy; integration and end-to-end tests with synthetic data | Pass |
| 7 | Deploy pre-prod | IaC apply; app deploy; **load test** (1.5 req/s, 30 min, PERF-01..04); DAST; smoke; scheduled drills (zone, kill switch, rotation) | Latency p95 within plan §5; error ratio < 0.5%; DAST no high findings |
| 8 | Change management | CAB record created automatically with eval report, what-if, test evidence, rollback plan | Standard change (pre-approved category: code fixes with unchanged model, prompt and guardrails) auto-approved by policy; **normal change** for model, prompt, guardrail, index schema or infra data-resource changes (CAB + MRM where §5.3 requires); emergency change path for P1 fixes (retro-approval within 2 business days) |
| 9 | Deploy prod (progressive) | Image promoted to prod ACR by digest (signature preserved; admission verifies); GitOps commit; **canary**: new version receives the canary cohort via App Configuration targeting (RM champions ≈ 10% of pilot RMs) and 1 pod per service, for at least 1 business day; then 50%; then 100% | Automated canary analysis vs the stable version: availability SLI, p95 latency, degraded rate (by `DEGRADED` reason), `GUARDRAIL_BLOCKED` rate, UC2 decline rate, RM thumbs-down rate; any breach → automatic rollback |
| 10 | Post-deploy smoke | Synthetic probes per use case (04 §8.6); audit completeness check for probe requests | Pass within 15 min |
| 11 | Rollback | GitOps revert to the previous image digests; App Configuration revert of prompt and model pins (they are versioned flags); APIM policy revert (APIOps); data changes are expand/contract so the previous version runs on the new schema | Rollback ≤ 15 min (RB-14) |

### 5.2 Deployment mechanics

- **GitOps** (Flux AKS extension) pulls signed manifests from the environment branch or path; admission control
  (image integrity / Ratify or the Bank's standard [confirm]) rejects unsigned images or images not from the Bank ACR.
- **Prompt and model versions are configuration**: pins live in App Configuration labels per environment, promoted by
  the pipeline with the eval report attached; a prompt change is a release like code.
- **APIOps** for APIM: APIs, products, policies (incl. `llm-token-limit`, body logging off, semantic cache off) in Git,
  extracted and published by pipeline.
- **Index changes** (schema or embedding model): blue/green index `policies-v{n+1}`, alias swap after retrieval eval
  (RB-07).

### 5.3 Release gates summary

| Change type | Eval gate | MRM | CAB | InfoSec |
|---|---|---|---|---|
| Code fix (no model, prompt, guardrail change) | Golden set smoke (50 items) + full nightly | n/a | Standard change | Only for security-path code |
| Prompt template | Full + adversarial | Informed (approval if behaviour class changes) | Normal | n/a |
| Guardrail or content-filter configuration | Full + adversarial + false-positive slice | Informed | Normal | Approve |
| Model version (RB-05) | Full + holdout + adversarial + latency | Approve | Normal | Informed |
| Embedding model or chunking | Retrieval + full | Approve | Normal | n/a |
| Infrastructure (data resources, network) | n/a | n/a | Normal | Approve network and identity changes |
| Emergency fix | Smoke + adversarial | Retro review | Emergency | As needed |

---

## 6. Secrets and key management

Principle: **identities, not secrets**. Every Azure-to-Azure call uses Entra (workload identity for pods, managed
identity for APIM and PaaS); `disableLocalAuth` on every service that supports it (§7). Secrets exist only for
non-Entra third parties and legacy backends, and live in Key Vault, consumed by APIM named values (never by pods).

| Item | Type | Store | Used by | Rotation | Overlap / procedure | Automation |
|---|---|---|---|---|---|---|
| **Approval-signing key** (C6) | EC-HSM P-256, ES256, `kid` = key version | `kv-rmc-sign-prod` | `web-bff` (`sign`), `tool-gateway` (public key, `keys/read`) | **Every 90 days** (plan §3.3): rotation policy `timeAfterCreate P90D`, `expiryTime P120D` | LLD schedule (02 §4.7): D0 new version N+1; D0 + 1 h N+1 added to `rmc:tool-gateway:approval:allowed_kids` (verifiers pre-fetch it within 30 s); D0 + 24 h `rmc:web-bff:approval:active_kid` = N+1; D0 + 8 d N removed from `allowed_kids`, its public key kept (JWKS snapshot in the audit store) for forensic verification. Persisted verdicts mean no queued item depends on the old key | Rotation policy + Event Grid `KeyNewVersionCreated` triggers the pipeline (RB-04) |
| CMKs (Cosmos, Storage, Event Hubs, AI Search [confirm CMK support per service]) | RSA 3072 HSM | `kv-rmc-cmk-prod` | Services' managed identities (`wrapKey`/`unwrapKey`) | Annual | Services configured for automatic key version update (versionless key URI) [confirm per service] | Rotation policy |
| Per-customer content keys | `cust-<customer_ref>` RSA-HSM 3072 wrapping per-event AES-256-GCM DEKs (02 §6.6); `audit-transport` for producer envelopes | `kv-rmc-cust-prod` | `audit-writer` | `cust-*`: no rotation (crypto-shredding unit); `audit-transport`: annual with overlap; `cust-none-<yyyy-mm>` monthly for content without a customer | Shred = delete all versions under `Audit.Erase` with a second approver; purge after 7-day soft-delete retention (05 §7.5) | Erasure endpoint |
| Image-signing key (Notation) | Key Vault certificate | `kv-rmc-shared-sign` [confirm Bank PKI] | CI pipeline identity | Annual | Trust store in the cluster lists current and previous certificate | Pipeline |
| TLS certificate for the internal Application Gateway listener | X.509 from the Bank's internal CA | `kv-rmc-sec-prod` | Application Gateway (managed identity) | Per CA policy (≤ 1 year) | Key Vault auto-renew at 80% lifetime if the CA is integrated [confirm]; Application Gateway picks up the latest version | Near-expiry alerts 30 / 7 days (FM-43) |
| mTLS client certificates for legacy SoR backends | X.509 | `kv-rmc-sec-prod` | APIM | Annual | New certificate registered with the backend before switch; 14-day overlap | RB-15 |
| News feed API credential | API key or OAuth client secret | `kv-rmc-sec-prod` | APIM named value | 90 days or vendor policy | Two keys if the vendor supports primary/secondary | RB-15 |
| Entra app registrations (`web-bff`, `tool-gateway`) | **Federated credentials / certificates, no client secrets** | n/a | OBO flows | Certificate annual (if used) | Two certificates overlapping | Identity team |
| CI/CD access to Azure | **OIDC federated credential** per environment and branch/environment protection rule | n/a | Pipelines | n/a (no secret) | n/a | n/a |
| Break-glass accounts | Entra emergency access accounts | Bank vault process | Platform | Per Bank policy | n/a | Monitored by Sentinel |

Access model: `web-bff` identity has the **Key Vault Crypto User restricted to `sign`** on the approval key only
(custom role with `keys/sign/action`) [confirm custom role]; `tool-gateway` has only `keys/read` (public part);
`orchestrator` has no Key Vault access; key administrators are PIM-eligible only, with approval, and cannot use keys.

---

## 7. Azure Policy assignments

Assigned at `mg-corp` (inherited by all copilot subscriptions) unless noted. Built-in policy display names change;
each is to be mapped to its definition id at implementation [confirm names]. Effects: Deny in prod and pre-prod, Audit
first in dev for one sprint, then Deny.

| # | Policy intent | Effect | Built-in / custom | Covers |
|---|---|---|---|---|
| 1 | **Allowed locations**: UAE North, UAE Central (geo-replicas and BCP) | Deny | Built-in | All resources and resource groups |
| 2 | **Deny public network access** on PaaS | Deny | Built-in per service (AI services, Cosmos DB, Storage, Key Vault, Event Hubs, AI Search, App Configuration, ACR, Managed Grafana, APIM [confirm]) | All PaaS |
| 3 | **Require private endpoints** and Private DNS zone registration | Audit + DeployIfNotExists (DNS zone groups) | Built-in (ALZ initiative) | All PaaS with private link |
| 4 | **`disableLocalAuth`** (no keys, SAS or local accounts) | Deny | Built-in per service (AI services, Cosmos DB, Event Hubs, Storage "shared key access", AI Search, App Configuration, Application Insights, AKS local accounts) | All |
| 5 | **Diagnostic settings** to the app workspace for all supported resources | DeployIfNotExists | Built-in initiative (resource logging by category group) | All |
| 6 | **Regional model deployments only** (no Global or Data Zone deployment types) | Deny | Custom on `Microsoft.CognitiveServices/accounts/deployments` `sku.name` in [`Standard`, `ProvisionedManaged`] [confirm alias and SKU names] | Foundry resources |
| 7 | **Telemetry content capture off** | Deny (cluster) + Audit (APIM) | Custom: Gatekeeper constraint requiring `OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` (or the SDK equivalent [confirm variable names]) on pods in `rm-copilot`; custom Azure Policy auditing APIM diagnostics with body bytes > 0 [confirm alias]; CI canary test (§5.1 stage 3) as the backstop | AKS, APIM |
| 8 | Key Vault: purge protection, soft delete, RBAC model, HSM-backed keys for signing | Deny | Built-in | Key Vaults |
| 9 | CMK required (Cosmos, Storage, Event Hubs, AI Search where supported) | Audit / Deny | Built-in per service | Data services |
| 10 | Storage: minimum TLS 1.2, secure transfer, no anonymous blob access | Deny | Built-in | Storage |
| 11 | AKS: private cluster, Azure Policy add-on, Defender profile, Entra integration, local accounts off | Deny / DINE | Built-in | AKS |
| 12 | Kubernetes workload baseline (restricted pod security, no privileged, no host network/path, read-only root FS, allowed registries = Bank ACR, signed images only) | Deny | Built-in Kubernetes initiatives + image integrity [confirm availability] | AKS |
| 13 | No public IPs in the spoke; NICs without public IP | Deny | Built-in | Network |
| 14 | Subnets must have NSGs; UDR to the firewall | Deny / Audit | Built-in / ALZ | Network |
| 15 | Defender for Cloud plans enabled (Containers, Key Vault, Storage, Cosmos DB, APIs, AI services [confirm plan names]) | DeployIfNotExists | Built-in | Subscriptions |
| 16 | Required tags (`env`, `app`, `owner`, `cost-centre`, `data-class`) | Deny / Modify (inherit from RG) | Built-in | All |
| 17 | Allowed SKUs in dev/test (prevent premium SKUs outside prod/pre-prod) | Deny | Built-in | Non-prod subscriptions |

Policy compliance is a pipeline gate (§5.1 stage 5) and a monthly report item.

---

## 8. Operations model

### 8.1 Hours and tiers

| Tier | Who | Hours | Scope |
|---|---|---|---|
| L1 | Bank IT service desk | Business hours, plus SOC 24×7 for security | Takes RM calls and tickets, checks the status page and known issues, triages by runbook "first response" steps, routes to L2 |
| L2 | Copilot SRE / application support (on-call rota) | **On-call 07:00–20:00 GST Mon–Fri** (paging); best effort outside | Alerts, runbooks, kill switch, degraded mode, rollback, SoR liaison |
| L3 | Copilot engineering (AI engineers, integration engineer) and cloud platform engineering | Business hours; called in by L2 for P1 | Code fixes, model and prompt issues, AKS, APIM, network |
| Vendor | Microsoft via the Bank's support plan (Unified or equivalent [confirm plan and severity response times]) | 24×7 for Severity A | Azure platform incidents (Sev A for production down; Sev B for degraded) |
| SoR resolver groups | CRM, data platform, lending, KYC, ECM, M365 teams | Per their OLA (04 §3) | Their systems |
| SOC | Security operations | 24×7 | Security incidents, Sentinel alerts (C15), FM-46, FM-55 to FM-58 |

Outside business hours: the copilot is not used by RMs (pilot), so non-security alerts queue for 07:00 GST. Platform
alerts that threaten data (audit consumer lag approaching Event Hubs retention, Key Vault access anomalies, chain
verification failure) page the platform on-call 24×7.

### 8.2 Escalation

| Severity (04 §2.5) | L2 acknowledges | Escalate to L3 | Escalate to Microsoft | Management |
|---|---|---|---|---|
| P1 | 15 min | Immediately | Immediately if Azure-caused (Sev A) | Product owner and IT service manager at 30 min; CIO delegate if > 2 h |
| P2 | 30 min | Within 1 h if not mitigated | Sev B if Azure-caused | Product owner informed |
| P3 | 4 business hours | As needed | Sev C | Weekly report |

### 8.3 SLO-based alerting

Paging alerts are based on **SLO burn rates and user-visible symptoms** (04 §8.3), not on every component metric.
Component alerts (04 IF tables) route to tickets unless they are leading indicators of a P1 (audit, key, entitlement,
security).

### 8.4 Dashboards (Managed Grafana, Azure Monitor workbooks)

| Dashboard | Audience | Panels |
|---|---|---|
| SLO overview | Product owner, SRE | Availability per use case (as experienced and copilot-attributable), error budget remaining, latency p95, degraded rate by cause |
| Request flow | SRE | Per-step latency vs the plan §5 budget (edge, entitlement, fan-out, PII, Shields, model TTFT and output, checks), deadline exceedances |
| Dependencies | SRE, SoR owners | Per interface: latency, errors, breaker state, contracted vs actual rate |
| AI gateway | SRE, FinOps | Tokens by deployment and use case, 429 ratio, TPM utilisation vs quota, failover count, content filter blocks |
| Quality | Product owner, MRM | Sampled groundedness, decline rate, false-positive blocks, UC3 edit and rejection rates, RM feedback |
| Security | SOC | Prompt Shields detections by source, `NOT_ENTITLED` by RM, approval rejects (invalid, replay), Key Vault `sign` callers, after-hours usage |
| Audit integrity | Audit, SRE | Event Hubs lag, last chain append, nightly verification result, completeness |
| Cost | FinOps, product owner | Daily cost by component, token cost per RM per use case, budget burn |

---

## 9. Runbooks

Each runbook is stored in the operations wiki with exact commands and links; below are trigger, steps and verification.
"Flag" means an App Configuration key changed through the audited flag pipeline or the Sentinel playbook.

### RB-01 Model throttling
- **Trigger:** APIM 429 ratio > 5% over 10 min; TPM utilisation > 80% at peak; FM-01, FM-03.
- **Steps:** 1) Check Azure Service Health and Foundry metrics for both backends. 2) Identify the consumer: per-use-case
  and per-RM token metrics (runaway RM or job → RB-12). 3) If the eval or ingestion product is consuming production
  quota by mistake, disable that APIM product. 4) Confirm the priority-2 backend is healthy and taking overflow. 5) If
  sustained organic growth: request a quota increase for the deployment (cloud platform team, Azure portal / support
  request) and raise the APIM limits accordingly. 6) If latency rather than 429: lower `max_tokens` where safe and
  check prompt size growth.
- **Verification:** 429 ratio < 1% for 30 min; degraded rate back to baseline; quota change recorded in the capacity log.

### RB-02 Model degraded mode
- **Trigger:** degraded rate > 5% over 30 min; breakers open on both model backends; AI Language or Content Safety
  outage (FM-02, FM-10, FM-11).
- **Steps:** 1) Confirm the cause (model, PII, Shields) from breaker metrics. 2) If not automatic, force degraded mode
  per use case with the flag `degraded.force` (use-case filter, 02 §2) (prevents slow
  timeouts for every request). 3) Post the status message ("Summaries temporarily unavailable; data and sources still
  shown"). 4) Open a Microsoft Sev B case if Azure-caused. 5) When healthy for 15 min, revert flags one use case at a
  time, UC2 first.
- **Verification:** synthetic probes return `ok`; degraded rate < 1%; flags back to `normal` in the audit log.

### RB-03 Kill switch on/off
- **Trigger:** AI incident (§10), zero-tolerance event, risk or compliance instruction, Sentinel playbook (C15).
- **Steps (on):** 1) Incident commander decides scope: one use case, all writes (`writes.enabled=false`), or all.
  2) Flip the flag (pipeline "kill switch" job or Sentinel playbook; both audited). 3) Watch config refresh age:
  all pods must report the new value within 30 s (15 s refresh, 02 §2); restart any pod whose refresh age exceeds 5 min (writes already fail closed in that pod) (FM-66). 4) Banner
  shown automatically; send the RM communication. 5) Record in the incident.
- **Steps (off):** 1) Requires root cause understood, fix released through the eval gate, and approval of the product
  owner plus MRM (and InfoSec for security incidents). 2) Re-enable for the canary cohort first, then all.
- **Verification:** probe for the disabled use case returns `503 FEATURE_DISABLED` with the flag in `details.flag`; on re-enable, probes `ok` and no
  recurrence in canary for 1 business day.

### RB-04 Approval key rotation (every 90 days, plan §3.3)
- **Trigger:** Key Vault rotation policy creates a new version (`KeyNewVersionCreated` event), or suspected key
  misuse (emergency rotation).
- **Steps (schedule from 02 §4.7):** 1) D0: confirm the new version N+1 exists in `kv-rmc-sign-prod`. 2) D0 + 1 h:
  pipeline adds N+1 to `rmc:tool-gateway:approval:allowed_kids`; verify every `tool-gateway` pod has pre-fetched the
  public key (metric `approval_kids_loaded`). 3) D0 + 24 h: pipeline sets `rmc:web-bff:approval:active_kid` = N+1.
  4) Watch `APPROVAL_INVALID` (must stay 0, in particular reason `UNKNOWN_KID`) and successful commits. 5) D0 + 8 d:
  pipeline removes N from `allowed_kids`; export N's public key to the audit store as a JWKS snapshot; never purge.
- **Emergency:** remove the compromised `kid` from `allowed_kids` (effective ≤ 30 s) and rotate immediately; RMs
  re-approve pending drafts; persisted verdicts stay valid unless the incident commander also sets
  `writes.enabled=false` and reviews queued items; open a security incident and review all commits signed with the
  compromised `kid` since the suspected time.
- **Verification:** commits succeed with the new `kid`; audit events show the new `kid`; rotation recorded; next
  rotation date set.

### RB-05 Model version upgrade (90-day runbook, C12)
- **Trigger:** retirement notice or a new GA model in UAE North; inventory job alert at 120 days before retirement.
- **Steps:**
  - Day 0 to 10: create the candidate deployment in **dev and pre-prod** with the new version; update the model
    inventory; check regional availability, quota and data terms.
  - Day 10 to 30: run the full golden set, holdout (MRM), adversarial set, false-positive slice and latency test;
    tune prompts if needed (each tuning re-runs the gate).
  - Day 30 to 45: MRM validation of results; CAB normal change.
  - Day 45 to 60: production deployment as a **new deployment name** alongside the old; App Configuration pin moves
    the canary cohort; canary analysis for ≥ 5 business days.
  - Day 60 to 75: 100% rollout; old deployment kept for rollback.
  - Day 75 to 90: delete the old deployment before retirement; close the change; update lineage records.
- **Verification:** eval report attached to the change; production quality metrics within thresholds for 10 business
  days; audit lineage shows the new version on 100% of calls.

### RB-06 Golden set update
- **Trigger:** policy published or withdrawn; incident converted into a test; quarterly review; drift alert (FM-08).
- **Steps:** 1) Curator drafts new or changed items with expected answers and citations (synthetic data only).
  2) Domain owner (policy office or RM champion) reviews; PR with two approvals. 3) Run the current production release
  against the new set version to establish the new baseline. 4) If the production release fails the updated set,
  raise a P3 quality issue (P2 if a policy withdrawal makes current answers wrong, and apply RB-07 immediately).
  5) MRM notified of each version; holdout changes only by MRM.
- **Verification:** new golden-set version tagged; baseline report stored; release gate uses the new version.

### RB-07 AI Search reindex or rebuild
- **Trigger:** index corruption or deletion, document count drop > 2%, embedding or schema change, superseded policy
  cited (FM-40, FM-09, FM-60), poisoned document (FM-59).
- **Steps (single document):** 1) For a withdrawal or poisoning: delete the document's chunks from the live index
  immediately (ingestion admin command) and mark it quarantined. 2) Fix the source in the DMS; re-ingest.
- **Steps (rebuild):** 1) Create `policies-v{n+1}` from the schema in Git. 2) Push stored chunks and embeddings from
  Blob (re-embed only for an embedding change). 3) Validate: document count equals the manifest, 50-query retrieval
  smoke and the UC2 golden slice. 4) Swap the alias `policies` to v{n+1}. 5) Keep v{n} for 7 days, then delete.
- **Verification:** retrieval metrics equal or better than before; alias points to v{n+1}; UC2 probes `ok`; time taken
  recorded against the 30 to 60 min estimate (05 §2.5).

### RB-08 Outbox poison items
- **Trigger:** item at max attempts or permanently rejected (FM-16); outbox oldest item > 30 min (FM-15).
- **Steps:** 1) List items by state (`queued`, `retrying`, `failed`) and error class. 2) For ambiguous outcomes, read
  the CRM by external reference `jti:item`: if present, mark `committed` and stop. 3) For permanent errors
  (validation, customer closed): mark `failed`, notify the RM in the UI with the reason; the RM edits and re-approves
  (new `jti`). 4) For systemic errors (CRM change broke the contract): pause deliveries (`outbox.delivery.enabled=false`, items stay `queued`, 02 §5.6), raise
  with the CRM owner, resume after the fix. 5) Never edit a draft or approval in place.
- **Verification:** no item older than 30 min in `queued`; every item `committed` or visible `failed`; reconciliation
  of the day's commits vs CRM by external reference shows no duplicates.

### RB-09 Entitlement incident (suspected leak)
- **Trigger:** report or alert that an RM saw a customer outside their portfolio; FM-23, FM-24, FM-25, FM-57.
- **Steps:** 1) Treat as P1 security incident; involve SOC, DPO, product owner. 2) Contain: kill switch for UC1 and UC3
  (RB-03) if the cause is not isolated to one RM; revoke the RM's sessions if misuse is suspected. 3) Evidence: audit
  query by RM `oid` and customer for the period: entitlement decisions, tool calls, outputs (content store access by
  the audited break-glass role). 4) Determine cause: cache staleness, delegation expiry, CRM data error, code defect.
  5) Fix (code through emergency change; data via the CRM owner). 6) DPO decides on notification. 7) Add the case to
  the golden set and entitlement tests.
- **Verification:** reproduction test now returns `NOT_ENTITLED`; Sentinel query shows no further occurrences; kill
  switch lifted per RB-03.

### RB-10 Audit chain verification failure
- **Trigger:** nightly verification fails (FM-33); `audit-writer` lag; gap in sequence numbers.
- **Steps:** 1) P1 (integrity of evidence). 2) Identify the first failing record and segment. 3) Determine whether
  it is a writer bug (missing append, wrong hash input), a storage issue, or tampering (WORM makes deletion impossible
  but check for writes outside `audit-writer`'s identity in storage logs). 4) Do not rewrite existing records; start a
  new segment anchored to the last good hash with a signed "discontinuity" record describing the gap. 5) Replay
  missing events from Event Hubs if still retained. 6) Inform internal audit and InfoSec; post-incident review.
- **Verification:** verification passes on the new segment; the discontinuity is documented and signed; completeness
  job reconciles request counts vs audit events for the affected window.

### RB-11 Prompt injection incident
- **Trigger:** confirmed injection success (zero-tolerance), red-team finding, Prompt Shields detection pattern on
  CRM notes, news or policy documents (FM-48 to FM-53, FM-59).
- **Steps:** 1) AI incident (§10), P1 if output reached an RM with harmful or false content or data crossed a
  boundary. 2) Contain: kill switch for the affected use case or source (for example `news.enabled=false`, or `uc1.enabled=false`).
  3) Identify the vector (source record id; never copy the payload into tickets: reference the audit id). 4) Neutralise
  the source: ask the CRM owner to correct the note, quarantine the document (RB-07), block the news item. 5) Fix:
  quarantine rule, Shields threshold, policy check; add the payload (sanitised) to the adversarial set. 6) Release
  through the eval gate; lift the kill switch per RB-03.
- **Verification:** adversarial set including the new case passes with 0 successes; no recurrence in 5 business days.

### RB-12 Cost spike
- **Trigger:** budget alert at 80% (actual) or 100% (forecast); cost anomaly alert; tokens per hour > 3× baseline
  (FM-47, FM-05).
- **Steps:** 1) Break down by component (Cost Management) and, for tokens, by deployment, use case and pseudonymous RM
  (APIM metrics). 2) Typical causes: runaway loop (step budget bypassed), misrouted eval or ingestion traffic, log flood
  (debug level left on), unexpected retries. 3) Contain: tighten APIM limits for the offender, disable the job, reset
  log levels, apply Log Analytics daily cap warning. 4) Fix the root cause through normal change.
- **Verification:** hourly cost and tokens back within 20% of baseline; budget forecast below 100%.

### RB-13 Region loss BCP
- **Trigger:** UAE North regional outage confirmed by Microsoft (FM-36).
- **Steps:** follow 05 §5.3: declare BCP; publish the static outage notice; RM communication (template: "The RM
  copilot is unavailable due to a cloud regional outage. Use CRM, the policy portal and manual call reports. Your saved
  drafts are retained."); decision at T+1 h on rebuild vs wait; if rebuild, deploy IaC to the pre-approved UAE
  Central landing zone, fail over storage, restore Cosmos, rebuild AI Search, run in degraded mode if no model is
  available; validate and reopen for RMs.
- **Verification:** smoke tests pass in the recovery region; audit chain segment anchored; communication sent; post
  incident review scheduled.

### RB-14 Deployment rollback (additional)
- **Trigger:** canary analysis failure, SLO fast burn after a release, bad APIM policy or WAF change (FM-37, FM-38,
  FM-41, FM-45).
- **Steps:** 1) Revert the GitOps commit (previous image digests). 2) Revert App Configuration pins to the previous
  labelled snapshot. 3) Revert APIM via APIOps or IaC to the previous tag. 4) Confirm schema compatibility (expand and
  contract rule). 5) Freeze further releases until the post-incident review.
- **Verification:** version metric shows the previous version on all pods; SLO burn returns to < 1; probes `ok`.

### RB-15 Certificate and secret expiry or compromise (additional)
- **Trigger:** near-expiry event (30 / 7 days), backend TLS failures, secret exposure (FM-43, FM-46).
- **Steps:** 1) Renew or issue the certificate or secret in Key Vault. 2) For mTLS backends, register the new
  certificate with the backend owner before switching. 3) Update APIM named values or Application Gateway listener to
  the latest version. 4) For compromise: revoke the old credential immediately, review access logs, security incident.
- **Verification:** handshake tests pass; old credential disabled; next expiry date tracked.

### RB-16 System-of-record or platform dependency outage (additional)
- **Trigger:** breaker open > 2 min on a dependency; Azure Service Health event; FM-13 to FM-22, FM-26 to FM-31,
  FM-35, FM-39, FM-42.
- **Steps:** 1) Confirm scope from the dependency dashboard. 2) Raise with the owner's resolver group (04 §3) or
  Microsoft (Sev per §8.2). 3) Post the RM status message describing what is missing (partial briefs) or unavailable.
  4) For hard dependencies (CRM, Cosmos, Event Hubs, APIM, Key Vault for approvals), consider the kill switch to replace
  slow failures with a clear message. 5) Track restoration; remove messages.
- **Verification:** breaker closed; probes `ok`; attribution recorded for the monthly report (04 §8.5).

---

## 10. AI incident management

An **AI incident** is any event where the copilot's AI behaviour causes, or could cause, harm: wrong or invented
content shown to an RM (especially figures, policy statements or commitments), data crossing an entitlement boundary,
successful prompt injection, harmful or inappropriate output, systematic bias, a control failure (guardrail, masking,
audit), or a privacy breach involving prompts or outputs.

| Severity | Examples | Kill switch |
|---|---|---|
| P1 | Entitlement leak; injection success with impact; wrong figure or policy statement reached a customer; telemetry content leak; audit integrity failure | Yes, immediately for the affected scope |
| P2 | Quality SLO breach (groundedness, decline), false-positive block surge, wrong answer reported by an RM and confirmed without customer impact | Consider (canary cohort only or source-level switch) |
| P3 | Isolated low-impact errors, near misses caught by guardrails | No |

Roles: **AI incident commander** (L2 on-call lead), **product owner** (business decisions and RM communication),
**MRM** (model risk assessment, approval to resume), **InfoSec / SOC** (security vectors), **DPO** (personal data,
notification decisions), **compliance** (regulatory notification decision per Bank policy [confirm CBUAE
requirements]), **communications** (RM-facing messages).

| Step | Target time (P1) | Actions |
|---|---|---|
| 1 Detect | n/a | Sources: zero-tolerance alerts, RM "report a problem" button, online sampling, Sentinel, red team, audit review |
| 2 Triage | 15 min | Classify severity; open the incident; assign roles; preserve evidence (audit ids, model and prompt versions from lineage) |
| 3 Contain | 30 min | Kill switch (RB-03) or source switch; revoke sessions if misuse; quarantine documents or sources |
| 4 Assess | 4 h | Scope: affected RMs, customers, outputs (audit replay); whether any output left the Bank; MRM impact assessment |
| 5 Notify | Per policy | DPO and compliance decide on customer or regulator notification; internal escalation to the AI governance board for P1 |
| 6 Remediate | Days | Fix (prompt, guardrail, code, data, document); add cases to golden and adversarial sets; release via the eval gate |
| 7 Recover | After approval | Lift the kill switch per RB-03 (canary first) with MRM approval |
| 8 Learn | 5 business days | Blameless post-incident review; actions tracked; MRM inventory and model card updated; quarterly AI governance pack |

Evidence handling: incident tickets reference audit ids; they never contain prompt or output text (C5). Access to
audit content for investigation uses the audited break-glass role with DPO approval.

---

## 11. FinOps

### 11.1 Budgets and alerts (C10)

| Scope | Budget basis | Alerts |
|---|---|---|
| `sub-rmcilot-prod` | Monthly, from the cost model (05 §3) filled with calculator prices; separate pilot and scale budgets | 50% (info), **80% actual (action: FinOps review, RB-12)**, 100% forecast (product owner), 110% actual (steering) |
| `sub-rmcilot-preprod` | Monthly; includes eval runs | 80% actual, 100% forecast |
| `sub-rmcilot-test`, `sub-rmcilot-dev` | Monthly; auto-stop schedules | 80% actual, 100% forecast |
| Azure OpenAI per environment | Resource-group or tag-filtered budget | 80% actual |
| Cost anomaly detection | Cost Management anomaly alerts per subscription [confirm feature availability] | On anomaly |
| Log Analytics | Daily cap warning (not a hard cap in prod, so security logs are not lost) | At 80% of expected daily GB |

### 11.2 Token cost per RM per use case

1. APIM `llm-emit-token-metric` emits prompt and completion tokens with dimensions `deployment`, `use_case`,
   `environment` and `rm_pid` (pseudonymous RM id: HMAC-SHA256 of the `oid` with a key in Key Vault, stable for the
   reporting year).
2. A monthly KQL job multiplies tokens by the **price table** held in configuration (updated from the agreement, never
   hard-coded) and adds a per-request share of the variable AI services (AI Language, Content Safety, semantic ranker,
   news feed).
3. Reports: cost per request and per RM per use case (median, p90), cost per brief, per policy answer, per committed
   call report; trend vs adoption. Individual RM figures are visible only to that RM's manager chain and FinOps, per
   HR and privacy guidance [confirm].
4. Unit-cost targets are set after the pilot baseline (deck metrics are productivity per meeting; FinOps pairs them
   with cost per meeting prepared).

### 11.3 Levers

| Lever | Effect | Guardrail |
|---|---|---|
| Model tiering (`gen-small` for routing and extraction) | Lowers token cost on two thirds of calls | Golden set per use case |
| Prompt size discipline (facts minimisation, fewer passages) | Input tokens dominate (≈ 97%) | Groundedness gate |
| Retry with fewer passages (05 §9 item 1) | Caps UC2 worst case | Eval |
| PTU decision with the utilisation formula (05 §3.3) | Avoids paying for idle throughput | Decision at Phase 2 gate |
| Log levels and table plans | Controls the main non-AI growth risk | Security log coverage unchanged |
| Non-prod auto-stop, smaller SKUs | Reduces environment overhead | Pre-prod stays prod-like during drills |
| News feed call reduction (daily cache, needs plan §7 change) | Fewer vendor calls | DPO approval |

---

## 12. Open points

| # | Item | Owner |
|---|---|---|
| 1 | APIM pilot unit count (1 vs 2) for zone resilience | Architecture (plan v3) |
| 2 | Microsoft support plan and Sev A response commitment | Vendor management |
| 3 | Custom Azure Policy aliases for deployment SKU and APIM body logging | Cloud platform |
| 4 | GitOps (Flux) vs push deployment from private runners: Bank standard | Cloud platform |
| 5 | Static outage page hosting independent of UAE North | Copilot team |
| 6 | Visibility rules for per-RM cost reporting | HR, DPO |
