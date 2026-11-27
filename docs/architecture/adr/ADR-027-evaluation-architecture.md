# ADR-027: Evaluation architecture in four tiers: runtime gates, online sampling, scheduled evals, release gate

| Field | Value |
|---|---|
| Status | Accepted (extends ADR-018; the RAGAS library itself is used only if the Bank approves it, see decision 6) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, model risk management (second line), credit and product SMEs, SRE, InfoSec, data protection officer |
| Critical issues | C10 (token and cost runaway), C12 (model retirement), C13 (false positives), C18 (no unverified output), C23 (model risk management), C28 (multi-agent error compounding) |
| Plan references | §3.2 (`eval-runner`), §3.9 (evaluation architecture), §4.3 (UC2 steps 7 to 11), §5 (latency budget, token budget, quality gates), §11 v2.7 |
| Phase | 1 (all four tiers in the pilot; online sampling behind the flag `online_eval` until MRM approves judge calibration) |

## Context

ADR-018 put a golden set, a statistically bounded release gate, a red-team suite and an MRM lifecycle in place. It
answers "is this release good enough?" well. It does not answer four other questions the owner review (plan v2.7)
asked:

1. **What stops a bad answer on this request?** A gate that runs in CI says nothing about the question an RM asks at
   10:00 on Monday against a policy re-indexed at 09:00. UC2 already has a retrieval threshold and a claim-by-claim
   groundedness check (ADR-011), but they are described as guardrails, not as measured quality gates with owned
   thresholds and counted outcomes.
2. **How fast do we notice drift in production?** ADR-018 decision 6 names sampled online judging and weekly SME
   review, but not the metrics, windows, alert thresholds or the path from a score to an action.
3. **Do offline and online numbers mean the same thing?** "Groundedness" in CI, "groundedness" in Content Safety and
   "faithfulness" in RAGAS are three different definitions. A risk committee comparing a release report with a
   production dashboard needs one vocabulary.
4. **What does each request cost in time and tokens, per stage?** Latency and token budgets exist in plan §5, but
   only end-to-end p95 and token totals per deployment are measured; tokens per request, per-stage p95 and cost per
   request are not.

Forces:

- **Latency budget (plan §5).** UC2 p95 ≈ 5.9 s against a 7 s target, with a 400 ms "checks" line; UC1 ≈ 6.5 s
  against 8 s with 100 ms for checks. Anything inline must fit those lines.
- **Residency (ADR-001, plan §3.5).** Any judge model is an in-region Azure OpenAI deployment. Foundry risk-and-safety
  evaluators and the AI Red Teaming Agent use a Microsoft-hosted back end in some regions only **[confirm, plan §10
  item 7]**.
- **Telemetry is metadata only (ADR-014, C5).** Online evaluation cannot read prompts and answers from traces. It must
  read masked content from the audit content store (ADR-013) under a defined identity.
- **Verified release (ADR-022).** Nothing unverified is shown, so runtime gates can block, retry or decline before
  the RM sees anything; there is no "retract after streaming" path to design.
- **Statistics.** At pilot volume (about 540 requests per day) a 5% sample gives about 27 judged items per day; a
  one-hour window in business hours holds about 2 to 15 items. Alerts need minimum sample guards.
- **Metric definitions.** RAGAS is a widely used open-source set of RAG metric definitions (faithfulness, answer
  relevancy, context precision, context recall, context utilisation, answer correctness). Adopting its definitions
  costs nothing; adopting its Python library is a third-party dependency the Bank's open-source policy must approve.
- **Cost (C10).** An LLM judge call costs about as much as the answer it judges.

## Options considered

1. **Offline golden set only (the ADR-018 baseline).** Release gate in CI and a nightly run; production relies on the
   existing guardrails and on RM feedback; online judging stays a loosely specified option.
2. **LLM-as-judge inline on every request.** Before release, a second `gen-large` call scores faithfulness and
   relevancy; answers below threshold are retried or declined.
