# ADR-003: API Management as AI gateway and system API facade

| Field | Value |
|---|---|
| Status | Accepted; amended 26 Nov 2026 for plan v2.1 (per-RM quota key) and v2.2 (2 units from the pilot) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, integration platform team, information security |
| Plan references | §3.2, §3.3 Gateway, §3.4 step 5, §5 (token budget, deadlines), §6 C8, C10, C14, §10 item 6 |

## Context

Two kinds of outbound calls need central control:

- **Model calls** from `orchestrator`, `tool-gateway` (query embeddings), `ingestion-worker` (document embeddings)
  and `eval-runner` to Azure OpenAI deployments `gen-large`, `gen-small` and `embed`. They need authentication,
  per-RM and per-use-case token limits (400k tokens per RM per day, plan §5), token metrics for cost, a backend pool
  with priority, circuit breaker and retry (for a later PTU plus Standard pair, ADR-004), version routing, and **no
  body logging** and **no semantic cache** (plan C5, C8).
- **System-of-record calls** from `tool-gateway` to CRM, core banking ODS, LOS, KYC, news. They need a single
  controlled path over ExpressRoute, JWT validation, per-consumer quotas to protect core systems (C14), and a way to
  pass the RM identity to legacy systems that do not accept Entra tokens (an APIM-signed header, plan §3.4 step 5).

The Bank already operates APIM for internal APIs **[confirm]**. Constraints: private by default (ADR-017), UAE North
only (ADR-001), zone redundancy (plan §3.1), deadline propagation with hop timeouts derived from the remaining
deadline (plan §5).

## Options considered

1. **Azure API Management Premium in internal VNet mode**, one instance with two API products: AI gateway (using
   the `llm-*` / `azure-openai-*` policy family) and system API facade.
2. **Direct SDK calls** from services to Azure OpenAI and to systems of record, with limits and metrics implemented
   in a shared library.
3. **Custom gateway on AKS** (for example an open-source LLM proxy plus Envoy) for model calls; existing Bank
   integration layer for systems of record.
4. **Foundry-only**: rely on Azure AI Foundry's own endpoint controls, quotas and tracing for model calls, with no
   gateway in front; systems of record through the Bank's existing integration layer.

## Trade-off analysis

| Criterion | 1 APIM Premium internal | 2 Direct SDK | 3 Custom gateway on AKS | 4 Foundry-only |
|---|---|---|---|---|
| Security (authn, policy enforcement point) | **Strong.** `validate-jwt`, managed identity to backends, central policy; services hold no model keys | **Weak.** Every service holds model access; controls only as good as the library and its adoption | **Moderate.** Strong if built well; we own the security of the proxy | **Moderate.** Entra auth and RBAC on the Foundry resource; no per-RM enforcement point |
| Per-RM token limits and cost metrics | **Native.** `llm-token-limit`, `llm-emit-token-metric` **[confirm daily quota behaviour across units, §10.6]** | Library code; per-pod counters need shared state | Available in some open-source proxies; shared state needed | Deployment-level quota only; no per-user limit **[confirm]** |
| Resilience (backend pool, breaker, retry) | **Native** backend pools with priority and circuit breaker | Library code in each service | Available | Not applicable (single endpoint per deployment) |
| System API facade and legacy RM header | **Same product**, signed header policy | Each service integrates each system | Not covered; second platform needed | Not covered |
| Residency | In-region instance; body logging off; semantic cache off | In-region | In-region | In-region |
| Latency | Adds one hop inside the VNet: tens of ms **[measure in pilot]** | None added | One hop | None added |
| Cost | **Highest fixed cost**: Premium tier per unit, needed for VNet injection and zones; qualitatively the largest single platform line at pilot scale **[confirm in pricing calculator, and whether Premium v2 fits]** | Lowest | Compute plus engineering time | Lowest |
| Operability | Managed service; policies as code; Bank team experienced **[confirm]** | Spread across services | We patch, scale and secure it | Simple |
| Lock-in | Moderate: APIM policy XML | Low | Low to moderate | High to Foundry endpoint behaviour |
| Team skills | Integration team knows APIM **[confirm]** | Familiar | New component to learn | Familiar |
| Time to pilot | Good if the Bank's APIM landing pattern exists; Premium provisioning can take hours | Fastest | Slowest | Fast |
| Evolvability (model swap, PTU spillover, A/B) | **Strong.** Routing in one place | Code change in each service | Strong | Weak |

