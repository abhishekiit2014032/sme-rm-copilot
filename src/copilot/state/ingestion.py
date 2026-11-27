"""Diagram box: Knowledge ingestion (lane C).
On a change event (CDC) from the policy library: parse, PII scan, chunk by clause, embed, index, invalidate cache.
Mocked: change events are raised by the demo instead of a CDC stream; documents are markdown files.
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Callable

from pydantic import BaseModel

from copilot.contracts.trace import Component, Trace
from copilot.execution.retrieval import ClauseIndex, PolicyClause
from copilot.request_path.input_guardrails import mask_pii

VERSION_BLOCK = re.compile(r"^---\n(?P<meta>.*?)\n---\n(?P<body>.*?)(?=^---\n|\Z)", re.S | re.M)
CLAUSE_HEADING = re.compile(r"^## §(?P<clause>[\d.]+) (?P<heading>.+?)(?: \{clearance: (?P<clearance>\w+)\})?$",
                            re.M)


class ChangeEvent(BaseModel):
    """What the CDC stream delivers: the full new content of one policy document."""

    source: str        # file name in the policy library
    content: str


class IngestionReport(BaseModel):
    doc_id: str
    versions: list[str]
    clauses_indexed: int
    pii_masked: int
    cache_entries_invalidated: int


class IngestionPipeline:
    def __init__(self, index: ClauseIndex, embed: Callable[[str, Trace], list[float]],
                 invalidate_cache: Callable[[str], int]) -> None:
        self._index, self._embed, self._invalidate_cache = index, embed, invalidate_cache

    def on_change(self, event: ChangeEvent, trace: Trace) -> IngestionReport:
        with trace.span(Component.INGESTION, f"CDC event: {event.source}") as span:
            clauses = self._parse_and_chunk(event, trace)
            clauses, pii_masked = self._pii_scan(clauses, trace)
            for clause in clauses:
                clause.embedding = self._embed(f"{clause.heading}. {clause.text}", trace)
            doc_id = clauses[0].doc_id
            self._index.replace_document(doc_id, clauses)
            invalidated = self._invalidate_cache(doc_id)
            versions = sorted({c.version for c in clauses}, reverse=True)
            span.detail = (f"{doc_id}: {len(clauses)} clauses indexed, versions {', '.join(versions)}, "
                           f"cache entries invalidated: {invalidated}")
            trace.event(Component.INDEX, f"index refreshed: {doc_id}", detail=f"{len(clauses)} clauses")
            trace.event(Component.SEMANTIC_CACHE, f"invalidate: {doc_id}", detail=f"entries removed: {invalidated}")
            return IngestionReport(doc_id=doc_id, versions=versions, clauses_indexed=len(clauses),
                                   pii_masked=pii_masked, cache_entries_invalidated=invalidated)

    def ingest_directory(self, folder: Path, trace: Trace) -> list[IngestionReport]:
        return [self.on_change(ChangeEvent(source=p.name, content=p.read_text(encoding="utf-8")), trace)
                for p in sorted(folder.glob("*.md"))]

    # ---- steps ----------------------------------------------------------------------------
    def _parse_and_chunk(self, event: ChangeEvent, trace: Trace) -> list[PolicyClause]:
        """Each version block has front matter; each '## §x.y Heading' starts one clause chunk."""
        with trace.span(Component.INGESTION, "parse + chunk by clause") as span:
            clauses: list[PolicyClause] = []
            for block in VERSION_BLOCK.finditer(event.content):
                meta = dict(line.split(": ", 1) for line in block["meta"].splitlines() if ": " in line)
                clauses += _chunk(block["body"], meta)
            span.detail = f"{len(clauses)} clause chunks"
            return clauses

    def _pii_scan(self, clauses: list[PolicyClause], trace: Trace) -> tuple[list[PolicyClause], int]:
        """Policy text should hold no personal data; anything found is masked before indexing."""
        with trace.span(Component.INGESTION, "PII scan") as span:
            masked = 0
            for clause in clauses:
                clause.text, vault = mask_pii(clause.text)
                masked += len(vault)
            span.detail = f"{masked} PII items masked"
            return clauses, masked


def _chunk(body: str, meta: dict[str, str]) -> list[PolicyClause]:
    headings = list(CLAUSE_HEADING.finditer(body))
    clauses = []
    for i, match in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(body)
        clauses.append(PolicyClause(
            doc_id=meta["doc_id"], doc_title=meta["title"], clause=match["clause"], heading=match["heading"],
            text=body[match.end():end].strip(), version=meta["version"],
            effective_from=date.fromisoformat(meta["effective_from"]), status=meta["status"],
            clearance=match["clearance"] or meta["clearance"],
        ))
    return clauses
