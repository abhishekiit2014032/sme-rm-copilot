# ADR-024: Three identities on every tool call: user, agent and workload

| Field | Value |
|---|---|
| Status | Accepted (Entra Agent ID used where available; app registration with a service principal otherwise) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, identity and access management, information security, platform team (AKS) |
| Plan references | §3.2, §3.3 Identity, §3.4 (identity chain), §3.7 (three identities), §6 C4, C9, C15, C24, §10 item 12 |
| Phase | 1 (pilot) |
| Extends | ADR-008 (OBO plus workload identity; entitlements in `tool-gateway`), which stays in force |

## Context

ADR-008 gives `tool-gateway` two principals per call: the RM (OBO token, `oid`) and the calling service (the
orchestrator's workload identity). With the supervisor-led agents of ADR-025 there are now four agents inside that
one service, each meant to call a different, small set of tools through the MCP gateway (ADR-023). If they all
present the same service identity, `tool-gateway` cannot tell `policy-agent` from `brief-agent`, and the tool
separation that contains prompt injection exists only in our own runtime code.

Forces:

- **Prompt injection is assumed** (ADR-008, ADR-011): any agent that reads untrusted text (news, policy documents,
  RM notes) may be steered. Its privileges, not its intentions, bound the damage.
- **The RM's entitlements always apply** (C4, C22): no agent may see a customer its RM cannot see.
- **No secrets** (plan §3.3 Platform: `disableLocalAuth` everywhere): credentials must not be copyable files or
  strings.
- **Identity sprawl** (C24): more identities mean more role assignments to review and more ways to over-grant.
- **Audit** (ADR-013): every tool call must say who asked (RM), which agent acted and from which workload.
- **Product maturity.** Microsoft Entra Agent ID, managed identity as a federated identity credential for an app
  or agent identity, and agent on-behalf-of flows with an actor are new or in preview **[confirm GA status, token
  shapes and availability for the Bank's tenant]** (plan §10 item 12).
- **Latency:** token acquisition must stay off the hot path (plan §5 allows 150 ms for edge, `web-bff` and cached OBO).

## Options considered

1. **Three-way identity**: the user via OIDC plus OBO (delegated scopes, agent as actor), a service principal per
   agent (Entra Agent ID where available) holding app roles per tool, and the workload's managed identity as the
   agent's federated credential (no secrets). Effective permission = user ∩ agent ∩ workload.
2. **Single service identity for the whole copilot**: ADR-008 as it stands; every agent uses the orchestrator's
   workload identity plus the user token; per-agent tool lists enforced only in the agent runtime.
3. **User token only**: agents act purely as the RM; `tool-gateway` sees no service or agent identity.
4. **Agent identity only**: app-only tokens with application permissions per agent; the RM is passed as a
   parameter.
5. **Per-agent client secrets (or certificates)** stored in Key Vault, instead of federated credentials.

## Trade-off analysis

| Criterion | 1 User ∩ agent ∩ workload | 2 Single service identity | 3 User token only | 4 Agent identity only | 5 Per-agent secrets |
|---|---|---|---|---|---|
| Blast radius of an injected agent prompt | **Smallest.** The agent's app roles cap the tools; the RM's entitlements cap the data | Medium: any agent can call any tool the service may call, within the RM's entitlements | **Large.** Agent inherits everything the RM can do via the tool scopes | **Large.** Agent can read any customer its roles allow; RM id is just a parameter | Same as 1 while the secret is safe |
| Stolen tool-call token used elsewhere | **Hard.** Needs the user token and an agent token; the agent token cannot be minted without the pod's federated credential | Medium: user token plus a workload token, both minted inside the pod | **Easy.** One bearer token is enough | **Severe.** App-only token reads any customer within its roles until expiry | Medium: a stolen secret mints agent tokens anywhere, for its whole validity |
| Credential hygiene | **No secrets**: federated chain rooted in the pod's projected service account token | No secrets | No secrets | No secrets if federated | **Weak.** Secrets to store, rotate and leak; certificates better but still copyable |
| RM entitlement enforced | **Yes**, from the `oid` of a signed user token (ADR-008) | Yes | Yes | **No**: identity asserted by the caller (rejected in ADR-008 option 2) | Yes |
| Least privilege per agent visible at `tool-gateway` | **Yes**, `roles` claim per agent | **No**, only in runtime code | No | Yes | Yes |
| Audit: who, which agent, which workload | **Complete** | RM and service only | RM only | Agent only | Complete |
| Identity sprawl (C24) | Medium: four agent identities, four (later separable) managed identities, role assignments as code | **Lowest** | Lowest | Medium | Medium plus secret inventory |
| Product dependency | **Entra Agent ID, managed identity as FIC and agent OBO [confirm]**; works today with app registrations and a two-token form | None new | None | None new | None new |
| Latency | Agent tokens cached and refreshed in the background; no hot-path cost | None | None | None | None |

## Decision

Adopt **option 1**. A tool call is allowed only if **all three** layers allow it.

1. **User (OIDC plus OBO).** As ADR-008 and plan §3.4: the RM signs in with Entra ID; `web-bff` obtains an OBO token
   for audience `tool-gateway` with `Tools.Read` (and `Tools.Write` only on approve, with authentication context
   `approve-write`). The RM is the `oid` of this token. **Target form:** an agent OBO token in which the agent is
   the client (`azp`) or appears as the actor, so one signed token binds RM and agent **[confirm the exact flow and
   claims Entra supports for agent identities]**. **Baseline form (available today):** the user OBO token is
   forwarded unchanged in `Authorization`, and the agent token travels in a second header (`x-agent-token`);
   `tool-gateway` records the agent as the actor.
2. **Agent (service principal per agent).** `supervisor`, `brief-agent`, `policy-agent` and `followup-agent` each
   have their own identity: an Entra Agent ID agent identity where available, else a single-tenant app registration
   with a service principal **[confirm]**. Each holds app roles on the `tool-gateway` API for exactly its tools:

   | Agent | App roles on `tool-gateway` |
   |---|---|
   | `supervisor` | none (it has no data tools; a call it makes is always denied) |
   | `brief-agent` | `Tools.CustomerData.Read`, `Tools.Memory.Recall` |
   | `policy-agent` | `Tools.PolicySearch` |
   | `followup-agent` | `Tools.CustomerData.Profile`, `Tools.CrmDrafts.Create` |

   No agent holds any role on the write API. Role assignments are made as code from the tool registry (ADR-023),
   admin-consented once per environment, and reviewed quarterly (C24) against the registry.
3. **Workload (managed identity as federated credential).** AKS workload identity federates the pod's Kubernetes
   service account to a user-assigned managed identity; that managed identity is the **only** credential of the
   agent identity (a federated identity credential on the agent app or agent identity) **[confirm managed identity
   as FIC for app registrations and for Entra Agent ID in the Bank's tenant]**. No client secret or certificate is
   ever created; an Entra app management policy blocks password and certificate credentials on these apps
   **[confirm]**. The workload layer also carries its own permissions: Cilium policy (agent pods reach only APIM and
   the data-plane endpoints they need: App Configuration, Cosmos DB, the memory-keys Key Vault), and Azure RBAC on
   those resources (read-only App Configuration, `checkpoints` container, wrap and unwrap on memory keys; ADR-026).
4. **Phase 1 runtime topology.** All agents run in the `orchestrator` deployment (ADR-025), so one service account
   can reach all four managed identities. Each agent still has its **own** managed identity, so splitting a
   specialist into its own deployment later changes one federation subject, not the identity model. Until then the
   workload layer separates the agent runtime from every other pod, not one agent from another (see Consequences).
5. **`tool-gateway` evaluates the intersection** (ADR-023 steps 5 to 11): user token valid with the tool's scope,
   RM entitled to the customer, agent token valid with the tool's role, request arrived through APIM from the agent
   runtime. Every audit event carries RM `oid` (subject), agent client id (actor) and workload (service account,
   pod).
6. **Model calls** keep ADR-003: the runtime's workload identity authenticates to the APIM AI gateway, with
   `x-rm-oid` and an `x-agent-id` header accepted only from that identity, for per-RM limits and per-agent token
   metrics.

### Tokens and claims

| Token | How obtained | Audience | Key claims | Carried in | Validated by |
|---|---|---|---|---|---|
| RM sign-in token | MSAL (CRM sidebar) or Teams SSO | `web-bff` | `oid`, `tid`, `scp`, `acrs` (contains `c1` after step-up) | Browser to `web-bff` | `web-bff` |
| User tool token (OBO) | `web-bff` OBO exchange; target: agent OBO **[confirm]** | `tool-gateway` | `oid` = RM, `tid`, `scp` = `Tools.Read` (`Tools.Write` only on approve), `azp` = `web-bff` (baseline) or the agent (target), actor claim if Entra issues one **[confirm]** | `Authorization` | APIM, `tool-gateway` |
| Agent token | Client credentials grant by the agent identity, client assertion = managed identity token | `tool-gateway` | app-only token, `azp` / `appid` = agent client id, `oid` = agent service principal, `roles` = that agent's tool roles, `tid` | `x-agent-token` (baseline); merged into the user tool token in the target form | APIM, `tool-gateway` |
| Managed identity token | AKS workload identity: projected service account token exchanged with Entra | `api://AzureADTokenExchange` | managed identity as subject | Never leaves the pod except to Entra | Entra (federation trust: issuer, subject, audience) |
| Service account token | Kubernetes projected volume | `api://AzureADTokenExchange` | `iss` = AKS OIDC issuer, `sub` = `system:serviceaccount:rm-copilot:orchestrator` | Pod filesystem only | Entra |
| Approval token (ADR-009) | `web-bff` signs with Key Vault | `tool-gateway` write API | `sub` = RM `oid`, `customer_id`, `op`, `draft_sha256`, `jti` | Write API only; rejected on MCP | `tool-gateway` |

Token lifetimes follow Entra defaults (about an hour for access tokens) **[confirm, including the effect of
Continuous Access Evaluation]**; agent tokens are cached per agent and refreshed before expiry off the request path.

### What each layer prevents

| Threat | User layer | Agent layer | Workload layer | Residual risk |
|---|---|---|---|---|
| Stolen user tool token (for example from a log or a heap dump) | Limited to that RM's entitlements and `Tools.Read`, for the token's life | **Blocks use alone**: no agent token, so APIM and `tool-gateway` reject the call | The agent token cannot be minted outside the pod | Attacker who also holds a live agent token can read as that RM until expiry |
| Stolen agent token | **Blocks customer data**: no user token, no `oid`, no entitlement | Limited to that agent's tools | Cannot be renewed outside the pod | Calls that need no customer (policy search) work until expiry; per-agent limits and Sentinel on unusual source |
| Compromised agent prompt (indirect injection in news, a policy or RM notes) | Cannot exceed the RM's entitlements (another RM's customer: `NOT_ENTITLED`) | **Blocks other tools**: `policy-agent` cannot read customers; `brief-agent` cannot draft; nobody can commit | n/a (code is not compromised) | Misuse within the agent's own tools for the RM's own customers; bounded by schemas, budgets and verified release (ADR-011) |
| Compromised agent runtime pod (code execution) | Only RMs with in-flight requests, only `Tools.Read` (a commit also needs an approval token the pod cannot mint) | **Not a barrier in Phase 1**: the pod can mint all four agent tokens | **Bounds reach**: Cilium allows only APIM and the runtime's own data planes; no approval-signing role; no other managed identities | Read access as active RMs until detection; Defender for Containers, Sentinel on per-RM volume (C15), kill switch (ADR-020); per-agent deployments reduce this (review trigger) |
| Rogue pod elsewhere in the cluster | n/a | Cannot obtain any agent token (federation trusts only the agent runtime's service account) | **Blocks**: network policy and federation subject | None identified |

## Consequences

**Positive**
- Tool separation between agents is enforced by `tool-gateway` from signed tokens, not by our runtime code alone.
- No secrets anywhere in the chain; nothing to rotate, nothing to paste into a config file.
- Each audit event answers "which RM, through which agent, from which workload", which model risk and the
  security operations centre both need.
- The design works today in its baseline form and becomes tighter (one bound token) when agent OBO is confirmed.

**Negative**
- More identities to govern: four agent identities, four managed identities and their role assignments per
  environment (C24).
- In the baseline form the user token and the agent token are two independent bearer tokens; `tool-gateway` can
  check both but cannot prove cryptographically that this agent was acting for this RM. The agent OBO target closes
  this **[confirm]**.
- In Phase 1 the workload layer does not separate agents from each other (shared runtime pod).
- A projected service account token copied out of a compromised pod can be exchanged with Entra until it expires;
  "no secrets" is not "no bearer material".
- Dependence on Entra features that are new or in preview; their token shapes may change before GA.

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| Entra Agent ID or agent OBO not available or not GA in the tenant **[confirm]** | Baseline form with app registrations and two tokens; the claims checked by `tool-gateway` stay the same |
| Managed identity as FIC not supported for the chosen identity type **[confirm]** | Federate the Kubernetes service account directly to the agent app registration (AKS workload identity supports it); still no secrets |
| Over-granted app role during a change | Role assignments generated from the tool registry; CI diff check; quarterly access review (C24); `tools/list` equality test (ADR-023) |
| Service account token exfiltrated and exchanged elsewhere | Short projected token lifetime; Conditional Access for workload identities restricting agent service principals to the Bank's egress locations where supported **[confirm]**; Entra sign-in logs for service principals in Sentinel |
| Pod compromise mints all agent tokens (Phase 1) | Network and RBAC limits above; no write without an approval token; Sentinel and kill switch; split deployments per the review trigger |
| Identity objects drift between environments | Identities, federation and role assignments in IaC (ADR-021); one set per environment, synthetic data only outside prod |

## Review trigger

- Entra Agent ID, managed identity as FIC and agent OBO (actor) tokens confirmed GA in the Bank's tenant: move from
  the baseline two-token form to the single bound token.
- An agent gains a write-capable or higher-risk tool, a new agent is added, or a penetration test shows pod-level
  token minting is practical: run each specialist in its own deployment and service account.
- Conditional Access or Continuous Access Evaluation for agent identities becomes available.
- Phase 2 brief prefetch (C9) introduces an agent acting without a live RM token.

## Related

- ADR-008 (OBO and entitlements, extended here); ADR-002 AKS (workload identity, Cilium); ADR-003 APIM; ADR-009
  approval token; ADR-013 audit; ADR-020 kill switch; ADR-021 IaC; ADR-023 MCP gateway; ADR-025 agents; ADR-026
  memory (data-plane RBAC of the workload).
- Plan §3.4, §3.7, §6 C4, C24, §10 item 12.
