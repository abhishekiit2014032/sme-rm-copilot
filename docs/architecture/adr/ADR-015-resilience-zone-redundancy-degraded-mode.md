# ADR-015: Resilience through zone redundancy, degraded mode and region loss as BCP

| Field | Value |
|---|---|
| Status | Accepted (Phase 1); warm standby re-evaluation is Proposed for Phase 2 |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, business continuity, platform operations, SME business owner |
| Critical issues | C2 (regional availability), C3 (no in-country model DR), C14 (protect systems of record) |
| Phase | 1, re-evaluated at Phase 2 |

## Context

- **Residency.** Customer data, prompts, completions, indexes, logs and backups stay in the UAE (§3.1). No
  cross-border failover, including for the model.
- **Regions.** UAE North has three availability zones. UAE Central is a restricted region with a limited
  catalogue and no availability zones **[confirm access and catalogue, §10 item 4]**. Whether AKS, APIM Premium, AI
  Search, Azure OpenAI and Cosmos DB can be deployed there is unknown.
- **Targets (§5).** Pilot availability SLO 99.0% in business hours (Mon to Fri 07:00 to 20:00 GST), scale 99.5%;
  a correct answer **or** a clearly flagged degraded answer counts as available. Zone failure: audit RPO 0, RTO
  minutes. Region loss: audit RPO ≤ 15 min, app RTO best effort 24 h (BCP).
- **The copilot is an assistant, not a system of record.** RMs can do their job from existing CRM screens. The
  business impact of a copilot outage is lost productivity, not lost transactions. This is what makes a modest
  resilience posture defensible.
- **Latency budgets and deadlines (§5).** Deadline set at `web-bff` (UC1 8 s), every hop's timeout = min(own cap,
  remaining deadline minus reserve); tool cap 2 s; model failover to a second backend only if ≥ 4 s remain.
- **Dependencies with their own availability**: Azure OpenAI Regional Standard (no customer zone control), AI
  Search (zone spread needs ≥ 2 replicas), APIM Premium (pilot 1 unit), Cosmos DB, Event Hubs, Key Vault, Entra
  ID, the systems of record over ExpressRoute with Bank-defined support hours. Composite availability maths sit in
  `05-nfr-scenarios-and-dr.md`.

## Options considered

1. **Zone redundancy in UAE North, degraded mode per use case, deadline propagation and circuit breakers; region
   loss handled as a business-continuity event** (GZRS for audit and documents, Cosmos backups, IaC rebuild,
   best-effort RTO 24 h).
2. **Warm standby in UAE Central**: a scaled-down copy of the stack, data replicated, failover by DNS.
3. **Cross-border model failover**: keep everything in UAE North but fail model calls over to a deployment in
   another country (Global or Data Zone deployment, or another region).
4. **Active-active across UAE North and UAE Central.**

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 Zones + degraded + BCP | 2 Warm standby UAE Central | 3 Cross-border model failover | 4 Active-active |
|---|---|---|---|---|
| Residency | ++ in-country; GZRS secondary is UAE Central | ++ in-country | -- prompts with customer data processed abroad; contradicts §3.1 | ++ |
| Feasibility | ++ all services support zones in UAE North **[confirm per service, §10 item 5]** | - depends on a restricted region's catalogue, which may lack AKS, APIM Premium, AI Search or the chosen model **[confirm]** | + technically easy through APIM backend pools | -- same catalogue blocker, plus no zones in UAE Central |
| Protection against zone loss | ++ automatic | ++ | 0 no change | ++ |
| Protection against region loss | - best-effort 24 h, and only if a rebuild target exists | + hours | 0 model only; the rest still fails | ++ |
| Protection against model outage or throttling | + degraded mode; second in-region deployment for quota | + if the model is offered in UAE Central | ++ | + |
| Cost (qualitative) | ++ one region | - a second footprint for a pilot of 30 RMs | + | -- double footprint plus data consistency engineering |
| Operability | + one region to run; DR drills are rebuild drills | - failover runbooks, replication health, drift | + | -- multi-region writes for `jti`, outbox, sessions |
| Time to pilot | ++ | - | + | -- |

## Decision

Adopt **option 1**.

1. **Zone redundancy, every tier in UAE North.**

   | Tier | Setting |
   |---|---|
   | Application Gateway WAF v2 | Zone-redundant (zones 1, 2, 3), autoscale minimum 2 |
   | AKS | System and user node pools across 3 zones; pod topology spread and PodDisruptionBudgets per service; ≥ 2 replicas of every service (3 for `tool-gateway`) |
   | APIM Premium | Pilot 1 unit, production ≥ 2 units across zones **[confirm zone behaviour of a 1-unit Premium instance and the unit count needed, §10 item 6]**. With 1 unit APIM is not zone-redundant (ADR-003), so in the pilot it is a single-zone dependency and appears as such in the composite maths, unless the lead decides on 2 units |
   | Azure OpenAI | Regional Standard deployments; zone resilience is Microsoft-managed and not configurable. A second deployment of each model (separate resource or quota pool, same region) in the APIM backend pool absorbs throttling, not regional outage |
   | AI Search | 2 replicas in pilot (read SLA), 3 in production; zone spread where supported **[confirm]** |
   | Cosmos DB | Zone-redundant account, single region (ADR-019) |
   | Event Hubs, Key Vault, App Configuration, Storage | Zone-redundant SKUs or defaults; Storage for audit and documents GZRS |
   | Azure Firewall | Zone-redundant |

