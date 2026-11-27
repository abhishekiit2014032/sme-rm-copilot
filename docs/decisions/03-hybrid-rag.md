# 03. Hybrid RAG

Status: Accepted · Date: 27 Nov 2026

## Context
Policy answers hinge on exact clause numbers, limits, amounts and product codes, as well as meaning. Policies change, and only current, effective, cleared clauses may be used. A weak match must not become a confident answer.

## Decision
- Ingest via CDC: parse (tables and numbers intact), PII scan, chunk by clause, embed and index with metadata.
- Metadata per clause: document, clause number, version, effective date, product, segment, clearance.
- Query: metadata filter, then sparse BM25 plus dense vector search, then rerank.
- Retrieval gate: if the best clause is below the relevance threshold, decline; no model call.
- Only policy is indexed; customer data is read live, never copied.

## Trade-offs
| Gain | Cost | How we contain the cost |
|---|---|---|
| Exact terms and meaning both found | Two indexes plus a reranker to run and tune | Single managed service with both built in |
| Current clauses only | Ingestion pipeline to operate | CDC re-embeds only changed clauses |
| Declines instead of guessing | Some valid questions declined | Decline rate monitored; weak evidence goes to the credit desk |

## Alternatives considered
- Vector-only search: misses exact numbers, clause references and codes.
- Fine-tuning on policy: stale on every policy change, no citations, no customer data used for training anyway.

## Where in code
- `src/copilot/execution/retrieval.py`
- `src/copilot/state/ingestion.py`
- `src/copilot/execution/mcp_servers/` (policy-search)

## Azure implementation
Azure AI Search hybrid query (BM25 + vector, filters, semantic ranker) with Azure OpenAI embeddings, fed by policy library change events.
