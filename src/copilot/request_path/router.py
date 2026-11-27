"""Diagram box: Router + orchestrator (lane A), the router.
FAST PATH: a UI action or one clear intent goes straight to one agent. PLANNER PATH: anything multi-step or open.
Mocked: the intent classifier is the scripted model on the mini tier.
"""
from __future__ import annotations

from pydantic import BaseModel

from copilot.contracts.context import RunContext
from copilot.contracts.plan import AgentName, RouteDecision, Task
from copilot.contracts.request import Request
from copilot.contracts.trace import Component
from copilot.execution.model_gateway import ModelGateway, ModelTier
from copilot.operations.flags import Flags

UI_ACTIONS: dict[str, AgentName] = {"prepare_brief": "brief", "draft_follow_up": "followup"}
MIN_CONFIDENCE = 0.8


class IntentGuess(BaseModel):
    intent: str
    multi_step: bool
    confidence: float


class Router:
    def __init__(self, models: ModelGateway, flags: Flags) -> None:
        self._models, self._flags = models, flags

    def route(self, request: Request, ctx: RunContext) -> RouteDecision:
        decision = self._decide(request, ctx)
        ctx.trace.event(Component.ROUTER_ORCHESTRATOR, f"route: {decision.path} path", detail=decision.reason)
        return decision

    def _decide(self, request: Request, ctx: RunContext) -> RouteDecision:
        if request.action:
            agent = UI_ACTIONS[request.action]
            return RouteDecision(path="fast", reason=f"UI action {request.action} -> {agent} agent",
                                 task=Task(agent=agent, instruction=request.masked_text or request.action,
                                           customer_id=request.customer_id))
        guess = self._models.generate(ModelTier.MINI, "router.classify_intent", {"text": request.masked_text},
                                      IntentGuess, ctx)
        clear = guess.intent in ("brief", "policy", "followup") and guess.confidence >= MIN_CONFIDENCE
        if clear and not guess.multi_step:
            return RouteDecision(path="fast", reason=f"one clear intent ({guess.intent}, {guess.confidence:.2f})",
                                 task=Task(agent=guess.intent, instruction=request.masked_text,
                                           customer_id=request.customer_id))
        if not self._flags.check("planner_path", ctx.trace):
            return RouteDecision(path="fast", reason="planner switched off; answering as one policy question",
                                 task=Task(agent="policy", instruction=request.masked_text,
                                           customer_id=request.customer_id))
        return RouteDecision(path="planner", reason="multi-step or open ask -> LLM planner")
