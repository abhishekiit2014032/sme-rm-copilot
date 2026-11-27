"""Demo scenarios (demo/scenarios.py), the recorded walk-through of every diagram lane.
Proves run_all returns the eight scenarios, each step ends with the expected status, and each trace touches the
lanes the scenario is meant to show.
"""
from __future__ import annotations

import pytest

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


@pytest.fixture(scope="module")
def runs():
    return {run.id: run for run in run_all()}


def test_run_all_returns_the_eight_scenarios_in_order(runs):
    assert list(runs) == [s.id for s in SCENARIOS] == list(EXPECTED)


@pytest.mark.parametrize("scenario_id", list(EXPECTED))
def test_each_step_has_the_expected_status_and_lanes(runs, scenario_id):
    steps = runs[scenario_id].steps
    assert len(steps) == len(EXPECTED[scenario_id])
    for step, (status, expected_lanes) in zip(steps, EXPECTED[scenario_id]):
        assert (step.answer.status.value if step.answer else "event") == status, step.label
        assert lanes(step.trace) == expected_lanes, step.label


def test_cdc_scenario_shows_miss_hit_invalidate_and_fresh_answer(runs):
    first, second, cdc, third = runs["cdc_cache"].steps
    assert (first.answer.path, second.answer.path, third.answer.path) == ("fast", "cache", "fast")
    assert cdc.event.cache_entries_invalidated == 1
    assert "AED 350" in first.answer.text and "AED 300" in third.answer.text


def test_scenario_runs_serialise_for_the_static_demo(runs):
    assert runs["policy"].model_dump_json()
