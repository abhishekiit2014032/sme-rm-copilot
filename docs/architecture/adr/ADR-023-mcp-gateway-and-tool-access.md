# ADR-023: APIM as the MCP gateway in front of tool-gateway MCP servers

| Field | Value |
|---|---|
| Status | Accepted (APIM-native MCP features are used where confirmed; the decision does not depend on them, see Decision step 4) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, integration platform team (APIM), information security, engineering lead |
| Plan references | §3.2 (`tool-gateway`), §3.3 Gateway, §3.4, §3.7 (MCP gateway), §4.1 to §4.3, §5 (token and step budgets), §6 C4, C10, C19, C24, C25, C27, §10 item 12 |
| Phase | 1 (pilot) |

## Context

Plan v2.6 turns the `orchestrator` into a supervisor-led graph of agents (ADR-025), each with its own identity
(ADR-024). The agents need tools: `brief-agent` reads customer data and recalls episodes, `policy-agent` searches
policies, `followup-agent` reads a profile and creates drafts. Until v2.5 the orchestrator called `tool-gateway`
over a typed REST client (ADR-008). v2.6 exposes the same tools as **Model Context Protocol (MCP) servers** hosted by
`tool-gateway` (`customer-data`, `policy-search`, `crm-drafts`, `memory`) behind **APIM as the MCP gateway** (plan
§3.7).

Forces:

- **Tool descriptions are prompt content.** The model reads a tool's name, description and schema to decide how to
  call it. Whoever can change a description can steer the model ("tool poisoning"), and an MCP server that changes
  its tool list after approval ("rug pull") changes what the agent can do (C25).
- **Least privilege per agent** (C24): an agent must not see, let alone call, a tool it does not need. A policy
  document carrying an indirect injection must not be able to make `policy-agent` read customer data.
- **`tool-gateway` stays the security boundary** (ADR-008): entitlement first, field minimisation and masking,
  audit as the only producer. Nothing about MCP may move a policy decision into the agent runtime or into the model.
- **Commit is not an agent tool** (plan §3.7): the CRM write runs only through the write API with an RM-signed
  approval token (ADR-009).
- **Private by default and UAE-resident** (ADR-001, ADR-017): no internet egress from AKS except the allow-list; no
  third-party hosted tool servers.
- **Latency** (plan §5): the UC1 tool budget is 1,500 ms p95 for the whole fan-out; the gateway hop must be small.
- **Maturity.** MCP is young and its specification has changed several times; APIM's MCP features are new
  **[confirm GA status, supported tiers including classic Premium in internal VNet mode, and availability in UAE
  North]**.

## Options considered

1. **APIM as the MCP gateway in front of internal `tool-gateway` MCP servers**: `tools/list` filtered per agent,
   tool names, descriptions and schemas versioned, pinned and hashed, internal registry only, `tool-gateway` as
   the policy decision point.
2. **Plain REST tools without MCP**: keep the typed REST client from ADR-008; tool schemas live in the agent code;
   APIM in front as today.
3. **Direct MCP from agents to `tool-gateway` MCP servers, no gateway**: the agent runtime calls the MCP endpoints
   inside the cluster; `tool-gateway` does all checks.
4. **Foundry Agent Service managed tools**: agents and their tool connections (MCP, OpenAPI, AI Search) defined in
   Azure AI Foundry Agent Service, which calls the tools **[confirm MCP tool support, private networking to
   internal endpoints, and availability in UAE North]**.
5. **Allow external MCP servers** as well (for example a vendor-hosted news or company-registry MCP server, or
   servers from a public registry), behind the same gateway.

## Trade-off analysis

