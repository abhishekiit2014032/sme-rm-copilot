"""Diagram box: Knowledge ingestion (lane C).
Proves a CDC change event is parsed into one chunk per clause with version metadata, scanned for PII,
embedded, indexed (replacing the old content) and used to invalidate the semantic cache.
"""
from __future__ import annotations

from copilot.contracts import Trace
from copilot.execution.retrieval import ClauseIndex
from copilot.state.ingestion import ChangeEvent, IngestionPipeline
from mocks.llm.scripted_llm import hash_embedding

CURRENT = """---
doc_id: demo-policy
title: Demo Policy
version: 2026.2
effective_from: 2026-01-01
status: current
clearance: internal
---
## §1.1 Contact
Questions go to policy.desk@bank.example or +971 4 123 4567.

## §1.2 Secret rule {clearance: restricted}
Only the credit desk sees this.
"""
SUPERSEDED = """---
doc_id: demo-policy
title: Demo Policy
version: 2025.1
effective_from: 2025-01-01
status: superseded
clearance: internal
---
## §1.1 Contact
Old contact rule.
"""


def ingest(*contents: str) -> tuple[ClauseIndex, list[str], list]:
    index, invalidated, reports = ClauseIndex(), [], []

    def invalidate_cache(doc_id: str) -> int:
        invalidated.append(doc_id)
        return 2                                  # pretend two cached answers cited the document

    pipeline = IngestionPipeline(index, lambda text, trace: hash_embedding(text), invalidate_cache)
    for content in contents:
        reports.append(pipeline.on_change(ChangeEvent(source="demo.md", content=content), Trace()))
    return index, invalidated, reports


def test_each_clause_becomes_one_embedded_chunk_with_version_status_and_clearance():
    index, _, reports = ingest(CURRENT + SUPERSEDED)
    assert reports[0].versions == ["2026.2", "2025.1"]
    chunks = {(c.clause, c.version): (c.status, c.clearance) for c in index.all_clauses()}
    assert chunks == {("1.1", "2026.2"): ("current", "internal"), ("1.2", "2026.2"): ("current", "restricted"),
                      ("1.1", "2025.1"): ("superseded", "internal")}
    assert all(len(c.embedding) == 256 for c in index.all_clauses())


def test_pii_in_policy_text_is_masked_before_indexing():
    index, _, reports = ingest(CURRENT)
    text = " ".join(c.text for c in index.all_clauses())
    assert reports[0].pii_masked == 2 and "policy.desk@bank.example" not in text and "<EMAIL_1>" in text


def test_a_change_event_replaces_the_document_and_invalidates_its_cache_entries():
    index, invalidated, reports = ingest(CURRENT + SUPERSEDED, CURRENT)
    assert len(index.all_clauses()) == 2
    assert invalidated == ["demo-policy", "demo-policy"] and reports[-1].cache_entries_invalidated == 2
