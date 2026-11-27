"""Diagram box: Router + orchestrator (lane A), the router.
Proves UI actions and single clear intents take the fast path, multi-step asks take the planner path,
and the planner kill switch falls back to the fast path.
"""
from __future__ import annotations

from copilot.contracts import Request
from copilot.operations.flags import Flags
from copilot.request_path.router import Router
from helpers import FEE_QUESTION, GULF_STAR, POLICY_QUESTION, run_context


def route(copilot, text: str = "", action: str | None = None, flags: Flags | None = None):
    ctx = run_context(copilot)
    request = Request(request_id="rq-test", user_id="rm-1", masked_text=text, action=action, customer_id=GULF_STAR)
    return Router(copilot.models, flags or Flags()).route(request, ctx), ctx


def models_called(ctx) -> list[str]:
    return [s.attrs["model"] for s in ctx.trace.spans if "model" in s.attrs]


def test_ui_actions_take_the_fast_path_to_their_agent_without_a_model_call(copilot):
    for action, agent in (("prepare_brief", "brief"), ("draft_follow_up", "followup")):
        decision, ctx = route(copilot, action=action)
        assert (decision.path, decision.task.agent, decision.task.customer_id) == ("fast", agent, GULF_STAR)
        assert models_called(ctx) == []


def test_single_clear_question_takes_the_fast_path_via_the_mini_tier(copilot):
    decision, ctx = route(copilot, text=FEE_QUESTION)
    assert (decision.path, decision.task.agent) == ("fast", "policy")
    assert models_called(ctx) == ["mini-llm"]


def test_multi_step_question_takes_the_planner_path_unless_the_planner_is_switched_off(copilot):
    assert route(copilot, text=POLICY_QUESTION)[0].path == "planner"
    fallback, _ = route(copilot, text=POLICY_QUESTION, flags=Flags(planner_path=False))
    assert (fallback.path, fallback.task.agent) == ("fast", "policy")
