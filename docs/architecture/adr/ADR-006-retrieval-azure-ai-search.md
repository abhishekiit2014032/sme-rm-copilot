# ADR-006: Retrieval on Azure AI Search with hybrid query and semantic ranker

| Field | Value |
|---|---|
| Status | Accepted (semantic ranker availability in UAE North **[confirm]**; fallback defined) |
| Date | 26 Nov 2026 |
| Deciders | Solution architecture, data platform team, policy content owners |
| Plan references | §2 UC2, §3.2 `ingestion-worker` and `tool-gateway`, §3.3 Retrieval and Ingestion, §4.3, §5 (UC2 ≤ 7 s, retrieval 700 ms, quality gates), §10 item 5 |

## Context

UC2 answers policy and product questions only from approved documents, with citations, and declines when evidence is
weak (plan §4.3). Retrieval quality drives the quality gates more than the model does: groundedness ≥ 0.95 and
citation accuracy ≥ 0.95 on a ≥ 300-item golden set (plan §5).

Forces:

- **Query shape**: RM questions mix exact tokens (product codes, clause numbers, acronyms) with paraphrase. Pure
  vector search misses exact identifiers; pure keyword search misses paraphrase. Hybrid search plus a reranker is
  the established pattern (deck "Why hybrid").
- **Mandatory filters before ranking**: `status = current`, `effective_from <= today`, `clearance <= RM clearance`
  (plan §4.3). Filtering is a security control (clearance) and a correctness control (no withdrawn policy).
- **Languages**: English first, Arabic evaluated in the pilot and GA in Phase 2; Arabic needs a proper analyzer for
  the keyword leg.
- **Latency**: embed plus search plus rerank within ≈ 700 ms p95 (plan §5 budget).
- **Corpus**: non-customer documents (policies, product guides, circulars, authority matrix); modest size,
  thousands to tens of thousands of clause-level chunks (illustrative).
- **Availability**: read SLA requires ≥ 2 replicas; index must be rebuildable from stored chunks and embeddings
  (plan §3.2 `ingestion-worker`).
- **Operations**: private endpoint, Entra auth, no local keys (ADR-017).

## Options considered

1. **Azure AI Search (Standard)**: hybrid BM25 plus vector with reciprocal rank fusion, **semantic ranker** for
   reranking, filterable fields for status, effective date and clearance, `ar.microsoft` analyzer for Arabic.
2. **Azure Cosmos DB for NoSQL vector search** (DiskANN-based vector index) with its full-text and hybrid search
   capabilities **[confirm GA status and Arabic support]**, reranking done by us.
3. **Azure Database for PostgreSQL Flexible Server with pgvector** (plus `tsvector` full-text search), hybrid fusion
   and reranking done by us.
4. **Azure AI Search without the semantic ranker, plus a self-hosted cross-encoder reranker on AKS** (the fallback if
   the semantic ranker is not offered in UAE North).

## Trade-off analysis

