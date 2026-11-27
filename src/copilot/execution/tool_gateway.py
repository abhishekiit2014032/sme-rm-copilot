"""Diagram box: Tool gateway (MCP) (lane B), the only path to the Bank's systems.
Every call checks OIDC user identity, RBAC (role + customer entitlement), agent identity and workload identity.
Mocked: identity provider and entitlement service (mocks/identity); production is an API gateway with MCP policies.
"""
from __future__ import annotations

from copilot.contracts.context import RunContext
from copilot.contracts.identity import Role
from copilot.contracts.ports import EntitlementService, IdentityProvider
from copilot.contracts.tools import ToolCall, ToolResult
from copilot.contracts.trace import Component
from copilot.execution.mcp_servers import McpServer
from copilot.operations.resilience import CircuitBreaker, CircuitOpen, TransientError, call_with_resilience
from copilot.state.audit_log import AuditLog

# Agent identity -> the only tools that agent may call.
AGENT_ALLOW_LIST: dict[str, set[str]] = {
    "agent-brief": {
        "customer-data.get_customer_profile", "customer-data.get_interactions", "customer-data.get_open_requests",
        "customer-data.get_accounts_summary", "customer-data.get_facilities", "customer-data.get_trade_positions",
        "memory.recall_episodes", "product-rules.get_product_suggestions",
    },
    "agent-policy": {"policy-search.search_policy"},
    "agent-followup": {
        "customer-data.get_customer_profile", "memory.get_session",
        "crm-drafts.create_note_draft", "crm-drafts.create_task_drafts", "crm-drafts.create_email_draft",
    },
}

# Role -> MCP servers the role may use at all (RBAC, before the customer entitlement check).
ROLE_SERVERS: dict[Role, set[str]] = {
    Role.RM: {"customer-data", "policy-search", "crm-drafts", "memory", "product-rules"},
    Role.CREDIT_DESK: {"customer-data", "policy-search"},
}

# Workload identities allowed to call the gateway (the orchestrator service that hosts the agents).
TRUSTED_WORKLOADS = {"wl-orchestrator"}


class ToolDenied(Exception):
    trace_status = "denied"

    def __init__(self, check: str, reason: str) -> None:
        super().__init__(reason)
        self.check, self.reason = check, reason


class ToolGateway:
    def __init__(self, servers: list[McpServer], idp: IdentityProvider, entitlements: EntitlementService,
                 audit: AuditLog) -> None:
        self._servers = {s.name: s for s in servers}
        self._idp, self._entitlements, self._audit = idp, entitlements, audit
        self._breakers: dict[str, CircuitBreaker] = {}

    def call(self, call: ToolCall, ctx: RunContext) -> ToolResult:
        with ctx.trace.span(Component.TOOL_GATEWAY, call.qualified_name, agent=call.agent_id) as span:
            try:
                self._check_user(ctx)
                self._check_rbac(call, ctx)
                self._check_agent(call)
                self._check_workload(call)
            except ToolDenied as denied:
                span.status, span.detail = "denied", f"{denied.check}: {denied.reason}"
                self._audit.append(ctx.identity.user_id, f"tool {call.qualified_name}", _target(call),
                                   f"denied ({denied.check})", ctx.request_id, ctx.trace)
                raise
            span.detail = "OIDC + RBAC + agent + workload: allowed"
            result = self._invoke(call, ctx)
            if not result.ok:
                span.status, span.detail = "degraded", f"allowed; {result.error}"
            self._audit.append(ctx.identity.user_id, f"tool {call.qualified_name}", _target(call),
                               "ok" if result.ok else "unavailable", ctx.request_id)
            return result

    # ---- the four checks --------------------------------------------------------------------
    def _check_user(self, ctx: RunContext) -> None:
        """OIDC: the RM's own token travels with every call and is verified again here."""
        try:
            claims = self._idp.verify(ctx.identity.token)
        except Exception as exc:
            raise ToolDenied("oidc", f"token rejected ({exc})") from exc
        if claims["sub"] != ctx.identity.user_id:
            raise ToolDenied("oidc", "token does not match the caller")

    def _check_rbac(self, call: ToolCall, ctx: RunContext) -> None:
        """RBAC: the role must allow the server, and the RM must be entitled to the customer."""
        allowed_servers = set().union(*(ROLE_SERVERS.get(r, set()) for r in ctx.identity.roles))
        if call.server not in allowed_servers:
            raise ToolDenied("rbac", f"role may not use {call.server}")
        customer_id = call.args.get("customer_id")
        if customer_id and not self._entitlements.is_entitled(ctx.identity.user_id, customer_id):
            raise ToolDenied("entitlement", f"{ctx.identity.user_id} is not entitled to customer {customer_id}")

    def _check_agent(self, call: ToolCall) -> None:
        if call.qualified_name not in AGENT_ALLOW_LIST.get(call.agent_id, set()):
            raise ToolDenied("agent", f"{call.agent_id} may not call {call.qualified_name}")

    def _check_workload(self, call: ToolCall) -> None:
        if call.workload_id not in TRUSTED_WORKLOADS:
            raise ToolDenied("workload", f"unknown workload {call.workload_id}")

    # ---- the call itself --------------------------------------------------------------------
    def _invoke(self, call: ToolCall, ctx: RunContext) -> ToolResult:
        """Call the MCP server with retries and a breaker per system. A failing source becomes 'unavailable'."""
        server = self._servers[call.server]
        system = server.system_for(call.tool)
        breaker = self._breakers.setdefault(system, CircuitBreaker(system))
        with ctx.trace.span(Component.SYSTEMS_OF_RECORD, f"{system}: {call.tool}") as span:
            try:
                result = call_with_resilience(lambda: server.call(call.tool, call.args, ctx),
                                              breaker=breaker, trace=ctx.trace)
            except (TransientError, CircuitOpen) as exc:
                span.status, span.detail = "unavailable", str(exc)
                return ToolResult(ok=False, error=f"{system} unavailable", source_system=system)
            span.detail = f"as of {result.as_of}"
            return result


def _target(call: ToolCall) -> str:
    return call.args.get("customer_id") or call.server
