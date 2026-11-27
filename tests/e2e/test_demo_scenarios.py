"""Demo scenarios (demo/scenarios.py), the recorded walk-through of every diagram lane.
Proves run_all returns the nine scenarios, each step ends with the expected status, and each trace touches the
lanes the scenario is meant to show.
"""
from __future__ import annotations

from demo.scenarios import SCENARIOS, run_all, run_scenario
from helpers import lanes

ALL = {"A", "B", "C", "D"}

# scenario id -> expected (status, lanes) per step; "event" marks the CDC ingestion step.
EXPECTED = {
    "brief": [("answered", ALL)],
    "policy": [("answered", ALL)],
    "followup": [("awaiting_approval", ALL), ("committed", ALL), ("rejected", ALL)],
    "injection": [("blocked", {"A", "C", "D"})],     # blocked before any lane B work
    "entitlement": [("denied", ALL)],
    "core_down": [("degraded", ALL), ("degraded", ALL)],
    "model_fallback": [("answered", ALL), ("declined", ALL)],
    "weak_evidence": [("declined", ALL)],
    "cdc_cache": [("answered", ALL), ("answered", ALL), ("event", ALL), ("answered", ALL)],
}


def test_run_all_returns_nine_scenarios_with_expected_statuses_and_lanes():
    runs = run_all()
    assert [r.id for r in runs] == [s.id for s in SCENARIOS] == list(EXPECTED)
    for run in runs:
        observed = [(step.answer.status.value if step.answer else "event", lanes(step.trace)) for step in run.steps]
        assert observed == EXPECTED[run.id], run.id
        assert run.model_dump_json()                  # serialisable for the static demo export


def test_cdc_scenario_shows_miss_hit_invalidate_and_fresh_answer():
    first, second, cdc, third = next(r for r in run_all() if r.id == "cdc_cache").steps
    assert (first.answer.path, second.answer.path, third.answer.path) == ("fast", "cache", "fast")
    assert cdc.event.cache_entries_invalidated == 1
    assert "AED 350" in first.answer.text and "AED 300" in third.answer.text


def test_core_down_scenario_shows_the_circuit_breaker_opening():
    first, second = run_scenario("core_down").steps
    assert not [s for s in first.trace.spans if s.name.startswith("circuit")]
    opened = [(s.name, s.status) for s in second.trace.spans if s.name.startswith("circuit")]
    assert opened == [("circuit opened: Core banking (ODS)", "open"), ("circuit open: Core banking (ODS)", "error")]


def test_model_fallback_scenario_shows_the_fallback_then_a_safe_decline():
    fallback, declined = run_scenario("model_fallback").steps
    assert "fallback model" in [s.name for s in fallback.trace.spans]
    assert "fallback-llm" in [s.attrs.get("model") for s in fallback.trace.spans]
    assert "Model gateway: no model available (primary and fallback failed)" in declined.answer.notices
