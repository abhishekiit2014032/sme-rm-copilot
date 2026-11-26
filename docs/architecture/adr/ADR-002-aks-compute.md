# ADR-002: AKS private cluster for compute

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, cloud platform team, information security |
| Plan references | §3.2, §3.3 Compute, §5, §6 C11, §8 WS-C |

## Context

The copilot consists of seven deployable services in Phase 1 (`web-bff`, `orchestrator`, `tool-gateway`,
`outbox-worker`, `ingestion-worker`, `audit-writer`, `eval-runner`) plus `bot-adapter` in Phase 2 (plan §3.2). Forces:

- **Security isolation.** `web-bff` holds the Key Vault `sign` permission for approval tokens; `tool-gateway` holds
  only the public key and is the entitlement boundary; the orchestrator holds no key (plan §3.3 Keys). The platform
  must let us express which service may call which (east-west policy) and give each service its own Entra identity.
- **Network.** Private by default (ADR-017): no public endpoints, egress forced through Azure Firewall, private
  endpoints to every PaaS service.
- **Observability.** `/metrics` scraped into Azure Monitor managed Prometheus (PodMonitor), OpenTelemetry traces to
  Application Insights (plan §3.3 Metrics, Telemetry).
- **Workload shape.** Low, steady interactive load (peak 0.5 requests/s at scale, plan §5) with I/O-bound requests
  of up to 10 s; background workers driven by Cosmos change feed and Event Hubs; a CI job for evaluations.
- **Availability.** Zone-redundant across 3 zones with an uptime SLA (plan §3.3).
- **Organisation.** The Bank standardises on AKS and has a platform team that operates it (plan C11). Container
  Apps is the documented alternative if no AKS platform team exists.
- **Prototype parity.** The mock-Azure prototype runs the same services as containers communicating over HTTP
  (plan §8 WS-C); the target should run those containers without code change.

## Options considered

1. **AKS private cluster**, Standard tier, Azure CNI Overlay with Cilium network policy, workload identity, KEDA,
   local accounts off.
2. **Azure Container Apps** (workload profiles environment, internal, VNet-integrated, zone-redundant).
3. **Azure App Service** (Premium v3, VNet integration, private endpoints, zone-redundant) for the HTTP services,
   with WebJobs or a second plan for workers.
4. **Azure Functions** (Flex Consumption or Premium with VNet integration), with Durable Functions for the saga.

## Trade-off analysis

| Criterion | 1 AKS private cluster | 2 Container Apps | 3 App Service | 4 Functions |
|---|---|---|---|---|
| Security isolation (east-west) | **Strong.** Kubernetes network policies (Cilium) express "only `orchestrator` may call `tool-gateway`"; per-service service accounts | **Moderate.** Per-app ingress restrictions and environment-level isolation, no pod-level network policy **[confirm current features]** | **Moderate.** Access restrictions per app; isolation by plan | **Moderate.** Same as App Service |
| Identity per service | Workload identity per service account | Managed identity per app | Managed identity per app | Managed identity per app |
| Network (private, forced egress) | **Strong.** Private API server, UDR to firewall, documented egress FQDNs | **Good.** Internal environment with UDR in workload profiles **[confirm]** | **Good.** VNet integration plus private endpoint; outbound via VNet | **Good** on Premium / Flex with VNet integration **[confirm Flex in UAE North]** |
| Observability fit | **Native.** PodMonitor into managed Prometheus, OTel collector | **Partial.** OTel export supported; no PodMonitor, Prometheus scrape needs a collector **[confirm]** | **Partial.** App Insights native; Prometheus needs a sidecar or push | **Partial.** App Insights native |
| Workload fit (long I/O requests, change-feed workers, CI job) | **Good.** Deployments plus KEDA scalers plus Jobs | **Good.** Apps, jobs, KEDA built in | **Moderate.** Workers awkward (WebJobs, Always On) | **Moderate.** Good for change feed triggers; request timeouts and cold starts need care; saga in Durable Functions is a different programming model |
| Zone redundancy and SLA | 3 zones; Standard tier uptime SLA | Zone-redundant environment | Zone-redundant Premium v3 | Zone-redundant on supported plans |
| Operability | **Heaviest.** Cluster upgrades, node images, add-ons; needs a platform team (exists at the Bank) | **Light.** No cluster to run | **Light** | **Light** |
| Cost (pilot scale) | Fixed: Standard tier fee plus at least 6 nodes across zones (system and user pools); qualitatively the highest floor | Lower floor with consumption or small dedicated profiles | Moderate: a zone-redundant plan has a minimum instance count | Lowest floor; per-execution billing |
| Lock-in | **Low.** Kubernetes manifests portable | Moderate: Container Apps resource model, though images are portable | Moderate | **High**: Functions programming model |
| Team skills | Bank platform team skilled; application team needs Kubernetes basics | Low barrier | Low barrier | Low barrier, different model |
| Time to pilot | **Good** if the Bank's landing zone already offers AKS; otherwise slower | **Fastest** greenfield | Fast | Fast for workers, slower for the whole system |
| Prototype parity | Same containers, HTTP between services | Same containers | Containers supported, less natural for workers | Rewrite into functions |

