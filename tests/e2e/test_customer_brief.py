"""Use case: Customer brief, end to end through lanes A to D.
Proves a Prepare brief click takes the fast path to the brief agent, fills every section and figure from the
systems of record, cites its sources with as-of times and passes the output guardrails.
"""
from __future__ import annotations

import pytest

from copilot.contracts import AnswerStatus
from helpers import AL_NOOR, GULF_STAR, PALM_COAST, ask, components, lanes

SECTIONS = ["### Summary", "### Facilities", "### Accounts", "### Trade finance", "### Open requests and pending items",
            "### Complaints and service requests", "### Recent interactions", "### Last approved meeting summary",
            "### Suggested products", "### Talking points"]


@pytest.fixture
def brief(copilot):
    return ask(copilot, "rm-1", action="prepare_brief", customer_id=GULF_STAR)


def test_brief_is_answered_on_the_fast_path(brief):
    assert brief.status == AnswerStatus.ANSWERED and brief.path == "fast"


def test_brief_has_every_section(brief):
    for section in SECTIONS:
        assert section in brief.text, section


def test_brief_figures_match_the_systems_of_record(brief):
    for figure in ("AED 2.0m", "82.5%", "AED 3.5m", "Gulf Star Trading LLC", "2026-12-02"):
        assert figure in brief.text
    assert "Import invoice financing" in brief.text


def test_brief_lists_each_source_with_its_as_of_time(brief):
    sources = {s.system: s for s in brief.sources}
    assert sources["Core banking (ODS)"].as_of == "2026-11-27T02:00:00+04:00"
    assert all(s.available for s in brief.sources)


def test_brief_recalls_the_last_rm_approved_meeting_summary(brief):
    assert "expand into Oman" in brief.text


def test_brief_trace_crosses_all_four_lanes_through_the_expected_boxes(brief):
    assert lanes(brief.trace) == {"A", "B", "C", "D"}
    assert {"API gateway + identity", "Input guardrails", "Router + orchestrator", "Brief agent",
            "Tool gateway (MCP)", "Systems of record", "Model gateway", "Output guardrails",
            "Secure storage", "Observability"} <= components(brief.trace)


def test_other_rm_gets_a_brief_for_their_own_customer(copilot):
    answer = ask(copilot, "rm-2", action="prepare_brief", customer_id=PALM_COAST)
    assert answer.status == AnswerStatus.ANSWERED
    assert "Palm Coast Engineering LLC" in answer.text and "AED 1.5m" in answer.text


def test_customer_without_approved_summaries_says_none_on_file(copilot):
    answer = ask(copilot, "rm-1", action="prepare_brief", customer_id=AL_NOOR)
    assert "### Last approved meeting summary\n- None on file" in answer.text
