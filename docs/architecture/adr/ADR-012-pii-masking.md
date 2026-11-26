# ADR-012: PII masking before every model call

| Field | Value |
|---|---|
| Status | Accepted (Arabic coverage open, see risks) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, InfoSec, data protection officer, model risk |
| Critical issues | C2 (regional availability), C5 (customer data in prompts), C17 (abuse monitoring), C21 (imperfect masking) |
| Phase | 1 |

## Context

Prompts carry customer data: meeting notes (UC3), free-text KYC remarks and news (UC1), RM questions (UC2). The
model is hosted in the Bank's own Azure tenant in UAE North on a private endpoint with `store=false`, and the
Bank applies for modified abuse monitoring (§3.5, C17). Masking is therefore **defence in depth and data
minimisation**, not the only control keeping data in-country. It still matters because:

- Until modified abuse monitoring is approved, prompts may be retained by Microsoft for up to 30 days for abuse
  review (§3.5). Masked prompts reduce what that retention could expose.
- Masked prompts are what we store in the audit content store (§7 "masked prompts, outputs"), so masking also
  limits what audit holds for a year.
- The model rarely needs identifiers. It needs roles ("the CFO"), not names; never an Emirates ID or an IBAN.

Constraints and forces:

- Tool output is already minimised in `tool-gateway` (no national ID, aggregated transactions, §4.1 step 6).
  Masking in the `orchestrator` targets free text and anything minimisation missed.
