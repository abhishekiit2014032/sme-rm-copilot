"""Diagram box: Tool gateway (MCP) (lane B).
Proves every tool call is checked for OIDC user identity, RBAC (role + customer entitlement), agent allow-list
and workload identity, that each denial is audited, and that a failing source becomes "unavailable".
"""
from __future__ import annotations

import pytest

from copilot.contracts import ToolCall
from copilot.execution.tool_gateway import ToolDenied
from helpers import GULF_STAR, PALM_COAST, run_context


def call(server: str = "customer-data", tool: str = "get_customer_profile", agent: str = "agent-brief",
         workload: str = "wl-orchestrator", customer_id: str = GULF_STAR, **args) -> ToolCall:
    return ToolCall(server=server, tool=tool, args={"customer_id": customer_id, **args}, agent_id=agent,
                    workload_id=workload)


def denied_check(copilot, tool_call: ToolCall, ctx) -> str:
    with pytest.raises(ToolDenied) as denied:
        copilot.tool_gateway.call(tool_call, ctx)
    return denied.value.check


def test_entitled_rm_through_an_allowed_agent_and_workload_gets_live_data(copilot):
    result = copilot.tool_gateway.call(call(), run_context(copilot, "rm-1"))
    assert result.ok and result.data["name"] == "Gulf Star Trading LLC" and result.as_of
    assert copilot.tool_gateway.call(call(customer_id=PALM_COAST), run_context(copilot, "rm-2")).ok


def test_rm2_is_denied_rm1_customer_and_the_denial_is_audited(copilot):
    ctx = run_context(copilot, "rm-2")
    assert denied_check(copilot, call(), ctx) == "entitlement"
    assert (copilot.audit.records[-1].target, copilot.audit.records[-1].outcome) == (GULF_STAR, "denied (entitlement)")
    assert ctx.trace.spans[0].status == "denied"


def test_agent_cannot_call_another_agents_tool(copilot):
    ctx = run_context(copilot, "rm-1")
    assert denied_check(copilot, call(agent="agent-policy"), ctx) == "agent"
    brief_writes_drafts = call(server="crm-drafts", tool="create_note_draft", text="x")
    assert denied_check(copilot, brief_writes_drafts, ctx) == "agent"


def test_oidc_rbac_and_workload_checks_each_deny(copilot):
    tampered = run_context(copilot, "rm-1")
    tampered.identity = tampered.identity.model_copy(update={"token": tampered.identity.token[:-2] + "xx"})
    assert denied_check(copilot, call(), tampered) == "oidc"
    impostor = run_context(copilot, "rm-1")
    impostor.identity = impostor.identity.model_copy(update={"user_id": "rm-2"})
    assert denied_check(copilot, call(customer_id=PALM_COAST), impostor) == "oidc"
    draft = call(server="crm-drafts", tool="create_note_draft", agent="agent-followup", text="x")
    assert denied_check(copilot, draft, run_context(copilot, "cd-1")) == "rbac"
    assert denied_check(copilot, call(workload="wl-laptop"), run_context(copilot, "rm-1")) == "workload"


def test_source_down_returns_unavailable_instead_of_raising(copilot):
    copilot.core_banking.down = True
    result = copilot.tool_gateway.call(call(tool="get_accounts_summary"), run_context(copilot, "rm-1"))
    assert not result.ok and result.error == "Core banking (ODS) unavailable"
