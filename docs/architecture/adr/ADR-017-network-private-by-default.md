# ADR-017: Network private by default

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, Bank network and cloud platform teams, InfoSec |
| Critical issues | C1 (no public listener), C5 (data exposure), C19 (output exfiltration), C14 (systems of record) |
| Phase | 1 |

## Context

The copilot handles customer data from the CRM, core banking (ODS), LOS, KYC and the policy DMS, all reached
over ExpressRoute (§3.6). It calls Azure OpenAI, AI Search, Cosmos DB, Key Vault and other PaaS services. Forces:

- **Exfiltration paths matter more for an AI workload.** A prompt-injected model cannot make network calls in this
  design (no browsing tool, no code execution), but any component that can reach the internet is a path for data
  to leave. Egress must be an explicit allow-list.
- **Plan boundary (§4.1).** Private from Application Gateway inwards, including Azure OpenAI and every PaaS
  service on private endpoints. Outbound only to Entra ID and Graph (Microsoft backbone), Azure Monitor ingestion
  via AMPLS, and the licensed news feed through the firewall allow-list.
- **Users are on managed devices** on the corporate network or Entra Private Access (§3.3 Edge row).
- **Bank landing zone.** The Bank is assumed to run a hub-spoke landing zone with ExpressRoute in the hub and a
  central firewall **[confirm the Bank's landing-zone pattern: hub-spoke or Virtual WAN]**.
- **Local authentication.** Keys and SAS tokens are bypasses around network and identity controls;
  `disableLocalAuth` everywhere (§3.3 Platform row).

## Options considered

1. **Hub-spoke, private by default**: spoke VNet for the copilot peered to the Bank hub; ExpressRoute to
   on-premises; internal Application Gateway WAF v2 as the only ingress; private endpoints for every PaaS service
   with public network access disabled; Azure Firewall Premium egress allow-list in the hub; AMPLS for Azure
   Monitor; private AKS cluster; `disableLocalAuth` on every service that supports it.
2. **Azure Front Door (Premium) as a public edge** with WAF and Private Link origins to the internal tier.
3. **Service endpoints** instead of private endpoints for PaaS services.
4. **Foundry managed virtual network** as the network boundary for model and agent resources.

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 Private by default | 2 Front Door public edge | 3 Service endpoints | 4 Foundry managed VNet |
|---|---|---|---|---|
| Attack surface | ++ no public listener at all | - public anycast endpoint, even if WAF-protected and origin-locked | 0 PaaS services keep public endpoints, restricted by VNet rules | 0 covers Foundry-managed compute only, not our AKS services |
| Data exfiltration control | ++ private endpoints map to specific resource instances; firewall egress allow-list | 0 ingress concern mostly; egress unchanged | - service endpoints allow traffic to any instance of the service type unless service endpoint policies are added, and policies exist only for some services | 0 managed outbound rules for Foundry compute; does not govern our cluster's egress |
| Residency | ++ traffic stays in UAE North and on ExpressRoute | - Front Door is a global service; TLS terminates at edge locations that may be outside the UAE **[confirm edge locations and data processing]** | + | + |
| On-premises reachability | ++ private endpoints resolve and route over ExpressRoute | n/a | -- service endpoints do not extend to on-premises clients | n/a |
| Off-network user access | - only via corporate network or Entra Private Access | ++ any internet location | n/a | n/a |
| Operability | - private DNS zones, endpoint sprawl, firewall rules, AMPLS: real operational work | + simpler for remote users | + simplest | + managed, but only for its scope |
| Fit with our workload | ++ AKS, APIM internal mode, PaaS all inside one boundary | 0 adds a layer in front of the same internal tier | - weaker | -- we do not run model workloads on Foundry-managed compute; it does not replace a VNet design |

## Decision

Adopt **option 1**.

1. **Topology.** Copilot spoke VNet in UAE North, peered to the Bank hub. Subnets: Application Gateway, AKS nodes
   (Azure CNI Overlay with Cilium network policy), APIM (internal VNet mode), private endpoints, and a subnet for
   in-network CI runners (ADR-021). Route tables send `0.0.0.0/0` from AKS and APIM subnets to Azure Firewall
   Premium in the hub.
2. **Ingress.** Internal Application Gateway WAF v2 (OWASP rule set, TLS 1.2+) is the only ingress, for the CRM
   sidebar and the Teams tab (ADR-016). No public IP on any copilot resource; Azure Policy denies public IPs in
   the spoke.
3. **PaaS access.** Private endpoints with public network access disabled for: Azure OpenAI and the Foundry
   resource, AI Search, AI Language, Content Safety, Document Intelligence, Cosmos DB, Key Vault, App
   Configuration, Event Hubs, Storage (blob), ACR, Managed Grafana. APIM reaches Azure OpenAI over private
   endpoints. AI Search reaches its data sources through shared private links only where indexers are used (our
   ingestion pushes, so none are expected).
4. **DNS.** Private DNS zones in the hub, linked to the spoke; on-premises resolution of private endpoint names
   through the Bank's DNS forwarding (Azure DNS Private Resolver or existing forwarders) **[confirm with the
   network team]**.
