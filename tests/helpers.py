"""Test helpers shared by every folder: fictional ids, a spy model and small request helpers."""
from __future__ import annotations

from typing import Any

from copilot.contracts import AskRequest, Identity, Role, Trace
from copilot.contracts.context import RunContext
from copilot.contracts.ports import LlmCompletion, RenderedPrompt
from copilot.wiring import Copilot
from mocks.llm.scripted_llm import ScriptedLlm

GULF_STAR = "C-1001"      # rm-1's customer
AL_NOOR = "C-1002"        # rm-1's customer
PALM_COAST = "C-2001"     # rm-2's customer
POLICY_QUESTION = "Can we go to AED 3.5m unsecured? If not, what collateral is acceptable?"
FEE_QUESTION = "What is the fee for a letter of credit amendment?"
FOLLOW_UP_NOTES = "Draft follow-up: owner will send audited financials by 5 Dec"


class SpyLlm(ScriptedLlm):
    """The scripted model, plus a record of every prompt it was sent (to prove what a model can see)."""

    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[RenderedPrompt] = []
        self.embedded: list[str] = []

    def complete(self, model: str, prompt: RenderedPrompt, params: dict[str, Any]) -> LlmCompletion:
        self.prompts.append(prompt)
        return super().complete(model, prompt, params)

    def embed(self, model: str, text: str) -> list[float]:
        self.embedded.append(text)
        return super().embed(model, text)

    def everything_seen(self) -> str:
        return "\n".join([p.system + p.user for p in self.prompts] + self.embedded)


def ask(copilot: Copilot, user: str, text: str = "", action: str | None = None,
        customer_id: str | None = None):
    """Send one request as `user`, the way the CRM sidebar does."""
    return copilot.pipeline.ask(copilot.token_for(user),
                                AskRequest(text=text, action=action, customer_id=customer_id))


def draft_follow_up(copilot: Copilot, user: str = "rm-1", notes: str = FOLLOW_UP_NOTES):
    return ask(copilot, user, notes, action="draft_follow_up", customer_id=GULF_STAR)


def run_context(copilot: Copilot, user_id: str = "rm-1", **overrides: Any) -> RunContext:
    """A run context for a signed-in user, as the pipeline builds it after the gateway."""
    token = copilot.token_for(user_id)
    claims = copilot.idp.verify(token)
    identity = Identity(user_id=user_id, name=claims["name"], roles=[Role(r) for r in claims["roles"]],
                        token=token)
    return RunContext(request_id="rq-test", identity=identity, trace=Trace(request_id="rq-test"), **overrides)


def components(trace: Trace) -> set[str]:
    return {s.component.value for s in trace.spans}


def lanes(trace: Trace) -> set[str]:
    return {s.lane.value for s in trace.spans}
