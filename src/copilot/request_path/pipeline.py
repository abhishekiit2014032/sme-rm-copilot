"""Diagram box: lane A end to end (RM -> gateway -> input guardrails -> cache -> router + orchestrator ->
output guardrails -> answer). Wires the boxes in diagram order and turns every outcome into an Answer with a trace.
Mocked: nothing here; each box it calls has its own mocks.
"""
from __future__ import annotations

from typing import Callable

from copilot.contracts.context import RunContext, new_id
from copilot.contracts.identity import Identity
from copilot.contracts.request import Answer, AnswerStatus, AskRequest, Request
from copilot.contracts.results import AgentResult
from copilot.contracts.trace import Component, Trace
from copilot.execution.approvals import ApprovalError
from copilot.execution.tool_gateway import ToolDenied
from copilot.operations.flags import Flags
from copilot.operations.tracing import Tracer
from copilot.request_path.gateway import ApiGateway, GatewayDenied
from copilot.request_path.input_guardrails import InputBlocked, InputGuardrails, mask_pii, unmask_pii
from copilot.request_path.orchestrator import Orchestrator, OrchestratorResult
from copilot.request_path.output_guardrails import OutputGuardrails
from copilot.request_path.router import Router
from copilot.request_path.semantic_cache import SemanticCache
from copilot.state.audit_log import AuditLog
from copilot.state.memory.session import SessionMemory

STATUS_FOR = {"ok": AnswerStatus.ANSWERED, "degraded": AnswerStatus.DEGRADED,
              "declined": AnswerStatus.DECLINED, "needs_approval": AnswerStatus.AWAITING_APPROVAL}
SAFE_DECLINE = "I could not verify an answer against the sources, so I will not guess. Please check with the " \
               "credit desk."


