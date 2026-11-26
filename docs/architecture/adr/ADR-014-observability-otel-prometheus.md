# ADR-014: Observability with OpenTelemetry, Application Insights and managed Prometheus, metadata only

| Field | Value |
|---|---|
| Status | Accepted |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, platform operations, InfoSec, data protection officer |
| Critical issues | C5 (customer data in prompts and completions), C10 (cost runaway), C15 (misuse detection) |
| Phase | 1 |

## Context

Operations need end-to-end traces across `web-bff`, `orchestrator`, `tool-gateway`, APIM and the systems of
record; service and AI metrics (latency per step against the §5 budget, tokens, cost, guardrail hits, declines,
breaker state); and logs for incidents. The forces:

- **Telemetry must not carry content (C5).** Prompts, completions, tool outputs and RM notes contain customer
  data. Several defaults now capture GenAI content: OpenTelemetry GenAI instrumentation can record messages,
  Foundry tracing can record content, APIM diagnostic settings can log request and response bodies, and exception
  messages often embed payload fragments. Content belongs only in the encrypted audit content store (ADR-013).
- **Residency.** Telemetry stays in UAE North (Log Analytics workspace, Azure Monitor workspace, Managed Grafana)
  **[confirm Managed Grafana and Azure Monitor workspace availability in UAE North]**.
- **Private network.** Private AKS cluster, no public ingestion: Azure Monitor Private Link Scope (AMPLS) and data
  collection endpoints (§3.3 Metrics row) **[confirm AMPLS mode and DCE requirements for managed Prometheus]**.
- **Retention.** Telemetry 90 days, metrics 18 months (§7).
- **The prototype already exposes `/metrics` and propagates traces** (workstream C acceptance criteria), so the
  production design should be the same shape.
- **Deck alignment.** The stack slide recommends "OpenTelemetry traces plus a self-hosted eval tool so prompts and
  outputs stay inside the bank". This ADR keeps OpenTelemetry and keeps content out of telemetry entirely, which
  makes a content-bearing observability tool unnecessary.

## Options considered

1. **OpenTelemetry → Application Insights (traces, logs) and Azure Monitor managed Prometheus + Azure Managed
   Grafana (metrics), metadata only, enforced by explicit settings, Azure Policy and a CI test.**
2. **Foundry tracing with content recording on**, using the Foundry portal for prompt-level debugging.
3. **Self-hosted LLM observability (Langfuse or Arize Phoenix) on AKS**, with prompts and completions stored
   in-cluster.
4. **Third-party SaaS APM** (an observability vendor with LLM tracing).

## Trade-off analysis

Scale: ++ strong, + adequate, 0 neutral, - weak, -- unacceptable.

| Criterion | 1 OTel + App Insights + Prometheus, metadata only | 2 Foundry tracing with content | 3 Self-hosted Langfuse / Phoenix | 4 SaaS APM |
|---|---|---|---|---|
| Customer data exposure | ++ none by design; content only in the audit content store | -- a second, broadly readable copy of prompts and completions in telemetry | - content stays in the Bank but becomes a new store of customer data that needs encryption, access control, retention and erasure | -- content or metadata leaves the Bank and the country |
| Residency | + UAE North workspaces **[confirm Grafana]** | + in-region project, but content in telemetry | ++ in-cluster | -- vendor regions |
| Prompt-level debugging | - engineers cannot see prompts in traces; they replay from the audit content store under controlled access | ++ best developer experience | ++ purpose-built LLM views | + |
| End-to-end distributed tracing | ++ W3C `traceparent` from `web-bff` through APIM to every service | + for model calls; weaker for the rest of the estate | 0 LLM spans only; needs OTel for the rest anyway | ++ |
| Metrics and alerting | ++ PodMonitor scraping into managed Prometheus, Grafana dashboards, Azure Monitor alerts | 0 | - limited operational metrics | ++ |
| Operability | + managed services; AMPLS and DCE configuration to get right | ++ | - another stateful app with its own database, upgrades, backups | + vendor-run |
| Lock-in | + OTel and Prometheus are open standards; exporters can change | - Foundry-specific | + open source | - vendor-specific agents and query languages |
| Cost (qualitative) | + ingestion priced per GB; metadata keeps volume low | - content multiplies ingestion volume | 0 compute and storage on AKS | - per-host or per-GB SaaS pricing plus egress |
| Outsourcing and procurement | ++ existing Azure agreement | ++ | ++ | -- new third-party processor review |

## Decision

Adopt **option 1**, with explicit settings, each enforced by Azure Policy where a policy exists and by a CI test
otherwise.