| Criterion | 1 AI Search + semantic ranker | 2 Cosmos DB vector | 3 PostgreSQL pgvector | 4 AI Search + own reranker |
|---|---|---|---|---|
| Retrieval quality (hybrid + rerank) | **Strongest out of the box**: built-in hybrid fusion and a managed reranker | Moderate: vector strong; hybrid and full-text newer; reranker ours | Moderate: good vector and full-text; fusion and reranker ours | Strong, depends on the reranker we choose and tune |
| Arabic keyword search | **Good**: `ar.microsoft` analyzer | **Uncertain** full-text language coverage **[confirm]** | Moderate: Arabic text search configuration **[confirm]** | Good |
| Filtering before ranking (security trimming) | Native filters on the query, applied with vector search (pre-filter mode) | Native `WHERE` with vector search | Native `WHERE`; filtered ANN recall needs tuning | Native |
| Latency (≈ 700 ms incl. embedding) | Good; semantic ranker adds latency **[measure]** | Good | Good at this corpus size | Adds a model hop on AKS (GPU or CPU) |
| Residency | In-region; semantic ranker region availability **[confirm]** | In-region | In-region | In-region |
| Availability | Read SLA with 2 replicas, read-write with 3; zones where supported **[confirm zones in UAE North]** | Zone-redundant | Zone-redundant HA option | Same as 1, plus our reranker deployment |
| Cost | Fixed per search unit (replicas × partitions) plus semantic ranker usage; qualitatively moderate **[confirm in pricing calculator]** | Low incremental if Cosmos DB already used (it is, ADR-019) | Moderate: a new server to run | AI Search units plus reranker compute |
| Operability | Managed service, new component for the team | **Reuses** an existing service | New engine, DBA skills | Two components |
| Lock-in | Moderate (index schema, query API) | Moderate | Low | Moderate |
| Team skills | Familiar pattern; documented | Familiar store, new feature | Common skill set | Needs ML serving skills |
| Time to pilot | **Fast** | Moderate: we build fusion and reranking | Moderate | Slowest |
| Blast radius | Separate from transactional state | **Shares** the store holding sessions, drafts and the jti replay store; noisy-neighbour and security blast radius | Separate | Separate |

## Decision

Adopt **option 1: Azure AI Search, Standard tier**, private endpoint, Entra auth, local keys disabled.

- Index per environment with clause-level chunks, heading path, document id, version, `status`, `effective_from`,
  `clearance`, product and language fields; vector field from `embed` (text-embedding-3-large).
- Query (`tool-gateway.search_policies`): query embedding via the APIM AI gateway; hybrid BM25 plus vector query
  with mandatory filters `status = current`, `effective_from <= today`, `clearance <= RM clearance`; semantic
  ranker; score threshold in the orchestrator decides decline (plan §4.3).
- `ar.microsoft` analyzer on Arabic fields.
- **2 replicas in the pilot (read SLA), 3 in production** (plan §3.3).
- Rebuild from stored chunks and embeddings held by `ingestion-worker` in Blob (no re-parsing needed).
- **Fallback (option 4)** if the semantic ranker is not offered in UAE North: hybrid with fusion only, then evaluate
  a self-hosted cross-encoder on AKS against the golden set; the threshold is recalibrated either way.

## Consequences

**Positive**
- Best retrieval quality for the least custom code; quality is measurable on the golden set from the first sprint.
- Filters as part of the query make clearance and currency hard controls, not prompt instructions.
- Separate from the transactional store, so a retrieval incident cannot touch sessions, drafts or replay protection.

**Negative**
- One more managed service with a fixed monthly floor that does not scale down for the pilot.
- Semantic ranker behaviour is a vendor model we do not control; changes to it can shift scores and thresholds.
- Index schema changes require reindexing (mitigated by stored chunks and alias swap).

**Risks and mitigations**

| Risk | Mitigation |
|---|---|
| Semantic ranker or zone support not available in UAE North | Fallback option 4; discovery item §10.5 |
| Clearance metadata wrong at ingestion leaks a restricted document | Clearance comes from the DMS record, not inferred; ingestion validation rejects documents without clearance; entitlement-leak tests in the golden set (gate = 0) |
| Score drift after a ranker or embedding change | Threshold calibrated per model version; nightly regression (ADR-018) |
| Arabic retrieval quality below gate | Arabic golden subset; analyzer and chunking tuned; Arabic GA only in Phase 2 |

## Review trigger

- Semantic ranker unavailable in UAE North at build, or ranker changes break the golden-set gates.
- Cosmos DB hybrid search with Arabic full-text and a managed reranker becomes GA in UAE North, making option 2 a
  consolidation opportunity.
- Corpus or query volume grows by an order of magnitude (partition and tier review).

## Related

- ADR-007 customer data live, not indexed (why only documents are indexed); ADR-004 model tiering (`embed`);
  ADR-011 guardrails (decline and groundedness); ADR-018 evaluation; ADR-019 Cosmos DB state store.
- `01-hld.md` §8.3, §9.5, §11; `02-lld.md` (index schema); plan §3.3, §4.3.
