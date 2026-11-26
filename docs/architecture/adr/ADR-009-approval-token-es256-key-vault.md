# ADR-009: Approval token signed with ES256 in Key Vault HSM

| Field | Value |
|---|---|
| Status | Accepted; amended 26 Nov 2026 for plan v2.1 to v2.4 (step-up via `acrs`, claim set and `jti` format, canonical draft scope, rotation overlap, aligned with `02-lld.md` §4) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, InfoSec (application security), CRM product owner |
| Critical issues | C6 (approval forgery and under-binding), C7 (retries after expiry), C4 (identity chain) |
| Phase | 1 (pilot), applies to every write action thereafter |

## Context

UC3 is the only use case that changes a system of record: after the RM edits and approves the drafts, the call
report and its tasks are written to the CRM (plan §4.2). The model, the orchestrator and any text the model has
read are untrusted for the purpose of authorising that write. The design must therefore prove, at the moment of
the write, that **this RM** approved **this exact content** for **this customer** and **this operation**, recently,
once.

Today's prototype (`code/rm_copilot/tools.py`, `ApprovalService`) shows the intent but not a bank-grade control:

- One symmetric HMAC-SHA256 key is used both to issue and to verify. Any component able to verify can also mint,
  so a compromised `tool-gateway` (or any holder of the secret) can forge approvals (C6).
- Claims are `{u, d, exp}` only: no audience, no customer, no operation, no schema version, no `jti`. A token is
  replayable for its 300 s life and can be replayed against a different customer if the draft digest collides
  in shape.
- The digest uses `json.dumps(sort_keys=True)`, which is Python-specific (whitespace, `ensure_ascii`, float
  formatting). A verifier in another language, or a different Python version, can compute a different hash for
  the same draft.
- There is no step-up, no CSRF protection and no persisted verdict, so a write retried after `exp` would fail
  (C7) or would tempt engineers to lengthen the token life.

Forces and constraints from the plan:

- Separation of duties between services (§3.2): `web-bff` mints, `tool-gateway` verifies with the public key
  only, `orchestrator` holds no key material (§3.3 Key Vault row).
- Identity chain (§3.4): the RM is the `oid` of the OBO token for the `tool-gateway` audience; `tool-gateway`
  never trusts an RM id in a body or a header.
- Freshness: approval requires the authentication context `approve-write` (`acrs` contains `c1`; ID token
  `auth_time` ≤ 10 min as the fallback), otherwise step-up (§3.4).
- Latency: commit p95 ≤ 2 s end to end (§5), which includes signature, verification, the `jti` write and the
  first CRM attempt.
- Residency: keys and signing operations stay in UAE North (§3.1).
- Rotation every 90 days with overlap (§3.3).
- Volume is small: an assumed upper bound of a few thousand approvals per day at 300 RMs (illustrative), far
  below any Key Vault throttling limit **[confirm current Key Vault HSM key operation limits]**.

Threats the control must address: prompt-injected orchestrator calling the write tool with altered content;
compromised `tool-gateway` pod; draft substitution between approval and commit; cross-customer substitution;
replay by an insider within the token life; cross-site request forgery against the approve endpoint; algorithm
confusion (`alg: none`, HS256 with the public key as secret).

## Options considered

1. **ES256 JWS signed by a Key Vault Premium HSM key, minted by `web-bff`, verified by `tool-gateway` with the
   public key** (claims bound to RM, customer, operation, schema and draft hash; RFC 8785 canonical JSON;
   atomic `jti` create; step-up on approve; verdict persisted).