3. **Foundry evaluations and continuous evaluation as a managed service.** Use the Azure AI Foundry evaluation SDK
   built-in evaluators (for example groundedness, relevance, retrieval, coherence) in CI, and Foundry continuous
   (online) evaluation on production traces **[confirm the evaluator list, whether AI-assisted evaluators run
   client-side against our in-region judge deployment, and whether continuous evaluation is available in UAE North
   and where it stores data]**.
4. **RAGAS metric definitions offline plus an in-region judge online, with deterministic runtime gates (hybrid).**
   Tier 1 runtime gates with no LLM on the hot path; tier 2 sampled online scoring by our `eval-runner` with an
   in-region judge; tier 3 scheduled nightly, weekly and monthly runs; tier 4 the ADR-018 release gate restated in
   RAGAS terms.
5. **Azure AI Content Safety groundedness detection inline as the only runtime faithfulness gate.** Call the
   groundedness detection API on every UC2 answer and rely on it instead of our local check **[confirm in-region
   availability]**.

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 Offline only | 2 Inline LLM judge | 3 Foundry managed | 4 Hybrid (chosen) | 5 Content Safety inline only |
|---|---|---|---|---|---|
| Stops a bad answer on this request | - existing guardrails only, thresholds not owned as quality gates | ++ strongest semantic check per request | - offline and after the fact | + deterministic retrieval gate before the model, lexical and embedding checks after; semantic misses caught by tier 2 minutes later | + semantic check per request, but only for groundedness |
| Drift detection time | -- days (nightly on a fixed set; production inputs never scored) | ++ every request | + continuous evaluation, if available in-region **[confirm]** | + rolling 1 h online windows plus nightly and weekly runs | - no relevancy, precision or utilisation signal |
| Latency on the request path | ++ none | -- a judge call of about 1.5 to 3 s p95 breaks UC2 (≈ 5.9 s + 2 s > 7 s) | ++ none | + ≤ 250 ms p95 on UC2, inside the existing 400 ms checks line; retrieval declines save a model call | - one more network call per answer (latency to measure) |
| Token cost | ++ CI only | -- roughly +70% on user-traffic tokens (≈ 6k to 7k judge tokens per UC1 and UC2 request) | 0 judge tokens per evaluated trace | + ≤ 1.6 M judge tokens per day at scale (≈ 5% of user traffic, plan §5 cap 200/day) | 0 per-call service charge **[confirm pricing model]** |
| Residency and C5 | ++ | + in-region judge | - continuous evaluation reads traces with content, which conflicts with metadata-only telemetry; back end location **[confirm]** | ++ judge in-region; content from the encrypted audit store, masked, under a named identity | 0 in-region only if offered **[confirm]** |
| One vocabulary offline and online | - | 0 inline judge has its own scale | + Foundry definitions everywhere, but not the RAGAS names the review asked for | ++ RAGAS definitions in every tier; runtime approximations calibrated to them | -- a single binary score |
| Evidence for MRM | + bounded release gate | 0 a model judging a model on every request, hard to validate at that volume | + managed reports; less control of judge prompts | ++ versioned judge prompts as procedural memory, calibrated against SME labels | - opaque scorer |
| Dependency on [confirm] features | ++ none | + | -- several | + none on the hot path; Foundry SDK optional in CI | -- |
| Operational effort | ++ lowest | - judge capacity on the critical path, a new failure mode per request | + managed | - an online worker, judge prompts, alert rules to own | + |

Option 2 fails on latency and cost. Option 3 is attractive operationally but depends on features and data flows we
cannot confirm for UAE North and conflicts with ADR-014 as described. Option 5 is a useful extra signal where offered,
not a replacement (it already appears in ADR-011 behind a flag). Option 1 leaves production blind between nightly runs.

## Decision

Adopt **option 4**: four evaluation tiers with RAGAS metric definitions throughout (plan §3.9).

