"""Security: no agent can write to a system of record (Tool gateway (MCP) and Human in the loop, lane B).
Proves no MCP server exposes a commit or update tool, agents may call only read and draft tools, and the only
code path that commits to the CRM is the outbox, after a verified approval.
"""
from __future__ import annotations

import re
from pathlib import Path

import copilot as copilot_package
from copilot.execution.tool_gateway import AGENT_ALLOW_LIST
from helpers import draft_follow_up

SRC = Path(copilot_package.__file__).parent
WRITE_VERBS = re.compile(r"commit|update|delete|write|save|send|submit|post|approve", re.IGNORECASE)


def exposed_tools(copilot) -> dict[str, set[str]]:
    servers = copilot.tool_gateway._servers
    return {name: set(server.tools()) for name, server in servers.items()}


def test_no_mcp_server_exposes_a_write_tool(copilot):
    for server, tools in exposed_tools(copilot).items():
        assert not [t for t in tools if WRITE_VERBS.search(t)], server


def test_crm_drafts_server_only_creates_drafts(copilot):
    assert all(t.endswith(("_draft", "_drafts")) for t in exposed_tools(copilot)["crm-drafts"])


def test_customer_data_tools_are_read_only(copilot):
    assert all(t.startswith("get_") for t in exposed_tools(copilot)["customer-data"])


def test_no_agent_is_allowed_a_write_tool():
    for agent, tools in AGENT_ALLOW_LIST.items():
        assert not [t for t in tools if WRITE_VERBS.search(t.split(".", 1)[1])], agent


def test_only_the_outbox_calls_crm_commit():
    callers = sorted(str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if ".commit(" in p.read_text())
    assert callers == ["execution/approvals.py"]


def test_agents_never_touch_systems_of_record_directly():
    agent_sources = "\n".join(p.read_text() for p in (SRC / "execution" / "agents").glob("*.py"))
    assert "mocks" not in agent_sources and "CrmSystem" not in agent_sources


def test_drafting_alone_never_reaches_the_crm(copilot):
    draft_follow_up(copilot)
    assert copilot.crm.committed == {}
