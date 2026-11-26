"""Azure API Management mock: the AI gateway in front of Azure OpenAI and the system API facade in front of the
systems of record (plan §3.3). Implements the policies the design depends on:

AI gateway  POST /openai/deployments/{deployment}/chat/completions | /embeddings   (same shape as Azure OpenAI)
  1. `validate-jwt`: workload token, audience `api://rm-copilot-apim`, role `AiGateway.Invoke`; chat deployments for
     the orchestrator only (LLD `AiGateway.Generate`), embeddings for any caller with the role.
  2. `rate-limit-by-key` per RM (calls per minute)                                   -> 429 + Retry-After
  3. `llm-token-limit` per RM (counter key = `x-rm-oid`, honoured only from the orchestrator and tool-gateway, which
     validate the RM token; any other caller is metered under its own identity):
     tokens per minute (prompt estimated up front)                                    -> 429 + Retry-After
     daily token quota                                                                -> 403 (APIM's quota behaviour)
  4. backend pool by priority (`aoai-primary` = 1, `aoai-secondary` = 2; both Regional Standard deployments in UAE
     North in the pilot, PTU is a post-pilot option) with a circuit breaker per backend that trips on
     429/5xx/timeouts honouring Retry-After, and a `retry` to the next backend, **only if at least
     `apim_min_failover_s` of the caller's deadline (`x-deadline-ms`) remains** (plan §5); otherwise 503 so the
     orchestrator answers in degraded mode.
  5. `llm-emit-token-metric`: `llm_tokens_total{deployment,use_case,type}`.
  `traceparent` / `x-request-id` pass through; request and response bodies are never logged.

System API facade  GET|POST /{system}/api/{path}   for crm, ods, los, kyc, news
  `validate-jwt` (tool-gateway or outbox-worker managed identity, `SystemApis.Read|Write`), a bulkhead circuit
  breaker per backend (5 failures in 30 s by default), timeout from the deadline, and the RM's `oid` forwarded in
  a header signed by APIM (`x-apim-rm-oid|ts|sig`) for legacy systems that cannot take Entra tokens (plan §3.4).

Admin (mock only): GET/POST /admin/policies, POST /admin/reset (counters and breakers).
Differences: in-memory counters on one "unit" (real APIM counters are per region/unit, see plan §10 item 6),
error bodies use APIM's `{"statusCode", "message"}` shape, no products/subscriptions, no semantic cache (off by design).
"""
from __future__ import annotations

import json
import math
import threading
import time
from collections import deque
from dataclasses import dataclass

from services.common.auth import TokenProvider
from services.common.config import AUD, SOR_SYSTEMS, StackConfig, scope
from services.common.httpkit import HttpError, Request, Response, TransportError, time_left
from services.common.resilience import STATE_CODE, CircuitBreaker
from services.common.telemetry import current_span

from ._base import Mock, realistic, sleep_ms
from .aoai import count_tokens
from .systems import sign_rm_header


@dataclass
class Backend:
    name: str
    url: str
    priority: int
    breaker: CircuitBreaker


CHAT_CALLERS = ("orchestrator",)                     # LLD §1.5: AiGateway.Generate (ingestion and tool-gateway: Embed only)
RM_KEY_CALLERS = ("orchestrator", "tool-gateway")     # validate an RM token before they set `x-rm-oid`


def _apim_error(status: int, message: str, headers: dict | None = None) -> Response:
    return Response(status, {"statusCode": status, "message": message}, headers=headers or {})


