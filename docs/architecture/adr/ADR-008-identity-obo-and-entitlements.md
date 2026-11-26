# ADR-008: Entra OBO plus workload identity; entitlements enforced in tool-gateway

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, identity and access management, information security, CRM owner |
| Plan references | §3.2 (`web-bff`, `orchestrator`, `tool-gateway`), §3.3 Identity, §3.4, §4.1 step 4, §6 C4, C9, C15, C22 |

## Context

`tool-gateway` is the security boundary (plan §3.2): it must know **which RM** is asking, without trusting the
`orchestrator`, because the orchestrator processes model output and untrusted text (documents, notes, news) and must
be assumed manipulable by prompt injection. Forces:

- **No trust in asserted identity**: an RM id in a body or header written by the orchestrator could be altered by a
  bug or a compromised pod (C4).
- **Two principals per call**: the RM (whose entitlements apply) and the calling service (which must be the
  orchestrator or the outbox worker, not any pod in the cluster).
- **Heterogeneous downstreams**: some systems of record accept Entra tokens, legacy ones do not (plan §3.4 step 5).
- **Entitlement rules** (portfolio, delegation with expiry, team-lead read-only, C22) live in the CRM; downstream
  systems enforce them inconsistently or not at all.
- **Insider misuse** (C15) must be detectable per RM.
- **Writes** additionally need a scope (`Tools.Write`) and a signed approval (ADR-009).
- **Latency**: OBO exchange and entitlement lookup must fit within 150 ms (cached) and 20 / 150 ms (cache hit /
  miss) budgets (plan §5).

## Options considered

1. **Entra OBO token for the `tool-gateway` audience, forwarded unchanged by the orchestrator, plus the
   orchestrator's workload identity token; entitlements enforced in `tool-gateway`** (plan §3.4).
2. **Pass the RM id** (from `web-bff` session) in a header or body; `tool-gateway` trusts the orchestrator's
   service identity (mTLS or workload token) and the asserted RM id.
3. **Downstream-only enforcement**: forward the RM's identity (OBO) to each system of record and rely on each to
   enforce entitlements; `tool-gateway` performs no entitlement check.
4. **Token never leaves `web-bff`**: the orchestrator receives an opaque session handle; `tool-gateway` resolves
   the handle to the RM identity by calling `web-bff` (token broker pattern).

## Trade-off analysis

| Criterion | 1 OBO + workload identity, enforce in tool-gateway | 2 Pass RM id | 3 Downstream-only | 4 Token broker in web-bff |
|---|---|---|---|---|
| Resistance to a manipulated orchestrator | **Strong.** Orchestrator cannot change the RM (`oid` is in a signed Entra token); it can only act within that RM's entitlements while the token is valid | **Weak.** Orchestrator can assert any RM id | Strong for identity, but see consistency | **Strongest**: orchestrator never holds a bearer token |
| Consistency of entitlement rules | **Strong.** One implementation (portfolio, delegation, team-lead read-only) for all tools | Strong if implemented in tool-gateway, but on an untrusted identity | **Weak.** Each system enforces differently; legacy systems may not enforce at all | Strong |
| Caller authentication | Workload token validated (`aud`, issuer, allowed client ids) | Service identity only | Service identity only | Service identity plus broker call |
| Legacy system support | APIM adds RM `oid` in a signed header; enforcement already done in tool-gateway | Same header, weaker origin | **Gap**: legacy systems cannot validate the RM | Same as 1 |
| Latency | OBO cached in `web-bff`; token validation local (cached JWKS); entitlement cache ≤ 60 s | Lowest | OBO per downstream | Extra hop to `web-bff` on every tool call |
| Coupling | Low: standard Entra flows | Low | Low | **High**: tool-gateway depends on web-bff availability |
| Auditability (who did what) | RM `oid` from a token in every audit event | Asserted id only | Distributed across systems | Strong |
| Operability | Standard MSAL and Entra configuration; app registrations per service | Simple | Simple for us, hard to evidence | More moving parts |
| Revocation | Token lifetime plus entitlement cache ≤ 60 s; Continuous Access Evaluation for custom APIs **[confirm]** | Session-bound | Token lifetime | Session-bound, immediate |
| Time to pilot | Good: established patterns | Fastest | Depends on each system | Slower |