5. **Egress allow-list (Azure Firewall Premium).** Entra ID and Graph (FQDN tags or service tags), the licensed
   news feed FQDN, Azure Monitor endpoints not covered by AMPLS (if any), package and image sources only for
   build agents (not for runtime pods). TLS inspection where the Bank's policy requires it, excluding Entra
   endpoints **[confirm the Bank's TLS inspection policy]**. Everything else denied and logged.
6. **AKS.** Private cluster (private API server), local accounts off, Entra ID integration, Cilium network policies
   default-deny per namespace with explicit flows matching §3.2 (`orchestrator` cannot reach Key Vault;
   only `tool-gateway` reaches the APIM system API facade; only `web-bff` reaches the Key Vault signing key).
7. **Monitoring.** Azure Monitor Private Link Scope (private-only ingestion and query) and data collection
   endpoints for managed Prometheus (ADR-014).
8. **Local authentication off.** `disableLocalAuth` (or the service's equivalent: `allowSharedKeyAccess = false`
   on Storage, local auth off on Cosmos DB, Event Hubs, App Configuration, AI services, APIM subscription keys not
   used for authorisation) enforced by Azure Policy in deny mode.
9. **Systems of record.** Reached only through the APIM system API facade over ExpressRoute; APIM quotas per
   consumer (C14). The Bank's on-premises firewall allows only the APIM subnet.
10. **Admin access.** Azure Bastion and PIM; no jump hosts with public IPs.

## Consequences

Positive

- No public listener and no general internet egress: the remaining exfiltration paths are enumerable and
  monitored.
- The residency argument for networking is simple: every hop is in UAE North or on the Bank's ExpressRoute.
- Policies make the posture testable (deny assignments), not a convention.

Negative

- Operational weight: dozens of private endpoints and DNS records, firewall rule management, AMPLS quirks. The
  Bank's platform team must own the hub and DNS changes, which puts them on the critical path for every
  environment.
- RMs off the corporate network need Entra Private Access; without it, no access.
- Build agents must run inside the network (ADR-021), which adds infrastructure to CI/CD.
- Some Azure features need public endpoints or have private-link gaps; each must be checked per service **[confirm
  private endpoint support for every service in §3.3 in UAE North]**.

Risks and mitigations

| Risk | Mitigation |
|---|---|
| DNS misconfiguration breaks private endpoint resolution | DNS as code in the hub; connectivity tests in every environment deploy |
| A new dependency silently needs internet egress | Firewall deny logs alert; dependency added only through ADR or change record |
| Firewall becomes a throughput or latency bottleneck | Runtime egress is small (Entra, Graph, news); latency measured against the §5 budget |
| Exfiltration through rendered output (links, images) | Not a network control: handled by plain-text rendering and CSP (ADR-011 L6, C19) |

## Review trigger

- The Bank adopts Virtual WAN or a different landing-zone pattern.
- A requirement for unmanaged-device or internet access appears (re-assess a public edge with strong device and
  identity controls).
- A Foundry-managed capability (for example hosted agents) becomes part of the design (re-assess option 4 for that
  scope).

## Related

- Plan: §3.3 (Edge, Compute, Gateway, Platform rows), §3.6, §4.1 "Private network boundary", §6 C1, C5, C14, C19.
- ADR-014 (AMPLS), ADR-016 (channels on internal ingress), ADR-020 (App Configuration on private endpoint),
  ADR-021 (in-network runners). ADR-001 (residency), ADR-002 (private AKS
  cluster, network policy), ADR-003 (APIM internal VNet mode), ADR-007 (systems of record read live over
  ExpressRoute).
