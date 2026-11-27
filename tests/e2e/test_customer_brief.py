"""Use case: Customer brief, end to end through lanes A to D.
Proves a Prepare brief click takes the fast path to the brief agent, fills every section and figure from the
systems of record, lists its sources with as-of times and passes the output guardrails.
"""
from __future__ import annotations

from copilot.contracts import AnswerStatus
from helpers import AL_NOOR, GULF_STAR, PALM_COAST, ask, components, lanes

SECTIONS = ["### Summary", "### Facilities", "### Accounts", "### Trade finance", "### Open requests and pending items",
            "### Complaints and service requests", "### Recent interactions", "### Last approved meeting summary",
            "### Suggested products", "### Talking points"]


def test_gulf_star_brief_has_every_section_with_figures_from_the_systems_of_record(copilot):
    brief = ask(copilot, "rm-1", action="prepare_brief", customer_id=GULF_STAR)
    assert (brief.status, brief.path) == (AnswerStatus.ANSWERED, "fast")
    for expected in SECTIONS + ["AED 2.0m", "82.5%", "AED 3.5m", "2026-12-02", "Import invoice financing",
                                "expand into Oman"]:
        assert expected in brief.text, expected
    sources = {s.system: s for s in brief.sources}
    assert sources["Core banking (ODS)"].as_of == "2026-11-27T02:00:00+04:00"
    assert all(s.available for s in brief.sources)


def test_brief_trace_crosses_all_four_lanes_through_the_expected_boxes(copilot):
    trace = ask(copilot, "rm-1", action="prepare_brief", customer_id=GULF_STAR).trace
    assert lanes(trace) == {"A", "B", "C", "D"}
    assert {"API gateway + identity", "Input guardrails", "Router + orchestrator", "Brief agent",
            "Tool gateway (MCP)", "Systems of record", "Model gateway", "Output guardrails",
            "Secure storage", "Observability"} <= components(trace)


def test_each_rm_gets_briefs_for_their_own_customers(copilot):
    palm_coast = ask(copilot, "rm-2", action="prepare_brief", customer_id=PALM_COAST)
    assert palm_coast.status == AnswerStatus.ANSWERED
    assert "Palm Coast Engineering LLC" in palm_coast.text and "AED 1.5m" in palm_coast.text
    al_noor = ask(copilot, "rm-1", action="prepare_brief", customer_id=AL_NOOR)
    assert "### Last approved meeting summary\n- None on file" in al_noor.text
