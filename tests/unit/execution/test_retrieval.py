"""Diagram box: Retrieval / RAG and Vector + keyword index (lane B).
Proves hybrid search: BM25 finds exact clause numbers and figures, the metadata filter drops superseded,
not-yet-effective and restricted clauses, the reranker orders by score and the gate declines weak evidence.
"""
from __future__ import annotations

from datetime import date

from copilot.contracts import Trace
from copilot.execution.retrieval import PolicyClause, bm25_scores, tokenize

RM_CLEARANCES = {"public", "internal"}


def search(copilot, query: str, clearances: set[str] = RM_CLEARANCES):
    return copilot.retriever.search(query, clearances, Trace())


def clause_ids(result) -> list[str]:
    return [f"{s.clause.doc_id}#{s.clause.clause}" for s in result.clauses]


def test_tokenizer_keeps_clause_numbers_and_decimal_figures_whole():
    assert "4.2" in tokenize("see §4.2") and "2.5" in tokenize("AED 2.5m")


def test_bm25_ranks_the_only_document_with_the_exact_clause_number_first():
    documents = [tokenize(t) for t in ("collateral under §4.6 for the excess", "unsecured cap per borrower",
                                       "acceptable collateral list")]
    scores = bm25_scores(tokenize("4.6"), documents)
    assert scores[0] > 0 and scores[1] == scores[2] == 0


def test_bm25_exact_figure_query_finds_the_fixed_deposit_clause(copilot):
    result = search(copilot, "business fixed deposit minimum 100,000")
    assert clause_ids(result)[0] == "product-terms#3.2"


def test_search_returns_current_version_not_the_superseded_one(copilot):
    result = search(copilot, "What is the unsecured lending cap for one borrower?")
    top = result.clauses[0].clause
    assert (top.doc_id, top.clause, top.version) == ("credit-policy", "4.2", "2026.2")
    assert all(s.clause.status == "current" for s in result.clauses)
    assert "1.5m" not in " ".join(s.clause.text for s in result.clauses)


def test_metadata_filter_counts_superseded_and_restricted_clauses_as_filtered(copilot):
    result = search(copilot, "unsecured lending cap")
    assert result.filtered_out == 3   # two superseded 2025.1 clauses and the restricted §9.1


def test_clause_not_yet_effective_is_filtered_out(copilot):
    index = copilot.retriever.index
    future = PolicyClause(doc_id="future", doc_title="Future Policy", clause="1.1", heading="Crypto mining lending",
                          text="Lending to cryptocurrency mining companies is allowed.", version="2027.1",
                          effective_from=date(2027, 1, 1), status="current", clearance="internal",
                          embedding=[0.0] * 256)
    index.replace_document("future", [future])
    assert "future#1.1" not in clause_ids(search(copilot, "cryptocurrency mining companies lending"))


def test_restricted_clause_is_visible_only_with_restricted_clearance(copilot):
    query = "pricing floors minimum margins by risk grade"
    assert "credit-policy#9.1" not in clause_ids(search(copilot, query))
    assert clause_ids(search(copilot, query, RM_CLEARANCES | {"restricted"}))[0] == "credit-policy#9.1"


def test_rerank_returns_clauses_in_descending_score_order(copilot):
    result = search(copilot, "What collateral is acceptable for a working capital limit above the cap?")
    scores = [s.score for s in result.clauses]
    assert scores == sorted(scores, reverse=True) and len(scores) <= 3


def test_retrieval_gate_declines_weak_evidence(copilot):
    result = search(copilot, "What is our policy on lending to cryptocurrency mining companies?")
    assert not result.passed_gate and result.clauses == []
    assert result.best_score < result.threshold


def test_retrieval_span_records_scores_for_the_trace(copilot):
    trace = Trace()
    copilot.retriever.search("letter of credit amendment fee", RM_CLEARANCES, trace)
    span = next(s for s in trace.spans if s.component.value == "Retrieval / RAG")
    assert span.attrs["best_score"] >= span.attrs["threshold"]
    assert "Vector + keyword index" in {s.component.value for s in trace.spans}
