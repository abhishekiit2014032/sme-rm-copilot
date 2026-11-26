"""The orchestrator's multi-agent graph (plan §3.7, §3.8; C24 to C29).

    supervisor ──► brief-agent ────┐
     (intent,  ──► policy-agent ───┼──► verify ──► respond                       (UC1, UC2)
      budgets) ──► followup-agent ─┘      └──► await_approval ┄┄ RM approves ┄┄► commit ──► respond   (UC3)

* `supervisor` (agent, no tools): takes the intent of the entry point (fixed workflows: no router call when the RM
  starts from a CRM record, plan §4.1 step 3), picks exactly one specialist, sets the budgets, loads session and
  profile memory.
* specialists (agents): run the unchanged `rm_copilot.Copilot` workflows (UC1 brief, UC2 policy answer, UC3 drafts),
  each with its own `McpTools`, i.e. its own Entra identity on the MCP gateway; the brief-agent also recalls this RM's
  RM-approved episodes with the customer; the follow-up agent stores its draft through `crm-drafts`.
* `verify` (deterministic code, replayable): placeholders filled, no commitments, citations present, task dates valid.
  The numeric, policy and groundedness checks inside the workflows are code as well; no agent verifies.
* `await_approval` (interrupt): UC3 pauses; the state is checkpointed (TTL 30 days while open). On approval web-bff
  sends the run id with the RM-signed approval; any orchestrator process resumes from the checkpoint.
* `commit` (deterministic code, not an agent tool): the approval-verified write API with the RM's `Tools.Write` token.
* `respond` (code): assembles the reply (which agent, which tools), writes the session turn.

Checkpoints and session memory go to the tool-gateway memory store (it holds the per-customer memory keys; the
orchestrator holds no key and no Cosmos DB access). Procedural memory (graph definition, prompt templates, tool
definitions) must match the pins in App Configuration, else 503 `PROCEDURAL_PIN_MISMATCH`.
"""
from __future__ import annotations

import re
import secrets
import time
from typing import TYPE_CHECKING

from rm_copilot import guardrails as g
from rm_copilot.orchestrator import Copilot
from services.common.httpkit import HttpError, TransportError, request_context, time_left
from services.common.identities import AGENT_SERVERS, AGENTS
from services.common.memory import memory_safe
from services.common.procedural import GRAPH_SPEC, PINS_KEY, local_registry, prompt_sha
from services.graph import GraphRuntime, RunContext
from services.orchestrator_adapters import ApimChatModel, HttpGuard, HttpTools, McpClient, McpTools

if TYPE_CHECKING:
    from services.orchestrator import Orchestrator

RUN_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
THREAD_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
PROFILE_VALUES = {"language": ("en", "ar"), "brief_format": ("standard", "short")}
DEFAULT_PROFILE = {"language": "en", "brief_format": "standard"}


def finalize(use_case: str, result: dict) -> dict:
    """Mode and warnings of a UC1/UC2/UC3 reply (LLD §1.4): degraded, partial or full."""
    degraded = bool(result.get("degraded")) or result.get("status") == "degraded"
    warnings = [{"code": "DEGRADED", "reason": "MODEL_UNAVAILABLE"}] if degraded else []
    warnings += [{"code": "SOURCE_UNAVAILABLE", "section": s} for s in result.get("partial", [])]
    return {**result, "mode": "degraded" if degraded else ("partial" if result.get("partial") else "full"), "warnings": warnings}


# --------------------------------------------------------------------------- procedural memory
class ProceduralGuard:
    """Pins of the graph definition, prompt templates and tool definitions (App Configuration `rmc:procedural:pins`)."""

    def __init__(self, svc: "Orchestrator", spec: dict):
        self.svc, self.spec = svc, spec

    def pins(self) -> dict:
        ctx = request_context()
        if "procedural_pins" not in ctx.data:  # one read per request
            ctx.data["procedural_pins"] = self.svc.config.get(PINS_KEY)
        p = ctx.data["procedural_pins"]
        return p if isinstance(p, dict) else {}

    @staticmethod
    def _refuse(reason: str) -> HttpError:
        return HttpError(503, "PROCEDURAL_PIN_MISMATCH", "The copilot's configuration does not match the approved release.",
                         details={"reason": reason}, retryable=False)

    def check(self) -> None:
        pins = self.pins()
        local = local_registry(self.spec)
        if not pins:
            raise self._refuse("PINS_UNAVAILABLE")
        if (pins.get("graph") or {}).get("sha256") != local["graph"]["sha256"]:
            raise self._refuse("GRAPH")
        for name, p in local["prompts"].items():
            if (pins.get("prompts") or {}).get(name, {}).get("sha256") != p["sha256"]:
                raise self._refuse(f"PROMPT_{name.upper()}")

    def check_prompt(self, system: str) -> None:
        if prompt_sha(system) not in {p.get("sha256") for p in (self.pins().get("prompts") or {}).values()}:
            raise self._refuse("PROMPT")


