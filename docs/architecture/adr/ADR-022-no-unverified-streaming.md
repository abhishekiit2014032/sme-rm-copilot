# ADR-022: No unverified streaming: buffer, verify, release

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, SME business owner, model risk, UX lead |
| Critical issues | C18 (streaming would show unverified text), C19 (output exfiltration), C2 (no hard dependency on optional features) |
| Phase | 1 |

## Context

Chat products stream tokens as they are generated, which makes long answers feel fast. In this copilot, every
generated line is checked after generation (ADR-011): placeholders are filled from tool output, the numeric check
rejects free figures and number words, the policy check rejects promises of approval, pricing or limits, and UC2
answers must pass claim-level groundedness or be retried once or declined. Streaming would show the RM text that
has not passed those checks (C18). Once read, a wrong figure or an implied approval cannot be "unseen", and an RM
might repeat it to a customer.

The latency cost of not streaming is real. Figures from the §5 latency budget (p95, ms):

| Moment the RM sees text | UC1 brief | UC2 answer | UC3 draft |
|---|---|---|---|
| Deterministic facts ready (edge + entitlement + fan-out) | 150 + 20 + 1,500 = **1,670** | n/a (no facts section) | n/a |
| First model token (edge + entitlement + retrieval or fan-out + PII/Shields + router + time to first token) | 150 + 20 + 1,500 + 300 + 0 + 1,000 = **2,970** | 150 + 700 + 400 + 600 + 1,000 = **2,850** | 150 + 20 + 300 + 400 + 0 + 700 = **1,570** |
| First verified unit if released per sentence or per talking point (first token + about 50 tokens for UC1, about 30 tokens for UC2 at about 16.5 ms per token, + about 50 ms check) | ≈ **3,850** | ≈ **3,400** | n/a (JSON object must be complete to validate) |
| Full verified output (§5 total) | ≈ **6,400** (table: ≈ 6,500, target 8,000) | ≈ **5,750** (table: ≈ 5,900, target 7,000) | ≈ **4,970** (table: ≈ 5,100, target 10,000) |

So at p95, buffering costs about 3.4 s of "time to first words" in UC1 and about 2.9 s in UC2 compared with token
streaming, and about 2.4 to 2.5 s compared with sentence-level verified streaming. UC3 gains nothing from streaming:
the output is a JSON draft that must be complete before schema validation, and the RM edits it before anything
happens.

Other forces:

- **Placeholders.** UC1 model output is a JSON array of talking points containing `{placeholders}`; raw tokens are
  not displayable without partial parsing and filling.
- **UC2 decisions are answer-level.** The retry-or-decline decision (§4.3) is taken on the whole answer. If the
  third sentence fails, earlier released sentences may be part of an answer that should have been declined.
- **Azure OpenAI content filtering in streaming mode** holds back output until it has been filtered in the default
  mode; the asynchronous filter mode releases content before filtering **[confirm current behaviour]**. Our own
  checks have the same problem one level up.
- **APIM token accounting** (`llm-token-limit`, `llm-emit-token-metric`) is simplest with non-streamed responses
  **[confirm streaming support and accuracy of these policies]**.
- **RM experience.** A 6 s wait with no feedback feels broken; a 6 s wait with visible, honest progress steps is
  acceptable for a pre-meeting brief that replaces 20 to 30 minutes of manual preparation (illustrative).

## Options considered

1. **Buffer, verify, release**, with progress steps shown while waiting, and early release of deterministic,
   tool-sourced sections (facts first).
2. **Token streaming** of model output to the RM, with checks running afterwards and a warning banner if a check
   fails.
3. **Sentence-level (or item-level) verified streaming**: generate with streaming internally, release each
   sentence or talking point once it passes the checks.
4. **Stream, then retract**: stream tokens, then replace or strike through any part that fails verification.

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 Buffer, verify, release | 2 Token streaming | 3 Sentence-level verified | 4 Stream then retract |
|---|---|---|---|---|
| Unverified content shown (C18) | ++ never | -- always, for seconds | + never per sentence, but answer-level decisions can be contradicted by already released sentences | -- shown, then retracted; the RM has read it |
| Perceived latency, UC1 | 0 facts at ≈ 1.7 s, wording at ≈ 6.4 s | ++ first words at ≈ 3.0 s | + first talking point at ≈ 3.9 s | ++ ≈ 3.0 s |
| Perceived latency, UC2 | - answer at ≈ 5.8 s (progress steps meanwhile) | ++ ≈ 2.9 s | + ≈ 3.4 s | ++ ≈ 2.9 s |
| Fit with UC2 retry and decline | ++ the RM sees only the final outcome | -- RM sees the failed attempt | - a retry must withdraw released sentences or append a contradicting decline | -- |
| Fit with placeholders (UC1) | ++ fill after generation | - partial JSON parsing and filling on the fly | + per item once each item is complete | - |
| Groundedness that needs whole-answer context (for example a claim split across sentences) | ++ | -- | - sentence-local only | -- |
| Audit clarity (what exactly did the RM see) | ++ one released output per request | - stream plus later warning | 0 sequence of releases to record | -- stream plus retractions |
| Engineering complexity | ++ simplest; one release step | + | - incremental checks, partial state, ordering, UI updates | - |
| Exfiltration risk via rendering (C19) | ++ plain text released once after checks | - | + | - |