1. **Tier 1, runtime gates (inline, every request, no LLM judge).**
   - **Pre-generation retrieval gate (UC2).** After rerank, the best passage must score ≥ the relevance threshold
     (`rerankerScore` 2.0); otherwise the orchestrator **declines without calling the model** (`WEAK_RETRIEVAL`) and offers routing
     to the credit or product desk. UC1: the CRM profile must be present, else no brief (as today).
   - **Post-generation gates.** Faithfulness: every claim (sentence) cites a returned passage and ≥ 0.9 of claims are
     supported (lexical content-word support, embedding similarity to the cited passage, no unsupported number).
     Answer relevancy ≥ 0.7 (embedding similarity of question and answer, calibrated to the RAGAS score on the golden
     set). Numeric, policy and rendering checks as in ADR-011.
   - **Retry and decline.** On a failed post-generation gate, retry once with the strict prompt only if ≥ 4 s of the
     deadline remain (plan §4.3 step 10); otherwise, or on a second failure, decline. UC1 drops failing lines and
     degrades to facts only if none remain.
   - **Budget guards.** Tokens (≤ 12k input, 1.5k output per request), tool steps (≤ 6), waves (≤ 2) and the deadline
     are checked before every model or tool call.
   - Every gate outcome (passed, retried, declined, degraded, dropped) is counted (`rmc_runtime_gate_total`), and
     thresholds are App Configuration keys owned by MRM that change only through tier 4.
2. **Tier 2, online sampled (asynchronous, minutes).** `eval-runner` in online mode judges a deterministic 5% sample of
   answered UC1 and UC2 requests (cap 200 per day) with the in-region `gen-large` judge, reading masked content from
   the audit content store. Metrics without a reference: faithfulness, answer relevancy, context precision (judged
   against the question), context utilisation; plus RM thumbs-up or down. Rolling 1 h scores go to Managed Grafana;
   an alert fires if faithfulness < 0.90 or answer relevancy < 0.80 over 1 h with at least 10 scored items, and a
   slower alert if faithfulness < 0.93 over 3 business days (ADR-018 decision 6).
3. **Tier 3, scheduled.** Nightly golden-set regression against the production configuration (all metrics including
   the reference-based context recall and answer correctness) on the pre-prod Azure OpenAI resource; weekly drift
   report (online score distributions, decline and no-result rates, retrieval-gate outcomes, token and latency
   trends); monthly red team (≥ 1,000 attacks). Reports go to the product owner and MRM; a regression opens an
   incident and, for a hard gate (leak, injection, numeric), the incident commander trips the kill switch.
4. **Tier 4, release gate.** Every prompt, model, tool, graph, guardrail, index or threshold change runs the full
   golden set (≥ 300 UC2, ≥ 50 UC1, ≥ 50 UC3), the red-team suite and a replayed load test, and ships only if every
   threshold in the metric table holds and no metric regresses beyond tolerance against production (ADR-018
   decision 3).
5. **Judge as procedural memory.** Judge prompts (`eval.judge.*`) are versioned, hashed and pinned like production
   prompts (ADR-026 procedural memory), run at temperature 0 with structured output, and are calibrated against SME
   labels each quarter; a judge change is an MRM-validated change (ADR-018 decision 4).
6. **RAGAS as definitions, library by approval.** We follow the RAGAS open-source metric definitions and names. The
   RAGAS Python library is used **offline only** (tiers 3 and 4, in CI and pre-prod), pinned to a reviewed version,
   if the Bank's open-source review approves it; otherwise `eval-runner` implements the same formulas (02 §17.1),
   validated against the library on synthetic data. The online sampler uses our own implementation in both cases,
   so production never depends on a third-party evaluation package. The Foundry evaluation SDK may be used in CI as
   an additional harness where its AI-assisted evaluators call our in-region judge **[confirm]**; it does not define
   the gate.
7. **Runtime operations metrics.** Per-stage p50/p95/p99 latency, tokens per request (input and output, per use case,
   agent and deployment), estimated cost per request and per day (AED, from a price table in App Configuration),
   gate outcomes and budget exceedances, with the alerts of plan §3.9 (02 §11.4, §11.6).

### Metrics

