"""Diagram box: Brief, Policy and Follow-up agents (lane B).
Proves each specialist agent stays inside its boundary: figures come from tools (never the model), policy
answers cite only retrieved clauses, and the follow-up agent only drafts.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from copilot.contracts import Task
from copilot.contracts.results import Citation
from copilot.execution.agents import fill_placeholders
from copilot.execution.agents.brief import BriefAgent
from copilot.execution.agents.followup import FollowUpAgent
from copilot.execution.agents.policy import PolicyAgent
from copilot.execution.agents.policy import _citations as keep_retrieved_citations
from helpers import FOLLOW_UP_NOTES, GULF_STAR, run_context
from mocks.data import BUSINESS_DATE


def run(copilot, agent_class, instruction: str = "", customer_id: str | None = GULF_STAR, user: str = "rm-1"):
    kwargs = {"today": BUSINESS_DATE.isoformat()} if agent_class is FollowUpAgent else {}
    agent = agent_class(copilot.tool_gateway, copilot.models, **kwargs)
    agent_name = {BriefAgent: "brief", PolicyAgent: "policy", FollowUpAgent: "followup"}[agent_class]
    ctx = run_context(copilot, user)
    return agent.run(Task(agent=agent_name, instruction=instruction, customer_id=customer_id), ctx), ctx


def test_placeholders_are_filled_from_tool_facts_and_unknown_keys_are_marked():
    text = "Limit {{wc_limit}}, rating {{made_up}}"
    assert fill_placeholders(text, {"wc_limit": "AED 2.0m"}) == "Limit AED 2.0m, rating [not available]"


def test_brief_figures_come_from_systems_of_record(copilot):
    result, _ = run(copilot, BriefAgent)
    assert result.status == "ok"
    assert "AED 2.0m" in result.text and "82.5%" in result.text and "AED 3.5m" in result.text
    assert "{{" not in result.text
    assert {s.system for s in result.sources} >= {"CRM", "Core banking (ODS)", "Lending", "Trade finance"}


def test_brief_suggests_products_from_the_rules_service(copilot):
    result, _ = run(copilot, BriefAgent)
    assert "Import invoice financing" in result.text


def test_brief_is_degraded_and_labelled_when_a_non_critical_source_is_down(copilot):
    copilot.core_banking.down = True
    result, _ = run(copilot, BriefAgent)
    assert result.status == "degraded"
    assert result.notices == ["Account data unavailable"]
    assert next(s for s in result.sources if s.detail == "accounts").available is False


def test_brief_declines_when_the_critical_crm_profile_is_down(copilot):
    copilot.crm.down = True
    result, _ = run(copilot, BriefAgent)
    assert result.status == "declined"


def test_policy_agent_cites_only_retrieved_current_clauses(copilot):
    result, _ = run(copilot, PolicyAgent, "Can we lend AED 2m unsecured to an SME borrower?")
    assert result.outcome == "yes"
    assert [(c.clause, c.version) for c in result.citations] == [("4.2", "2026.2")]


def test_citation_to_a_clause_that_was_not_retrieved_is_dropped(copilot):
    retrieval = copilot.retriever.search("unsecured lending cap", {"public", "internal"}, run_context(copilot).trace)
    kept = keep_retrieved_citations(["credit-policy@2026.2#4.2", "credit-policy@2025.1#4.2", "made-up#1"],
                                    retrieval.clauses)
    assert [c.clause for c in kept] == ["4.2"] and all(isinstance(c, Citation) for c in kept)


def test_policy_agent_declines_below_the_gate_without_calling_the_model(copilot):
    result, ctx = run(copilot, PolicyAgent, "What is our policy on lending to cryptocurrency mining companies?")
    assert result.status == "declined" and result.citations == []
    assert not [s for s in ctx.trace.spans if s.component.value == "Model gateway" and "policy.answer" in s.name]


@pytest.mark.xfail(strict=True, raises=ValidationError,
                   reason="BUG execution/agents/policy.py:32-33: the agent never checks found.ok, so an "
                          "unavailable policy index raises a pydantic ValidationError instead of a decline")
def test_policy_agent_declines_when_the_policy_index_is_unavailable(copilot):
    copilot.llm.down_models.add("embedding-model")
    result, _ = run(copilot, PolicyAgent, "What is the fee for a letter of credit amendment?")
    assert result.status == "declined"


def test_follow_up_agent_only_drafts_and_waits_for_approval(copilot):
    result, _ = run(copilot, FollowUpAgent, FOLLOW_UP_NOTES)
    assert result.status == "needs_approval"
    assert sorted(d.kind for d in result.drafts) == ["email", "note", "task", "task"]
    assert copilot.crm.committed == {}


def test_follow_up_email_names_come_from_crm_and_identity_not_the_model(copilot):
    result, _ = run(copilot, FollowUpAgent, FOLLOW_UP_NOTES)
    email = next(d for d in result.drafts if d.kind == "email")
    assert "Dear Faisal," in email.body and "Layla Haddad" in email.body
    assert "To: faisal.rahman@gulfstar.example" in email.body
    assert next(d for d in result.drafts if d.kind == "task").due_date == "2026-12-05"
