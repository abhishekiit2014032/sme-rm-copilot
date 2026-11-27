"""Demo scenarios (demo/scenarios.py), the recorded walk-through of every diagram lane.
Proves run_all returns the eight scenarios, each step ends with the expected status, and each trace touches the
lanes the scenario is meant to show.
"""
from __future__ import annotations

from demo.scenarios import SCENARIOS, run_all
from helpers import lanes

ALL = {"A", "B", "C", "D"}

# scenario id -> expected (status, lanes) per step; "event" marks the CDC ingestion step.
EXPECTED = {
    "brief": [("answered", ALL)],
    "policy": [("answered", ALL)],
    "followup": [("awaiting_approval", ALL), ("committed", ALL), ("rejected", ALL)],
    "injection": [("blocked", {"A", "C", "D"})],     # blocked before any lane B work
    "entitlement": [("denied", ALL)],
    "core_down": [("degraded", ALL)],
    "weak_evidence": [("declined", ALL)],
    "cdc_cache": [("answered", ALL), ("answered", ALL), ("event", ALL), ("answered", ALL)],
}


def test_run_all_returns_eight_scenarios_with_expected_statuses_and_lanes():
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
