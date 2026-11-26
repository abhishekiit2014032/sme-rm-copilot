# 07 Architecture Review Board: final review log

Status: **ARB final review of the design pack, 26 Nov 2026** · Chair: ARB · Baseline: `00-plan.md` v2.4 (v2.3 at
the start of the review; the v2.4 changes come from this review and are logged in plan §11) · Workstream F (plan §8)

Everything is fictional and unbranded ("the Bank"). Figures are illustrative; Azure facts marked **[confirm]** are
tracked in plan §10.

---

## 1. Scope and method

**In scope.** `00-plan.md` (v2.3, authoritative), `01-hld.md`, `adr/README.md` and ADR-001 to ADR-022, `02-lld.md`,
`04-interfaces-and-slas.md`, `05-nfr-scenarios-and-dr.md`, `06-deployment-and-operations.md`. The documents were
written in parallel while the plan moved from v2 to v2.3, so drift was expected.

**Out of scope.** `code/`, `deck/`, `prototype/` and `README.md` were not edited. A read-only search of the deck and
the prototype for the drift patterns found below returned nothing of concern; prototype differences that the LLD
already lists (§14.3) are recorded as open items for workstream C.

**Method.**

1. Read the plan end to end, including the §11 iteration log, and listed every decision changed in v2.1 to v2.3.
2. Read the HLD, the LLD (all 14 sections), 04, 05 and 06 in full, and every ADR's decision, consequences and
   status. Checked each ADR for the sections plan §8 requires (context, options, trade-off table, decision,
   consequences, review trigger): all 22 have them.
3. Cross-document sweeps (pattern search across the pack) for service, container, flag, header, metric, error-code,
   event and claim names; for numbers (latency budgets, timeouts, SLOs, token budgets, retention, unit counts); and
   for phase assignments (no bot, Redis, Service Bus or ADX in Phase 1; Event Hubs Premium and PTU only at scale).
4. Recomputed the composite availability per use case under plan v2.3 rules, and the Wilson lower-bound pass
   thresholds (z = 1.96 and z = 1.645, n = 300 and 312) in a scratch script.