| Criterion | 1 APIM MCP gateway + internal servers | 2 Plain REST tools | 3 Direct MCP, no gateway | 4 Foundry managed tools | 5 External MCP servers allowed |
|---|---|---|---|---|---|
| Tool poisoning and rug pull (C25) | **Strong.** Internal servers only; hashes pinned on both sides; a changed description is rejected | **Strong.** Schemas are compiled into our code; nothing is discovered at runtime | Good if pinning is built; no second checkpoint outside `tool-gateway` | **Weak to medium.** Tool definitions held in the service; our pinning must be re-implemented around it **[confirm]** | **Weak.** Descriptions written by a third party can change at any time; pinning only detects it |
| Least privilege per agent | **Strong.** Agent token checked at APIM and again in `tool-gateway`; `tools/list` shows only allowed tools | Good: per-agent client code, but enforcement relies on `tool-gateway` alone and agents can still see every route | Good in `tool-gateway`; no coarse outer check | Medium: per-agent tool lists in the service; identity mapping to our app roles unclear **[confirm]** | Weak: external servers cannot check our agent roles or RM entitlements |
| Central controls (limits, metrics, logging) | **Strong.** Per-agent, per-RM and per-server limits in the same APIM as the AI gateway; metadata-only logs | Strong: APIM already fronts the system APIs | **Weak.** Limits and metrics re-built in `tool-gateway` | Medium: service-level quotas; per-RM limits unclear | Medium: limits apply but data leaves our boundary |
| Separation of policy decision from enforcement | **Clear.** APIM enforces coarse rules; `tool-gateway` decides | Clear (same as today) | Single point: simpler, but one bug opens everything | Decision split between Foundry and `tool-gateway` | Decision partly made by a third party |
| Interoperability and reuse | **Good.** Standard discovery; Agent Framework and LangGraph both ship MCP clients; other Bank agents could reuse the same governed tools later | Weak: bespoke client per consumer | Good protocol, but no governed entry point for other consumers | Good inside Foundry; harder outside it | Best catalogue, worst control |
| Latency | One extra in-VNet hop per tool call (illustrative 10 to 20 ms p95, to measure); UC1 fan-out stays one call | **Lowest**: same hop count as today | Lowest MCP option | Service round trips plus private link to our endpoints; per-step deadlines not ours **[confirm]** | Internet round trips; breaks the 1,500 ms UC1 budget for any slow vendor |
| Residency and network | **In-region, private.** No new egress | In-region, private | In-region, private | Depends on Foundry agent runtime and thread storage in UAE North **[confirm]** | **Fails** the private-by-default rule unless the vendor hosts in the UAE and is on the firewall allow-list |
| Maturity and change risk | **Medium.** MCP spec and APIM MCP features still moving; mitigated by pinning one spec version and a fallback (Decision step 4) | **Low.** Proven pattern | Medium: MCP spec churn without the gateway features | Medium to high: service API and feature set still moving | High |
| Operational effort | Medium: APIM MCP API, registry, manifests, two validation points | **Low** | Low to medium | Medium: second platform to run and evidence | High: vendor due diligence, outsourcing approvals |
| Lock-in | Low to moderate: MCP is open; APIM policies are Azure-specific | Low | Low | **High** | Moderate, plus vendor dependency |

## Decision

Adopt **option 1**.

1. **Servers.** `tool-gateway` hosts four MCP servers on internal routes, using the Streamable HTTP transport in
   stateless mode (no server-side session affinity, so pods scale behind KEDA) and one pinned MCP specification
   version **[confirm SDK support for stateless mode in the chosen framework]**:

   | MCP server | Tools (Phase 1) | App role required (agent) | Delegated scope (RM) |
   |---|---|---|---|
   | `customer-data` | `get_brief_inputs` (UC1 fan-out: profile, facilities, transactions summary, KYC status, news), `get_profile` | `Tools.CustomerData.Read` (all tools); `Tools.CustomerData.Profile` (`get_profile` only) | `Tools.Read` |
   | `policy-search` | `search_policies` | `Tools.PolicySearch` | `Tools.Read` |
   | `crm-drafts` | `create_followup_drafts` (validates `followup/1`, assigns item ids, runs the policy check; nothing reaches the CRM) | `Tools.CrmDrafts.Create` | `Tools.Read` |
   | `memory` | `recall_episodes` (ADR-026) | `Tools.Memory.Recall` | `Tools.Read` |

   The UC1 fan-out stays **one** tool call that `tool-gateway` parallelises server-side, so MCP adds one hop, not
   five. Servers expose **tools only**: the `resources`, `prompts`, `sampling` and `elicitation` capabilities are
   disabled, so a server can never ask the agent's model to generate text or ask the RM a question.