class ApimMock(Mock):
    name = "apim"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        s = cfg.settings
        self.policies = {"tokens_per_minute": s.apim_tokens_per_minute, "token_quota_per_day": s.apim_token_quota_per_day,
                         "calls_per_minute": s.apim_calls_per_minute, "min_failover_s": s.apim_min_failover_s}
        self.backends = [Backend("aoai-primary", cfg.url("aoai-primary"), 1, CircuitBreaker(1, 10, s.apim_breaker_trip_s)),
                         Backend("aoai-secondary", cfg.url("aoai-secondary"), 2, CircuitBreaker(1, 10, s.apim_breaker_trip_s))]
        self.sor_breakers = {k: CircuitBreaker(s.sor_breaker_failures, s.sor_breaker_window_s, s.sor_breaker_trip_s) for k in SOR_SYSTEMS}
        self._minute: dict[str, deque] = {}
        self._calls: dict[str, deque] = {}
        self._day: dict[str, tuple[str, int]] = {}
        self._lock = threading.Lock()
        self.tokens = TokenProvider(self.client, cfg.token_url, "apim", cfg.secrets["apim"])
        m = self.app.telemetry.metrics
        self.m_tokens = m.counter("llm_tokens_total", "Tokens through the AI gateway (llm-emit-token-metric)", ("deployment", "use_case", "type"))
        self.m_backend = m.counter("apim_backend_requests_total", "Requests forwarded per backend", ("backend", "status"))
        self.m_reject = m.counter("apim_policy_rejections_total", "Requests rejected by a gateway policy", ("policy",))
        self.m_breaker = m.gauge("circuit_breaker_state", "0 closed, 1 open, 2 half-open", ("backend",))
        self._publish_breakers()
        a = self.app.route
        a("POST", "/openai/deployments/{deployment}/chat/completions")(self._ai)
        a("POST", "/openai/deployments/{deployment}/embeddings")(self._ai)
        a("GET", "/{system}/api/{rest:path}")(self._sor)
        a("POST", "/{system}/api/{rest:path}")(self._sor)
        a("GET", "/admin/policies")(lambda r: {**self.policies, "breakers": self._breaker_states()})
        a("POST", "/admin/policies")(self._set_policies)
        a("POST", "/admin/reset")(lambda r: (self.reset(), {"status": "reset"})[1])

    # ------------------------------------------------------------------ admin
    def _set_policies(self, r: Request) -> dict:
        for k, v in r.json().items():
            if k in self.policies:
                self.policies[k] = type(self.policies[k])(v)
        return self.policies

    def reset(self) -> None:
        with self._lock:
            self._minute.clear()
            self._calls.clear()
            self._day.clear()
        for b in self.backends:
            b.breaker.reset()
        for br in self.sor_breakers.values():
            br.reset()
        self._publish_breakers()

    def _breaker_states(self) -> dict:
        return {**{b.name: b.breaker.state() for b in self.backends}, **{k: v.state() for k, v in self.sor_breakers.items()}}

    def _publish_breakers(self) -> None:
        for name, st in self._breaker_states().items():
            self.m_breaker.set(STATE_CODE[st], backend=name)

    # ------------------------------------------------------------------ AI gateway
    def _admit(self, key: str, estimate: int) -> Response | None:
        now, day = time.time(), time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            calls = self._calls.setdefault(key, deque())
            while calls and now - calls[0] > 60:
                calls.popleft()
            if len(calls) >= self.policies["calls_per_minute"]:
                self.m_reject.inc(policy="rate-limit-by-key")
                wait = max(1, math.ceil(60 - (now - calls[0])))
                return _apim_error(429, f"Rate limit is exceeded. Try again in {wait} seconds.", {"Retry-After": str(wait)})
            used_day = self._day.get(key, (day, 0))[1] if self._day.get(key, (day, 0))[0] == day else 0
            if used_day + estimate > self.policies["token_quota_per_day"]:
                self.m_reject.inc(policy="llm-token-limit-quota")
                return _apim_error(403, "Token quota is exceeded. Try again after the quota period resets.", {"x-apim-quota": "exceeded"})
            window = self._minute.setdefault(key, deque())
            while window and now - window[0][0] > 60:
                window.popleft()
            used = sum(t for _, t in window)
            if used + estimate > self.policies["tokens_per_minute"]:
                self.m_reject.inc(policy="llm-token-limit-tpm")
                wait = max(1, math.ceil(60 - (now - window[0][0]))) if window else 60
                return _apim_error(429, f"Token limit is exceeded. Try again in {wait} seconds.", {"Retry-After": str(wait)})
            calls.append(now)
        return None

    def _account(self, key: str, tokens: int) -> int:
        now, day = time.time(), time.strftime("%Y-%m-%d", time.gmtime())
        with self._lock:
            self._minute.setdefault(key, deque()).append((now, tokens))
            d, used = self._day.get(key, (day, 0))
            self._day[key] = (day, (used if d == day else 0) + tokens)
            return self.policies["tokens_per_minute"] - sum(t for _, t in self._minute[key])

    def _ai(self, r: Request) -> Response:
        if realistic(self.cfg):
            sleep_ms(4)
        self.require_api_version(r)
        claims = self.caller(r, AUD["apim"], scopes=("AiGateway.Invoke",), app_only=True)
        deployment, use_case = r.params["deployment"], (r.header("x-use-case") or "unknown")[:24]
        if r.path.endswith("/chat/completions") and claims["azp"] not in CHAT_CALLERS:
            self.m_reject.inc(policy="validate-jwt-role")
            return _apim_error(403, "Caller is not allowed to call generation deployments.")
        # Per-RM quota key: only callers that hold a validated RM token may name the RM; anyone else is metered as itself.
        key = (r.header("x-rm-oid") if claims["azp"] in RM_KEY_CALLERS else None) or claims["azp"]
        body = r.json()
        text = json.dumps(body.get("messages") if "messages" in body else body.get("input"))
        rejected = self._admit(key, count_tokens(text))
        if rejected:
            return rejected
        sp = current_span()
        last: Response | None = None
        attempted, skipped_for_deadline = 0, False
        for b in sorted(self.backends, key=lambda x: x.priority):
            if not b.breaker.allow():
                continue
            if attempted and time_left() < self.policies["min_failover_s"]:
                skipped_for_deadline = True
                break
            attempted += 1
            try:
                up = self.client.request("POST", f"{b.url}{r.path}?api-version={r.query.get('api-version')}", data=r.body,
                                         headers={"content-type": "application/json", "authorization": "Bearer " + self.tokens.app_token(scope("cognitive"))},
                                         timeout=30.0, peer=b.name)
            except TransportError as e:
                b.breaker.failure()
                self.m_backend.inc(backend=b.name, status=e.kind)
                last = _apim_error(504 if e.kind in ("timeout", "deadline") else 502, f"Backend {e.kind}")
                continue
            self.m_backend.inc(backend=b.name, status=str(up.status))
            if up.status == 429 or up.status >= 500:
                ra = float(up.headers.get("retry-after-ms", 0)) / 1000 or float(up.headers.get("retry-after", 0) or 0)
                b.breaker.failure(ra)
                last = Response(up.status, up.body, {k: v for k, v in up.headers.items() if k.startswith("retry-after")}, content_type="application/json")
                continue
            if up.ok:
                b.breaker.success()
            self._publish_breakers()
            usage = up.json().get("usage", {}) if up.ok else {}
            remaining = self._account(key, int(usage.get("total_tokens", 0)))
            self.m_tokens.inc(usage.get("prompt_tokens", 0), deployment=deployment, use_case=use_case, type="prompt")
            self.m_tokens.inc(usage.get("completion_tokens", 0), deployment=deployment, use_case=use_case, type="completion")
            if sp:
                sp.set(**{"rmc.backend": b.name, "gen_ai.request.model": deployment, "rmc.use_case": use_case})
            return Response(up.status, up.body, {"x-apim-backend": b.name, "x-apim-remaining-tokens-minute": str(max(0, remaining)),
                                                 "x-ms-region": up.headers.get("x-ms-region", "")}, content_type="application/json")
        self._publish_breakers()
        reason = "deadline" if skipped_for_deadline else ("all-open" if not attempted else "all-failed")
        self.m_reject.inc(policy=f"backend-pool-{reason}")
        if sp:
            sp.set(**{"rmc.reason": reason})
        return _apim_error(503, "No backend in the pool is available.", {"x-apim-failover": reason, **(last.headers if last else {})})

    # ------------------------------------------------------------------ system API facade
    def _sor(self, r: Request) -> Response:
        if realistic(self.cfg):
            sleep_ms(4)
        system = r.params["system"]
        if system not in SOR_SYSTEMS:
            raise HttpError(404, "NotFound", "no such API")
        claims = self.caller(r, AUD["apim"], scopes=("SystemApis.Read",) if r.method == "GET" else ("SystemApis.Write",),
                             app_only=True, azp=("tool-gateway", "outbox-worker"))
        breaker = self.sor_breakers[system]
        if not breaker.allow():
            self._publish_breakers()
            return _apim_error(503, f"Backend {system} circuit breaker is open.", {"x-apim-breaker": "open"})
        path = f"/api/{r.params['rest']}"
        url = f"{self.cfg.url(system)}{path}"
        if r.query:
            import urllib.parse
            url += "?" + urllib.parse.urlencode(r.query)
        headers = {"content-type": r.header("content-type") or "application/json", "x-caller-app": claims["azp"]}
        oid = r.header("x-rm-oid")
        if oid:
            ts = int(time.time())
            backend_path = "/" + url.split("/", 3)[3].split("?")[0]
            headers.update({"x-apim-rm-oid": oid, "x-apim-rm-ts": str(ts),
                            "x-apim-rm-sig": sign_rm_header(self.cfg.secrets["apim-backend-signing"], oid, r.method, backend_path, ts)})
        try:
            up = self.client.request(r.method, url, data=r.body or None, headers=headers, timeout=10.0, peer=system)
        except TransportError as e:
            breaker.failure()
            self._publish_breakers()
            return _apim_error(504 if e.kind in ("timeout", "deadline") else 502, f"Backend {system} {e.kind}.")
        if up.status >= 500:
            breaker.failure()
        else:
            breaker.success()
        self._publish_breakers()
        return Response(up.status, up.body, {}, content_type=up.headers.get("content-type", "application/json"))