## Decision

Adopt **option 1**. One **APIM Premium** instance, **internal VNet mode**, in UAE North, with:

- **AI gateway** product: `validate-jwt` on caller workload identities; managed identity to Azure OpenAI;
  `llm-token-limit` per RM and per use case; `llm-emit-token-metric` (metrics to Application Insights, shown in
  Grafana); backend pool with priority, circuit breaker and `retry`; timeout from the propagated deadline header;
  request and response **body logging off** (body bytes 0); **semantic cache off** (C8).
- **System API facade** product: one API per system of record; `validate-jwt`; per-consumer quotas and rate limits
  (C14); for legacy systems, the RM `oid` added as an APIM-signed header (plan §3.4 step 5); routing over
  ExpressRoute, and to the news feed through the firewall allow-list.
- Sizing: **2 units across zones from the pilot**, 3 at scale **[confirm unit rules and the zone-redundant SLA,
  plan §10 item 6]** (plan §3.3, v2.2).
- Per-RM token limit keyed on an `x-rm-oid` header that APIM accepts only from the orchestrator's workload identity,
  plus a per-caller ceiling (plan §3.3, v2.1; `02-lld.md` §7.1).

## Consequences

**Positive**
- One enforcement point for model access, token budgets and cost metrics (C10); services hold no model keys.
- Backend pools make the Phase 2 PTU plus Standard spillover a configuration change (ADR-004).
- System-of-record access is uniform, quota-protected and auditable; legacy systems still receive the RM identity.

**Negative**
- APIM Premium is a high fixed cost for a 30-RM pilot and provisioning and scaling are slow (tens of minutes to
  hours).
- The gateway is on every critical path; 2 zone-spread units from the pilot remove the single-zone dependency (the
  earlier 1-unit pilot was a conflict with plan §3.1, `01-hld.md` §15 item 2, resolved in plan v2.2) at the price
  of a second Premium unit in the pilot.
- Policy XML is a specialised skill and harder to unit-test than code.
- `llm-token-limit` sees only the calling workload identity unless the RM identity is supplied; resolved with the
  orchestrator-only `x-rm-oid` header (cost control, not access control) and a per-caller ceiling.

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| APIM outage stops all use cases | 2 zone-spread units from the pilot; APIOps rollback for bad policies; degraded mode cannot help here, so APIM is a hard dependency in the composite availability (`04` §9) |
| Body logging or diagnostic settings accidentally capture prompts | Azure Policy denying non-zero body bytes; CI test on exported APIM configuration (plan §3.3 Telemetry) |
| Token quota semantics differ across units or reset unexpectedly | Confirm with Microsoft (§10.6); orchestrator also enforces per-request token budgets locally (plan §5) |
| Policy misconfiguration blocks traffic after a change | APIOps pipeline with pre-prod tests; revision and release model in APIM |
| Added hop latency | Measured in the pilot against the plan §5 budget (150 ms covers edge, `web-bff` and OBO only, so APIM hops are inside the tool and generation budgets) |

## Review trigger

- APIM Premium v2 (or another tier) offers VNet injection, zones and the `llm-*` policies in UAE North at lower fixed
  cost **[confirm]**.
- Azure AI Foundry provides per-user token limits and backend pooling natively and in-region.
- Measured APIM latency exceeds 100 ms p95 per hop.
- The Bank replaces its integration platform.

## Related

- ADR-004 model hosting and tiering; ADR-008 identity (legacy RM header); ADR-014 observability (token metrics);
  ADR-017 network; ADR-020 feature flags (model pins).
- `01-hld.md` §5, §6, §11, §15; `04-interfaces-and-slas.md` (per-system quotas and timeouts); plan §3.3, C8, C10,
  C14.