2. **No commit over MCP.** The write API (ADR-009, ADR-010) is a separate REST route that is not registered as an
   MCP server and never appears in any model's tool list; only the graph's deterministic commit step (code, after
   the RM's approval, ADR-025) calls it. No MCP tool requires or honours `Tools.Write`; that scope is checked only by
   the write API, together with a valid approval token.
3. **Internal registry only.** The tool registry is a Git-managed file (server, tool, version, required role and
   scope, schema, description). CI generates from it: the APIM MCP API definitions (Bicep), the per-agent
   manifests pinned in App Configuration (procedural memory, ADR-026), and the `tool-gateway` registry. APIM has
   routes only to registered servers; agent pods may reach only APIM (Cilium network policy); Azure Firewall blocks
   all other egress. MCP clients in the agent runtime are built only from the manifest: no server URL ever comes
   from model output or from a tool result. **No external MCP servers**; external data (news, company registry)
   stays a REST integration behind `tool-gateway` and the firewall allow-list.
4. **APIM as the MCP gateway.** APIM fronts the four servers as an MCP API product. Core controls use standard APIM
   policies and do not depend on MCP-specific features: `validate-jwt` on both the user token and the agent token
   (ADR-024), a server-level role check, rate limits per agent, per RM (`oid`) and per server, request size limits,
   metadata-only logging (body bytes 0, plan C5). APIM-native MCP features (MCP server pass-through, per-agent
   `tools/list` filtering at the gateway) are used where confirmed **[confirm features, tier and UAE North]**. If
   they are not available on the Bank's APIM tier, APIM routes the MCP endpoint as a plain HTTP API with the same
   policies, and `tools/list` filtering happens only in `tool-gateway`, which is authoritative in any case.
5. **Pinned and hashed tool definitions.** Each tool's definition (name, description, input schema, output schema,
   annotations) is canonicalised (RFC 8785, as in ADR-009) and hashed (SHA-256). The agent manifest pins the
   expected hash per tool. At agent start-up the runtime calls `tools/list` and refuses to become ready on any
   mismatch or missing tool (alert raised; extra tools are ignored and alerted). On every `tools/call` the agent
   sends the pinned hash in the request `_meta`; `tool-gateway` rejects a mismatch with `TOOL_VERSION_MISMATCH`.
   `notifications/tools/list_changed` is ignored at runtime: the tool set changes only by deployment. Because
   descriptions are prompt content, any change goes through the ADR-018 eval gate like a prompt change.
6. **`tool-gateway` is the policy decision point.** APIM is a coarse enforcement point; every decision below is
   re-made in `tool-gateway`, which never relies on APIM having checked.

Authorization decision for one `tools/call` (deny on the first failure; every decision is audited):

1. **Network.** The caller is an agent pod in namespace `rm-copilot` allowed by Cilium policy to reach the APIM MCP
   endpoint; nothing else in the cluster can.
2. **APIM: user token.** `validate-jwt`: issuer, tenant, signature, expiry, audience `tool-gateway`, `scp` contains
   `Tools.Read`.
3. **APIM: agent token.** `validate-jwt`: issuer, tenant, signature, expiry, audience `tool-gateway`, app-only token,
   client id in the agent allow-list, `roles` contains at least one role for the target server.
4. **APIM: limits.** Per-agent, per-RM (`oid` from the validated user token, never from a header) and per-server
   rate limits; request size cap. APIM forwards both tokens unchanged.
5. **`tool-gateway`: re-validate both tokens** independently (cached JWKS). The RM is the `oid` of the user token;
   the actor is the agent's client id (ADR-024).
6. **Method.** `tools/list` returns only tools whose required role is in the agent's `roles` and whose required
   scope is in the user's `scp`, then stops. `tools/call` continues.
7. **Tool registered and enabled.** Tool exists in the registry, its per-tool kill switch is off (App Configuration,
   ADR-020), and the `_meta` hash equals the registry hash.
8. **Agent holds the tool's app role** (for example `policy-agent` calling `get_profile` fails here, whatever the RM
   is entitled to).