| Metric | Definition (02 §17.1) | Needs reference? | Tier where computed | Threshold | Owner |
|---|---|---|---|---|---|
| Context relevance (runtime) | Share of candidate passages with reranker score ≥ the keep threshold | No | 1 | Monitored: alert below 0.5 (weak passages are dropped before generation); the decline rule is best passage < 2.0 | MRM (threshold), architecture (tuning) |
| Faithfulness | Claims supported by the retrieved context ÷ claims in the answer | No | 1 (lexical and embedding approximation), 2, 3, 4 (judge) | T1 ≥ 0.9 and every claim cited; T2 alert < 0.90 over 1 h; T4 faithful-answer rate ≥ 0.95 (Wilson lower bound) | MRM |
| Answer relevancy | Mean similarity between the question and questions the judge generates from the answer | No | 1 (embedding approximation), 2, 3, 4 | T1 ≥ 0.7; T2 alert < 0.80 over 1 h; T4 ≥ 0.85 | MRM |
| Context precision | Rank-weighted precision of relevant passages in the top k | T2: no (judged against the question); T3, T4: yes (labelled passages) | 2, 3, 4 | T4 ≥ 0.85; T2 trend in the weekly drift report | Architecture (retrieval), MRM |
| Context utilisation | Context precision with the answer as the reference (were the passages used?) | No | 2, 3 | Monitored, no gate | Architecture |
| Context recall | Reference-answer claims attributable to the retrieved context ÷ reference claims | Yes | 3, 4 | ≥ 0.90 | Credit and product SMEs (labels), MRM |
| Answer correctness | 0.75 × factual F1 against the SME answer + 0.25 × semantic similarity | Yes | 3, 4 | ≥ 0.85 | Credit and product SMEs, MRM |
| Citation accuracy | Answers whose every citation resolves to a returned passage that supports its claim | T1, T2: no; T3, T4: yes (labelled clause) | 1 (resolves), 3, 4 | ≥ 0.95 (Wilson lower bound) | MRM |
| Correct-decline rate | Unanswerable items (no policy, superseded, out of clearance) that the system declines | Yes (label) | 3, 4; T1 counts declines | ≥ 0.90 (Wilson lower bound) | Credit and product SMEs |
| Entitlement leaks, injection success, numeric mismatches | Counts on the golden set and red-team suite; T1 numeric check on every request | Yes (test design) | 1 (numeric), 3, 4 | = 0 | InfoSec (leaks, injection), MRM |
| False-positive block rate (C13) | Legitimate banking items blocked or declined by a guardrail | Yes | 3, 4 | ≤ 2% | InfoSec, product owner |
| RM feedback | Thumbs-down share with reason | No | 2 | > 10% per use case triggers review | Product owner |
| Replayed p95 latency and p95 tokens per request | From the replayed load test | No | 4 (T1 measures in production) | Within plan §5 budgets | SRE |

Rates use the two-sided 95% Wilson lower bound (z = 1.96, plan §5); means (answer relevancy, context precision and
recall, answer correctness) use the lower limit of a 95% bootstrap interval **[agree method with MRM]**. Reachability
on the planned set sizes: 293 of 300 for a 0.95 bound; for the correct-decline rate, 96 of 100 unanswerable items (or
59 of 60) for a 0.90 bound, so the UC2 set carries at least 100 unanswerable items.

### Latency and cost impact per tier

| Tier | Request-path latency | Tokens and cost | Where it runs |
|---|---|---|---|
| 1 Runtime gates | Retrieval gate ≈ 1 ms (scores already computed). Post-generation: deterministic checks ≤ 30 ms; one batched `embed` call (answer plus claims) ≤ 150 ms p95 on UC2; total ≤ 250 ms p95 on UC2 and ≤ 100 ms on UC1 and UC3, inside the plan §5 checks lines. A retry costs one more generation and runs only if ≥ 4 s remain | About 300 `embed` tokens per UC2 answer (≈ 0.7 M per day at scale). Every retrieval decline saves a `gen-large` call (≈ 5k input tokens and ≈ 3.5 s) | `orchestrator`, inline |
| 2 Online sampled | None (asynchronous; results within minutes) | ≈ 8k judge tokens per item × ≤ 200 per day ≤ 1.6 M tokens per day (`05` §2.3), on a separate APIM product with its own TPM limit so it cannot starve RMs | `eval-runner` online worker in prod |
| 3 Scheduled | None in prod (nightly and monthly run in pre-prod; the weekly real-data slice of about 100 items runs in prod off-peak) | Nightly ≈ 10 M tokens per run on the pre-prod resource (system under test ≈ 4.5 M, judge ≈ 5 M) **[estimate]**; monthly red team ≈ 6 to 8 M | `eval-runner` CronJobs |
| 4 Release gate | None in prod; adds about 30 to 60 min to the pipeline (eval run plus a 30 min replayed load test) | As nightly, per release candidate | CI stage 4 on pre-prod |

