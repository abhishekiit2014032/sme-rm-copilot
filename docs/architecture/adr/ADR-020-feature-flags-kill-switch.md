# ADR-020: Feature flags and kill switches in Azure App Configuration

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, operations, InfoSec (SOC), SME business owner |
| Critical issues | C3 (degraded mode), C12 (model version pins), C15 (insider misuse) |
| Phase | 1 |

## Context

Each use case must be switchable off independently, and all writes must be stoppable in one action, by
operations or by the SOC, within minutes and without a deployment (security slide: "each use case can be
switched off independently"; §3.3 Config row). Forces:

- **Speed.** A misbehaving prompt, a model regression, a suspected injection campaign or insider misuse (C15)
  needs a stop measured in seconds to minutes, not a release cycle.
- **Granularity.** Per use case (UC1, UC2, UC3 drafting), per capability (all writes, a specific write operation,
  Arabic, a specific source such as news), per cohort (pilot RMs, a single RM for investigation).
- **Configuration that is not a flag**: prompt version pins, model deployment pins, guardrail rule set version,
  `kid` allow-list for approval tokens (ADR-009). These change the system's behaviour and need the same audit
  trail.
- **Automation.** A Sentinel analytic that detects misuse should be able to flip a kill switch through a playbook
  (§3.3).
- **Audit.** Every change recorded with who, when, old and new value.
- **Failure behaviour.** If the configuration store is unreachable, services must not fail open on writes.
- **Private network** (ADR-017): the store sits on a private endpoint with local auth disabled.

## Options considered

1. **Azure App Configuration** feature flags and key-values (Standard tier, private endpoint, Entra auth only),
   read by services through the provider library with a refresh interval and a sentinel key; Sentinel playbook
   flips kill switches.
2. **Kubernetes ConfigMaps** managed by GitOps, mounted into pods.
3. **Redeploy to disable**: change a value in the Helm chart or remove the route and run the pipeline.

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 App Configuration | 2 ConfigMaps (GitOps) | 3 Redeploy |
|---|---|---|---|
| Time to stop a use case | ++ seconds to a minute (refresh interval) | 0 minutes: commit, sync, pod reload or restart | -- pipeline duration plus approvals, tens of minutes or more |
| Automation from Sentinel | ++ playbook writes a key with a managed identity | - playbook must commit to Git or call the cluster API | -- |
| Audit trail | + data-plane change history and diagnostic logs, plus Activity Log for control plane **[confirm retention of revision history per tier]** | ++ Git history | ++ pipeline history |
| Targeting (cohorts, single RM) | ++ feature filters (targeting) | - custom code | -- |
| Failure behaviour | + last known good cached in process; explicit fail-closed rule for writes | ++ local to the pod | n/a |
| Change control | 0 needs RBAC and a four-eyes rule for re-enabling | ++ pull request review | ++ |
| Operability | + one managed service, private endpoint | + no new service | ++ none |
| Lock-in | - Azure-specific SDK, though the flag model is simple | ++ | ++ |

## Decision

Adopt **option 1**, with GitOps as the source of truth for non-emergency configuration.

1. **Store.** One App Configuration store per environment, Standard tier, UAE North, private endpoint, local auth
   disabled, purge protection on, geo-replica not used (in-country single region, ADR-015).
2. **Flags.** `uc1.enabled`, `uc2.enabled`, `uc3.drafting.enabled`, `writes.enabled` (master switch for every
   write), `writes.crm.commit_followup.enabled`, `lang.ar.enabled`, `source.news.enabled`, plus targeting filters
   for pilot cohorts. Flags default to off in code: a missing flag means disabled.
3. **Pins.** Prompt versions, model deployment names and versions, guardrail rule set version, approval `kid`
   allow-list, as key-values with labels per environment. Pins are changed only by pipeline from Git after the eval
   gate (ADR-018, ADR-021), so App Configuration holds the deployed state and Git holds the reviewed state.
4. **Refresh.** Services use the App Configuration provider with a sentinel key and a 30 s refresh interval
   (illustrative, tuned for request cost) **[confirm provider behaviour and request quotas for the tier]**.
   Kill-switch flags are additionally pushed through Event Grid change notifications where supported, to cut
   propagation to seconds **[confirm]**.
5. **Failure behaviour.** Last known good values stay in memory. If the store has been unreachable for more than
   15 minutes, `writes.enabled` is treated as **off** (fail closed) while read use cases continue with the last
   known values. Pods that start without ever reaching the store start with every flag off.
6. **Who can flip what.**
   - Turning a switch **off**: on-call operations (App Configuration Data Owner via PIM, scoped to the kill-switch
     keys) and the Sentinel playbook's managed identity (scoped role on the same keys). One person, no approval,
     audited.
   - Turning a switch **on**: pull request in the configuration repository, reviewed by a second person
     (four-eyes), applied by the pipeline. Re-enabling after a security incident also needs the SOC's sign-off.
7. **Sentinel playbook.** Analytics for C15 (briefs per hour above threshold, repeated `NOT_ENTITLED`, after-hours
   bulk use) trigger a Logic App playbook that can disable a use case for one RM (targeting filter) or globally,
   and opens an incident. The Logic App needs network reach to the private endpoint (VNet-integrated plan)
   **[confirm Logic Apps networking option the Bank uses]**.
8. **Kill switch behaviour in the product.** A disabled use case returns its degraded mode (ADR-015) with a
   message, not an error. With `writes.enabled` off, approvals are refused before signing and queued outbox items
   are held (ADR-010), not dropped.
9. **Monthly test.** Operations flip each kill switch in production during business hours (with the business owner's
   agreement) and measure propagation time; result recorded.

## Consequences

Positive

- Stops are fast, scoped and automatable; the SOC can act without the delivery team.
- Configuration that changes behaviour (prompts, models, rules, keys) is versioned, audited and visible in one
  place.
- Fail-closed for writes when configuration is unknown is consistent with ADR-009 and ADR-010.

Negative

- One more runtime dependency; a long outage of App Configuration disables writes after 15 minutes.
- Two sources of truth (Git and App Configuration) must be reconciled; a drift check in the pipeline compares
  them and alerts.
- An attacker with the kill-switch role can cause denial of service by disabling use cases; acceptable (fail
  safe) and audited, but it must be in the threat model.

Risks and mitigations

| Risk | Mitigation |
|---|---|
| Flag propagation slower than expected | Monthly measured test; push notification path; worst case equals refresh interval |
| Someone re-enables via portal, bypassing four-eyes | Write role for "on" values not granted to humans; Azure Policy and alert on data-plane writes outside the pipeline identity |
| Flag sprawl | Flags have owners and expiry dates; stale flags removed each quarter |
| Misconfigured targeting disables everyone | Targeting changes go through the pipeline; only global kill switches are manual |

## Review trigger

- Measured propagation time exceeds 2 minutes in the monthly test.
- The Bank standardises on another feature-management platform.
- Phase 2 multi-region or Phase 3 multi-segment configuration needs.

## Related

- Plan: §3.3 (Config, Security ops rows), §6 C3, C12, C15.
- ADR-009 (`kid` allow-list), ADR-010 (outbox hold), ADR-015 (degraded mode), ADR-017 (private endpoint),
  ADR-018 (pins change only after the eval gate), ADR-021 (configuration as code), ADR-004 (model version pins), ADR-008 (Sentinel
  analytics on entitlement denials).
- Deck: `deck/project/slides/security.html`, `governance.html` (kill switch per use case).