5. Parsed every ```` ```mermaid ```` block with `mermaid@11.4.1` under `jsdom` in a scratch folder outside the repo
   (the checker was first shown to reject two deliberately broken blocks). Result: **15 of 15 blocks parse** before
   and after the fixes.
6. Style sweep: en and em dashes (19 en dashes found, 0 left), real bank names (none), invented precise prices (none;
   05 and 06 keep formulas and "[from calculator]").
7. Fixed every finding that could be fixed in the documents, preferring alignment to the plan; where the plan was
   silent or wrong, made a minimal plan edit and logged it as **v2.4** in plan §11.

Severity scale: **Critical** (a wrong decision or number that changes whether a target is met, or a control that does
not work as specified), **High** (an ARB would block: contradiction on a control, an unowned component or interface,
a failure mode without detection, an untested control), **Medium** (contradiction that would cause build or
operations errors), **Low** (naming, references, wording, style).

---

## 2. Findings

| Id | Severity | Document and section | Finding | Resolution | Status |
|---|---|---|---|---|---|
| R-01 | Critical | `02` §2.2, §2.3, §3.2, §3.4, §10.3; `04` IF-02, IF-03, §9; `05` FM-18; `01` §8.1 | UC1 treated LOS facilities (with ODS balances) as **critical** (`critical_ok` false, `503`), contrary to plan v2.2 (only the CRM profile is critical; LOS, ODS, KYC and news degrade with a flag). The composite maths therefore made LOS hard: UC1 99.06% pilot and 99.41% scale, so the scale target was reported as missed. | LLD: `critical_ok` false only for the profile; facilities flagged as a section; diagrams and notes updated. 04: IF-03 degradable (≥ 99.5%), IF-02 note removed, §9 recomputed (§3 below). 05 FM-18 partial brief. HLD diagram note fixed. | Fixed |
| R-02 | Critical | `04` §9, `05` (none), plan §5 | Composite availability used superseded inputs: APIM single unit caveat, Event Hubs hard on every path, LOS hard. Plan §5 said the maths was in `05`, which had none. | 04 §9 rewritten with the maths shown (common path, per-use-case products, conservative variant, sensitivity); 05 §1.1 summary added; plan §5 pointer changed to `04` §9 and `05` §1.1 (v2.4). | Fixed |
| R-03 | High | `02` §1.3, §1.5, §7.1, §10.1, §14.3 | The orchestrator forwarded the RM's OBO token (audience `tool-gateway`) to the APIM AI gateway, which validated it as the quota key: a token presented to a party that is not its audience, contrary to plan v2.1 (`x-rm-oid` accepted only from the orchestrator's workload identity) and with no per-caller ceiling. | `x-rm-oid` header defined; AI gateway policy rejects it from any other `azp`, adds a per-caller `llm-token-limit`, strips the header before the backend; `x-rm-token` no longer sent to the AI gateway; hop table and sequence updated. | Fixed |
| R-04 | High | `02` §4.4 to §4.6, §5.5, §5.7, §6.1; `adr/ADR-009`, `ADR-010`, `ADR-019`; `04` IF-15 | Cosmos design diverged: the LLD used separate `jti` (`/id`), `approvals` (`/id`) and `outbox` (`/saga_id`) containers with crash-recovery sweeps, while plan §3.3 and ADR-019 specify one `approvals` container (`/jti`) holding marker, verdict, saga and outbox items in one transactional batch (ADR-009 assumed the batch). Partition keys and writers also differed (ADR-019 let the orchestrator write drafts). | LLD aligned to ADR-019: one batch per approval, marker TTL 1 day, approval and items TTL -1 until final then 30 days; `reserved` and `abandoned` states removed; change feed filtered by `type`. ADR-019 aligned to LLD naming, drafts written by `web-bff` only, `idempotency` and `audit-chain-heads` added. 04 IF-15 container list corrected. | Fixed |
| R-05 | High | `adr/ADR-013` §Decision 1; `01` §8.1; `02` §2.2 | Audit producers: ADR-013 had `web-bff`, `orchestrator` and `tool-gateway` producing; the HLD UC1 sequence had the orchestrator writing `brief_built` to Event Hubs. Plan v2.3: `tool-gateway` is the only producer. | ADR-013 and HLD corrected (orchestrator posts to the internal audit endpoint); LLD wording updated. | Fixed |
| R-06 | High | `02` §2.3, §6.4; `04` §2.3, IF-16; `05` FM-31, TB8; `adr/ADR-013` | Event Hubs failure semantics were contradictory: LLD, 04 and 05 failed closed on every path; plan v2.2 says the Cosmos `audit-spool` takes over; ADR-013 spools reads only and keeps writes fail closed on a `write_intent` event that does not exist in the LLD catalogue. No component drained the spool in the LLD, and no metric, alert or failure mode covered it. | Plan v2.4 states the rule (spool for reads, drafts and post-write status events; `approval_verified` must be acknowledged by Event Hubs before any CRM write). LLD: spool write path, `audit-writer` drains into chain `s0`, metrics `rmc_audit_spool_*`, alerts `RmcAuditSpoolBacklog`, tests; commit order changed so the ack precedes the Cosmos batch. 04, 05 (FM-31, new FM-68, TB8), ADR-013, HLD and 06 RB-16 aligned. | Fixed |
| R-07 | High | `02` §2.6, §6.5; `06` §1, §3; `04` IF-14, IF-17 | Tamper evidence: the LLD wrote **hourly** anchors signed in the copilot's own Key Vault and copied to Log Analytics, while plan §3.3 and ADR-013 require **5-minute seals** copied to a seal store in a separately administered subscription. The seal store had no owner, no subscription and no resource in 06; seal copy failure had no detection. | LLD seals every 5 min (`chain_seal`, key `audit-seal`), container `audit-seals` with create-only access; 06 adds `sub-rmc-audit-seal` (owned by IT risk) and `stsealrmcprod`; metric `rmc_audit_seal_age_seconds`, alert `RmcAuditSealStale`, FM-69; plan names IT risk as owner (v2.4). | Fixed |
| R-08 | High | `01` §7, §8.2; `02` §1.4, §2.1, §10.5, §12; `adr/ADR-008`, `ADR-009`; `05` FM-27, TB1 | Step-up: HLD, ADR-008, ADR-009 and 05 used `auth_time` ≤ 10 min only; the LLD required both `auth_time` and `acrs` on the access token (on which `auth_time` is not reliably issued). Plan v2.1: `acrs` for authentication context `approve-write`, ID token `auth_time` as fallback. The context name was never mapped to an Entra id (ids are `c1` to `c99`). | All documents now check `acrs` contains `c1` with the ID token `auth_time` fallback; plan v2.4 names `approve-write` = `c1` and adds the id format to §10 item 8; LLD tests `test_approve_requires_acrs_c1` and `test_approve_auth_time_fallback`. | Fixed |
| R-09 | High | `01` §11, §12, §15; `02` §7; `adr/ADR-003`, `ADR-015`; `04` IF-19, §9.5, §10; `05` §2.8, §3.1, §5.2, FM-37; `06` §3, §12 | APIM sized at **1 unit** in the pilot in nine places, contrary to plan v2.2 (2 units across zones from the pilot; 3 at scale [confirm]). | All aligned; ADR-003 and ADR-015 amended; pre-prod also 2 units so zone drills match prod. | Fixed |
| R-10 | High | `02` §6.1; `04` IF-15; `05` §3.1, §5.3, §5.4; `adr/ADR-019` | Cosmos backup: the LLD specified "continuous backup with point-in-time restore plus periodic backup copies", which an account cannot have (one backup mode per account); 04, 05 and ADR-019 specified continuous backup, which does not survive region loss, contrary to plan §3.1 and §3.3 (periodic with geo-redundant backup storage). 05 promised restore in ≤ 2 h although periodic restore is a support request. | All aligned to periodic backup with geo-redundant backup storage, restore by support request, restore time measured rather than promised; plan §10 item 13 added **[confirm]**. | Fixed |
| R-11 | High | `05` §8.4; `02` §2.7; `06` §5.1 | Golden set: 05 composed 300 items as UC1 80, UC2 150, UC3 70, so the groundedness and citation gates (UC2) would run on 150 items, where a Wilson bound of 0.95 needs about 98% observed, not the plan's ≥ 293/300. ADR-018 had ≥ 300 UC2 items plus 50 UC1 and 50 UC3. | Plan v2.4 states ≥ 300 scored UC2 answers plus ≥ 50 UC1 and ≥ 50 UC3; 05, LLD and 06 aligned to ADR-018. | Fixed |
| R-12 | High | `05` SEC-02, §8.4; `06` §5.1 stage 4 | Red-team suite sized at **≥ 100** attacks in the release gate, against plan §5 and ADR-018 (≥ 1,000; zero in 300 only bounds the rate below about 1%). | 05 and 06 aligned to ≥ 1,000. | Fixed |
| R-13 | High | `04` IF-01; `02` §2.3, §7 | Portfolio-change events (entitlement invalidation, C22): 04 had them on "the Bank's event backbone", the LLD had the CRM writing straight to Event Hubs; plan v2.3 routes them through APIM. No API, owner, authentication or failure behaviour was defined for this inbound interface. | LLD §7.3 defines `rmc-crm-events` (owner, caller authentication, forwarding, limits, failure, monitoring, `rmc_portfolio_event_lag_seconds`); 04 IF-01 lists the interface and owners; plan §10 item 17 **[confirm]**. | Fixed |
| R-14 | High | `04` IF-05; `02` §2.5, §7.2; `01` §6 | DMS ingestion had no consistent owner or path: 04 had `ingestion-worker` pulling the DMS through the APIM facade with its own identity, but the facade accepts only `tool-gateway` (02 §7.2, HLD §6), and the LLD said the DMS "drops" files into `policy-inbox` without saying who does it. | 04 IF-05 rewritten: a DMS export connector owned by the ECM platform team pushes file and manifest to `policy-inbox` (Blob private endpoint, scoped role); `ingestion-worker` never calls the DMS or the facade; error contract and monitoring updated. | Fixed |
| R-15 | Medium | `02` §2.1 | "Facts first" (plan §4.1 step 10, ADR-022) had no API mechanism: `POST /v1/briefs` returned one JSON body. | LLD defines an event stream of verified sections (`progress`, `facts`, `brief`), never model tokens, behind flag `facts_first`; SLO latency measured on the final `brief` event; HLD and LLD sequences show it. | Fixed |
| R-16 | Medium | `01` §5 | HLD container view had `web-bff` calling Graph; plan v2.3 gives the Graph calendar call to `tool-gateway`. The view also lacked the seal store, the spool and the CRM event path. | Diagram and responsibility table updated. | Fixed |
| R-17 | Medium | `01` §11; `05` §2.8, §3.1; `06` §3; `04` IF-16 | Event Hubs tier at scale shown as "Standard with auto-inflate; Premium if isolation is needed" or "Standard or Premium"; plan v2.2: Premium at scale (a condition of the scale SLO). | Aligned to Premium at scale. | Fixed |
| R-18 | Medium | `04` IF-16 | Claimed CMK at rest on Event Hubs Standard; plan §3.3 says Standard has no CMK **[confirm]**, which is why content is encrypted before Event Hubs. | Corrected (Microsoft-managed keys on Standard, CMK with Premium at scale); plan §10 item 14. | Fixed |
| R-19 | Medium | `02` §2.7 | Wilson method was one-sided (z = 1.645, ≥ 292/300); plan §5 says ≥ 293/300, which is the lower limit of the two-sided 95% interval (z = 1.96). The report example's citation bound (0.953) did not match either method. | LLD uses z = 1.96 and 293/300 (example 0.959 and 0.954 for 306 and 305 of 312); plan states z explicitly (v2.4). | Fixed |
| R-20 | Medium | `02` §2, §2.1, §4.4, §11.6; `04` IF-18; `05` FM-39, FM-66; `06` RB-03 | Write kill switch fails closed after **5 min** without a config refresh; plan §3.3 and ADR-020 say **15 min**. | Aligned to 15 min; the 5-min alert kept as an early warning; RB-03 restarts pods that miss a change within 60 s. | Fixed |
| R-21 | Medium | `adr/ADR-020`; `04` IF-07, IF-18; `06` RB-02, RB-03, RB-04, RB-08, RB-11 | Feature-flag names drifted (`uc1.enabled`, `writes.enabled`, `news.enabled`, `degraded.force`, `outbox.delivery.enabled`, `lang.ar.enabled`) from the LLD and prototype set (`uc1`, `writes`, `news`, `degraded_force`, `outbox_delivery`, `arabic`). | All aligned to the LLD names; `facts_first` added. | Fixed |
| R-22 | Medium | `adr/ADR-020` | A disabled use case "returns its degraded mode, not an error", while the LLD and 04 return `503 FEATURE_DISABLED`, excluded from the SLO; refresh 30 s vs 15 s; pods without config "start with every flag off" vs "not ready". | ADR-020 amended to the LLD behaviour; degraded mode forced by the separate `degraded_force` flag. | Fixed |
| R-23 | Medium | `02` §1.3, §6.1, §14.2 | Idempotency records kept 24 h (LLD said 1 day in its plan-gap table); plan §7 v2.3: 7 days. | 7 days (604,800 s). | Fixed |
| R-24 | Medium | `02` §4.6, §5.7, §6.1; plan §7 | Verdict and saga TTL ran 30 days from creation; plan v2.3: 30 days after the last item is final. Plan §7 also had two rows for approval verdicts with different retention. | TTL -1 while open, 30 days once final; plan §7 rows split (drafts vs `approvals`), spool and seal rows added (v2.4). | Fixed |
| R-25 | Medium | `04` §8.1; `02` §11.6; plan §5 | SLO counting: 04 counted `NOT_ENTITLED` and `BUDGET_EXCEEDED` as good; the LLD excluded client errors from the denominator; plan silent. 04 also used a `mode: ok` value that does not exist (`full`). | Plan v2.4: client errors and correct refusals excluded from the denominator; 04 aligned; `full` used. | Fixed |
| R-26 | Medium | `adr/ADR-009` | ADR claim set and rules drifted from the LLD and plan: `jti` as UUIDv4 (LLD: 16 random bytes, base64url), `kid` missing from the payload, canonical scope excluding meeting date and email (so the compose link could carry unapproved text), Key Vault budget 500 ms (LLD cap 400 ms), rotation overlap 24 h (LLD and 06: until day 8). | ADR-009 amended to the LLD. | Fixed |
| R-27 | Medium | `adr/ADR-013` | Content key hierarchy described as per-customer data keys wrapped by a shared KEK ("one secret per customer"), contradicting plan §3.3 and the LLD (one Key Vault key per customer wrapping per-event DEKs); erasure event `content_shredded` (LLD `erasure_executed`); lag alert 15 min (LLD 5 min). | ADR-013 amended. | Fixed |
| R-28 | Medium | `adr/ADR-015` | Deadline carried as an absolute header (LLD: relative `x-deadline-ms`); degraded-mode trigger "< 4 s before generation" conflated with the failover rule; partial sources listed as news and KYC only; replicas ≥ 2 (LLD ≥ 3 on the request path). | ADR-015 amended. | Fixed |
| R-29 | Medium | `05` §6 TB1 | STRIDE claimed the session cookie is `SameSite=strict`; the LLD uses `SameSite=None` because CRM and Teams frame the app, which is why CSRF and Origin checks are mandatory. | Corrected. | Fixed |
| R-30 | Medium | `04` §2.4; `01` §9.2 | Two different classification schemes (04 put balances, drafts and audit content in Restricted and policies in Confidential; HLD put them in Confidential and Internal). | 04 aligned to the HLD scheme, still [confirm] with the CISO office. | Fixed |
| R-31 | Medium | `04` IF-09; `06` §11.2, RB-12 | Per-RM token metrics: 04 and 06 put a pseudonymous RM id (`rm_pid`) on the APIM token metric; the LLD deliberately has no RM dimension (cardinality, privacy) and uses the restricted audit table. | 04 and 06 aligned to the LLD. | Fixed |
| R-32 | Medium | `04` IF-01, IF-02, IF-19 | Header names: the APIM-signed backend header was called `x-rm-oid` (LLD `x-rm-assertion`; `x-rm-oid` is the caller-side header); the limit-source header was `x-apim-error-source` (LLD `x-rmc-limit`). | Aligned. | Fixed |
| R-33 | Medium | `04` IF-07 | News query included the trade licence number and URLs were "shown as text"; the LLD sends the legal name only and removes URLs. Over-sharing with a third party. | Aligned to the LLD. | Fixed |
| R-34 | Medium | `04` IF-14 | Soft-delete 90 days stated for every vault, contradicting the 7-day minimum on `kv-rmc-cust-<env>` that bounds the crypto-shred deadline. | Per-vault retention stated. | Fixed |
| R-35 | Medium | `04` IF-01 | Without CRM external-reference support "the outbox keeps the guarantee but double-post risk remains", contradicting plan §10 item 11 (UC3 stays draft-only). | Aligned to the go-live gate. | Fixed |
| R-36 | Medium | plan §10 | [confirm] facts stated in plan §3 and §5 but missing from §10: Cosmos periodic backup, Event Hubs Standard CMK and Premium geo-replication, GZRS priority replication, AMPLS and data collection endpoints, Document Intelligence retention, CRM events and deferred writes. | Plan §10 items 13 to 17 (v2.4). | Fixed |
| R-37 | Medium | plan §3.3, §3.4 | The authentication context was named `approve-write`, but Entra authentication context ids are `c1` to `c99` and the LLD used `c1` with no stated mapping (an Azure fact stated without [confirm]). | Plan v2.4: `approve-write` = `c1`, id format [confirm] in §10 item 8. | Fixed |
| R-38 | Medium | `05` §4 | Failure modes without detection: spool backlog, seal copy failure, CRM event path loss had no metric or alert. | FM-68, FM-69, FM-23 detection, LLD metrics and alerts. | Fixed |
| R-39 | Medium | `02` §12 | Untested controls: spool takeover, commit Event Hubs ack, seal store write-only, `acrs` step-up. | Tests added to LLD §12 control 2 and 21; spool drill added to 04 §10 item 13. | Fixed |
| R-40 | Medium | `05` §5.3 | Static outage page for region loss (BCP step 2) has no host outside UAE North and no owner decision. | Kept as an open item (05 §9 item 4, 06 §12 item 5). | Open (copilot product team, before pilot) |
| R-41 | Medium | `code/` (prototype) vs `02` §14.3 | Prototype keeps separate `jti` and `outbox` containers, an HMAC approval service in `code/rm_copilot/`, and `x-apim-rm-sig` instead of `x-rm-assertion`. Code was out of scope for this review. | Recorded in LLD §14.3. The stack (`code/services/`) uses the ES256 Key Vault approval path, the error envelope, primary/secondary model backends, tool-gateway as the only audit producer and the UC1 critical-source rule; the remaining differences are labelled as mock simplifications in `code/README.md`. The HMAC `ApprovalService` in `code/rm_copilot/` remains only for the original in-process sample and its tests. Smoke run passed (26 Nov). | Accepted (documented mock simplifications) |
| R-42 | Medium | `04` IF-09; `05` §8.1 M-02, M-07 | The local groundedness check was described as a `gen-small` call; the LLD check is rule-based. 05 sizes the calls as capacity headroom only. | Purpose and MRM inventory corrected; volumetrics kept as labelled headroom. | Fixed |
| R-43 | Medium | `05` FM-14 | CRM slow was said to give partial briefs (`skipped_deadline`, a code that does not exist); the profile is critical, so the outcome is `504 DEPENDENCY_TIMEOUT`. | Corrected. | Fixed |
| R-44 | Medium | all ADRs; `adr/README.md` | Twenty-two ADRs are marked Accepted before ARB approval, and README rule 5 forbids rewriting accepted ADRs, yet eight needed amendment to match the plan. | Amendments recorded in each status row and in the README; rule 5 applies after approval. | Accepted risk (design authority; ARB ratifies statuses with this verdict) |
| R-45 | Low | `02` §7.1, §10.6, §6.4, §14 | Model backends named `aoai-uaen-01` / `aoai-uaen-02`; plan v2.3 and the prototype use `aoai-primary` / `aoai-secondary` (both Regional Standard). | Renamed (resource names unchanged). | Fixed |
| R-46 | Low | `01` §8.3, §15; `02` §14.2; `04` §10; `05` §9; `06` §12 | Stale open points already resolved in plan v2.1 to v2.3 (UC2 numbering, APIM units, portfolio events, quota key, retention rows). | Marked resolved with the plan version. | Fixed |
| R-47 | Low | `01` §1, §15; `02` §5.1; `04` IF-01; `adr/ADR-001`, `ADR-007`, `ADR-010`, `ADR-013`, `ADR-018` | Cross-references to plan §10 used an old numbering ("§10.8" for the CRM and Bank items, now items 11 and 12). | Corrected. | Fixed |
| R-48 | Low | `04`, `05`, `06` | Metric names outside the LLD catalogue (`dependency_latency_seconds`, `llm_429_total`, `gen_ttft_seconds`, `outbox_failed_total`, `approval_kids_loaded`, `ingestion_lag_seconds`). | Mapped to `rmc_*` names; `rmc_approval_kids_loaded` added to the catalogue. | Fixed |
| R-49 | Low | `04` IF-10, IF-11, IF-18; `05` FM-10, FM-11 | Audit event names outside the LLD catalogue (`guardrail_degraded`, `pii_degraded`, `config_changed`). | Fallbacks recorded in `guardrails[]` with `result: fallback_local`; `kill_switch_changed` and `model_pin_changed` used. | Fixed |
| R-50 | Low | `04` IF-12; `05` §5.4; `06` §5.2, RB-07; `adr/ADR-006` | AI Search "alias `policies`" and "alias swap" vs the LLD's pinned index name. | Aligned to the pin flip. | Fixed |
| R-51 | Low | `04` IF-04, IF-06, IF-09 | KYC field list differed from the tool contract; Graph `$select` requested `location`, which is then dropped; `max_tokens` per use case (300, 800) differed from the templates (350, 900). | Aligned to the LLD. | Fixed |
| R-52 | Low | `05` §1 PRIV-01; §8.5 | Lineage field `pii_mask_version` not in the audit schema; online sampling 5% (05, ADR-018) vs 2% (LLD); citation accuracy gate not stated as a lower bound. | `guardrail_version` used; LLD set to 5% capped at 200/day; lower bound stated. | Fixed |
| R-53 | Low | `05` §6 | STRIDE trust-boundary ids TB1 to TB8 reuse the HLD ids for different boundaries. | Mapping to HLD §9.1 added. | Fixed |
| R-54 | Low | `01` §11 | AKS pools shown as system plus one user pool; 05 and 06 size four pools (`system`, `signer`, `verifier`, `core`). | HLD aligned. | Fixed |
| R-55 | Low | `06` §1, §11 | Subscription names `sub-rmcilot-*` (typo). | Renamed `sub-rmc-*`. | Fixed |
| R-56 | Low | `01` §3, §12; `adr/ADR-001`, `ADR-015` | Scale SLO 99.5% stated without the plan's conditions. | Conditions stated. | Fixed |
| R-57 | Low | plan §5, §7 | SLO cell repeated the definition of "available"; retention pointer "§retention". | Cleaned (v2.4). | Fixed |
| R-58 | Low | plan, `04`, `05`, `06` | 19 en dashes used in ranges (weekdays, business hours, workstream letters, step numbers, severities). | Replaced with "to". | Fixed |
| R-59 | Low | `adr/ADR-010` | External reference format `<jti>:report`, `<jti>:task:1` vs LLD and plan `<jti>:cr`, `<jti>:t1`. | Aligned. | Fixed |
| R-60 | Low | plan §3.3; `04` §9 | The SLA for a 2-unit zone-redundant APIM Premium instance is not stated anywhere; the maths uses the 99.95% Premium floor. | Plan §3.3 states the floor; §10 item 6 [confirm]; 04 §9 sensitivity shows the 99.99% case. | Accepted risk (conservative floor; cloud platform team to confirm before ARB sign-off of scale) |
| R-61 | Medium | `adr/ADR-013`; plan §3.3 | Commits fail closed when Event Hubs is down (no CRM write before an Event Hubs-acknowledged `approval_verified`), costing up to 0.05% of commit availability on Standard. | Kept by design and made explicit in the plan (v2.4); Premium at scale reduces it to 0.01%. | Accepted risk (business owner, with audit) |

Mermaid check (method step 5): all 15 blocks parsed before and after the changes; no finding.

### 2.1 Counts

| Severity | Total | Fixed | Accepted risk | Open |
|---|---|---|---|---|
| Critical | 2 | 2 | 0 | 0 |
| High | 12 | 12 | 0 | 0 |
| Medium | 31 | 27 | 3 | 1 |
| Low | 16 | 15 | 1 | 0 |
| **Total** | **61** | **56** | **4** | **1** |

---

## 3. Composite availability after the fix

Method and full table in `04-interfaces-and-slas.md` §9 (summary in `05` §1.1). Business hours, Azure SLAs as
floors [confirm], Bank systems at required OLAs, 17,160 business minutes a month.

Common platform path (Entra 99.99% × App Gateway 99.95% × ExpressRoute 99.95% × AKS nodes 99.99% × Private Link
99.99% × Cosmos DB 99.99% × APIM 99.95%) = 0.9999⁴ × 0.9995³ = 0.99960 × 0.99850 = **0.99810**. Event Hubs is soft on
read paths (Cosmos `audit-spool`, which shares the already-hard Cosmos dependency) and hard on commits.

| Use case | Pilot: CRM 99.8%, app tier 99.7%, Event Hubs Standard 99.95% | Scale: CRM 99.9%, app tier 99.85%, Event Hubs Premium 99.99% | Before the fix (pilot / scale) |
|---|---|---|---|
| UC1 brief | 0.99810 × 0.998 × 0.997 = **99.31%** (118 min) | 0.99810 × 0.999 × 0.9985 = **99.56%** (75 min) | 99.06% / 99.41% |
| UC2 policy | 0.99810 × 0.999 (AI Search) × 0.997 = **99.41%** (101 min) | **99.56%** (75 min) | 99.36% / 99.51% |
| UC3 draft | 0.99810 × 0.998 × 0.997 = **99.31%** (118 min) | **99.56%** (75 min) | 99.26% / 99.51% |
| UC3 commit | 0.99312 × 0.9999 (Key Vault) × 0.9995 = **99.25%** (128 min) | 0.99561 × 0.9999 × 0.9999 = **99.54%** (79 min) | 99.25% / 99.50% |
| Target | 99.0% (172 min) | 99.5% (86 min) | |

- Conservative variant until the spool drill passes (Event Hubs hard everywhere): pilot UC1 and UC3 draft 99.26%,
  UC2 99.36%; scale 99.55%. Targets still met.
- Sensitivity: CRM and application tier at 99.5% in the pilot gives UC1 98.81% (miss). Event Hubs Standard at scale
  gives commit 99.50% (no margin). A confirmed 99.99% zone-redundant APIM SLA adds about 0.04 points.
- Without degraded mode and partial briefs, UC1 at scale would be about 96.3%; at floor values a UC1 brief is
  complete about 96.7% of business time, so the degraded-rate SLO (≤ 5% pilot, ≤ 3% scale) and the LOS, ODS, KYC and
  news OLAs still matter.

---

## 4. Remaining open items and [confirm] facts

### 4.1 Open items (owner, needed by)

| # | Item | Owner | Needed by |
|---|---|---|---|
| 1 | CRM capabilities: external-reference lookup or idempotency key (UC3 go-live gate), portfolio events through `rmc-crm-events`, deferred writes with `x-rm-assertion` (plan §10 items 11, 17) | CRM platform owner | End of Phase 0 |
| 2 | OLAs: CRM ≥ 99.8% pilot / ≥ 99.9% scale; LOS and ODS ≥ 99.5%; KYC, news ≥ 99.0%; DMS export connector (04 §10 items 2, 3) | System owners, ECM platform team | End of Phase 0 |
| 3 | Static outage page hosted outside UAE North (R-40) | Copilot product team | Before pilot |
| 4 | Spool failover drill in pre-prod before Event Hubs is reported as soft (04 §10 item 13) | SRE | Before pilot |
| 5 | Prototype alignment (R-41): closed; differences labelled as mock simplifications in `code/README.md`, smoke run passed | Workstream C lead | Done |
| 6 | Synthetic test customer in production systems of record for probes (04 §8.6) | Bank operations, compliance | Before pilot |
| 7 | Posture when AI Language or Content Safety is down (continue on local rules vs fail into degraded mode) | InfoSec, DPO | Before pilot |
| 8 | Region loss excluded from the availability SLO; best-effort 24 h RTO accepted | Business sponsor | ARB sign-off |
| 9 | Remaining retention proposals (Event Hubs, restricted audit table, policy chunks, golden-set evidence; 05 §7.3) | Records management | Before pilot |
| 10 | Wilson method and online sampling of masked audit content by the in-region judge (LLD §14.1 item 16) | MRM | Before pilot |
| 11 | Microsoft support plan and Sev A response; per-RM cost report visibility (06 §12) | Vendor management; HR and DPO | Before pilot |

### 4.2 [confirm] facts (plan §10, consolidated)

1. Models offered as Regional Standard and Provisioned in UAE North, retirement dates, PTU minimums (item 1).
2. Modified abuse monitoring eligibility and review location (item 2) (go-live gate, C17).
3. Bot Service residency (item 3); UAE Central access and catalogue (item 4).
4. Prompt Shields and groundedness detection, semantic ranker, AI Search zones, AI Language Arabic PII, Managed Redis
   in UAE North (item 5).
5. APIM: `llm-token-limit` behaviour across units, zone-redundant unit rules and **SLA figure** (item 6).
6. Regions of the Foundry risk-and-safety evaluators (item 7).
7. `acrs` on the access tokens `web-bff` receives, `auth_time` on ID tokens, authentication context id `c1` for
   `approve-write` (item 8).
8. CRM iframe embedding, Teams tab on mobile through Entra Private Access (item 9); Logic Apps networking, in-network
   CI runners, DNS forwarding (item 10).
9. Bank approvals, retention, erasure rules, MRM standard, CRM idempotency, SoR SLAs (items 11, 12).
10. Cosmos DB periodic backup with geo-redundant backup storage in UAE North (item 13).
11. Event Hubs Standard without CMK; Premium availability, SLA and geo-replication in UAE North (item 14).
12. GZRS replication lag and geo priority replication; immutability with protected append writes on GZRS (item 15).
13. AMPLS and data collection endpoints from a private AKS cluster; Document Intelligence and Speech retention
    (item 16).
14. CRM event publishing through APIM and acceptance of deferred writes with the APIM-signed header (item 17).
15. LLD-level facts (LLD §14.1): `idtyp` optional claim, OBO with a federated credential, Teams `frame-ancestors`,
    Key Vault api-version and ES256 `R||S` encoding, APIM policy expressions (`HMACSHA256`, several
    `llm-token-limit` policies in one scope, Event Hubs forwarding), structured outputs for the pinned models, Outlook
    compose deeplink limits, OpenTelemetry GenAI content-capture switches, Cosmos transactional batch limits.

---

## 5. ARB verdict

**Approve with conditions.** After the fixes, the pack is internally consistent with plan v2.4, every interface and
component has an owner, every critical failure mode has detection and a runbook, and the composite availability
meets the pilot target with at least 0.25 points of headroom on every use case. The conditions are:

1. **Before real customer data (pilot go-live):** modified abuse monitoring approved (C17); residency and outsourcing
   approvals (plan §10 item 12); the CRM go-live gate for UC3 writes met (external reference or idempotency key,
   plan §10 item 11), otherwise UC3 runs draft-only.
2. **Before pilot:** the spool failover drill passes in pre-prod (reads continue, commits fail closed, spool drains
   into chain `s0`, seals verify); the step-up (`acrs` = `c1`) and Conditional Access policy tests pass in the Teams
   tab and the CRM sidebar; the static outage page is hosted outside UAE North; the InfoSec and DPO posture for
   guardrail-service outages is decided.
3. **Before the scale commitment (99.5%):** three months of pilot measurements with the platform path ≥ 99.85% in
   business hours; CRM OLA ≥ 99.9% signed; Event Hubs Premium in place (with CMK and geo-replication if offered in
   UAE North); the APIM zone-redundant SLA confirmed or the floor accepted.
4. **Before the stack smoke run:** workstream C aligns the prototype with LLD §14.3 or labels each difference as a
   mock simplification in the code. **Met 26 Nov:** differences labelled in `code/README.md`; smoke run and tests passed (97 after the red-team round).
5. **Release gates as specified:** golden set of ≥ 300 scored UC2 items plus ≥ 50 UC1 and ≥ 50 UC3, Wilson lower
   bound with z = 1.96, and a red-team suite of ≥ 1,000 attacks in every release gate from the first pilot release.
6. **Governance:** every [confirm] fact in §4.2 is closed or risk-accepted by its owner before go-live; ADR statuses
   are ratified by this verdict and any later change follows ADR README rule 5 (new ADR, old one superseded).

## 6. Red-team of the working prototype (code)

Scope: the mock-Azure stack in `code/` (services over HTTP, Azure-shaped mocks), attacked over HTTP by an
independent reviewer after the design review. Method: each attack written as a failing test first
(`code/tests/test_redteam.py`), then the minimal fix, then the full suite run three times.

| Id | Severity | Attack | Before | Fix |
|---|---|---|---|---|
| CR-01 | High | Queue a commit while CRM is down, switch the `writes` kill switch off, CRM recovers | Outbox still wrote to CRM | Outbox checks `writes` every tick and holds items without spending attempts |
| CR-02 | High | Customer id with path characters (`harbourline/brief-inputs?a=`) | Data returned; audit content keyed to an alias, so crypto-shredding the customer would miss it | Plain-id validation at web-bff, orchestrator and the audit relay; internal path segments encoded |
| CR-03 | Medium | Commit straight to tool-gateway with `writes` off; App Configuration unreachable for a long time | 201; a stale `writes=on` honoured indefinitely | tool-gateway enforces `writes`; fails closed after 15 min of staleness (plan §3.3) |
| CR-04 | Medium | Item queued, then the RM loses the customer | Outbox wrote it | Outbox re-checks entitlement after read-before-retry; item fails `ENTITLEMENT_REVOKED` |
| CR-05 | Medium | UAE PII in other formats (dotted Emirates ID, `+971 (0)50`, spaced IBAN) | Reached the model | Wider always-on patterns; guardrail version 2026.11.3 |
| CR-06 | Medium | Client sends its own `x-request-id` or another RM's `traceparent` | Text landed in spans; another RM's trace viewable | web-bff mints request ids and traces at the edge; client values ignored |
| CR-07 | Medium | Another workload calls the AI gateway with `x-rm-oid` | Charged to an RM's token quota | Chat deployments and the RM quota header accepted only from the orchestrator (and tool-gateway for embeddings) |
| CR-08 | Medium | audit-writer restarts on an existing chain | Chain restarted at 0 and reported false tampering | Writer resumes from the last sequence and hash |
| CR-09 | Low | Approval token with extra claims, `exp < iat`, `jku` header, oversized | Accepted | Exact claim and header sets, `0 < exp - iat <= 300`, 2 KiB cap, constant-time hash compare |
| CR-10 | Low | Malformed drafts (list body, tasks as a string, 11+ tasks) | 500 or 60 CRM writes | Schema validation (max 10 tasks), NFC normalisation before hashing |
| CR-11 | Low | Read shredded content after new content for the same customer | 500 | 410 `CONTENT_SHREDDED` |
| CR-12 | Low | Stale authentication on Approve in the console | Console never prompted step-up (error code mismatch) | Error code aligned |

Held (tests kept): wrong audience, expired, `alg:none`, HS256-with-public-key and re-signed `oid` tokens rejected;
cross-RM access denied; 8 parallel commits of one token give exactly one 201; cross-customer token reuse and
single-character draft edits rejected; news injection screened; quoted instructions quarantined; no customer
data in spans, logs or `/metrics`; generic error envelopes; text-only rendering under a strict CSP; signer and
verifier separated in Key Vault; mid-chain audit tampering detected.

Follow-up gaps closed in the same round: approval key allow-list (`rmc:tool-gateway:approval:allowed_kids`, reason
`kid_not_allowed`, rotation with overlap tested) for emergency key revocation; `approval_rejected` audit events for
every rejected commit, with no draft content; CRM portfolio-change events through APIM (`rmc-crm-events`) to the
Event Hub `crm-portfolio-events`, evicting cached entitlements at once; Arabic-Indic and Eastern Arabic-Indic digits
normalised before PII matching (ADR-012), including UC1 facts before JSON escaping.

Result: 97 tests green on three consecutive runs; evals gate PASS; careless-model gate FAIL as intended; smoke
and load test within budget. Remaining mock limitations are listed in `code/README.md`.