## Decision

Adopt **option 1**.

1. **No model-generated text reaches the RM before it passes every check** for its use case (ADR-011 L5). This
   applies to every channel (ADR-016), including the Phase 2 bot.
2. **Facts first (UC1).** Sections built only from tool output (customer snapshot, watch list, signals with
   sources) are deterministic and already minimised by `tool-gateway`; they are released as soon as the fan-out
   completes (≈ 1.7 s at p95). Talking points follow after verification. This is the same content as the UC1
   degraded data-only brief (ADR-015), so the RM experiences degraded mode as "the second half did not arrive",
   not as a different screen. This is a presentation refinement of plan §4.1 step 10, which describes one
   verified response; the released facts are verified by construction.
3. **Progress steps.** `web-bff` relays orchestrator milestones to the UI by server-sent events or short polling
   (ADR-016): "Checking your access", "Reading CRM, LOS, KYC, news (n of 5)", "Searching approved policies",
   "Writing", "Verifying figures and policy wording". Steps are truthful (emitted when the step actually starts),
   carry no content, and stop at the deadline with a degraded or declined outcome.
4. **Model calls are non-streamed** between `orchestrator` and APIM in Phase 1, which keeps APIM token metrics and
   limits simple. Internal streaming (to start checks earlier or abort generation on a first failure) is a later
   optimisation, not a user-facing feature.
5. **Measure the experience.** Pilot metrics: time to facts, time to verified output, abandonment before release,
   RM satisfaction with wait time. These decide whether option 3 is worth its complexity for UC1 in Phase 2.

## Consequences

Positive

- The RM never sees a figure, promise or claim that failed verification; the audit record of "what was shown" is
  one released output per request.
- The decline path in UC2 stays clean: the RM sees either a verified answer or an honest decline with sources.
- Simplest implementation and testing; no partial-output state in the UI.

Negative

- Perceived latency is worse than a chat product's: about 3 s later for first words in UC1 and UC2 at p95. RMs used
  to consumer chat tools may perceive the copilot as slow. Progress steps and facts-first mitigate but do not
  remove this.
- UC2 has no deterministic section to show early; the full wait (≈ 5.8 s at p95) is visible.
- If future use cases produce long-form text (for example credit memo drafting), buffering will cost much more
  and this ADR must be revisited for them.

Risks and mitigations

| Risk | Mitigation |
|---|---|
| RMs abandon before release | Facts first in UC1, progress steps, p95 targets as SLOs; abandonment metric with alert |
| Pressure to "just turn streaming on" after the pilot | This ADR plus the model risk sign-off; any change goes through ARB and the eval gate |
| Progress events leak content (for example a source name with a customer name) | Fixed vocabulary of step messages with counts only |
| SSE blocked by an intermediary on the corporate network | Short polling fallback |

## Review trigger

- Pilot data show abandonment before release above an agreed threshold, or RM satisfaction with wait time below
  target (evaluate option 3 for UC1 talking points, which are independent items).
- A long-form generation use case enters scope.
- Latency budget changes materially (for example PTU deployments cutting time to first token and output time),
  which would shrink the gap that motivates streaming.

## Related

- Plan: §4.1 step 10, §4.3 ("released only after it passes"), §5 latency table and deadlines, §6 C18, C19.
- ADR-011 (what is verified), ADR-015 (degraded mode equals facts-only brief, deadlines), ADR-016 (progress steps in
  the sidebar and tab), ADR-013 (released output recorded once), ADR-005 (orchestrator milestones), ADR-004
  (time to first token and PTU as the latency lever).
- Code: `code/rm_copilot/orchestrator.py` (`brief` builds `snapshot` and `watch` from tool output before calling the
  model; `ask` returns only after `groundedness` passes or declines).
