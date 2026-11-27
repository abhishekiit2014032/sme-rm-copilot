"""Diagram box: Tool gateway (MCP) (lane B), the crm-drafts MCP server.
Creates note, task and email DRAFTS only. There is deliberately no commit tool: only code commits, after approval.
Mocked: drafts are held in memory; production stores them in the CRM's draft area.
"""
from __future__ import annotations

import uuid
from typing import Any, Callable

from copilot.contracts.context import RunContext
from copilot.contracts.results import Draft
from copilot.contracts.tools import ToolResult
from copilot.execution.mcp_servers import McpServer


class CrmDraftsServer(McpServer):
    name = "crm-drafts"

    def __init__(self) -> None:
        self.drafts: dict[str, Draft] = {}

    def tools(self) -> dict[str, Callable[..., ToolResult]]:
        return {"create_note_draft": self.create_note_draft,
                "create_task_drafts": self.create_task_drafts,
                "create_email_draft": self.create_email_draft}

    def system_for(self, tool: str) -> str:
        return "CRM drafts"

    def create_note_draft(self, customer_id: str, text: str, ctx: RunContext) -> ToolResult:
        return self._saved([self._new("note", customer_id, "Meeting note", text)])

    def create_task_drafts(self, customer_id: str, tasks: list[dict[str, Any]],
                           ctx: RunContext) -> ToolResult:
        drafts = [self._new("task", customer_id, t["title"], f"Owner: {t['owner']}", t["due_date"]) for t in tasks]
        return self._saved(drafts)

    def create_email_draft(self, customer_id: str, to: str, subject: str, body: str,
                           ctx: RunContext) -> ToolResult:
        return self._saved([self._new("email", customer_id, subject, f"To: {to}\n\n{body}")])

    def _new(self, kind: str, customer_id: str, title: str, body: str, due: str | None = None) -> Draft:
        draft = Draft(draft_id="dr-" + uuid.uuid4().hex[:8], kind=kind, customer_id=customer_id, title=title,
                      body=body, due_date=due)
        self.drafts[draft.draft_id] = draft
        return draft

    def _saved(self, drafts: list[Draft]) -> ToolResult:
        return ToolResult(ok=True, data=[d.model_dump() for d in drafts], source_system="CRM drafts",
                          as_of="now")