| Setting | Value | Enforcement |
|---|---|---|
| OpenTelemetry instrumentation | Azure Monitor OpenTelemetry distro in every service; W3C trace context; sampling 100% for errors, ratio sampling for success **[tune in pilot]** | Code review, smoke test checks spans exist |
| GenAI content capture | Off (`OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=false` and the Azure SDK equivalent content recording flag set false) **[confirm current variable names for the SDK versions used]** | Helm values linted in CI; startup check refuses to start if set true |
| Foundry tracing content recording | Off | Deployment config and CI assertion |
| APIM diagnostics | Frontend and backend request and response body bytes = 0; headers allow-list only (no `Authorization`, no cookies); LLM message logging off | Azure Policy (custom) on APIM diagnostic settings; Bicep defaults |
| Exceptions and logs | Message scrubbing in a log processor; structured logs with an allow-listed field set; customer id as keyed hash only | CI test: run the synthetic suite, capture all exported telemetry, fail on any known synthetic name, ID, IBAN or note fragment |
| Span attributes | Use case, step, model deployment and version, token counts, latency, guardrail outcome codes, breaker state, deadline remaining; never text | Attribute allow-list in a span processor |
| Metrics | Each service exposes `/metrics`; `PodMonitor` objects scraped by the managed Prometheus add-on; APIM token metrics (`llm-emit-token-metric`) land in Application Insights and are shown in Grafana through the Azure Monitor data source (§3.3) | Grafana dashboards as code |
| Network | AMPLS in private-only mode for ingestion and query; data collection endpoints for the private cluster; Managed Grafana with private endpoint | Azure Policy: no public network access |
| Retention | Telemetry 90 days; metrics 18 months (§7) | Bicep |
| Alerts | Latency per use case vs §5 budget, error budget burn for the 99.0% business-hours SLO, guardrail hit spikes, decline rate, breaker open, token spend vs budget (C10), outbox age (ADR-010), audit lag (ADR-013) | Alert rules as code |

Prompt-level debugging uses the audit content store (ADR-013) through a controlled, logged break-glass access
path for named engineers during an incident, not telemetry. Security analytics for misuse (C15) run in Sentinel on
audit metadata and Entra sign-in logs, not on prompt content.

## Consequences

Positive

- No second copy of customer data in observability tooling, which keeps the DPO and outsourcing conversations
  simple and makes the "metadata only" claim testable.
- One standard (OTel, Prometheus) across the AI and non-AI parts of the estate; the prototype and production use
  the same shape.

Negative

- Engineers lose prompt-level debugging in traces. Diagnosing a bad answer requires the audit content store and an
  access request, which slows incident analysis. Mitigation: synthetic reproductions in test with content
  capture allowed (synthetic data only, §3.3 Delivery row).
- Two telemetry back ends (Application Insights for traces and logs, managed Prometheus for metrics) and two query
  languages (KQL, PromQL); dashboards join them in Grafana.
- Scrubbing is a deny-by-default allow-list; new fields need a change, which slows development slightly.

Risks and mitigations

| Risk | Mitigation |
|---|---|
| An SDK upgrade changes a default and starts capturing content | Startup self-check, CI telemetry capture test on every build, Azure Policy on APIM |
| Exception text includes payloads from a dependency | Log processor scrubbing; exceptions recorded with type and code, message replaced by a hash |
| AMPLS misconfiguration silently drops telemetry | Synthetic heartbeat metric and alert on absence |
| Deck stack slide contradicts this ADR | Update the slide (workstream D) to "OpenTelemetry, metadata only; evaluation tooling runs on synthetic or audit-store data in-region" |

## Review trigger

- A Foundry or Azure Monitor feature offers content capture into a customer-controlled, per-customer-encrypted
  store in-region.
- Incident reviews show that diagnosis time is driven by lack of content in traces.
- Managed Grafana or managed Prometheus is not available in UAE North (fall back to Azure Monitor workbooks and
  Container Insights metrics).

## Related

- Plan: §3.3 (Telemetry, Metrics, Security ops rows), §5, §6 C5, C10, C15, §7, §8 workstream C.
- ADR-011 (guardrail metrics), ADR-012 (no token map in telemetry), ADR-013 (content only in the audit store),
  ADR-015 (breaker state and deadline metrics), ADR-017 (AMPLS and private endpoints), ADR-018 (online evaluation
  uses the audit store, not telemetry), ADR-002 (AKS managed Prometheus add-on), ADR-003 (APIM diagnostics and
  token metrics).
- Code: services in `code/services/` (workstream C) expose `/metrics` and propagate `traceparent`.