9. **RM holds the tool's delegated scope** (`Tools.Read`).
10. **Arguments match the pinned schema**: strict JSON Schema, `additionalProperties: false`, enums, length limits,
    identifier formats; no free-form URLs or query languages.
11. **Entitlement first** for customer-scoped tools: the RM (or a valid delegate, team lead read-only) is entitled
    to `customer_id` (cache ≤ 60 s plus event invalidation, ADR-008). Denied: 403 `NOT_ENTITLED`, no data moves.
12. **Classification.** The tool's output classification is ≤ the RM's clearance (policy search also filters on
    clearance, ADR-006).
13. **Budgets.** Request step count ≤ 6 tool steps and the hop deadline (plan §5); else `BUDGET_EXCEEDED`.
14. **Execute, minimise, mask** (ADR-012), attach provenance (source system, record ids, retrieval time, tool
    version), write the audit event (RM `oid`, agent client id, workload, server, tool, definition hash, decision,
    latency) and return.

Denials return typed MCP errors (`TOOL_NOT_ALLOWED`, `NOT_ENTITLED`, `SCHEMA_INVALID`, `TOOL_VERSION_MISMATCH`,
`BUDGET_EXCEEDED`) and feed Sentinel analytics (C15): an agent asking for a tool outside its roles is a likely
injection signal.

## Consequences

**Positive**
- A poisoned or silently changed tool description cannot reach a model: the runtime will not start with it, and
  `tool-gateway` will not execute a call made against a different version.
- Two independent checks on agent privilege (APIM and `tool-gateway`) and one place for policy decisions.
- Indirect injection is contained by construction: `policy-agent` has no route to customer data, `brief-agent` has
  no draft or write tool, `followup-agent` cannot commit.
- Tools become reusable by other Bank agents through a governed, audited entry point, without opening a new path
  around `tool-gateway`.

**Negative**
- More moving parts than REST: a registry, generated manifests, two validation points, MCP SDKs in two services.
- One extra hop per tool call; small, but it must be measured and fits only because UC1 stays one fan-out call.
- MCP's specification and Azure's MCP features are moving targets; pinning one spec version means planned upgrades.
- Pinning makes every description tweak a release with an eval run, which slows prompt iteration on tools.

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| APIM MCP features not GA, not on the Bank's tier or not in UAE North **[confirm]** | Decision step 4 fallback: plain HTTP API with the same `validate-jwt` and limit policies; filtering in `tool-gateway` |
| MCP SDK defect or spec change breaks calls | Pinned spec and SDK versions; contract tests per tool in CI; REST client kept as a tested fallback path for one release |
| Gateway mis-configuration lets an agent see extra tools | `tool-gateway` filtering is authoritative; CI test asserts each agent's `tools/list` equals its manifest |
| Insider changes a description and the hash in the same commit | Registry changes need four-eyes review and the ADR-018 eval gate; hash and description changes are reported to MRM |
| Agent pod calls `tool-gateway` directly, bypassing APIM | Cilium policy: only APIM may reach the `tool-gateway` MCP routes; `tool-gateway` still validates everything |
| Arguments used to exfiltrate data (for example encoding data in a search query) | Strict schemas, no free-form URLs; outputs plain text only (C19); per-RM limits and Sentinel volume analytics |

## Review trigger

- APIM MCP gateway features confirmed GA on the Bank's tier in UAE North (adopt gateway-side filtering), or
  withdrawn (stay on the step 4 fallback).
- A new MCP specification version changes authorization or transport rules.
- A business case for an external MCP server (for example a licensed data vendor offering MCP hosted in the UAE):
  requires a new ADR, vendor due diligence and an outsourcing approval.
- Measured MCP gateway overhead above 50 ms p95 per call, or above 5% of any use case's latency budget.
- Another Bank agent platform asks to consume these tools.

## Related

- ADR-003 APIM (same instance, AI gateway and system API facade); ADR-008 identity and entitlements; ADR-009
  approval token (commit stays outside MCP); ADR-012 PII masking; ADR-013 audit; ADR-018 eval gate (description
  changes); ADR-020 kill switches; ADR-024 three identities; ADR-025 agents; ADR-026 memory (`memory` server).
- Plan §3.7, §6 C24, C25, C27, §10 item 12.
