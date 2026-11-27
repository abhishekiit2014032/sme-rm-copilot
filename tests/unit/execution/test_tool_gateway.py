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
         workload: str = "wl-orchestrator", **args) -> ToolCall:
    return ToolCall(server=server, tool=tool, args=args or {"customer_id": GULF_STAR}, agent_id=agent,
                    workload_id=workload)


def denied_check(copilot, tool_call: ToolCall, user: str = "rm-1", **ctx) -> str:
    with pytest.raises(ToolDenied) as denied:
        copilot.tool_gateway.call(tool_call, run_context(copilot, user, **ctx))
    return denied.value.check


def test_entitled_rm_with_allowed_agent_and_workload_gets_data(copilot):
    result = copilot.tool_gateway.call(call(), run_context(copilot, "rm-1"))
    assert result.ok and result.data["name"] == "Gulf Star Trading LLC"
    assert result.source_system == "CRM" and result.as_of


def test_rm2_is_denied_rm1_customer(copilot):
    assert denied_check(copilot, call(), user="rm-2") == "entitlement"


def test_rm2_is_allowed_own_customer(copilot):
    assert copilot.tool_gateway.call(call(customer_id=PALM_COAST), run_context(copilot, "rm-2")).ok


def test_agent_cannot_call_another_agents_tool(copilot):
    policy_calls_crm = call(agent="agent-policy")
    assert denied_check(copilot, policy_calls_crm) == "agent"
    brief_writes_drafts = call(server="crm-drafts", tool="create_note_draft", customer_id=GULF_STAR, text="x")
    assert denied_check(copilot, brief_writes_drafts) == "agent"


def test_unknown_workload_is_denied(copilot):
    assert denied_check(copilot, call(workload="wl-laptop")) == "workload"


def test_tampered_user_token_is_denied_on_every_call(copilot):
    ctx = run_context(copilot, "rm-1")
    ctx.identity = ctx.identity.model_copy(update={"token": ctx.identity.token[:-2] + "xx"})
    with pytest.raises(ToolDenied) as denied:
        copilot.tool_gateway.call(call(), ctx)
    assert denied.value.check == "oidc"


def test_identity_that_does_not_match_token_subject_is_denied(copilot):
    ctx = run_context(copilot, "rm-1")
    ctx.identity = ctx.identity.model_copy(update={"user_id": "rm-2"})
    with pytest.raises(ToolDenied) as denied:
        copilot.tool_gateway.call(call(customer_id=PALM_COAST), ctx)
    assert denied.value.check == "oidc"


def test_role_without_server_access_is_denied_by_rbac(copilot):
    draft = call(server="crm-drafts", tool="create_note_draft", agent="agent-followup", customer_id=GULF_STAR,
                 text="x")
    assert denied_check(copilot, draft, user="cd-1") == "rbac"


def test_denied_call_is_audited_and_traced_as_denied(copilot):
    ctx = run_context(copilot, "rm-2")
    with pytest.raises(ToolDenied):
        copilot.tool_gateway.call(call(), ctx)
    assert copilot.audit.records[-1].outcome == "denied (entitlement)"
    assert copilot.audit.records[-1].target == GULF_STAR
    assert ctx.trace.spans[0].component.value == "Tool gateway (MCP)" and ctx.trace.spans[0].status == "denied"


def test_source_down_returns_unavailable_instead_of_raising(copilot):
    copilot.core_banking.down = True
    result = copilot.tool_gateway.call(call(tool="get_accounts_summary"), run_context(copilot, "rm-1"))
    assert not result.ok and result.error == "Core banking (ODS) unavailable"