## Decision

Adopt **option 1**, exactly as plan §3.4:

1. RM signs in (CRM sidebar: MSAL; Teams tab: Teams SSO); token audience = `web-bff`.
2. `web-bff` validates it and performs **OBO** for audience `tool-gateway`, scope `Tools.Read`; `Tools.Write` is
   requested only on approve, with fresh authentication (`auth_time` ≤ 10 min, step-up otherwise).
3. `orchestrator` forwards the OBO token **unchanged** and authenticates itself with its **AKS workload identity**
   token.
4. `tool-gateway` validates both (`iss`, `aud`, `tid`, `scp`, signature, expiry; caller client id on an allow-list).
   The RM is the `oid` of the user token. It **never** trusts an RM id in a body or header.
5. Systems of record that accept Entra tokens receive an OBO token for their audience; legacy ones are called with
   `tool-gateway`'s managed identity through APIM with the RM `oid` in an APIM-signed header. **Entitlements are
   always enforced in `tool-gateway`**, regardless of the downstream.

Entitlements: checked **first** on every customer-scoped tool, from CRM portfolio and delegation records (with
expiry) and app roles (team lead read-only); cached ≤ 60 s and invalidated on portfolio change events; denial
returns 403 `NOT_ENTITLED` and no data moves (plan §4.1 step 4). Repeated `NOT_ENTITLED` feeds Sentinel analytics
(C15).

## Consequences

**Positive**
- A manipulated orchestrator cannot read another RM's customers or approve writes; the blast radius of prompt
  injection is bounded by the RM's own entitlements (deck principle "the model never decides who can see what").
- One entitlement implementation, tested once, with `entitlement leaks = 0` as a release gate (plan §5).
- Every audit event carries an RM `oid` taken from a validated token.

**Negative**
- The orchestrator holds the RM's OBO token for the request; a compromised orchestrator could replay it **within
  its lifetime and within that RM's read entitlements**. Option 4 would remove this but at a coupling and latency
  cost we do not accept now.
- The Bank must maintain several app registrations, scopes and admin consent; token validation logic must be
  correct in two places (`web-bff`, `tool-gateway`).
- Entitlement staleness of up to 60 s after a portfolio change (C22) is accepted.
- Legacy systems rely on an APIM-signed header; their own logs show the service identity plus a header, not an
  Entra user token.

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| Token replay by a compromised orchestrator pod | Short token lifetime; network policy (only orchestrator and outbox-worker reach tool-gateway); workload token must match an allow-listed client; Defender for Containers; Sentinel rule on unusual per-RM volume (C15) |
| Write performed with a read token | `Tools.Write` scope required plus approval token bound to `sub` = `oid` (ADR-009) |
| Entitlement cache serves a revoked portfolio | ≤ 60 s TTL plus event invalidation; source of events in Phase 1 to be fixed (`01-hld.md` §15 item 3) |
| Delegation records wrong or expired | Expiry enforced in code; delegation reads audited; team-lead role read-only |
| Phase 2 brief prefetch (C9) has no RM present | Separate system identity, RM portfolio enforced, output readable only by that RM, audit "system on behalf of"; only after risk sign-off |

## Review trigger

- Continuous Access Evaluation or token binding (proof-of-possession) becomes available for custom APIs in the
  Bank's tenant, enabling shorter effective token exposure **[confirm]**.
- A penetration test or red-team shows token replay from the orchestrator is practical.
- Systems of record adopt Entra tokens with consistent entitlement enforcement (could simplify step 5, not remove
  the tool-gateway check).
- Phase 2 bot or prefetch introduces a non-interactive principal.

## Related

- ADR-009 approval token; ADR-002 AKS (workload identity, network policy); ADR-003 APIM (legacy RM header);
  ADR-007 customer data live; ADR-016 channels; ADR-017 network.
- `01-hld.md` §7, §9; `02-lld.md` (token claims, validation rules, app registrations); plan §3.4, C4, C22.
