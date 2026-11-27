"""Diagram box: Session + memory store (lane C), procedural memory.
Versioned prompts and model parameters used by the router and all agents; changes go through the release gate.
Mocked: prompts are defined in code; production loads pinned versions from a config store.
"""
from __future__ import annotations

import json
from string import Template
from typing import Any

from pydantic import BaseModel

from copilot.contracts.ports import RenderedPrompt

TOOL_SCHEMA_VERSION = "2026.11.1"


class PromptTemplate(BaseModel):
    prompt_id: str
    version: str
    system: str
    user: str
    params: dict[str, Any] = {}


_PROMPTS = [
    PromptTemplate(
        prompt_id="router.classify_intent", version="1.1.0", params={"temperature": 0.0, "max_tokens": 50},
        system="Classify the RM's ask as brief, policy, followup or unknown. Say if it needs several steps. "
               "Reply as JSON.",
        user="Ask: $text"),
    PromptTemplate(
        prompt_id="planner.plan", version="1.3.0", params={"temperature": 0.0, "max_tokens": 400},
        system="Break the ask into at most 4 steps. Each step names one agent (brief, policy, followup). "
               "A step may run only if an earlier step ends with a given outcome. Reply with JSON matching Plan.",
        user="Ask: $text\nCustomer: $customer_id"),
    PromptTemplate(
        prompt_id="policy.answer", version="2.0.1", params={"temperature": 0.1, "max_tokens": 300},
        system="Answer only from the clauses given. Cite every clause you use as §number. If the clauses do not "
               "answer the question, say so. Never state a figure that is not in a clause or in the question. "
               "Reply as JSON with outcome, answer and cited_refs.",
        user="Question: $question\nClauses: $clauses"),
    PromptTemplate(
        prompt_id="brief.summarise", version="1.4.0", params={"temperature": 0.2, "max_tokens": 400},
        system="Write a short meeting summary and talking points. Never write numbers, names or dates: use the "
               "{{placeholders}} listed in fact_keys and the code fills them in. Reply as JSON.",
        user="Fact keys: $fact_keys\nUnavailable sources: $unavailable"),
    PromptTemplate(
        prompt_id="followup.extract", version="1.2.0", params={"temperature": 0.0, "max_tokens": 400},
        system="Turn the RM's meeting notes into one CRM note, tasks with due dates and an email draft. "
               "Use {{contact_first_name}} and {{rm_name}} placeholders for names. Reply as JSON.",
        user="Notes: $notes\nToday: $today"),
]


class PromptRegistry:
    def __init__(self) -> None:
        self._prompts = {p.prompt_id: p for p in _PROMPTS}

    def get(self, prompt_id: str) -> PromptTemplate:
        return self._prompts[prompt_id]

    def render(self, prompt_id: str, variables: dict[str, Any]) -> tuple[RenderedPrompt, PromptTemplate]:
        template = self.get(prompt_id)
        as_text = {k: v if isinstance(v, str) else json.dumps(v) for k, v in variables.items()}
        rendered = RenderedPrompt(
            prompt_id=prompt_id, version=template.version, system=template.system,
            user=Template(template.user).safe_substitute(as_text), variables=variables,
        )
        return rendered, template
