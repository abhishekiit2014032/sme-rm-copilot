"""Diagram box: Knowledge ingestion (lane C).
Proves a CDC change event is parsed into one chunk per clause with version metadata, scanned for PII,
embedded, indexed (replacing the old content) and used to invalidate the semantic cache.
"""
from __future__ import annotations

from copilot.contracts import Trace
from copilot.execution.retrieval import ClauseIndex
from copilot.state.ingestion import ChangeEvent, IngestionPipeline
from mocks.llm.scripted_llm import hash_embedding

DOC = """---
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
---
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


def pipeline(invalidated: list[str] | None = None) -> tuple[IngestionPipeline, ClauseIndex]:
    index = ClauseIndex()
    log = invalidated if invalidated is not None else []

    def invalidate(doc_id: str) -> int:
        log.append(doc_id)
        return 2

    return IngestionPipeline(index, lambda text, trace: hash_embedding(text), invalidate), index


def test_one_chunk_per_clause_with_version_and_status_metadata():
    ingest, index = pipeline()
    report = ingest.on_change(ChangeEvent(source="demo.md", content=DOC), Trace())
    assert report.clauses_indexed == 3 and report.versions == ["2026.2", "2025.1"]
    statuses = {(c.clause, c.version): c.status for c in index.all_clauses()}
    assert statuses == {("1.1", "2026.2"): "current", ("1.2", "2026.2"): "current",
                        ("1.1", "2025.1"): "superseded"}


def test_clause_level_clearance_overrides_document_clearance():
    ingest, index = pipeline()
    ingest.on_change(ChangeEvent(source="demo.md", content=DOC), Trace())
    clearances = {c.clause: c.clearance for c in index.all_clauses() if c.version == "2026.2"}
    assert clearances == {"1.1": "internal", "1.2": "restricted"}


def test_pii_in_policy_text_is_masked_before_indexing():
    ingest, index = pipeline()
    report = ingest.on_change(ChangeEvent(source="demo.md", content=DOC), Trace())
    assert report.pii_masked == 2
    text = " ".join(c.text for c in index.all_clauses())
    assert "policy.desk@bank.example" not in text and "<EMAIL_1>" in text


def test_every_clause_is_embedded():
    ingest, index = pipeline()
    ingest.on_change(ChangeEvent(source="demo.md", content=DOC), Trace())
    assert all(len(c.embedding) == 256 for c in index.all_clauses())


def test_change_event_replaces_the_document_and_invalidates_its_cache_entries():
    invalidated: list[str] = []
    ingest, index = pipeline(invalidated)
    ingest.on_change(ChangeEvent(source="demo.md", content=DOC), Trace())
    update = DOC.split("---\ndoc_id: demo-policy\ntitle: Demo Policy\nversion: 2025.1")[0]
    report = ingest.on_change(ChangeEvent(source="demo.md", content=update), Trace())
    assert len(index.all_clauses()) == 2
    assert invalidated == ["demo-policy", "demo-policy"] and report.cache_entries_invalidated == 2


def test_ingestion_spans_are_lane_c_and_touch_index_and_cache():
    ingest, _ = pipeline()
    trace = Trace()
    ingest.on_change(ChangeEvent(source="demo.md", content=DOC), trace)
    by_component = {s.component.value: s.lane.value for s in trace.spans}
    assert by_component["Knowledge ingestion"] == "C"
    assert {"Vector + keyword index", "Semantic cache"} <= set(by_component)


def test_startup_ingests_the_whole_policy_library(copilot):
    docs = {c.doc_id for c in copilot.retriever.index.all_clauses()}
    assert docs == {"checklists", "credit-policy", "fees", "product-terms"}
