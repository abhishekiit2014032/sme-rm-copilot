# ADR-001: Azure only, UAE North, Regional deployments, zone redundancy, no cross-border failover

| Field | Value |
|---|---|
| Status | Accepted (conditional on the residency and outsourcing approvals in plan §10 item 12) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, information security, data protection officer, technology risk |
| Plan references | §1, §3.1, §3.5, §5 (RPO/RTO), §6 C2, C3, C17, §10 items 1, 2, 4, 12 |

## Context

The Bank has directed that the copilot is built on Microsoft Azure only. The copilot processes identifiable SME
customer data (balances, facilities, KYC status, meeting notes) and sends masked extracts of it to large language
models. The forces are:

- **Residency.** Customer data, prompts, completions, indexes, logs and backups must stay in the UAE **[confirm
  residency policy and outsourcing approval]** (plan §3.1). The Bank must be able to show the regulator where every
  byte is processed, including inside Microsoft-operated AI services.
- **Regional catalogue.** Azure has two UAE regions. UAE North is the general-purpose region with availability
  zones. UAE Central is a restricted region with a limited catalogue and no availability zones **[confirm access
  and catalogue]** (plan §3.1, §10.4).
- **Model availability.** Azure OpenAI offers several deployment types. Global deployments may process a request in
  any Azure region; Data Zone deployments process within a defined geography (currently the US and the EU
  **[confirm whether any data zone covers the UAE]**); Regional (Standard or Provisioned) deployments process in
  the resource's region. Not every model is offered as Regional in UAE North, and the newest models typically
  arrive there later (plan C2).
- **Availability.** Pilot SLO 99.0% in business hours, scale 99.5% (conditional, plan §5), with a flagged degraded answer counted as
  available (plan §5). RPO/RTO for zone failure: audit RPO 0, RTO minutes; for region loss: audit RPO ≤ 15 min,
  app RTO best effort 24 h.
- **Business tolerance.** The copilot is an assistive tool. If it is unavailable, RMs fall back to existing CRM
  screens and manual call reports; no customer transaction depends on it.

## Options considered

1. **UAE North only, zone-redundant, Regional model deployments, region loss as BCP** (no cross-border failover;
   GZRS and backups to the paired region inside the UAE).
2. **UAE North primary plus UAE Central warm standby** for the application tier and models.
3. **UAE North primary plus a cross-border secondary** (for example a European region) for model inference and/or
   the full stack, used only on failover.
4. **UAE North for data, Global or Data Zone model deployments** for inference (data at rest in the UAE, inference
   wherever Microsoft routes it).

## Trade-off analysis

| Criterion | 1 UAE North, zones, BCP | 2 Plus UAE Central warm standby | 3 Plus cross-border secondary | 4 Global / Data Zone inference |
|---|---|---|---|---|
| Residency | **Strong.** All processing and storage in the UAE; simplest regulatory narrative | **Strong** if UAE Central services are available; still in-country | **Fails** unless the Bank obtains an explicit regulatory exception for failover processing abroad | **Fails** for prompts containing customer data: inference location is not guaranteed to be in the UAE |
| Availability (zone failure) | **Strong.** 3 zones for AKS, Cosmos DB, AI Search, Event Hubs, App Gateway, Firewall | Same as 1 | Same as 1 | Same as 1 for the app tier; model capacity higher |
| Availability (region loss) | **Weak.** BCP only, RTO best effort 24 h; RMs use CRM screens | **Moderate**, but only if UAE Central has AKS, APIM Premium, AI Search, Cosmos DB, Event Hubs and Regional models **[confirm]**; no zones there | **Strong** technically | **Strong** for models only |
| Model choice and capacity | **Constrained** to models offered as Regional in UAE North; quota per region | Same constraint, doubled quota requests | Wider in the secondary region | **Widest** catalogue and highest default quota |
| Latency | Good: users and systems of record in-country | Good | Failover adds cross-border round trips | Variable; routing not controlled |
| Cost | **Lowest**: one region, one set of PaaS instances | Roughly a second footprint (warm) plus replication; qualitatively 1.5 to 2 times platform cost | Second footprint plus cross-border egress and compliance overhead | Lowest model unit cost; no extra footprint |
| Operability | One region to run and test | Failover runbooks and regular DR tests across regions with different catalogues | Failover runbooks plus legal sign-off at failover time | Simple |
| Time to pilot | **Fastest** | Slower: depends on UAE Central access approval and catalogue | Slowest: legal and regulatory work | Fast, but blocked by residency review |
| Regulatory narrative | Clear: "nothing leaves the UAE; if the region is lost, the tool is off" | Clear, with more evidence to produce | Requires exception and notification | Hard to defend for customer data |