# --------------------------------------------------------------------------- checkpointer (tool-gateway memory store)
class MemoryStore:
    def __init__(self, svc: "Orchestrator"):
        self.svc = svc
        m = svc.metrics
        self.m_failures = m.counter("checkpoint_failures_total", "Checkpoints that could not be written (non-interrupt nodes continue)", ())

    def _req(self, method: str, path: str, body: dict | None = None):
        headers = {**self.svc.auth_headers("tool-gateway"), "x-rm-authorization": "Bearer " + request_context().data["rm_token"]}
        try:
            r = self.svc.client.request(method, f"{self.svc.cfg.url('tool-gateway')}/internal/v1/memory{path}", json_body=body, headers=headers,
                                        timeout=2.0, peer="tool-gateway")
        except TransportError:
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "memory store unavailable", details={"reason": "MEMORY"}) from None
        if not r.ok:
            raise HttpError(503 if r.status >= 500 else r.status, "DEPENDENCY_UNAVAILABLE" if r.status >= 500 else r.error_code() or "MEMORY",
                            "memory store request failed", details={"reason": "MEMORY"})
        return r.json()

    def save(self, ctx: RunContext, node: str, step: int, status: str, state: dict) -> None:
        body = {"run_id": ctx.run_id, "thread_id": ctx.thread_id, "step": step, "node": node, "status": status, "use_case": state.get("use_case"),
                "customer_id": ctx.customer_id, "graph_version": state.get("graph_version"), "state": memory_safe(state)}
        try:
            self._req("POST", "/checkpoints", body)
        except HttpError:
            if status == "interrupted":
                raise  # a run cannot pause for approval without a durable checkpoint
            self.m_failures.inc()

    def load(self, run_id: str) -> list[dict]:
        return self._req("GET", f"/checkpoints/{run_id}").get("value", [])

    def session(self, thread: str) -> dict:
        try:
            return self._req("GET", f"/sessions/{thread}")
        except HttpError:
            return {"turns": 0, "tokens": 0, "summary": ""}

    def add_turn(self, thread: str, turn: dict) -> dict | None:
        try:
            return self._req("POST", f"/sessions/{thread}/turns", {"turn": turn})
        except HttpError:
            return None


# --------------------------------------------------------------------------- deterministic verification node
_CITED = re.compile(r"\[\d+\]")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def verify(state: dict, ctx: RunContext | None = None) -> dict:
    """Last deterministic checks on the specialist's output. Pure: replayable from the previous checkpoint."""
    uc, out = state.get("use_case"), dict(state.get("output") or {})
    checks, dropped = [], 0
    if uc == "uc1":
        points = list(out.get("talking_points", []))
        kept = [p for p in points if not g.policy_violations(p) and not re.search(r"\{[^}]+\}", p)]
        dropped, out["talking_points"] = len(points) - len(kept), kept
        checks = ["placeholders_filled", "no_commitments"]
    elif uc == "uc2":
        checks = ["citations"]
        if out.get("status") == "answered":
            sentences = [s for s in re.split(r"(?<=[.!?])\s+", str(out.get("answer", "")).strip()) if s]
            if not sentences or not all(_CITED.search(s) for s in sentences):
                dropped = 1
                out = {"status": "declined", "reason": "answer failed verification (citations)", "source_ids": out.get("source_ids", []),
                       "message": "I can't confirm this from approved documents. I can route it to the credit or product desk.", "closest_sources": out.get("sources", [])}
    elif uc == "uc3":
        tasks = list(out.get("tasks", []))
        kept = [x for x in tasks if isinstance(x, dict) and not g.policy_violations(str(x.get("title", ""))) and (not x.get("due") or _ISO.match(str(x["due"])))]
        dropped, out["tasks"] = len(tasks) - len(kept), kept
        checks = ["no_commitments", "task_dates"]
    return {"output": out, "verification": {"checks": checks, "dropped": dropped, "passed": dropped == 0}}


