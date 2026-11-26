# ADR-018: Evaluation, release gates and model risk management

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, model risk management (second line), credit and product SMEs, internal audit (observer) |
| Critical issues | C12 (model retirement), C13 (false positives), C23 (no MRM) |
| Phase | 1 (evals as a CI job, §2) |

## Context

A risk committee will not accept "it seems to work". The copilot must be governed like any other model in the
Bank (governance slide), and every change (prompt, model version, retrieval configuration, index rebuild,
guardrail rule) must be shown not to make things worse. Forces:

- **Gates from §5**: groundedness ≥ 0.95 and citation accuracy ≥ 0.95 with a 95% confidence bound on a ≥ 300-item
  golden set; entitlement leaks = 0; injection success = 0; numeric mismatches = 0; false-positive block rate
  ≤ 2%.
- **Statistics of small sets.** On 300 items, a two-sided 95% Wilson lower bound ≥ 0.95 needs at least 293 passes
  (97.7% observed). Zero failures in 300 trials only bounds the true failure rate below about 1.0% (one-sided
  95%). "Zero" gates on a 300-item set are evidence, not proof.
- **Residency of evaluation.** AI-assisted evaluators need a judge model; the judge must be an in-region
  deployment. Foundry risk-and-safety evaluators and the AI Red Teaming Agent use a Microsoft-hosted back end that
  runs only in some regions **[confirm regions, §10 item 7]**. They cannot receive Bank data unless that back end
  is in the UAE.
- **Synthetic data outside prod (§3.3 Delivery row).** Golden-set items built from real cases can only be used
  inside the production boundary; CI in lower environments uses synthetic equivalents.
- **Model retirement (C12).** Model versions retire roughly yearly; every upgrade must pass the same gate within a
  90-day runbook.
- **Ownership.** Labels must come from people who know credit and product policy, not from the delivery team.
- **Existing assets.** The prototype runs evals offline with a deterministic mock (`code/rm_copilot/llm.py`
  `MockLLM`) and the evals slide describes a 300+ question and 50 brief golden set, online RM feedback, draft
  edit tracking, sampled judging with weekly SME review.

## Options considered

1. **Golden set owned by credit and product SMEs, automated release gate with confidence bounds, Foundry
   evaluators in CI with an in-region judge only, red-team suite (PyRIT; AI Red Teaming Agent where available
   in-region), and a full MRM lifecycle** (inventory, independent validation, lineage, ongoing monitoring,
   AI-assisted marking).
2. **Manual UAT only**: business testers try scenarios before each release and sign off.
3. **Vendor benchmarks**: rely on published model benchmarks and Microsoft's model cards and safety evaluations
   to accept a model version.

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 Golden set + gate + red team + MRM | 2 Manual UAT only | 3 Vendor benchmarks |
|---|---|---|---|
| Evidence quality for a risk committee | ++ repeatable, versioned, statistically bounded | - anecdotal, not repeatable, no confidence bounds | -- not our data, our prompts, our policies or our languages |
| Regression detection on every change | ++ runs in CI on prompt, model, index and rule changes | -- too slow and costly to run on every change | -- says nothing about our changes |
| Coverage of our failure modes (entitlement, injection in notes, numbers, Arabic) | ++ designed for them | 0 depends on tester imagination | -- |
| Residency | + in-region judge only; Microsoft-hosted safety evaluators only on synthetic data until confirmed in-region | ++ | ++ (no data involved) |
| Cost (qualitative) | 0 SME labelling time is the largest cost; judge tokens are small | - recurring tester time per release | ++ |
| Time to pilot | - golden set v1 must exist in Phase 0 (§2) | + | ++ |
| Independence (MRM) | + second line validates the set, the judge and the gate | 0 | - |

## Decision

Adopt **option 1**.

1. **Golden set.** At least 300 UC2 questions and 50 UC1 briefs and 50 UC3 note sets at pilot start (v1 in Phase 0,
   §2), labelled and owned by credit and product SMEs, versioned with the policy corpus version it was labelled
   against. Composition targets: answerable, unanswerable (correct decline expected), ambiguous, out-of-clearance,
   cross-portfolio, Arabic and code-switched (growing toward Arabic GA), adversarial. Every production failure
   (RM thumbs-down with reason, SME review finding, incident) becomes a candidate golden item. Lower environments
   use a synthetic twin of each item; the real-data set lives only in production storage and runs in the
   production `eval-runner`.
2. **Metrics.** Retrieval: recall@8 and ranking quality on labelled passages. Generation: groundedness, citation
   accuracy, correctness against SME answer, correct-decline rate, false-decline rate. Safety: entitlement leaks,
   injection success, numeric mismatches, policy violations, false-positive block rate (C13), PII recall
   (ADR-012). Operational: latency per step vs §5, tokens per request.