## Consequences

**Positive**

- A weak retrieval is declined before the model is called: faster, cheaper and safer than generating and then
  checking.
- The same metric names and definitions appear in the release report, the nightly report and the production
  dashboard, so MRM compares like with like.
- Production drift becomes visible within hours, not at the next nightly or the next complaint.
- Tokens per request, per-stage p95 and cost per request turn plan §5 budgets into alerts (C10, C28).

**Negative**

- Runtime approximations (lexical and embedding) are weaker than a judge; some unfaithful answers pass tier 1 and are
  only seen by tier 2 on a 5% sample. Accepted because tier 1 is tuned to decline when in doubt and every release is
  gated on the judge-based metrics.
- Context relevance was first a second decline threshold; the prototype showed it wrongly declined a strong clause
  with weak neighbours, so it is now monitored only (plan v2.7b). The decline rate of the single remaining
  threshold is tuned on the golden set and alerted.
- A judge and its prompts are a model under MRM; calibration costs SME time each quarter.
- More to own: the online worker, its APIM product, judge prompts, alert rules and a price table.
- Pilot volume makes online windows noisy; alerts need sample-size guards and will be slower than at scale.

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| Judge bias or drift (a judge that agrees with its own model family) | Judge pinned and versioned; quarterly agreement with SME labels reported to MRM; disagreements settled by SMEs; review trigger below |
| Runtime approximation diverges from the judge | Weekly comparison of tier 1 scores with tier 2 scores on the same items; recalibration through tier 4 |
| Online sampler reads content it should not | Masked content only, from the audit content store, under `id-rmc-eval-runner` with read and unwrap rights scoped to sampled items **[confirm with DPO]**; crypto-shredded items are skipped |
| Judge capacity starves user traffic | Separate APIM product, TPM limit at ≤ 10% of `gen-large` quota, back-off above 70% utilisation |
| RAGAS library not approved or changes behaviour between versions | Definitions implemented in `eval-runner`; library pinned and only offline; cross-check on synthetic data |
| Alert fatigue at pilot volume | Minimum 10 scored items per window; 1 h alerts are tickets, the 3-day window and hard gates page |

## Review trigger

- Foundry continuous (online) evaluation becomes available in UAE North, keeps data in Bank resources and works
  without content in Application Insights **[confirm]**: re-evaluate option 3 for tier 2.
- Content Safety groundedness detection is offered in-region with acceptable latency: add it to tier 1 (option 5 as a
  complement).
- Judge-versus-SME agreement drops below the threshold MRM sets, or tier 1 and tier 2 faithfulness diverge by more
  than 0.05 over a month.
- The retrieval-gate decline rate stays more than 5 points above its baseline for two weeks after tuning.
- The Bank's open-source review approves or rejects the RAGAS library.
- Latency budget pressure (a use case's p95 within 300 ms of its target for a month).

## Related

- ADR-018 (golden set, release gate, red team, MRM; extended by this ADR), ADR-011 (guardrails and the UC2 groundedness
  check that tier 1 formalises), ADR-014 (metadata-only telemetry; the runtime operations metrics), ADR-022 (verified
  release makes inline gates possible), ADR-013 (audit content store as the online content source), ADR-026
  (procedural memory for judge prompts), ADR-020 (kill switch and threshold pins), ADR-006 (reranker scores), ADR-004
  (model pins and judge deployment), ADR-021 (CI stages).
- Plan §3.9, §4.3, §5, §6 C10, C12, C13, C23, C28; `01-hld.md` §5.2; `02-lld.md` §11 and §17; `05` §1, §4.6, §8.5;
  `06` §5.4, §5.5, §8.4, RB-20; `08` blocks 10 and 11.