# --------------------------------------------------------------------------- the graph
class AgentGraph:
    def __init__(self, svc: "Orchestrator", spec: dict | None = None):
        self.svc = svc
        self.spec = spec or GRAPH_SPEC
        model, guard = ApimChatModel(svc), HttpGuard(svc)
        self.clients = {a: McpClient(svc, a) for a in AGENTS if AGENT_SERVERS[a]}
        self.copilots = {a: Copilot(model, McpTools(svc, c), guard, time_left=time_left) for a, c in self.clients.items()}
        self.rest = HttpTools(svc)  # the write API for the commit node: not an agent tool
        self.memory = MemoryStore(svc)
        self.procedural = ProceduralGuard(svc, self.spec)
        handlers = {"supervisor": self.supervisor, "brief-agent": self.brief_agent, "policy-agent": self.policy_agent,
                    "followup-agent": self.followup_agent, "verify": verify, "await_approval": self.await_approval, "commit": self.commit,
                    "respond": self.respond}
        self.runtime = GraphRuntime(self.spec, handlers, self.memory, svc.telemetry)
        m = svc.metrics
        self.m_hops = m.histogram("agent_hops", "Agent nodes run per request (max 2: supervisor + one specialist)", ("use_case",), buckets=(0, 1, 2, 3))
        self.m_resumes = m.counter("checkpoint_resumes_total", "Runs resumed from a checkpoint (UC3 approval)", ())

    # ------------------------------------------------------------------ entry points
    @staticmethod
    def _context(context: dict | None) -> tuple[str, dict]:
        context = context if isinstance(context, dict) else {}
        session = context.get("session")
        thread = session if isinstance(session, str) and THREAD_RE.fullmatch(session) else "default"
        raw = context.get("profile") if isinstance(context.get("profile"), dict) else {}
        profile = {k: raw.get(k) if raw.get(k) in allowed else DEFAULT_PROFILE[k] for k, allowed in PROFILE_VALUES.items()}
        return thread, profile

    def run(self, use_case: str, user, customer_id: str | None, inputs: dict, context: dict | None) -> dict:
        self.procedural.check()
        thread, profile = self._context(context)
        ctx = RunContext(run_id=secrets.token_urlsafe(16), thread_id=thread, user=user, customer_id=customer_id, inputs={**inputs, "profile": profile})
        state = {"run_id": ctx.run_id, "use_case": use_case, "customer_id": customer_id, "graph_version": self.spec["version"], "path": []}
        try:
            final = self.runtime.run(state, ctx)
        finally:
            self.m_hops.observe(ctx.hops, use_case=use_case)
        return final["response"]

    def resume(self, run_id: str, user, customer_id: str, inputs: dict, context: dict | None) -> dict:
        self.procedural.check()
        if not RUN_RE.fullmatch(run_id or ""):
            raise HttpError(404, "NOT_FOUND", "no paused follow-up for this draft")
        cps = self.memory.load(run_id)  # only this RM's checkpoints (oid from the RM token at the memory store)
        paused = [c for c in cps if c["status"] == "interrupted"]
        if not paused:
            raise HttpError(404, "NOT_FOUND", "no paused follow-up for this draft")
        if any(c["status"] == "completed" for c in cps):
            done = next(c for c in reversed(cps) if c["status"] == "completed")
            jti = ((done.get("state") or {}).get("commit") or {}).get("jti")
            raise HttpError(409, "ALREADY_COMMITTED", "This draft was already approved and committed.", details={"jti": jti}, retryable=False)
        last = paused[-1]
        if last["integrity"] != "ok" or not isinstance(last.get("state"), dict):
            raise HttpError(409, "RUN_STATE", "The paused draft can no longer be resumed.", details={"reason": last["integrity"].upper()})
        if last["state"].get("customer_id") != customer_id:
            raise HttpError(409, "RUN_STATE", "The approval is for another customer than the paused draft.", details={"reason": "CUSTOMER_MISMATCH"})
        thread, profile = self._context(context)
        ctx = RunContext(run_id=run_id, thread_id=thread, user=user, customer_id=customer_id, inputs={**inputs, "profile": profile},
                         step=max(c["step"] for c in cps) + 1)
        final = self.runtime.resume(last["node"], last["state"], ctx)
        self.m_resumes.inc()
        self.m_hops.observe(ctx.hops, use_case="commit")
        return final["response"]

    # ------------------------------------------------------------------ nodes
    def supervisor(self, state: dict, ctx: RunContext) -> dict:
        session = self.memory.session(ctx.thread_id)
        b = request_context().data.get("budget")
        left = time_left()
        return {"intent": state["use_case"], "specialist": self.runtime.next_node("supervisor", state),
                "context": {"profile": ctx.inputs["profile"], "session": {"turns": session.get("turns", 0), "tokens": session.get("tokens", 0)}},
                "budget": {"max_tool_steps": getattr(b, "max_tool_steps", 6), "max_input_tokens": getattr(b, "max_input_tokens", 12000),
                           "max_output_tokens": getattr(b, "max_output_tokens", 1500), "max_agent_hops": self.spec["max_agent_hops"],
                           "deadline_ms": None if left == float("inf") else int(left * 1000)}}

    def brief_agent(self, state: dict, ctx: RunContext) -> dict:
        cid = state["customer_id"]
        out = self.copilots["brief-agent"].brief(ctx.user, cid)
        episodes = self.clients["brief-agent"].call("memory", "recall_episodes", {"customer_id": cid, "limit": 3}).get("value", [])
        out["previous_interactions"] = [{"text": e["text"], "source": f"RM-approved call report, {str(e['provenance'].get('time', ''))[:10]}",
                                         "provenance": e["provenance"]} for e in episodes]
        if ctx.inputs["profile"]["brief_format"] == "short":
            out["talking_points"] = out["talking_points"][:2]
        out["language"] = ctx.inputs["profile"]["language"]
        return {"output": out}

    def policy_agent(self, state: dict, ctx: RunContext) -> dict:
        return {"output": self.copilots["policy-agent"].ask(ctx.user, ctx.inputs["question"])}

    def followup_agent(self, state: dict, ctx: RunContext) -> dict:
        cid = state["customer_id"]
        out = self.copilots["followup-agent"].draft_followup(ctx.user, cid, ctx.inputs["notes"])
        tasks = [{"type": str(x.get("type") or "follow_up")[:32], "title": str(x.get("title", ""))[:300], "due": str(x.get("due") or "")[:32]}
                 for x in out["tasks"] if isinstance(x, dict)]
        d = self.clients["followup-agent"].call("crm-drafts", "create_followup_draft",
                                                {"customer_id": cid, "run_id": ctx.run_id, "call_report": str(out["call_report"])[:8000], "tasks": tasks,
                                                 "email": str(out.get("email_draft") or "")[:8000]})
        return {"output": {**out, "draft_id": d.get("draft_id")}}

    def await_approval(self, state: dict, ctx: RunContext) -> dict:
        return {"response": self._assemble(state, ctx, finalize("uc3", state["output"])), "awaiting": "rm_approval"}

    def commit(self, state: dict, ctx: RunContext) -> dict:
        result = self.rest.create_call_report(ctx.user, state["customer_id"], ctx.inputs["draft"], approval_token=ctx.inputs.get("approval_token"))
        return {"commit": {k: result.get(k) for k in ("status", "jti", "items", "http_status")}}

    def respond(self, state: dict, ctx: RunContext) -> dict:
        if state.get("commit") is not None:
            return {"response": self._assemble(state, ctx, dict(state["commit"]), use_case="commit")}
        return {"response": self._assemble(state, ctx, finalize(state["use_case"], state["output"]))}

    def _assemble(self, state: dict, ctx: RunContext, result: dict, use_case: str | None = None) -> dict:
        use_case = use_case or state["use_case"]
        calls = list(request_context().data.get("tool_calls", []))
        result["run_id"] = ctx.run_id
        result["agents"] = {"graph": self.spec["version"], "supervisor": "supervisor", "specialist": state.get("specialist"), "hops": ctx.hops,
                            "tools": calls, "resumed": ctx.resumed, "path": [*state.get("path", []), "respond" if use_case != "uc3" else "await_approval"],
                            "verification": state.get("verification")}
        outcome = result.get("status") or result.get("mode") or "ok"
        turn = {"ts": int(time.time()), "use_case": use_case, "customer_id": state.get("customer_id"), "agent": state.get("specialist"),
                "outcome": str(outcome)[:24], "tools": [f"{c['server']}/{c['tool']}" for c in calls]}
        self.memory.add_turn(ctx.thread_id, turn)
        return result