class RequestPipeline:
    def __init__(self, gateway: ApiGateway, input_guardrails: InputGuardrails, cache: SemanticCache,
                 router: Router, orchestrator: Orchestrator, output_guardrails: OutputGuardrails,
                 tracer: Tracer, flags: Flags, session: SessionMemory, audit: AuditLog) -> None:
        self.gateway, self.input_guardrails, self.cache = gateway, input_guardrails, cache
        self.router, self.orchestrator, self.output_guardrails = router, orchestrator, output_guardrails
        self.tracer, self.flags, self.session, self.audit = tracer, flags, session, audit

    # ---- POST /ask ------------------------------------------------------------------------------
    def ask(self, bearer_token: str, body: AskRequest) -> Answer:
        request_id = new_id("rq")
        trace = self.tracer.start(request_id)
        identity: Identity | None = None
        try:
            identity = self.gateway.admit(bearer_token, trace)
            trace.user_id = identity.user_id
            if not self.flags.check("copilot_enabled", trace):
                return self._answer(trace, AnswerStatus.DECLINED, "The copilot is switched off right now.")
            screened = self.input_guardrails.screen(body.text, trace)
            request = Request(request_id=request_id, user_id=identity.user_id, masked_text=screened.masked_text,
                              action=body.action, customer_id=body.customer_id)
            ctx = RunContext(request_id=request_id, identity=identity, trace=trace,
                             customer_id=body.customer_id, pii_vault=screened.pii_vault)
            answer = self._answer_request(request, ctx)
        except GatewayDenied as denied:
            answer = self._answer(trace, AnswerStatus.DENIED, denied.reason)
        except InputBlocked as blocked:
            answer = self._answer(trace, AnswerStatus.BLOCKED, str(blocked), notices=[blocked.category])
        except ToolDenied as denied:
            answer = self._answer(trace, AnswerStatus.DENIED,
                                  "You are not entitled to this customer's data, so nothing was retrieved."
                                  if denied.check == "entitlement" else f"Access denied ({denied.check}).",
                                  notices=[f"{denied.check}: {denied.reason}"])
        return self._finish(answer, identity, body, trace)

    def _answer_request(self, request: Request, ctx: RunContext) -> Answer:
        cached = self._from_cache(request, ctx)
        if cached is not None:
            return self._release(OrchestratorResult(path="cache", result=cached), request, ctx)
        route = self.router.route(request, ctx)
        outcome = self.orchestrator.run(route, request, ctx)
        answer = self._release(outcome, request, ctx, retry=lambda: self.orchestrator.run(route, request, ctx))
        if answer.status == AnswerStatus.ANSWERED and self.flags.semantic_cache and route.path == "fast":
            self.cache.store(request.masked_text, outcome.result, ctx.trace)
        return answer

    def _from_cache(self, request: Request, ctx: RunContext) -> AgentResult | None:
        if request.action or not self.flags.check("semantic_cache", ctx.trace):
            return None
        return self.cache.lookup(request.masked_text, ctx.trace)

    def _release(self, outcome: OrchestratorResult, request: Request, ctx: RunContext,
                 retry: Callable[[], OrchestratorResult] | None = None) -> Answer:
        """Output guardrails; on failure one stricter retry (temperature 0), then a safe decline."""
        verdict = self.output_guardrails.check(outcome.result, request.masked_text, ctx)
        if not verdict.passed and retry is not None:
            ctx.trace.event(Component.OUTPUT_GUARDRAILS, "retry once, stricter", status="retry",
                            detail="; ".join(verdict.failures))
            ctx.strict = True
            outcome = retry()
            verdict = self.output_guardrails.check(outcome.result, request.masked_text, ctx)
        if not verdict.passed:
            return self._answer(ctx.trace, AnswerStatus.DECLINED, SAFE_DECLINE, path=outcome.path,
                                notices=verdict.failures)
        result = outcome.result
        return Answer(
            request_id=ctx.request_id, status=STATUS_FOR[result.status], path=outcome.path,
            text=unmask_pii(result.text, ctx.pii_vault), citations=result.citations, sources=result.sources,
            drafts=[d.model_copy(update={"body": unmask_pii(d.body, ctx.pii_vault)}) for d in result.drafts],
            approval_token=outcome.approval_token, notices=result.notices, trace=ctx.trace,
        )

    # ---- POST /approve --------------------------------------------------------------------------
    def approve(self, bearer_token: str, approval_token: str) -> Answer:
        request_id = new_id("ap")
        trace = self.tracer.start(request_id)
        identity: Identity | None = None
        try:
            identity = self.gateway.admit(bearer_token, trace)
            trace.user_id = identity.user_id
            if not self.flags.check("crm_writes", trace):
                answer = self._answer(trace, AnswerStatus.DECLINED, "CRM writes are paused (kill switch). "
                                                                   "Your drafts are kept; approve again later.")
            else:
                ctx = RunContext(request_id=request_id, identity=identity, trace=trace)
                report = self.orchestrator.resume(approval_token, ctx)
                lines = [f"- {c['kind']}: {c['title']} ({c['crm_id']})" for c in report.committed]
                text = "Saved to the CRM:\n" + "\n".join(lines)
                if report.duplicates_ignored:
                    text += f"\n\nA lost CRM reply was retried; {report.duplicates_ignored} duplicate " \
                            "write(s) were ignored thanks to the idempotency key."
                answer = self._answer(trace, AnswerStatus.COMMITTED, text)
        except GatewayDenied as denied:
            answer = self._answer(trace, AnswerStatus.DENIED, denied.reason)
        except (ApprovalError, LookupError) as error:
            answer = self._answer(trace, AnswerStatus.REJECTED, f"Approval rejected: {error}.")
        return self._finish(answer, identity, AskRequest(text="approve follow-up"), trace, action="approve")

    # ---- shared ---------------------------------------------------------------------------------
    def _answer(self, trace: Trace, status: AnswerStatus, text: str, path: str = "none",
                notices: list[str] | None = None) -> Answer:
        return Answer(request_id=trace.request_id, status=status, path=path, text=text, notices=notices or [],
                      trace=trace)

    def _finish(self, answer: Answer, identity: Identity | None, body: AskRequest, trace: Trace,
                action: str | None = None) -> Answer:
        """Answer span, audit record, session turn, token accounting and trace export, for every outcome."""
        trace.event(Component.ANSWER, answer.status.value, detail=f"path {answer.path}")
        actor = identity.user_id if identity else "unknown"
        self.audit.append(actor, action or body.action or "ask", body.customer_id or "-", answer.status.value,
                          answer.request_id, trace)
        if identity:
            self.session.add_turn(identity.user_id, mask_pii(body.text)[0][:200], answer.status.value, trace)
            self.gateway.record_tokens(identity.user_id, int(trace.total("tokens_in") + trace.total("tokens_out")))
        self.tracer.finish(trace)
        return answer
