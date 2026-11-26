# ADR-004: Model hosting and tiering on Azure OpenAI in Azure AI Foundry

| Field | Value |
|---|---|
| Status | Accepted for the pilot; the PTU decision is **Proposed** and taken at the post-pilot gate |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, model risk management, information security, procurement |
| Plan references | §3.1, §3.3 Models, §3.5, §5 (latency budget, token budget, quality gates), §6 C2, C3, C10, C12, C13, C17, §10 items 1, 2 |

## Context

The copilot needs three model capabilities:

- **Generation** with high writing quality: UC1 brief wording (≈ 200 output tokens), UC2 cited answers (≈ 150 output
  tokens), both on the critical latency path (plan §5).
- **Fast, cheap language tasks**: UC2 intent and query rewrite, UC3 JSON extraction (≈ 400 output tokens).
- **Embeddings** for policy retrieval in English and Arabic (ADR-006).

Constraints and forces:

- **Residency** (ADR-001): inference must stay in UAE North; Microsoft abuse monitoring must be modified before real
  data (plan §3.5, C17); `store=false` on every call.
- **Availability of models**: only models offered as **Regional** deployments in UAE North are eligible. The plan
  names models by availability: "the latest GA GPT-class model offered as Regional Standard in UAE North at build
  time" **[confirm model, version, retirement date]** (plan §3.1, §10.1).
- **Latency**: generation is the largest item in every budget (UC1 1,000 ms time to first token plus 3,300 ms
  output). Pay-per-token Standard deployments have no latency guarantee; Provisioned (PTU) deployments give
  reserved throughput.
- **Volume**: peak 0.5 requests/s, about 5,400 requests/day at scale, ≤ 12k input and 1.5k output tokens per
  request. As an illustrative order of magnitude only, that is tens of millions of tokens per day at scale, small
  enough that reserved capacity may not pay back **[confirm with measured pilot volumes and the pricing
  calculator]**.
- **Model lifecycle**: GA model versions retire roughly yearly (C12); behaviour changes between versions can break
  guardrail thresholds (C13) and the golden-set quality gates (plan §5).
- **Model risk**: every model version is an inventoried model under the Bank's MRM standard (C23).

## Options considered

1. **Azure OpenAI in Azure AI Foundry, Regional deployments, tiered**: `gen-large` (frontier GPT-class),
   `gen-small` (small GPT-class), `embed` (text-embedding-3-large); **Regional Standard in the pilot**; **PTU as a
   post-pilot gate** on measured tokens; **versions pinned**, auto-upgrade off.
2. **Global Standard or Data Zone deployments** of the same models (wider catalogue, higher quota, Microsoft-routed
   processing location).
3. **Open-weights models self-hosted on AKS GPU node pools** (for example a vLLM-style server) for some or all
   tasks.
4. **Foundry Models from other providers** (non-OpenAI models sold directly by Azure, or partner models through
   serverless API or managed compute) instead of, or alongside, Azure OpenAI.

## Trade-off analysis

| Criterion | 1 Azure OpenAI Regional, tiered | 2 Global / Data Zone | 3 Open-weights on AKS GPUs | 4 Other Foundry Models |
|---|---|---|---|---|
| Residency | **Strong.** Regional processing in UAE North; enterprise data terms; `store=false` | **Fails** for customer data: processing may occur outside the UAE; no UAE data zone known **[confirm]** | **Strongest**: weights and inference inside the Bank's cluster | **Uncertain**: many models are offered only as Global or in few regions; partner terms differ **[confirm per model]** |
| Quality (EN; AR) | **Strong** for the frontier tier; Arabic evaluated in the pilot | Same models, sometimes newer | **Weaker** today at comparable cost for drafting and cited answers; acceptable for extraction **[validate on golden set]** | Varies; some strong Arabic-capable models exist, availability in UAE North unknown **[confirm]** |
| Latency | Moderate, no guarantee on Standard; PTU improves predictability | Often better (more capacity) | Depends on GPU SKU and our tuning; predictable once sized | Varies |
| Capacity and quota | **Constrained** by regional quota in one region | Highest | Bounded by GPU quota and availability in UAE North **[confirm GPU SKUs]** | Varies |
| Cost | Pay per token in the pilot; PTU only if utilisation justifies it **[confirm in pricing calculator]** | Lowest per token | **High fixed cost**: GPU nodes run whether used or not, plus engineering; uneconomic at 0.5 requests/s | Varies; often per token |
| Operability | Managed; version pins and retirement runbook | Managed | **Heavy**: model serving, patching, scaling, safety filters, evaluation of every weight update | Managed, but another vendor relationship and terms |
| Safety controls | Built-in content filters with Prompt Shields; abuse monitoring modifiable | Same | We build or host all filters | Content filter support varies by model **[confirm]** |
| Lock-in | Moderate: OpenAI API shape, mitigated by APIM and our client abstraction | Same | Low | Moderate |
| Model risk management | One vendor family, documented model cards, in-region evaluation | Same plus routing uncertainty | Full validation burden on the Bank | Additional vendor due diligence |
| Time to pilot | **Fastest** | Fast, but blocked at residency review | Slowest | Slow: per-model due diligence |