3. **Release gate.** A change ships only if: every hard gate is met (zero entitlement leaks, zero injection
   successes, zero numeric mismatches on the set); groundedness and citation accuracy lower bounds (95% Wilson) are
   ≥ 0.95; false-positive block rate ≤ 2%; and no metric regresses beyond its tolerance versus the current
   production baseline (paired comparison on the same items). Deterministic properties (entitlements, approval
   token checks) are additionally covered by unit and integration tests, because statistical gates are the wrong
   tool for them.
4. **Evaluators.** Deterministic evaluators (citations resolve, numeric check, policy check) run first. AI-assisted
   evaluators (Foundry evaluators such as groundedness and relevance) run in CI with **an in-region judge
   deployment only**, pinned and versioned like production models. The judge is calibrated against SME labels on
   a held-out subset (agreement reported each quarter); disagreement is settled by SMEs. Foundry
   risk-and-safety evaluators and the AI Red Teaming Agent are used on synthetic data only until their back end is
   confirmed in-region.
5. **Red team.** A suite of at least 1,000 generated and curated attacks (direct and indirect injection in notes,
   news and policy text; cross-portfolio requests; PII extraction; number-word evasion; approval-language
   injection; Arabic variants) built with PyRIT against the in-region endpoints, re-run on every release and
   expanded quarterly. Larger than the golden set because zero-tolerance properties need more trials to bound the
   rate (zero in 1,000 bounds it below about 0.3%).
6. **Online monitoring.** RM feedback with reason codes; draft edit distance before approval; decline and
   escalation rates; guardrail hits; tool errors; retrieval drift as policies change. Sampled online judging (the
   evals slide says 5% of traffic) reads masked content from the audit content store (ADR-013) and uses the
   in-region judge; weekly SME review of a stratified sample. Thresholds and owners per metric.
7. **Model risk management (C23).**
   - **Inventory**: one inventory entry per use case, where the "model" is the system: model deployment and
     version, prompts, retrieval configuration, guardrail rule versions, golden-set version, judge version.
   - **Tiering and independent validation**: second line validates each use case before go-live and on material
     change (new model version, new use case, new write action), including the evaluators and the judge.
   - **Lineage**: every audit event carries model version, prompt hash, tool contract versions, guardrail version,
     index version (ADR-013), so any answer maps to a validated configuration.
   - **Change classes**: prompt or rule change (gate plus first-line sign-off), model version change (gate plus
     second-line review, C12 90-day runbook), new use case or write action (full validation and governance board).
   - **AI-assisted marking**: CRM call reports carry "AI-assisted, RM-approved" (§4.2 step 10); the UI labels
     generated text.
8. **Placement.** `eval-runner` is a CI job in Phase 1 (§2), triggered on every pull request that touches prompts,
   rules, retrieval configuration or model pins, and nightly against the current production configuration.

## Consequences

Positive

- Changes are accepted on evidence the second line can re-run; model upgrades become routine instead of risky.
- The statistical framing is honest: the board sees bounds, not just point estimates.
- The red-team suite turns security properties into regression tests.

Negative

- SME time is the critical path: 400 labelled items in Phase 0 is weeks of expert effort, and the set must be
  maintained as policies change.
- A 300-item gate is coarse: it cannot detect regressions of one or two percentage points on rare categories.
  Category-level results are reported but not gated until each category is large enough.
- The judge is itself a model under MRM; its calibration costs SME time each quarter.
- Synthetic twins in CI may not reproduce real-data failure modes; the production `eval-runner` remains the
  authority.

Risks and mitigations

| Risk | Mitigation |
|---|---|
| Golden set overfits (prompts tuned to the set) | Held-out split never used for tuning; quarterly refresh with new production cases |
| Labels drift as policies change | Items tagged with policy version; re-labelling triggered by policy re-index |
| Microsoft-hosted safety evaluators never available in-region | PyRIT with in-region targets and judge covers red teaming; safety categories covered by content filter metrics |
| Gate blocks an urgent fix | Emergency change path: kill switch or rollback first (ADR-020), then the fix goes through the gate |

## Review trigger

- Foundry risk-and-safety evaluators or the AI Red Teaming Agent become available with an in-region back end
  (§10 item 7).
- The Bank publishes or revises its MRM standard for generative AI (§10 item 12).
- Arabic GA (Phase 2): Arabic golden set sized to its own gate.
- A judge-versus-SME agreement drop below the agreed threshold.

## Related

- Plan: §2 (Phase 0 golden set, Phase 1 evals as a CI job), §3.2 (`eval-runner`), §5 quality gates, §6 C12, C13,
  C23, §10 items 7 and 12.
- ADR-011 (what the gate measures), ADR-012 (PII recall), ADR-013 (lineage and content store for online sampling),
  ADR-014 (operational metrics), ADR-020 (kill switch and rollback), ADR-021 (CI pipeline and synthetic data), ADR-004 (model pins, upgrade runbook,
  content filter tuning), ADR-006 (retrieval metrics), ADR-005 (deterministic workflows under test).
- Code: `code/rm_copilot/llm.py` (`MockLLM` for offline evals), `code/rm_copilot/guardrails.py` (deterministic
  evaluators reuse these checks).
- Deck: `deck/project/slides/evals.html`, `governance.html`.