2. **Deadline propagation.** `web-bff` sets an absolute deadline and passes it in a request header with the W3C
   `traceparent`. Each hop computes its timeout as min(own cap, remaining deadline minus a reserve for its own
   post-processing). Work that cannot finish before the deadline is not started.
3. **Circuit breakers and bulkheads.** APIM backend pool with priority, circuit breaker and `retry` in front of
   the model deployments. `tool-gateway` keeps one breaker and one bounded connection pool per system of record
   (bulkheads, C14), so a slow LOS cannot exhaust capacity for CRM reads. Retries only on idempotent reads, at
   most one, and only within the deadline.
4. **Degraded mode per use case (C3).** Triggered by an open model breaker, the remaining deadline (< 4 s before
   generation), or a kill switch (ADR-020). Always flagged in the UI and counted as degraded.

   | Use case | Degraded output | Still works |
   |---|---|---|
   | UC1 brief | Data-only brief: snapshot, watch list, signals from tool output, no generated wording | Entitlements, sources, audit |
   | UC2 policy | Ranked passages with citations and no generated answer; offer routing to the credit or product desk | Search, filters, audit |
   | UC3 follow-up | Manual call report form; no extraction. Commit of an already-approved draft is independent of the model and continues (ADR-010) | Approval, saga, outbox |
   | Partial sources | A failed non-critical source (news, KYC) yields a partial brief with the section flagged (§4.1 step 5) | Everything else |

5. **Region loss is a BCP event.** RMs fall back to CRM screens. Recovery assets: audit WORM and document store on
   GZRS (audit RPO ≤ 15 min), Cosmos DB backups with geo-redundant backup storage in-country **[confirm backup
   storage redundancy options and that they stay in the UAE]**, stored chunks and embeddings for AI Search
   rebuild, Bicep for the whole stack (ADR-021), container images replicated in ACR **[confirm ACR geo-replication
   target availability in UAE Central]**. Best-effort RTO 24 h **only if a rebuild target region with the required
   services exists**; otherwise the copilot stays unavailable until UAE North recovers. The business owner signs
   this off explicitly.
6. **Drills.** Quarterly zone-failure game day in pre-prod (Chaos Studio or node pool cordon **[confirm Chaos
   Studio availability in UAE North]**); semi-annual rebuild drill of the app tier from IaC into an empty
   subscription; monthly degraded-mode test in production during business hours with the kill switch.

## Consequences

Positive

- Zone failures are absorbed automatically, which covers the most likely infrastructure failures.
- Model outages and throttling turn into flagged degraded answers instead of errors, and still count toward the
  SLO as defined in §5.
- No cross-border processing, no second footprint to pay for and keep in sync during the pilot.

Negative

- Region loss means hours to days without the copilot. This is a business decision, not a technical oversight;
  it must be recorded as accepted risk.
- The 24 h RTO is conditional on UAE Central's catalogue, which is unknown. If the catalogue is insufficient, the
  honest RTO is "when UAE North returns".
- Degraded mode is a product surface that needs design, testing and RM training; RMs may perceive it as broken.
- A pilot APIM with one unit may be a single-zone dependency; zone redundancy is not end to end until production
  unit counts are in place.

Risks and mitigations

| Risk | Mitigation |
|---|---|
| Regional Azure OpenAI capacity issues during business hours | Second in-region deployment, APIM priority pool, degraded mode; PTU evaluated post-pilot on measured volume (§2) |
| Retry storms against a recovering system of record | Breakers with half-open probes, jittered backoff, APIM quotas per consumer (C14) |
| Degraded mode silently becomes the norm | Degraded rate as an SLI with its own alert threshold; it counts as available but not as healthy |
| ExpressRoute single circuit failure | Bank network design: redundant circuits and peering locations **[confirm with the Bank's network team]** |
| Rebuild drill reveals manual steps | Every manual step becomes a Bicep or pipeline change before the next drill |

## Review trigger

- Microsoft confirms UAE Central access and a catalogue that includes AKS, APIM Premium, AI Search, Cosmos DB and
  the chosen model: re-evaluate option 2 for Phase 2 (§3.1).
- Scale SLO (99.5%) not met in two consecutive months, or region-level incidents in UAE North.
- The copilot becomes part of a regulated process (for example a step in credit origination), which would raise
  the required RTO.

## Related

- Plan: §3.1, §3.3, §4.1 step 5, §5 (SLO, RPO/RTO, deadlines), §6 C2, C3, C14, §10 items 4 to 6.
- ADR-010 (outbox continues during model outage), ADR-013 (GZRS audit), ADR-014 (breaker and deadline metrics),
  ADR-019 (Cosmos zone redundancy and backups), ADR-020 (kill switches trigger degraded mode), ADR-021 (rebuild
  from IaC), ADR-022 (deadline-aware release). ADR-001 (UAE North, no cross-border failover),
  ADR-002 (AKS zones), ADR-003 (APIM backend pool, circuit breaker, unit count), ADR-004 (Regional Standard
  deployments, PTU gate), ADR-006 (AI Search replicas and index rebuild).
- Docs: `05-nfr-scenarios-and-dr.md` (composite availability, failure-mode catalogue).
- Code: `code/rm_copilot/orchestrator.py` (decline path; production adds degraded mode).
