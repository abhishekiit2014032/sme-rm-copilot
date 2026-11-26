"""HTTP adapters that let the unchanged `rm_copilot.Copilot` workflows run inside the orchestrator service.

* `ApimChatModel`  (LLMClient) -> APIM AI gateway -> Azure OpenAI chat completions
* `HttpTools`      (ToolPort)  -> tool-gateway (RM token forwarded unchanged + orchestrator workload token)
* `HttpGuard`      (GuardPort) -> Azure AI Content Safety and Azure AI Language, with the local rules as a floor
                                  that always runs (plan C2): if a service is unreachable the local check decides.

Per-request state (RM token, budgets, lineage) lives in the request context, never in the shared objects.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from rm_copilot import guardrails as g
from rm_copilot import tools as t
from rm_copilot.llm import EXTRACT_SYSTEM, QUERY_REWRITE_SYSTEM, ModelUnavailable
from rm_copilot.ports import LocalGuard
from services.common.contracts import FOLLOWUP_SUBJECT, outlook_compose_link
from services.common.httpkit import ClientResponse, HttpError, TransportError, request_context, run_parallel
from services.common.telemetry import current_span

if TYPE_CHECKING:
    from services.orchestrator import Orchestrator

AOAI_API_VERSION = "2024-10-21"
CS_API_VERSION = "2024-09-01"
LANG_API_VERSION = "2024-11-01"
PII_LOCAL_NAME = {"EmiratesIdNumber": "national_id", "InternationalBankingAccountNumber": "iban", "CreditCardNumber": "card",
                  "TradeLicenceNumber": "trade_licence", "Email": "email", "PhoneNumber": "phone"}


@dataclass
class Budget:
    """Per-request budget (plan §5): at most 6 tool steps, 12k input and 1.5k output tokens."""
    max_tool_steps: int = 6
    max_input_tokens: int = 12000
    max_output_tokens: int = 1500
    tool_steps: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    lineage: dict = field(default_factory=dict)

    def step(self) -> None:
        self.tool_steps += 1
        if self.tool_steps > self.max_tool_steps:
            raise HttpError(429, "BUDGET_EXCEEDED", "tool step budget exceeded", details={"reason": "REQUEST_STEPS"}, retryable=False)

    def output_cap(self) -> int:
        if self.input_tokens >= self.max_input_tokens or self.output_tokens >= self.max_output_tokens:
            raise HttpError(429, "BUDGET_EXCEEDED", "token budget exceeded", details={"reason": "REQUEST_TOKENS"}, retryable=False)
        return self.max_output_tokens - self.output_tokens


def _budget() -> Budget:
    return request_context().data.setdefault("budget", Budget())


def _lineage() -> dict:
    return request_context().data.setdefault("lineage", {})


# --------------------------------------------------------------------------- model
class ApimChatModel:
    def __init__(self, svc: "Orchestrator"):
        self.svc = svc
        self.m_tokens = svc.metrics.counter("llm_tokens_total", "Tokens used by the orchestrator", ("deployment", "use_case", "type"))

    def complete(self, system: str, user: str) -> str:
        deployment = "gen-small" if system.startswith((EXTRACT_SYSTEM[:40], QUERY_REWRITE_SYSTEM[:40])) else "gen-large"
        ctx = request_context()
        use_case = ctx.data.get("use_case", "unknown")
        body = {"messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "temperature": 0, "max_tokens": _budget().output_cap(), "store": False}
        headers = {**self.svc.auth_headers("apim"), "x-rm-oid": ctx.data.get("rm_oid", ""), "x-use-case": use_case}
        try:
            r = self.svc.client.request("POST", f"{self.svc.cfg.url('apim')}/openai/deployments/{deployment}/chat/completions?api-version={AOAI_API_VERSION}",
                                        json_body=body, headers=headers, timeout=20.0, peer="apim")
        except TransportError as e:
            raise ModelUnavailable(f"model call {e.kind}") from None
        self._raise_for_gateway(r)
        j = r.json()
        content = j["choices"][0]["message"]["content"]
        usage = j.get("usage", {})
        b = _budget()
        b.input_tokens += usage.get("prompt_tokens", 0)
        b.output_tokens += usage.get("completion_tokens", 0)
        self.m_tokens.inc(usage.get("prompt_tokens", 0), deployment=deployment, use_case=use_case, type="prompt")
        self.m_tokens.inc(usage.get("completion_tokens", 0), deployment=deployment, use_case=use_case, type="completion")
        prompt_hash = hashlib.sha256(system.encode()).hexdigest()[:16]
        lin = _lineage()
        lin.setdefault("models", set()).add(f"{deployment}:{j.get('model')}")
        lin.setdefault("prompts", {})[deployment] = prompt_hash
        sp = current_span()
        if sp:
            sp.set(**{"gen_ai.request.model": deployment, "gen_ai.response.model": j.get("model"),
                      "gen_ai.usage.input_tokens": usage.get("prompt_tokens"), "gen_ai.usage.output_tokens": usage.get("completion_tokens")})
        # Masked prompt and output go to the audit content store only (encrypted per customer), never to telemetry.
        self.svc.audit.emit("model_call", content={"input": user, "output": content}, deployment=deployment, model=j.get("model"),
                            prompt_template=prompt_hash, backend=r.headers.get("x-apim-backend"),
                            tokens_in=usage.get("prompt_tokens"), tokens_out=usage.get("completion_tokens"))
        return content

    @staticmethod
    def _raise_for_gateway(r: ClientResponse) -> None:
        if r.ok:
            return
        msg = str(r.json().get("message", ""))
        if r.status == 429:
            reason = "TOKENS_PER_MINUTE" if "Token" in msg else "REQUESTS_PER_MINUTE"
            raise HttpError(429, "RATE_LIMITED", "Your copilot usage limit was reached. Try again shortly.",
                            headers={"Retry-After": r.headers.get("retry-after", "60")},
                            details={"reason": reason, "retry_after_ms": 1000 * int(r.headers.get("retry-after", "60"))})
        if r.status == 403 and "quota" in msg.lower():
            raise HttpError(429, "BUDGET_EXCEEDED", "Your daily copilot allowance is used up. It resets tomorrow.", headers={"Retry-After": "3600"},
                            details={"reason": "RM_DAILY"}, retryable=False)
        if r.status == 400 and r.error_code() == "content_filter":
            raise HttpError(422, "GUARDRAIL_BLOCKED", "The request was blocked by the model's content filter.", details={"reason": "CONTENT_FILTER"})
        raise ModelUnavailable(f"gateway returned {r.status} ({r.headers.get('x-apim-failover', 'error')})")


# --------------------------------------------------------------------------- tools
class HttpTools:
    def __init__(self, svc: "Orchestrator"):
        self.svc = svc

    def _call(self, method: str, path: str, body: dict | None = None, timeout: float | None = None) -> ClientResponse:
        _budget().step()
        ctx = request_context()
        headers = {**self.svc.auth_headers("tool-gateway"), "x-rm-authorization": "Bearer " + ctx.data["rm_token"]}
        try:
            r = self.svc.client.request(method, f"{self.svc.cfg.url('tool-gateway')}{path}", json_body=body, headers=headers,
                                        timeout=timeout or 6.0, peer="tool-gateway")
        except TransportError as e:
            raise HttpError(504, "DEPENDENCY_TIMEOUT", "The tool layer did not answer in time.") if e.kind in ("timeout", "deadline") else \
                HttpError(503, "DEPENDENCY_UNAVAILABLE", "The tool layer is unavailable.", details={"reason": "TOOL_GATEWAY"})
        if r.headers.get("x-tool-contract"):
            _lineage()["tool_contract"] = r.headers["x-tool-contract"]
        if r.status in (401, 403) and r.error_code() == "NOT_ENTITLED":
            raise t.NotEntitled("not entitled to this customer")
        if r.status >= 400 and r.status not in (409,):
            err = r.json().get("error", {}) if isinstance(r.json(), dict) else {}
            raise HttpError(r.status, err.get("code", "TOOL_ERROR"), err.get("message", "tool call failed"),
                            details={k: v for k, v in err.items() if k not in ("code", "message")})
        return r

    def brief_inputs(self, user: t.User, customer_id: str) -> dict:
        return self._call("GET", f"/tools/customers/{customer_id}/brief-inputs").json()

    def get_customer_profile(self, user: t.User, customer_id: str) -> dict:
        return self._call("GET", f"/tools/customers/{customer_id}/profile").json()

    def search_policies(self, user: t.User, query: str, k: int = 3) -> list[dict]:
        j = self._call("POST", "/tools/policies/search", {"query": query, "k": k}).json()
        _lineage()["index_version"] = j.get("index_version")
        return j["value"]

    def meetings(self) -> list[dict]:
        return self._call("GET", "/tools/meetings").json()["value"]

    def write_status(self, jti: str) -> ClientResponse:
        return self._call("GET", f"/tools/writes/{jti}")

    def draft_email(self, user: t.User, customer_id: str, body: str) -> dict:
        # No mail scope (plan C20): the email stays text in the UI and opens in Outlook via a compose deeplink.
        return {"to_customer": customer_id, "body": body, "status": "draft", "compose_link": outlook_compose_link(FOLLOWUP_SUBJECT, body)}

    def create_call_report(self, user: t.User, customer_id: str, draft: dict, approval_token: str | None = None) -> dict:
        r = self._call("POST", f"/tools/customers/{customer_id}/call-reports", {"draft": draft, "approval_token": approval_token},
                       timeout=self.svc.cfg.settings.deadline_commit_s)
        return {**r.json(), "http_status": r.status}

    def audit(self, event: str, **fields) -> None:
        fields.pop("user", None)  # the RM identity in audit events comes from the validated token, not from workflow fields
        self.svc.audit.emit(event, **{k: v for k, v in fields.items() if isinstance(v, (str, int, float, bool, list)) or v is None})


# --------------------------------------------------------------------------- guardrails
class HttpGuard:
    def __init__(self, svc: "Orchestrator"):
        self.svc = svc
        self.local = LocalGuard()
        m = svc.metrics
        self.m_events = m.counter("guardrail_events_total", "Guardrail detections", ("type",))
        self.m_fallback = m.counter("guardrail_fallback_total", "Safety service unreachable; local rules decided", ("service",))

    def _post(self, service: str, path: str, body: dict) -> dict | None:
        try:
            r = self.svc.client.request("POST", f"{self.svc.cfg.url(service)}{path}", json_body=body,
                                        headers=self.svc.auth_headers("cognitive"), timeout=2.0, peer=service)
        except TransportError:
            r = None
        if r is None or not r.ok:
            self.m_fallback.inc(service=service)
            return None
        return r.json()

    def _shield(self, user_prompt: str, documents: list[str]) -> tuple[bool, list[bool]]:
        chunks = [documents[i:i + 5] for i in range(0, len(documents), 5)] or [[]]
        results = run_parallel({str(i): (lambda c=c, i=i: self._post("content-safety", f"/contentsafety/text:shieldPrompt?api-version={CS_API_VERSION}",
                                                                     {"userPrompt": user_prompt if i == 0 else "", "documents": c}))
                                for i, c in enumerate(chunks)})
        prompt_attack, docs = False, []
        for i, c in enumerate(chunks):
            j = results[str(i)][0]
            if j is None:
                docs += [False] * len(c)
                continue
            prompt_attack |= bool(j.get("userPromptAnalysis", {}).get("attackDetected")) and i == 0
            docs += [bool(d.get("attackDetected")) for d in j.get("documentsAnalysis", [])]
        return prompt_attack, docs

    def prompt_attack(self, text: str) -> bool:
        remote, _ = self._shield(text, [])
        hit = remote or self.local.prompt_attack(text)
        if hit:
            self.m_events.inc(type="prompt_attack")
        return hit

    def _doc_flags(self, documents: list[str]) -> list[bool]:
        _, remote = self._shield("", documents)
        return [r or l for r, l in zip(remote, self.local.shield_documents(documents))]

    def shield_documents(self, documents: list[str]) -> list[bool]:
        flags = self._doc_flags(documents)
        if any(flags):
            self.m_events.inc(sum(flags), type="document_attack")
        return flags

    def quarantine_quotes(self, text: str) -> tuple[str, list[str]]:
        quotes = [m.group(0) for m in re.finditer(r'"[^"]*"|“[^”]*”', text)]
        flagged = set(q for q, bad in zip(quotes, self._doc_flags(quotes)) if bad) if quotes else set()
        removed: list[str] = []

        def repl(m: re.Match) -> str:
            if m.group(0) in flagged:
                removed.append(m.group(0))
                return "[quoted customer text removed]"
            return m.group(0)
        out = re.sub(r'"[^"]*"|“[^”]*”', repl, text)
        if removed:
            self.m_events.inc(len(removed), type="quote_quarantined")
        return out, removed

    def mask_pii(self, text: str) -> tuple[str, list[str]]:
        j = self._post("language", f"/language/:analyze-text?api-version={LANG_API_VERSION}",
                       {"kind": "PiiEntityRecognition", "parameters": {"modelVersion": "latest", "redactionPolicy": {"policyKind": "entityMask"}},
                        "analysisInput": {"documents": [{"id": "1", "language": "en", "text": text}]}})
        found: list[str] = []
        if j:
            ents = (j.get("results", {}).get("documents") or [{}])[0].get("entities", [])
            for e in sorted(ents, key=lambda e: e["offset"], reverse=True):
                name = PII_LOCAL_NAME.get(e["category"], "pii")
                text = text[:e["offset"]] + f"[{name.upper()}]" + text[e["offset"] + e["length"]:]
                if name not in found:
                    found.append(name)
        text, local_found = g.mask_pii(text)  # regex floor always runs (plan §3.3)
        found += [n for n in local_found if n not in found]
        if found:
            self.m_events.inc(type="pii_masked")
        return text, found

    def groundedness(self, answer: str, passages: list[dict]) -> dict:
        check = self.local.groundedness(answer, passages)  # always runs
        j = self._post("content-safety", "/contentsafety/text:detectGroundedness?api-version=2024-09-15-preview",
                       {"domain": "Generic", "task": "Summarization", "text": answer, "groundingSources": [p["text"] for p in passages]})
        if j and j.get("ungroundedDetected") and check["ok"]:
            check = {**check, "ok": False, "service": "ungrounded"}
        if not check["ok"]:
            self.m_events.inc(type="ungrounded")
        return check

    def harmful(self, text: str) -> bool:
        j = self._post("content-safety", f"/contentsafety/text:analyze?api-version={CS_API_VERSION}", {"text": text, "outputType": "FourSeverityLevels"})
        hit = bool(j) and any(c.get("severity", 0) >= 4 for c in j.get("categoriesAnalysis", []))
        if hit:
            self.m_events.inc(type="harm_blocked")
        return hit


def rm_user(claims: dict) -> t.User:
    upn = claims.get("preferred_username", "")
    return t.User(id=upn.split("@")[0], name=claims.get("name", ""), portfolio=frozenset(), clearance="internal")
