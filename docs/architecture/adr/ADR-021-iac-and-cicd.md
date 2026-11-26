# ADR-021: Infrastructure as code with Bicep and Azure Verified Modules, CI/CD with OIDC

| Field | Value |
|---|---|
| Status | Accepted (CI platform, GitHub Actions or Azure DevOps, follows the Bank's standard **[confirm]**) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, Bank cloud platform team, InfoSec, release management |
| Critical issues | C5 (no customer data outside prod), C12 (model pins and upgrades), C15 (supply chain as an insider path) |
| Phase | 1 |

## Context

The platform spans roughly twenty Azure services per environment (§3.3), all private, with strict settings
(`disableLocalAuth`, no public network access, telemetry content capture off, TTLs, immutability policies). It
must be rebuilt from code for region-loss BCP (ADR-015) and for each environment. Forces:

- **Environments**: dev, test, pre-prod, prod (§3.3 Delivery row). **Synthetic data only outside prod.**
  **Separate Azure OpenAI per environment** (quota isolation, no shared keys, no accidental prod traffic from
  test).
- **No long-lived secrets in pipelines**: workload identity federation (OIDC) from the CI platform to Entra ID.
- **Supply chain**: images signed (Notation) and verified at admission (§3.3 Platform row); ACR Premium.
- **Private resources**: AKS API server, Key Vault, Cosmos DB and others are reachable only inside the network
  (ADR-017), so deployment agents for data-plane steps must run inside it.
- **Settings must be provable**: a reviewer should be able to read the code and see, for example, APIM body
  logging at 0 bytes and the audit container's locked retention.
- **Bank standards**: the Bank's cloud platform team may already standardise on Terraform or Bicep, and on GitHub
  or Azure DevOps **[confirm]**.

## Options considered

1. **Bicep with Azure Verified Modules (AVM)**, deployment stacks, `what-if` in pull requests; GitHub Actions or
   Azure DevOps with OIDC federation; Helm for workloads.
2. **Terraform** (AzureRM and AzAPI providers, AVM for Terraform where available), same CI approach.
3. **Portal and scripts** (click-ops with runbooks), as some pilots start.

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 Bicep + AVM | 2 Terraform | 3 Portal and scripts |
|---|---|---|---|
| Day-zero support for new Azure features | ++ Bicep tracks resource provider API versions directly | + AzAPI covers gaps; AzureRM lags for new features | ++ but not repeatable |
| State management | ++ no state file; Azure is the state; deployment stacks track managed resources | - state file contains resource attributes and sometimes secrets; needs a secured, locked, in-country backend | -- no state, no drift detection |
| Multi-cloud and non-Azure resources | - Azure only (Entra app registrations need Microsoft Graph Bicep extension **[confirm maturity]** or scripts) | ++ one tool for Azure, Entra, GitHub, Grafana dashboards | -- |
| Drift detection and preview | + `what-if`; deployment stacks with deny settings | ++ `plan` is mature | -- |
| Secure-by-default modules | + AVM modules expose private endpoint, diagnostic, RBAC and CMK parameters consistently | + AVM for Terraform exists for many resources **[confirm coverage]** | -- |
| Skills and Bank standard | ? depends on the Bank | ? depends on the Bank | n/a |
| BCP rebuild (ADR-015) | ++ | ++ | -- the 24 h RTO is not credible without code |
| Auditability of settings | ++ code review shows every setting | ++ | -- |

Option 3 is rejected: it cannot support the rebuild-based BCP or provable settings. Between Bicep and Terraform
the technical difference is small; **the Bank's existing standard should win**. This ADR chooses Bicep because the
estate is Azure-only by mandate (§1), there is no state file to secure in-country, and AVM gives secure-by-default
modules. If the Bank's platform team runs Terraform with an established, secured state backend, switch.

## Decision

Adopt **option 1**.

1. **Repositories.** `infra` (Bicep, AVM, one parameter file per environment), `platform-config` (App
   Configuration values and flags, ADR-020), application repositories (services, Helm charts, prompts, rules,
   golden-set metadata). Prompts and guardrail rules are versioned code.
2. **Subscriptions and environments.** One subscription per environment (dev, test, pre-prod, prod) under the
   Bank's landing-zone management groups. Each has its own Azure OpenAI (Foundry) resource, Key Vault, Cosmos DB,
   App Configuration and Log Analytics workspace. Pre-prod mirrors prod sizing for zone and load tests.
3. **Data rule.** Only synthetic data in dev, test and pre-prod, enforced by: no network route from lower
   environments to the systems of record (they use mocks that speak the same REST shape, workstream C), separate
   Entra app registrations, and an Azure Policy that denies private endpoints from lower-environment VNets to
   production resources. Real-data evaluation runs only in prod (`eval-runner`, ADR-018).
4. **Pipelines.**
   - Pull request: lint, unit tests, Bicep build and `what-if` against the target environment, policy-as-code
     checks (no public network access, local auth off, APIM body bytes 0, telemetry content capture off),
     telemetry capture test (ADR-014), eval gate when prompts, rules, retrieval configuration or model pins change
     (ADR-018), container build, SBOM, vulnerability scan.
   - Merge: deploy to dev, then test with smoke tests and the fault-injection suite; pre-prod with load and
     zone-failure tests; prod with a manual approval by release management (CI environment protection rules).
   - Identity: OIDC federated credentials per environment, each scoped to that environment's subscription;
     the prod identity can deploy only from the protected main branch and the prod environment.
5. **Runners.** Control-plane deployments run on hosted agents; data-plane steps (Helm to the private AKS cluster,
   App Configuration writes, Cosmos container settings) run on in-network agents in a dedicated subnet (GitHub
   private networking for hosted runners, Azure DevOps Managed DevOps Pools, or self-hosted runners on AKS)
   **[confirm the option available to the Bank's CI platform in UAE North]**.
6. **Supply chain.** Images built once, pushed to ACR Premium, signed with Notation using a Key Vault signing key,
   promoted by digest (never rebuilt per environment). AKS admission enforces signature verification (Ratify with
   Azure Policy, or the AKS image integrity feature) **[confirm which is GA for AKS at build time]**; unsigned
   images are denied in test, pre-prod and prod.
7. **Deployment stacks** with deny settings on prod resource groups so that portal changes to managed resources are
   blocked; break-glass via PIM is logged.
8. **Model upgrades (C12).** A model version change is a pull request that changes a pin, runs the full eval gate in
   test (synthetic twin set) and in prod (`eval-runner` against the new deployment, real set, before any user
   traffic reaches it), then switches the App Configuration pin, first for a pilot cohort through a targeting
   filter (ADR-020). 90-day runbook in `06-deployment-and-operations.md`.
9. **Pipeline logs** must not contain customer data; since lower environments are synthetic and prod deployments
   do not handle business data, this is enforced by design and by log scrubbing in the prod eval job.

## Consequences

Positive

- Every environment and the BCP rebuild come from the same reviewed code; settings that matter for the ARB are
  visible in pull requests.
- No long-lived deployment secrets; per-environment identities limit blast radius.
- Signed, digest-pinned images close the "push a modified image" insider path.

Negative

- Entra app registrations, Conditional Access authentication contexts and Teams app manifests are only partly
  expressible in Bicep; some steps remain scripted (Microsoft Graph) with their own review.
- In-network runners are infrastructure to run and patch.
- Four separate Azure OpenAI resources multiply quota requests and model deployment work; quota lead times
  (§10 item 1) apply four times.
- Synthetic-only lower environments mean some real-data failure modes appear first in prod; mitigated by the prod
  eval run before user exposure and by targeting flags for gradual rollout (ADR-020).

Risks and mitigations

| Risk | Mitigation |
|---|---|
| CI platform stores code and logs outside the UAE | Code and logs contain no customer data by design; outsourcing file records the CI platform's data location **[confirm with the Bank's residency policy]** |
| AVM module gaps for a newer resource setting | Fall back to a native Bicep resource for that setting; contribute or track upstream |
| OIDC subject claims too broad | Subject bound to repository, branch or environment; reviewed by InfoSec |
| Drift from emergency portal changes | Deployment stack deny settings; nightly `what-if` drift report |

## Review trigger

- The Bank's platform team confirms Terraform (or a different CI platform) as the standard.
- Microsoft Graph Bicep extension (or equivalent) matures enough to bring app registrations into code.
- A change in residency policy that covers source code or pipeline metadata.

## Related

- Plan: §1 (Azure only, mock prototype), §3.3 (Platform, Delivery rows), §6 C5, C12, §8 workstream C, §10 items
  1 and 8.
- ADR-014 (telemetry test in CI), ADR-015 (rebuild for BCP), ADR-017 (in-network runners), ADR-018 (eval gate),
  ADR-019 (TTLs in code), ADR-020 (configuration as code), ADR-002 (AKS admission and image policy), ADR-004
  (separate Azure OpenAI per environment, quotas).
- Docs: `06-deployment-and-operations.md` (pipelines, runbooks).
