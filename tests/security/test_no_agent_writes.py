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


def test_no_mcp_server_exposes_a_write_tool_and_no_agent_is_allowed_one(copilot):
    exposed = {name: set(server.tools()) for name, server in copilot.tool_gateway._servers.items()}
    for server, tools in exposed.items():
        assert not [t for t in tools if WRITE_VERBS.search(t)], server
    assert all(t.startswith("get_") for t in exposed["customer-data"])
    assert all(t.endswith(("_draft", "_drafts")) for t in exposed["crm-drafts"])
    for agent, tools in AGENT_ALLOW_LIST.items():
        assert not [t for t in tools if WRITE_VERBS.search(t.split(".", 1)[1])], agent


def test_only_the_outbox_calls_crm_commit_and_agents_never_import_a_system(copilot):
    callers = sorted(str(p.relative_to(SRC)) for p in SRC.rglob("*.py") if ".commit(" in p.read_text())
    assert callers == ["execution/approvals.py"]
    agent_sources = "\n".join(p.read_text() for p in (SRC / "execution" / "agents").glob("*.py"))
    assert "mocks" not in agent_sources and "CrmSystem" not in agent_sources


def test_drafting_alone_never_reaches_the_crm(copilot):
    draft_follow_up(copilot)
    assert copilot.crm.committed == {} and copilot.crm_drafts.drafts
