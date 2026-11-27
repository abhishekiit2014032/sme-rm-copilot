"""orchestrator: UC1/UC2/UC3 as a fixed multi-agent graph (plan §3.2, §3.7, §3.8, §4).

The orchestrator node is deterministic code with no model call: it takes the intent from the entry point (fixed
workflows) and hands one task to one specialist (brief-agent, policy-agent, followup-agent), so the prototype runs a
single wave per request (orchestrator + one specialist, at most 2 agent hops). Specialists never call each other and
call tools only through the APIM MCP gateway, each with its own Entra agent identity (`services/agents.py`). The
specialists run the same `rm_copilot.Copilot` workflows as the in-process demo and evals, with HTTP adapters
(`orchestrator_adapters.py`) for the model (APIM AI gateway), the tools (MCP) and the safety services (Content
Safety, AI Language). Verification and commit are deterministic code. A checkpoint is written after every node; UC3
pauses for the RM's approval and resumes from its checkpoint (`POST /v1/commit` with `run_id`), in any process.
It holds no keys, no Cosmos DB access and cannot mint approvals.

Inbound: `Authorization` = web-bff workload token (role `Copilot.Invoke`); `X-RM-Authorization` = the RM's OBO
token for tool-gateway, validated here for the RM's `oid` (quota key, audit) and forwarded unchanged.
Deadlines come from web-bff in `x-deadline-ms`; every hop derives its timeout from what is left. Feature flags
and kill switches come from App Configuration (cached `flags_ttl_s`; on outage the last known value is used,
and writes fail closed). Output is buffered and verified before it is returned (no streaming, plan C18).

Evaluation (plan §3.9, LLD §17): the Tier 1 runtime gates run inside the workflows (`rm_copilot/gates.py`, thresholds
from App Configuration `rmc:orchestrator:gate:*`); every outcome is counted (`rmc_runtime_gate_total`,
`rmc_runtime_gate_score`), traced and returned under `evaluation`. Every UC1/UC2/UC3 reply carries `ops` metadata
(tokens, estimated cost from `rmc:finops:*`, stage times). A share of released UC1/UC2 answers
(`rmc:eval:online:sample_rate`, default 5%, `max_per_day` 200) is sent to the audit content store as an encrypted
`eval_sample` event for the eval-worker's online judge (Tier 2); telemetry stays metadata.
"""
from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.parse

from rm_copilot import tools as t
from services.agents import AgentGraph
from services.common.config import AUD, StackConfig
from services.common.contracts import JTI_RE, RemoteAuditSink, valid_customer_id
from services.common.flags import FLAG_DEFAULTS, ConfigValues, FeatureFlags  # noqa: F401 - re-exported
from services.common.httpkit import HttpError, Request, Response, TransportError, request_context
from services.common.opsmetrics import FINOPS_PREFIX, ONLINE_PREFIX, OpsRecorder, online_from, prices_from
from services.common.service import Service
from services.common.telemetry import current_span
from services.orchestrator_adapters import Budget, HttpTools, rm_user


def decline_code(reason: str, reason_code: str | None = None) -> str:
    if reason_code:
        return reason_code.lower()  # LLD §17.2 decline codes (WEAK_RETRIEVAL, LOW_ANSWER_RELEVANCY, ...)
    for needle, code in (("no approved document", "weak_retrieval"), ("insufficient", "insufficient_evidence"),
                         ("no time to retry", "ungrounded_no_time"), ("twice", "ungrounded"), ("content safety", "harm")):
        if needle in reason:
            return code
    return "other"


class EvalSampler:
    """Tier 2 producer side: picks a deterministic share of released UC1/UC2 answers (hash of the request id) within a
    daily cap and sends question, kept passages and answer to the audit content store (encrypted per customer key)."""

    def __init__(self, svc: "Orchestrator"):
        self.svc = svc
        self._day, self._count = "", 0
        self._lock = threading.Lock()
        self.m_items = svc.metrics.counter("rmc_online_eval_items_total", "Online evaluation items by outcome", ("use_case", "outcome"))

    def maybe(self, material: dict | None) -> bool:
        if not material:
            return False
        cfg = online_from(self.svc.config.get_prefix(ONLINE_PREFIX))
        ctx = request_context()
        draw = int(hashlib.sha256(ctx.request_id.encode()).hexdigest()[:8], 16) / 0x100000000
        if draw >= float(cfg["sample_rate"]):
            return False
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            if day != self._day:
                self._day, self._count = day, 0
            if self._count >= cfg["max_per_day"]:
                self.m_items.inc(use_case=material["use_case"], outcome="skipped_cap")
                return False
            self._count += 1
        try:
            self.svc.audit.emit("eval_sample", content=material, use_case=material["use_case"], contexts=len(material["contexts"]))
        except HttpError:
            self.m_items.inc(use_case=material["use_case"], outcome="sample_error")  # sampling never fails the RM's request
            return False
        self.m_items.inc(use_case=material["use_case"], outcome="sampled")
        return True