## Decision

Adopt **option 1: AKS private cluster** in UAE North, **Standard tier** (uptime SLA), node pools across **3
zones**, **Azure CNI Overlay with Cilium network policy**, **workload identity**, **KEDA**, **local accounts
disabled**, image signing (Notation) with admission policy, images from ACR Premium over a private endpoint. All
services run in namespace `rm-copilot` with default-deny network policies and one service account (and Entra
identity) per service. `web-bff` (signer) and `tool-gateway` (verifier) run on separate pods with no shared
identity. **Azure Container Apps is the documented alternative** if the Bank's AKS platform team cannot onboard the
workload in time (plan C11); the services are kept free of AKS-specific code so that switch is a deployment change.

## Consequences

**Positive**
- Network policy gives an enforceable, auditable call graph that matches the trust boundaries in `01-hld.md` §9.
- Native fit with managed Prometheus (PodMonitor) and OpenTelemetry; the same containers as the prototype.
- Reuses the Bank's AKS standards, controls and support model; lowest platform lock-in.

**Negative**
- Highest fixed cost and operational load for a 30-RM pilot: the platform cost does not scale down.
- Cluster upgrades (Kubernetes minor versions, node images) are recurring work with change windows.
- A private cluster requires self-hosted CI runners or `command invoke` for deployments (ADR-021).

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| Platform team capacity delays onboarding | Container Apps fallback kept viable (no AKS-specific code; Helm values and Bicep modules for both) |
| Misconfigured network policy silently allows traffic | Policy-as-code tests in CI; Cilium policy audit mode in test; Defender for Containers |
| Node image or Kubernetes upgrade breaks a service | Blue/green node pools, pre-prod soak, PodDisruptionBudgets, ≥ 3 replicas across zones |
| AKS egress requirements wider than the allow-list | Use the documented AKS outbound FQDN list for the enabled add-ons **[confirm]**; monitor firewall denies |
| Over-provisioned nodes for pilot load | Small node SKUs, cluster autoscaler; review sizing in `06` after the first month |

## Review trigger

- The Bank changes its compute standard, or no AKS platform team is available for this workload.
- Container Apps gains pod-level network policy and PodMonitor-equivalent scraping in UAE North.
- Phase 2 or 3 adds GPU or self-hosted model workloads (would strengthen AKS) or the service count shrinks to one or
  two (would weaken it).

## Related

- ADR-017 network private by default; ADR-008 identity (workload identity); ADR-014 observability; ADR-021 IaC and
  CI/CD; ADR-015 resilience.
- `01-hld.md` §5, §6, §11; `06-deployment-and-operations.md` (sizing, upgrades); plan §3.2, §3.3, C11.