## Decision

Adopt **option 1**.

- **Hosting**: one Azure AI Foundry resource and project per environment, with Azure OpenAI deployments of type
  **Regional Standard** in UAE North for the pilot. No Global, Data Zone, Assistants or stored-response features;
  `store=false` on every call (plan §3.5).
- **Tiering**: `gen-large` for UC1 wording and UC2 answers; `gen-small` for routing, query rewrite and UC3
  extraction; `embed` = text-embedding-3-large for retrieval. The model behind each alias is "the latest GA
  GPT-class model offered as Regional Standard in UAE North at build time" **[confirm]**.
- **Version pinning**: explicit model version per deployment; **auto-upgrade off**; version pins held in App
  Configuration (ADR-020) and recorded in every audit event's lineage; upgrades only through the eval gate and the
  90-day upgrade runbook (C12, ADR-018).
- **Safety settings**: content filters with Prompt Shields (jailbreak and indirect attack) on; thresholds tuned on
  the golden set with the false-positive rate as a release gate (C13).
- **PTU gate (post-pilot, Proposed)**: move `gen-large` (and only then others) to Regional Provisioned if, on
  measured pilot data, (a) Standard p95 generation latency breaks the plan §5 budget, or (b) sustained token volume
  makes PTU cheaper than Standard, and (c) the minimum regional PTU size and lead time are acceptable **[confirm
  §10.1]**. If adopted, APIM routes PTU first with Standard spillover (ADR-003).
- **Open-weights** remain an option for high-volume extraction in Phase 3, entered only through APIM and the same
  eval gate.

## Consequences

**Positive**
- Residency and data-handling position is clear and matches ADR-001.
- Tiering keeps cost and latency down: the two high-volume or latency-sensitive steps that do not need frontier
  quality (rewrite, extraction) use `gen-small`.
- Pins plus the eval gate make model changes deliberate, reviewable events for model risk.

**Negative**
- Model choice lags other regions (C2); the copilot may run on an older model than competitors.
- Regional Standard capacity is shared and quota-limited; latency variance is outside our control in the pilot.
- Pinned versions still retire; each retirement forces an upgrade project within Microsoft's timeline.
- A single model family means a single vendor's failure modes; there is no second in-region provider for failover.

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| Required model or embedding model not offered as Regional in UAE North | Discovery item §10.1 before build; eval decides if the available model meets gates; degraded mode (C3) |
| Regional quota exhausted or throttled (429) | APIM per-RM and per-use-case limits; backend pool; quota increase requests early; degraded mode if < 4 s remain |
| Latency p95 breaks budget | Measure from day one; PTU gate; shorter prompts; `gen-small` where quality allows |
| Version retired before successor passes evals | Model inventory with retirement dates; start the upgrade runbook 90 days ahead; golden set ready |
| Content filters block normal banking language | Golden-set tuning; false-positive rate ≤ 2% gate (plan §5); approved filter configuration **[confirm configurability for the Bank]** |
| Modified abuse monitoring not approved | Go-live gate: no real data until approved (C17) |

## Review trigger

- Pilot exit: PTU decision on measured latency and volume.
- A new model is offered as Regional in UAE North with materially better quality on the golden set, or the pinned
  model's retirement date is announced.
- A data zone or sovereign offering covering the UAE becomes available.
- Open-weights quality on the golden set reaches the gates for extraction at a lower total cost.

## Related

- ADR-001 region and residency; ADR-003 APIM AI gateway; ADR-011 guardrails; ADR-015 degraded mode; ADR-018
  evaluation and model risk; ADR-020 feature flags (version pins).
- `01-hld.md` §8, §10, §11; `05-nfr-scenarios-and-dr.md` (capacity, cost, MRM); plan §3.1, §3.3, §3.5, C12.