class Orchestrator(Service):
    name = "orchestrator"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self.audit = RemoteAuditSink(cfg, self.client, self.tokens, self.name, transport_key=self.transport_key)
        self._transport: tuple[float, dict] | None = None
        self.tools = HttpTools(self)  # REST: meetings, write status and the commit (not agent tools)
        self.flags = FeatureFlags(self)
        self.config = ConfigValues(self)
        self.ops = OpsRecorder(self.metrics)
        self.m_gate_config_rejected = self.metrics.counter("rmc_gate_config_rejected_total", "Gate thresholds outside the sanity range (default used)", ("key",))
        self.sampler = EvalSampler(self)
        self.agents = AgentGraph(self)
        self.procedural = self.agents.procedural
        m = self.metrics
        self.m_requests = m.counter("usecase_requests_total", "Use-case requests by outcome", ("use_case", "outcome"))
        self.m_declines = m.counter("declines_total", "UC2 declines by reason", ("use_case", "reason"))
        self.m_degraded = m.counter("degraded_responses_total", "Degraded-mode responses (model unavailable)", ("use_case",))
        self.m_rejected = m.counter("guardrail_events_total", "Guardrail detections", ("type",))
        a = self.app.route
        a("POST", "/v1/brief")(lambda r: self._run(r, "uc1", self._brief))
        a("POST", "/v1/ask")(lambda r: self._run(r, "uc2", self._ask))
        a("POST", "/v1/followup")(lambda r: self._run(r, "uc3", self._followup))
        a("POST", "/v1/commit")(lambda r: self._run(r, "commit", self._commit))
        a("GET", "/v1/writes/{jti}")(lambda r: self._run(r, "status", self._writes))
        a("GET", "/v1/meetings")(lambda r: self._run(r, "meetings", lambda r, u: {"value": self.tools.meetings()}))

    def prices(self) -> dict:
        """Price table (App Configuration `rmc:finops:*`, illustrative); the built-in illustrative values where missing."""
        return prices_from(self.config.get_prefix(FINOPS_PREFIX))

    def workload_assertion(self) -> str:
        """The pod's workload identity assertion: the only credential the agent identities federate with (C24)."""
        return self.tokens.app_token(f"{AUD['token-exchange']}/.default")

    def transport_key(self) -> dict:
        """Public half of Key Vault key `audit-transport`, published in App Configuration (cached 5 min)."""
        if self._transport and self._transport[0] > time.monotonic():
            return self._transport[1]
        key = urllib.parse.quote("audit/transport-public-key", safe="")
        try:
            r = self.client.request("GET", f"{self.cfg.url('appconfig')}/kv/{key}?api-version=2023-11-01",
                                    headers=self.auth_headers("appconfig"), timeout=1.0, peer="appconfig")
        except TransportError:
            r = None
        if r is None or not r.ok:
            if self._transport:
                return self._transport[1]
            raise HttpError(503, "DEPENDENCY_UNAVAILABLE", "audit transport key unavailable", details={"reason": "AUDIT"})
        self._transport = (time.monotonic() + 300, json.loads(r.json()["value"]))
        return self._transport[1]

    # ------------------------------------------------------------------ plumbing
    def _run(self, r: Request, use_case: str, fn) -> Response | dict:
        self.validator("orchestrator").validate(r.bearer(), scopes=("Copilot.Invoke",), azp=("web-bff",), app_only=True)
        rm_token = r.bearer("x-rm-authorization")
        claims = self.validator("tool-gateway").validate(rm_token, scopes=("Tools.Read",), app_only=False)
        request_context().data.update(rm_token=rm_token, rm_oid=claims["oid"], use_case=use_case, budget=Budget(), lineage={}, tool_calls=[])
        if (sp := current_span()) is not None:
            sp.set(**{"rmc.use_case": use_case})
        flag = {"uc1": "uc1", "uc2": "uc2", "uc3": "uc3", "commit": "writes"}.get(use_case)
        if flag and not self.flags.enabled(flag):
            self.m_requests.inc(use_case=use_case, outcome="feature_disabled")
            raise HttpError(503, "FEATURE_DISABLED", "This feature is switched off right now.", details={"flag": flag}, retryable=False)
        data = request_context().data
        try:
            result = fn(r, rm_user(claims))
        except t.NotEntitled:
            self.m_requests.inc(use_case=use_case, outcome="not_entitled")
            raise HttpError(403, "NOT_ENTITLED", "You are not entitled to this customer.") from None
        except HttpError as e:
            self.m_requests.inc(use_case=use_case, outcome=e.code.lower())
            if e.code == "BUDGET_EXCEEDED":
                reason = str(e.details.get("reason") or "")
                budget = e.details.get("budget") or {"REQUEST_STEPS": "tool_steps", "AGENT_HOPS": "hops", "RM_DAILY": "rm_daily"}.get(reason, reason.lower())
                self.ops.budget.inc(use_case=use_case, budget=budget, action="refused")
            elif e.code == "DEPENDENCY_TIMEOUT":
                self.ops.budget.inc(use_case=use_case, budget="deadline", action="refused")
            if use_case == "uc1" and e.details.get("source") == "crm":
                self.ops.gate(use_case, "profile_present", "declined", {})  # no brief without the CRM profile
            raise
        finally:
            b = data.get("budget")
            if use_case in ("uc1", "uc2", "uc3") and b is not None:
                self.ops.request_tokens(use_case, b.input_tokens, b.output_tokens)
        if isinstance(result, dict):  # mode and warnings were set by the graph's respond node
            outcome = "degraded" if result.get("degraded") or result.get("status") == "degraded" else str(result.get("status", "ok"))
            self.m_requests.inc(use_case=use_case, outcome=outcome)
            if outcome == "degraded":
                self.m_degraded.inc(use_case=use_case)
            for gate in (result.get("evaluation") or {}).get("gates", []):
                self.ops.gate(use_case, gate["gate"], gate["outcome"], gate.get("scores") or {})
            if use_case in ("uc1", "uc2", "uc3"):
                b = data.get("budget")
                sampled = self.sampler.maybe(data.get("eval_material")) if use_case in ("uc1", "uc2") and outcome not in ("declined", "degraded") else False
                result["ops"] = {"tokens": {"input": getattr(b, "input_tokens", 0), "output": getattr(b, "output_tokens", 0)},
                                 "model_calls": getattr(b, "model_calls", 0), "cost_aed": round(data.get("cost_aed", 0.0), 6),
                                 "stages_ms": dict(data.get("stages") or {}), "sampled_for_eval": sampled}
                if (sp := current_span()) is not None:
                    sp.set(**{"rmc.sampled": sampled, "rmc.cost_aed": round(data.get("cost_aed", 0.0), 6)})
        return result

    @staticmethod
    def _body(r: Request) -> dict:
        body = r.json()
        if not isinstance(body, dict):
            raise HttpError(400, "VALIDATION_FAILED", "the request body must be a JSON object", details={"reason": "SCHEMA"})
        return body

    def _customer(self, r: Request) -> str:
        # A plain id only: it is interpolated into tool-gateway URLs and names the audit content key (C21).
        cid = self._body(r).get("customer_id", "")
        if not valid_customer_id(cid):
            raise HttpError(400, "VALIDATION_FAILED", "customer_id is required", details={"reason": "SCHEMA", "field": "/customer_id"})
        request_context().data["customer_id"] = cid
        return cid

    # ------------------------------------------------------------------ use cases (the agent graph)
    def _context(self, r: Request) -> dict | None:
        """Session id and RM profile, set by web-bff (it overwrites anything a client sent under `context`)."""
        ctx = self._body(r).get("context")
        return ctx if isinstance(ctx, dict) else None

    def _brief(self, r: Request, user: t.User) -> dict:
        out = self.agents.run("uc1", user, self._customer(r), {}, self._context(r))
        if out["rejected_model_output"]:
            self.m_rejected.inc(len(out["rejected_model_output"]), type="model_output_rejected")
        return out

    def _ask(self, r: Request, user: t.User) -> dict:
        q = str(self._body(r).get("question", "")).strip()
        if not q or len(q) > 2000:
            raise HttpError(400, "VALIDATION_FAILED", "question is required (max 2000 characters)", details={"reason": "SCHEMA"})
        out = self.agents.run("uc2", user, None, {"question": q}, self._context(r))
        if out["status"] == "declined":
            self.m_declines.inc(use_case="uc2", reason=decline_code(out["reason"], out.get("reason_code")))
        return out

    def _followup(self, r: Request, user: t.User) -> dict:
        notes = str(self._body(r).get("notes", ""))
        if not notes.strip() or len(notes) > 8000:
            raise HttpError(400, "VALIDATION_FAILED", "notes are required (max 8000 characters)", details={"reason": "SCHEMA"})
        return self.agents.run("uc3", user, self._customer(r), {"notes": notes}, self._context(r))

    def _commit(self, r: Request, user: t.User) -> Response:
        body = self._body(r)
        cid = self._customer(r)
        draft = body.get("draft") or {}
        # The full approved draft (incl. email text) travels unchanged: the token's hash covers all of it.
        if body.get("run_id"):  # resume the paused UC3 run from its checkpoint (any orchestrator process)
            result = self.agents.resume(str(body["run_id"]), user, cid, {"draft": draft, "approval_token": body.get("approval_token")},
                                        self._context(r))
        else:  # a commit without a paused run (e.g. a draft typed by the RM): the same write API, no agent involved
            result = self.tools.create_call_report(user, cid, draft, approval_token=body.get("approval_token"))
        return Response(result.pop("http_status", 200) or 200, result)

    def _writes(self, r: Request, user: t.User) -> Response:
        if not JTI_RE.fullmatch(r.params["jti"]):
            raise HttpError(404, "NOT_FOUND", "no write with this id for you")
        resp = self.tools.write_status(r.params["jti"])
        return Response(resp.status, resp.json())
