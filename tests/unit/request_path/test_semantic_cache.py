"""Diagram box: Semantic cache (lane A).
Proves the cache is off by default, stores only grounded public-policy answers (never customer data),
expires by TTL and is invalidated when knowledge ingestion re-indexes a document (CDC).
"""
from __future__ import annotations

from copilot.contracts import AgentResult, Citation, Trace
from copilot.operations.flags import Flags
from copilot.request_path.semantic_cache import SemanticCache
from copilot.state.ingestion import ChangeEvent
from demo.scenarios import FEES_UPDATE
from helpers import FEE_QUESTION, GULF_STAR, ask, components
from mocks.llm.scripted_llm import hash_embedding


def embed(text: str, trace: Trace) -> list[float]:
    return hash_embedding(text)


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


def policy_result(clearance: str) -> AgentResult:
    citation = Citation(doc_id="fees", doc_title="SME Fees and Charges", clause="1.3", heading="LC amendment",
                        version="2026.2", clearance=clearance)
    return AgentResult(agent="policy", status="ok", text="Each amendment costs AED 350 (§1.3).",
                       citations=[citation])


def test_cache_is_off_by_default_so_repeat_questions_are_answered_fresh(copilot):
    assert Flags().semantic_cache is False
    assert [ask(copilot, "rm-1", FEE_QUESTION).path for _ in range(2)] == ["fast", "fast"]


def test_only_public_policy_answers_are_stored_never_internal_or_customer_data():
    cache = SemanticCache(embed)
    brief = AgentResult(agent="brief", status="ok", text="Gulf Star Trading LLC: AED 2.0m limit")
    assert not cache.store("brief", brief, Trace())
    assert not cache.store("cap?", policy_result("internal"), Trace())
    assert cache.store(FEE_QUESTION, policy_result("public"), Trace())
    assert cache.lookup("What is the tenor of a working capital limit?", Trace()) is None
    assert cache.lookup(FEE_QUESTION, Trace()) is not None


def test_customer_actions_bypass_an_enabled_cache(cached_copilot):
    paths = [ask(cached_copilot, "rm-1", action="prepare_brief", customer_id=GULF_STAR).path for _ in range(2)]
    assert paths == ["fast", "fast"]


def test_entries_expire_after_their_ttl():
    clock = Clock()
    cache = SemanticCache(embed, ttl_s=10, clock=clock)
    cache.store(FEE_QUESTION, policy_result("public"), Trace())
    clock.now = 11
    assert cache.lookup(FEE_QUESTION, Trace()) is None


def test_hit_is_still_validated_and_a_cdc_change_invalidates_it(cached_copilot):
    ask(cached_copilot, "rm-1", FEE_QUESTION)
    hit = ask(cached_copilot, "rm-1", FEE_QUESTION)
    assert hit.path == "cache" and "Output guardrails" in components(hit.trace)
    report = cached_copilot.ingestion.on_change(ChangeEvent(source="fees.md", content=FEES_UPDATE), Trace())
    assert report.cache_entries_invalidated == 1
    fresh = ask(cached_copilot, "rm-1", FEE_QUESTION)
    assert fresh.path == "fast" and "AED 300" in fresh.text