## Decision

Adopt **option 1**. The copilot runs in **UAE North** across **3 availability zones**. All model deployments are
**Regional** (Standard in the pilot; Provisioned only through the gate in ADR-004). There is **no cross-border
failover** of any component. Model unavailability triggers **degraded mode** (plan C3, ADR-015). Loss of the UAE
North region is a **business continuity event**: GZRS replication for the audit WORM store and the document store,
Cosmos DB backup copies, IaC rebuild of the application tier, best-effort RTO 24 h, RMs fall back to existing CRM
screens. Phase 2 re-evaluates a warm standby in UAE Central if the needed services are offered there.

## Consequences

**Positive**
- The residency position is simple to state and to evidence: one region, Regional deployments, private endpoints,
  GZRS secondary inside the UAE.
- One region to build, test, monitor and cost; fastest path to the pilot.
- Zone redundancy covers the most likely infrastructure failure (a zone or a datacentre) automatically.

**Negative**
- Region loss means the copilot is unavailable for up to about a day. Accepted because the copilot is assistive and
  manual fallbacks exist.
- Model choice is limited to what UAE North offers as Regional; new model versions may lag other regions by months
  (plan C2). Quality and latency targets must be met with the models available, not the newest ones.
- Regional Standard quota in one region is a hard ceiling on throughput during the pilot (ADR-004).

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| A required model is not offered as Regional in UAE North at build time | Model named by availability, not by version (plan §3.1); eval gate decides whether the available model meets the quality gates; degraded mode as fallback; discovery item §10.1 |
| Regional retirement of the pinned model with no in-region successor | Model inventory and 90-day upgrade runbook (C12); early engagement with Microsoft on regional roadmap **[confirm]** |
| GZRS secondary in UAE Central is not readable or not restorable in practice | Account failover test in pre-prod; RA-GZRS considered for the audit store **[confirm support with immutability policies]** |
| A Microsoft-operated feature (abuse monitoring review, Defender prompt evidence, evaluators) processes data outside the UAE | Modified abuse monitoring approval as a go-live gate (C17); Defender prompt evidence off until confirmed; evaluators only in-region (plan §10.2, §10.7) |
| Residency approval narrower than assumed (for example excludes Microsoft personnel access) | Customer Lockbox and access review evidence prepared in discovery **[confirm applicability]** |

## Review trigger

- UAE Central becomes generally accessible to the Bank with AKS, APIM Premium, AI Search, Cosmos DB, Event Hubs and
  Regional models (plan §10.4).
- A Data Zone (or equivalent) covering the UAE is offered and accepted by the regulator.
- The copilot becomes business-critical (for example a write path on which a customer process depends), making a
  24 h RTO unacceptable.
- A regulatory change to residency or outsourcing rules.

## Related

- ADR-004 model hosting and tiering; ADR-015 resilience, zone redundancy and degraded mode; ADR-017 network private
  by default; ADR-013 audit WORM hash chain.
- `01-hld.md` §6, §10, §12; `05-nfr-scenarios-and-dr.md` (composite availability, BCP); plan §3.1, §3.5, §10.
