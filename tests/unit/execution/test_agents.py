"""Diagram box: Brief, Policy and Follow-up agents (lane B).
Proves each specialist agent stays inside its boundary: figures come from tools (never the model), policy
answers cite only retrieved clauses, and the follow-up agent only drafts.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from copilot.contracts import Task
from copilot.execution.agents import fill_placeholders
from copilot.execution.agents.brief import BriefAgent
from copilot.execution.agents.followup import FollowUpAgent
from copilot.execution.agents.policy import PolicyAgent
from helpers import FOLLOW_UP_NOTES, GULF_STAR, run_context
from mocks.data import BUSINESS_DATE

AGENT_NAMES = {BriefAgent: "brief", PolicyAgent: "policy", FollowUpAgent: "followup"}


def run(copilot, agent_class, instruction: str = ""):
    kwargs = {"today": BUSINESS_DATE.isoformat()} if agent_class is FollowUpAgent else {}
    agent = agent_class(copilot.tool_gateway, copilot.models, **kwargs)
    ctx = run_context(copilot, "rm-1")
    task = Task(agent=AGENT_NAMES[agent_class], instruction=instruction, customer_id=GULF_STAR)
    return agent.run(task, ctx), ctx


def test_brief_figures_come_from_systems_of_record_not_the_model(copilot):
    result, _ = run(copilot, BriefAgent)
    assert result.status == "ok" and "{{" not in result.text
    for figure in ("AED 2.0m", "82.5%", "AED 3.5m", "Import invoice financing"):
        assert figure in result.text
    assert fill_placeholders("{{wc_limit}} / {{made_up}}", {"wc_limit": "AED 2.0m"}) == "AED 2.0m / [not available]"


def test_brief_is_degraded_without_core_banking_and_declined_without_the_crm(copilot):
    copilot.core_banking.down = True
    degraded, _ = run(copilot, BriefAgent)
    assert degraded.status == "degraded" and degraded.notices == ["Account data unavailable"]
    copilot.crm.down = True
    assert run(copilot, BriefAgent)[0].status == "declined"


def test_policy_agent_cites_only_retrieved_clauses_and_declines_below_the_gate(copilot):
    result, _ = run(copilot, PolicyAgent, "Can we lend AED 2m unsecured to an SME borrower?")
    assert (result.outcome, [(c.clause, c.version) for c in result.citations]) == ("yes", [("4.2", "2026.2")])
    weak, ctx = run(copilot, PolicyAgent, "What is our policy on lending to cryptocurrency mining companies?")
    assert weak.status == "declined" and weak.citations == []
    assert not [s for s in ctx.trace.spans if "policy.answer" in s.name]     # the model is never asked


@pytest.mark.xfail(strict=True, raises=ValidationError,
                   reason="BUG execution/agents/policy.py PolicyAgent.run: the agent never checks found.ok, so an "
                          "unavailable policy index raises a pydantic ValidationError instead of a decline")
def test_policy_agent_declines_when_the_policy_index_is_unavailable(copilot):
    copilot.llm.down_models.add("embedding-model")
    assert run(copilot, PolicyAgent, "What is the fee for a letter of credit amendment?")[0].status == "declined"


def test_follow_up_agent_only_drafts_with_names_from_crm_and_identity(copilot):
    result, _ = run(copilot, FollowUpAgent, FOLLOW_UP_NOTES)
    assert result.status == "needs_approval" and copilot.crm.committed == {}
    assert sorted(d.kind for d in result.drafts) == ["email", "note", "task", "task"]
    email = next(d for d in result.drafts if d.kind == "email")
    assert "Dear Faisal," in email.body and "Layla Haddad" in email.body
    assert next(d for d in result.drafts if d.kind == "task").due_date == "2026-12-05"