2. **Shared-secret HMAC (today's code)**, with the secret moved into Key Vault and the claim set extended.
3. **Entra-issued token with custom claims**: obtain a fresh Entra access token at approval time and inject the
   draft hash and customer through a custom claims provider (token issuance extension).
4. **CRM-native approval workflow**: the copilot writes drafts into the CRM in a "pending RM approval" state and
   the RM approves inside the CRM, which then publishes the records.
5. **No approval token**: trust the `web-bff` session; the approve click causes `web-bff` to request an OBO token
   with `Tools.Write`, and `tool-gateway` accepts any write carrying that scope.

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 ES256 in Key Vault HSM | 2 Shared HMAC | 3 Entra custom claims | 4 CRM-native approval | 5 No token |
|---|---|---|---|---|---|
| Forgery resistance | ++ verifier holds only the public key; the private key never leaves the HSM | -- every verifier can mint; one leaked secret forges all approvals | + Entra signs, but the content claim comes from our own claims API, so that API becomes the forgery point | ++ approval recorded by the system of record under the RM's CRM session | -- any holder of a `Tools.Write` token (the orchestrator forwards it) can write arbitrary content |
| Binding to exact content and customer | ++ `draft_sha256`, `customer_id`, `op`, `schema` in signed claims | + possible if claims are extended, same weakness as above | - token issuance is not designed per transaction; claims would be cached with the token | ++ the RM approves the record itself | -- scope is coarse; no content binding |
| Replay resistance | ++ `jti` atomic create, 300 s `exp` | + possible with the same `jti` store | - Entra access tokens live for an hour or more by default and are cached by MSAL; no per-approval `jti` we control | + CRM state machine prevents double approval | -- token reusable for its whole lifetime |
| Auditability and evidence | ++ signed token, verdict and `kid` persisted; verifiable later with the public key | - a MAC proves only that someone with the secret produced it | 0 Entra sign-in logs exist but do not show the content approved | ++ evidence lives in the CRM audit trail | -- only "a session clicked something" |
| Latency (commit ≤ 2 s) | + one in-region Key Vault `sign` call (tens of ms, **[confirm]**) plus one Cosmos create | ++ in-process | - interactive or silent token request plus an external claims callout on the approve path | - RM context switch into the CRM; minutes, not seconds, of user time | ++ none added |
| Operability | + key rotation, `kid` allow-list and Key Vault dependency to run | 0 secret rotation and distribution to two services | - custom claims provider, app registration changes, Entra team ownership | - CRM customisation by the CRM team, release cycle outside our control | ++ nothing to run |
| Lock-in and portability | + JWS ES256 is a standard; Key Vault only holds the key | + standard | - Entra-specific extension mechanism | -- tied to one CRM product; does not cover other write targets | n/a |
| Time to pilot | + library work plus Key Vault RBAC; well-trodden pattern | ++ exists today | - unclear support for OBO flows and per-request claims **[confirm]** | -- depends on CRM backlog and Bank change calendar | ++ |

## Decision

Adopt **option 1**.

1. **Key.** An EC P-256 key `approval-signing`, HSM-protected, in a Key Vault Premium instance in UAE North with
   purge protection and RBAC authorisation. `web-bff`'s workload identity holds a custom role with only
   `keys/sign/action` and `keys/read` on that key. `tool-gateway` holds a custom role with `keys/read` only (public
   key). `orchestrator` has no role on the vault. Admin roles are PIM-eligible only.
2. **Token format.** Compact JWS, header `{alg: "ES256", typ: "approval+jwt", kid: "<key version>"}`, claims
   `{iss: "web-bff", aud: "tool-gateway", sub: <RM oid>, customer_id, op: "crm.commit_followup", schema:
   "followup/1", draft_sha256, iat, exp: iat + 300, jti: <16 random bytes, base64url, 22 characters>, kid}` (plan
   §4.2 step 6); `kid` is in both the header and the payload and must match (`02-lld.md` §4.1). `web-bff` computes the
   SHA-256 of the JWS signing input and calls Key Vault `sign` with `ES256`; the returned 64-byte `r||s` value is
   the JWS signature **[confirm signature encoding in the Key Vault SDK used]**.
3. **Canonicalisation.** The whole `followup/1` draft (schema, customer, meeting date, call report, tasks with item
   ids, and the email, so the compose link carries only approved text) is canonicalised with RFC 8785 (JCS) and
   hashed with SHA-256 (`02-lld.md` §4.2). Three rules close the known gaps in JCS: strings are normalised to
   Unicode NFC at the `web-bff` boundary when the draft is saved (Arabic text can arrive in different
   normalisation forms); the schema contains no floating-point numbers (dates as ISO strings, amounts, if ever
   added, as strings or integers in minor units); the same JCS implementation version is pinned in both services
   and a shared set of test vectors (English, Arabic, emoji, escaped characters) runs in CI for both.
4. **Approve endpoint.** `web-bff` checks Origin, a CSRF token (double submit) and authentication freshness: the
   access token's `acrs` contains `c1` (authentication context `approve-write`, sign-in frequency 10 min), with the
   ID token's `auth_time` ≤ 10 min as the fallback; otherwise it returns a claims challenge for `c1` that forces
   re-authentication (plan §3.4) **[confirm `acrs` on the access token `web-bff` receives, plan §10 item 8]**. The approve request carries the draft id and its Cosmos ETag, so an RM
   approves the version they saw; a concurrent edit returns 412 and the UI re-renders.
5. **Verification in `tool-gateway`**, in this order, failing closed on the first error: `alg` must equal
   `ES256` (reject everything else, including `none` and HS256); `kid` in the allow-list (current and previous
   key version, read from App Configuration, ADR-020); signature; `iss`, `aud`; `exp` with 30 s skew and `iat`
   not in the future; `sub` equals the `oid` of the RM token on the same call (§3.4); `customer_id`, `op` and
   `schema` equal the request; entitlement for `customer_id` re-checked; SHA-256 of the canonical submitted draft
   equals `draft_sha256`; then an **atomic create** of the `jti` marker in Cosmos DB, where HTTP 409 means replay.
   The marker is written in one Cosmos transactional batch with the verdict, the saga state and the outbox items
   (`approvals`, partition `/jti`; ADR-010, ADR-019), so a replay can never produce a verdict.
6. **Verdict persisted.** `{jti, sub, customer_id, op, draft_sha256, kid, verified_at, result}` and the approved
   snapshot of the draft are stored in `approvals` (30 days after the last item is final, §7). Saga retries after `exp` (ADR-010) rely on the persisted verdict and never re-verify
   the token. The token itself, the verdict and the `kid` go into the `approval_issued` and `write_*` audit
   events (ADR-013).
7. **Rotation.** New key version every 90 days. The new `kid` is added to the verifier allow-list 1 h after
   creation, the signer switches at 24 h, and the previous `kid` stays allowed until day 8 (`02-lld.md` §4.7; token
   life is 300 s, so the overlap is generous). Emergency revocation disables the key version in Key Vault
   and removes the `kid` from the allow-list; outstanding tokens die within the App Configuration refresh interval.
8. **Failure behaviour.** If Key Vault `sign` fails or times out (cap 400 ms, p95 budget 100 ms of the 2 s commit,
   `02-lld.md` §1.6), approve fails closed with a retryable message; the draft stays saved. No fallback signing path exists.

The prototype's `ApprovalService` stays HMAC-based for offline demos, with a comment stating it is not the
production control; the mock-Azure prototype (workstream C) implements the ES256 flow against a mocked Key Vault
`sign` endpoint.

## Consequences

Positive

- A compromised `tool-gateway` or orchestrator cannot mint approvals. A compromised `web-bff` can sign, but the
  token is useless without the RM's own `Tools.Write` OBO token on the same call (`sub` = `oid` check), so the
  attacker needs both the signer and a live RM session.
- Every committed CRM record traces to a signed, independently verifiable statement of who approved which bytes,
  when, under which key version.
- Retries after expiry are well defined (C7) without lengthening the token life.

Negative

- Key Vault becomes a runtime dependency of the approve step. A Key Vault outage blocks new approvals (reads and
  drafting continue).
- Canonical JSON across two services is a real engineering risk: a mismatch shows up as every approval failing
  the hash check. Test vectors and a contract test are mandatory, and the schema must stay float-free.
- The signature is the Bank's (a service key), not the RM's. It is strong technical evidence, not a qualified
  electronic signature. If a future write needs legal non-repudiation by the individual, this ADR is insufficient.
- More moving parts than today's HMAC: custom RBAC roles, `kid` allow-list, `jti` store, verdict documents.

Risks and mitigations

| Risk | Mitigation |
|---|---|
| Hash mismatch from Unicode or serialisation drift | NFC at the boundary, pinned JCS library, shared test vectors, synthetic end-to-end approval in every deploy smoke test |
| Algorithm confusion or `kid` injection | Hard-coded `alg` check; `kid` must match the allow-list exactly; public keys fetched only from Key Vault, never from a URL in the token |
| `web-bff` compromise used to mass-approve | `sub` = `oid` check, step-up freshness, Sentinel analytic on approvals per RM per hour (C15), write kill switch (ADR-020) |
| Clock skew between pods | AKS node time sync; 30 s skew window; `iat` future check |
| `jti` store unavailable | Fail closed; Cosmos is zone-redundant (ADR-019); commit returns "try again" and the draft is kept |
| Key deleted by mistake | Purge protection and soft delete on the vault; key admin via PIM with approval |

## Review trigger

- The CRM product gains a native approval API that the Bank prefers (re-assess option 4, especially for Phase 3
  write actions).
- A write action with legal or financial effect beyond a call report or task (for example a limit change request)
  enters scope: re-assess whether an individual e-signature is needed.
- Key Vault operation limits or latency observed in pilot exceed 10% of the commit budget.
- A second signer service or a second verifier service is proposed.

## Related

- Plan: §3.2 (`web-bff`, `tool-gateway`), §3.3 (Key Vault row), §3.4 identity chain, §4.2 write path steps 5 to 10,
  §6 C4, C6, C7, §7 retention.
- ADR-010 (saga and outbox consume the persisted verdict), ADR-013 (audit events), ADR-019 (Cosmos containers
  and partition keys for `jti` and verdicts), ADR-020 (`kid` allow-list and write kill switch). ADR-008 (OBO
  token and `oid` that the `sub` check relies on), ADR-002 (signer and verifier pod isolation on AKS).
- Code: `code/rm_copilot/tools.py` (`ApprovalService`, `create_call_report`), `code/rm_copilot/orchestrator.py`
  (`commit`).
