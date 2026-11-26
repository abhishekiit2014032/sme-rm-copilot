"""Azure OpenAI mock (one Foundry / Azure OpenAI resource; the stack runs two, `aoai-primary` and `aoai-secondary`,
both standing in for Regional Standard deployments in UAE North).

Imitates (api-version 2024-10-21):
  POST /openai/deployments/{deployment}/chat/completions   -> chat.completion with `usage`, content filter results
  POST /openai/deployments/{deployment}/embeddings         -> list of embeddings with `usage`
Deployments: `gen-large`, `gen-small` (chat) and `embed`. Answers come from `rm_copilot.llm.MockLLM`, so they
are deterministic and identical to the in-process evals. A built-in content filter mirrors Prompt Shields
(jailbreak in user messages -> HTTP 400 `content_filter`). `store` must not be true (plan §3.5).

Fault injection target = the instance name (`aoai-primary` / `aoai-secondary`), e.g. `status=429, error_rate=1,
retry_after_ms=2000` returns the real 429 shape with `retry-after-ms` / `retry-after` headers; `status=500` or
`outage=1` for server errors; `latency_ms` for slowness. With the realistic latency profile the response time is
time-to-first-token + per-output-token time for the deployment.

Differences: token counts are estimated (characters / 4, not tiktoken); embeddings are deterministic hashed
bag-of-words vectors (256 dims by default) rather than a neural model; no streaming, tools or logprobs.
Every request body received is recorded in `self.received` so tests can prove no PII reached the model.
"""
from __future__ import annotations

import hashlib
import math
import secrets
import threading
import time
from collections import deque

from rm_copilot.guardrails import INJECTION
from rm_copilot.llm import MockLLM
from rm_copilot.retrieval import tokenize
from services.common.config import AUD, StackConfig
from services.common.httpkit import HttpError, Request, Response

from ._base import REALISTIC_AOAI, Mock, realistic, sleep_ms

MODELS = {"gen-large": "gpt-large-2026-01-15", "gen-small": "gpt-small-2026-01-15", "embed": "text-embedding-3-large-1"}
CHAT = ("gen-large", "gen-small")


def count_tokens(text: str) -> int:
    return max(1, math.ceil(len(text) / 4))


def embed(text: str, dims: int = 256) -> list[float]:
    v = [0.0] * dims
    for w in tokenize(text):
        h = int.from_bytes(hashlib.sha256(w.encode()).digest()[:8], "big")
        v[h % dims] += 1.0 if (h >> 63) & 1 else -1.0
    norm = math.sqrt(sum(x * x for x in v)) or 1.0
    return [round(x / norm, 6) for x in v]


class AzureOpenAIMock(Mock):
    def __init__(self, cfg: StackConfig, name: str):
        self.targets = (name,)
        super().__init__(cfg, name)
        self.llm = MockLLM()
        self.received: deque[dict] = deque(maxlen=2000)
        self._lock = threading.Lock()
        self.app.route("POST", "/openai/deployments/{deployment}/chat/completions")(self._chat)
        self.app.route("POST", "/openai/deployments/{deployment}/embeddings")(self._embeddings)

    def _common(self, r: Request) -> str:
        self.require_api_version(r)
        self.caller(r, AUD["cognitive"], azp=("apim",))  # only the gateway's managed identity reaches the model
        dep = r.params["deployment"]
        if dep not in MODELS:
            raise HttpError(404, "DeploymentNotFound", "The API deployment for this resource does not exist.")
        self.faults.before(self.name)
        return dep

    def _chat(self, r: Request) -> Response:
        dep = self._common(r)
        if dep not in CHAT:
            raise HttpError(400, "OperationNotSupported", "The chatCompletion operation does not work with the specified model.")
        body = r.json()
        with self._lock:
            self.received.append({"deployment": dep, "body": body})
        if body.get("store") is True:
            raise HttpError(400, "invalid_request_error", "store=true is disabled for this resource")
        msgs = body.get("messages") or []
        system = "\n".join(m.get("content", "") for m in msgs if m.get("role") == "system")
        user = "\n".join(m.get("content", "") for m in msgs if m.get("role") == "user")
        if INJECTION.search(user):
            raise HttpError(400, "content_filter", "The response was filtered due to the prompt triggering Azure OpenAI's content management policy.",
                            details={"innererror": {"code": "ResponsibleAIPolicyViolation", "content_filter_result": {"jailbreak": {"filtered": True, "detected": True}}}})
        try:
            content = self.llm.complete(system, user)
        except ValueError:
            raise HttpError(400, "invalid_request_error", "unsupported prompt for the mock model") from None
        prompt_t, completion_t = count_tokens(system + user), count_tokens(content)
        max_tokens = int(body.get("max_tokens") or 4096)
        finish = "length" if completion_t > max_tokens else "stop"
        if realistic(self.cfg):
            ttft, per_token = REALISTIC_AOAI[dep]
            sleep_ms(ttft + per_token * completion_t)
        self.faults.after(self.name)
        safe = {c: {"filtered": False, "severity": "safe"} for c in ("hate", "self_harm", "sexual", "violence")}
        return Response(200, {
            "id": "chatcmpl-" + secrets.token_hex(12), "object": "chat.completion", "created": int(time.time()), "model": MODELS[dep],
            "system_fingerprint": "fp_mock",
            "prompt_filter_results": [{"prompt_index": 0, "content_filter_results": {**safe, "jailbreak": {"filtered": False, "detected": False}}}],
            "choices": [{"index": 0, "finish_reason": finish, "message": {"role": "assistant", "content": content},
                         "content_filter_results": {**safe, "protected_material_text": {"filtered": False, "detected": False}}}],
            "usage": {"prompt_tokens": prompt_t, "completion_tokens": completion_t, "total_tokens": prompt_t + completion_t},
        }, headers={"x-ms-region": "UAE North", "x-ratelimit-remaining-tokens": "100000", "x-ratelimit-remaining-requests": "1000"})

    def _embeddings(self, r: Request) -> Response:
        dep = self._common(r)
        if dep != "embed":
            raise HttpError(400, "OperationNotSupported", "The embeddings operation does not work with the specified model.")
        body = r.json()
        inputs = body.get("input")
        inputs = [inputs] if isinstance(inputs, str) else list(inputs or [])
        dims = int(body.get("dimensions") or 256)
        if realistic(self.cfg):
            sleep_ms(REALISTIC_AOAI["embed"][0])
        tokens = sum(count_tokens(t) for t in inputs)
        return Response(200, {"object": "list", "model": MODELS["embed"],
                              "data": [{"object": "embedding", "index": i, "embedding": embed(t, dims)} for i, t in enumerate(inputs)],
                              "usage": {"prompt_tokens": tokens, "total_tokens": tokens}}, headers={"x-ms-region": "UAE North"})