- UAE identifiers have known formats: Emirates ID `784-YYYY-NNNNNNN-C`, UAE IBAN `AE` + 2 check digits + 19
  digits (23 characters), mobile numbers `+971 5x xxx xxxx`, card numbers (Luhn). Trade licence numbers vary by
  emirate and free zone and have weak formats **[confirm formats with the Bank's KYC team]**.
- Arabic and mixed-script text: Arabic names, Latin transliterations with many spellings, Arabic-Indic digits
  (٧٨٤), code-switching within a sentence. Azure AI Language PII support for Arabic, and whether it has
  UAE-specific entity types, is **[confirm, §10 item 5]**.
- Latency: PII plus Prompt Shields have 300 ms (UC1) and 400 ms (UC2, UC3) in the §5 budget, run in parallel
  with the tool fan-out where possible.
- The output must refer back to real names and values (the email salutation, the customer name in the brief).
  This is already solved for figures with placeholders (ADR-011); the same mechanism must work for masked
  entities.

## Options considered

1. **Azure AI Language PII detection plus regex for UAE identifiers**, run in the `orchestrator` before every model
   call, with request-scoped reversible pseudonyms.
2. **Microsoft Presidio self-hosted on AKS** (open source analyzer and anonymizer) with custom recognisers.
3. **No masking**: rely on the private endpoint, in-region processing, `store=false` and the abuse monitoring
   exemption.
4. **Tokenisation vault**: a persistent tokenisation service (format-preserving tokens, detokenised only at
   authorised boundaries) for all customer identifiers.

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 AI Language + regex | 2 Presidio on AKS | 3 No masking | 4 Tokenisation vault |
|---|---|---|---|---|
| Recall on English PII | + managed NER models, maintained by Microsoft | + good with spaCy or transformer models; we maintain them | -- | ++ for structured identifiers it knows; free-text names still need NER |
| Recall on Arabic PII | ? depends on AI Language Arabic support **[confirm]**; regex covers structured IDs only | - no maintained Arabic NER out of the box; would need a custom model and labelled data | -- | - same NER gap for free text |
| Residency | + in-region if the PII feature is available in UAE North; private endpoint | ++ runs inside the cluster | + data stays in the tenant | ++ inside the Bank |
| Latency | + one in-region call (budget 300 to 400 ms, shared with Prompt Shields) | + in-cluster, model-size dependent | ++ none | 0 extra service hop per call |
| Operability | + managed service; quotas and document size limits to respect **[confirm limits]** | - model hosting, updates, scaling, security patching are ours | ++ | -- a new tier-1 service with its own HA, key management and audit |
| Cost (qualitative) | + per-text-record consumption | 0 compute on AKS plus engineering time | ++ | -- product licence or build effort |
| Data minimisation evidence for regulators | + documented control per call | + | -- hard to defend to a regulator or DPO | ++ strongest |
| Fit with placeholders | ++ request-scoped pseudonyms reuse the placeholder pattern | ++ same | n/a | 0 tokens persist beyond the request, more than we need |
| Time to pilot | ++ | - | ++ | -- |

Option 4 solves a broader problem (persistent pseudonymisation across systems) that the copilot does not have: we
never need a token to mean the same customer across requests, because tool calls re-read live data under the RM's
identity. Option 3 is rejected because it makes the abuse-monitoring decision and the audit content store carry
raw identifiers, and because masking is cheap relative to the evidence value. Option 2 remains the fallback if
AI Language is unavailable or inadequate in UAE North.

## Decision

Adopt **option 1**.

1. **Where.** The `orchestrator` masks every text that goes to any model deployment (`gen-small`, `gen-large`)
   and the UC2 query before embedding. `ingestion-worker` runs the same scan on policy documents and quarantines
   any document with customer PII for human review (policies should not contain customer data). Tool output is
   minimised first in `tool-gateway`, then masked.
2. **Two detectors, union of results.** Azure AI Language PII detection (language `en` or `ar` per detected
   language, with a domain-specific category allow-list: person, phone, email, address, IBAN, credit card,
   passport and national ID types where offered) **and** a regex pack that is always on: Emirates ID (hyphenated
   or not), UAE IBAN with mod-97 validation, cards with Luhn validation, UAE phone formats, trade licence patterns
   per issuing authority, email. Arabic-Indic and Eastern Arabic-Indic digits are normalised to ASCII before regex
   matching, and tatweel and zero-width characters are stripped.
3. **Request-scoped reversible pseudonyms.** Detected entities are replaced with typed tokens (`[PERSON_1]`,
   `[PHONE_1]`). The token map lives only in the `orchestrator` request context, is never logged, never sent to
   the model, never persisted. Verified output may contain tokens; the `orchestrator` re-hydrates only tokens that
   the release step allows (names in salutations, the customer's contact) and drops any other token. Identifiers
   such as Emirates ID or IBAN are never re-hydrated into model-generated text.
4. **Not masked:** the SME company name and the RM's own name, which the wording needs and which are business
   information already visible to the RM. Sole proprietorships whose trade name is a person's name are a known
   edge case: treated as company names for wording, and the content store still encrypts them per customer
   (ADR-013).
5. **Fail-safe.** If AI Language is unavailable or exceeds its timeout (derived from the deadline), the regex pack
   runs alone and the request continues with a `pii_degraded` flag in audit and metrics. If regex finds a
   structured identifier that minimisation should have removed, a `minimisation_gap` event is raised against the
   tool contract.
6. **Evaluation.** A PII golden set (English, Arabic, transliterated Arabic, code-switched notes) with per-entity
   recall and precision; recall targets for structured identifiers = 100% on the set; free-text names reported
   and gated once the Arabic set exists. Part of the release gate (ADR-018).
7. **Service settings.** AI Language resource in UAE North, private endpoint, `disableLocalAuth`, managed identity
   access from the `orchestrator` and `ingestion-worker` only; synchronous API so no job results are stored
   service-side **[confirm data handling for the synchronous PII API]**.

## Consequences

Positive

- Identifiers the model never needs never reach it, whatever happens to abuse-monitoring approval.
- The audit content store holds masked text, reducing the blast radius of a key compromise.
- The pseudonym approach reuses the placeholder pattern; one re-hydration step, one set of tests.

Negative

- Masking is imperfect (C21). Some names and free-form identifiers will pass through, especially in Arabic. The
  design must not claim "no PII reaches the model"; it claims "identifiers are minimised and masked, with
  measured recall".
- Over-masking harms quality: a company called after a person, or a street name, can be masked and make a
  talking point vague. Tracked through the golden set and RM feedback.
- One more in-path service with its own quota and document-size limits; long notes must be split into
  documents within the limit and results merged by offset.

Risks and mitigations

| Risk | Mitigation |
|---|---|
| AI Language PII does not support Arabic, or not in UAE North | Regex fallback always on; Arabic use stays at "evaluated" in Phase 1 (§2); evaluate AI Language PII container on AKS or Presidio with an Arabic NER model as the Phase 2 fallback **[confirm container availability for PII]** |
| Transliterated names (Mohammed, Muhammad, Mohamed) missed | Name gazetteer from CRM contact records for the customer in scope, applied as an extra dictionary match per request (data already entitled to the RM) |
| Token map leaks into logs | Map held in a dedicated object with no `__repr__` content, excluded from OTel attributes; CI test asserts no token map in captured telemetry (ADR-014) |
| Masking latency exceeds budget on long notes | Run in parallel with the profile fetch; deadline-derived timeout; regex-only fallback |
| Masked tokens confuse the model | Typed tokens with consistent numbering; prompt explains tokens; eval cases with masked inputs |

## Review trigger

- Microsoft confirms Arabic PII support (or its absence) in UAE North.
- Golden-set recall for structured identifiers below 100%, or name recall below the gate agreed with the DPO.
- Phase 2 voice memos (speech transcripts have no punctuation or casing, which lowers NER recall).
- A new use case needs persistent pseudonyms across requests (re-assess option 4).

## Related

- Plan: §3.3 (PII row), §3.5, §4.1 step 7, §4.2 step 2, §4.3, §5 latency table, §6 C5, C17, C21, §7, §10 item 5.
- ADR-011 (L2 in the guardrail layers), ADR-013 (masked content in the audit content store), ADR-014 (no content
  in telemetry), ADR-018 (PII golden set), ADR-001 (residency), ADR-004 (abuse monitoring and `store=false`),
  ADR-007 (field minimisation in `tool-gateway`).
- Code: `code/rm_copilot/guardrails.py` (`PII_PATTERNS`, `mask_pii`: national ID, IBAN, card, trade licence, email and
  UAE phone patterns, without checksum validation (mod-97, Luhn), digit-script normalisation or reversible
  pseudonyms, which this ADR adds), `code/rm_copilot/tools.py` (`get_customer_profile` field
  minimisation).
