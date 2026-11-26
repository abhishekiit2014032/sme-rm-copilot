"""Azure AI Content Safety mock.

Imitates (api-version 2024-09-01; groundedness 2024-09-15-preview):
  POST /contentsafety/text:shieldPrompt         {"userPrompt", "documents": [...]}
        -> {"userPromptAnalysis": {"attackDetected"}, "documentsAnalysis": [{"attackDetected"}]}
  POST /contentsafety/text:analyze              {"text", "categories"?, "outputType"?}
        -> {"blocklistsMatch": [], "categoriesAnalysis": [{"category", "severity"}]}  (severity 0/2/4/6)
  POST /contentsafety/text:detectGroundedness   {"domain", "task", "qna": {"query"}, "text", "groundingSources": [...]}
        -> {"ungroundedDetected", "ungroundedPercentage", "ungroundedDetails": [{"text"}]}

Detection is rule-based: Prompt Shields uses the same injection rules as `rm_copilot.guardrails` (plus a few
jailbreak phrases); groundedness wraps the local claim-by-claim check (`guardrails.groundedness`), which expects
`[n]` citations into `groundingSources`; harm categories use a tiny phrase lexicon tuned so normal banking
language ("kill switch", "exposure", "default", "execute the transfer") scores 0 (plan C13).
Differences: no ML classifiers, English only, no blocklists, no protected-material or custom categories.
"""
from __future__ import annotations

import re

from rm_copilot.guardrails import INJECTION, groundedness
from services.common.config import AUD, StackConfig
from services.common.httpkit import HttpError, Request

from ._base import Mock

JAILBREAK = re.compile(r"\b(developer mode|do anything now|jailbreak|pretend (you are|to be) (an? )?unrestricted)\b", re.I)
HARM = {
    "Violence": [(re.compile(r"\b(kill|hurt|attack|shoot|stab) (you|him|her|them|everyone)\b", re.I), 4)],
    "SelfHarm": [(re.compile(r"\b(kill myself|end my life|hurt myself)\b", re.I), 6)],
    "Hate": [(re.compile(r"\b(hate|despise) (all )?(foreigners|immigrants|women|men)\b", re.I), 4)],
    "Sexual": [(re.compile(r"\b(sexually explicit|explicit sex)\b", re.I), 4)],
}
RBAC = {"orchestrator": {"use"}, "ingestion-worker": {"use"}}


class ContentSafetyMock(Mock):
    name = "content-safety"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self.app.route("POST", "/contentsafety/text:shieldPrompt")(self._shield)
        self.app.route("POST", "/contentsafety/text:analyze")(self._analyze)
        self.app.route("POST", "/contentsafety/text:detectGroundedness")(self._grounded)

    def _auth(self, r: Request) -> dict:
        self.require_api_version(r)
        self.simulate("content-safety")
        self.rbac(self.caller(r, AUD["cognitive"]), RBAC, "use")
        body = r.json()
        if not isinstance(body, dict):
            raise HttpError(400, "InvalidRequestBody", "body must be a JSON object")
        return body

    @staticmethod
    def _attack(text: str) -> bool:
        return bool(INJECTION.search(text or "") or JAILBREAK.search(text or ""))

    def _shield(self, r: Request) -> dict:
        body = self._auth(r)
        docs = body.get("documents") or []
        if len(docs) > 5 or any(len(d) > 10000 for d in docs):
            raise HttpError(400, "InvalidRequestBody", "at most 5 documents of 10k characters")
        return {"userPromptAnalysis": {"attackDetected": self._attack(body.get("userPrompt", ""))},
                "documentsAnalysis": [{"attackDetected": self._attack(d)} for d in docs]}

    def _analyze(self, r: Request) -> dict:
        body = self._auth(r)
        text = body.get("text", "")
        cats = body.get("categories") or list(HARM)
        out = []
        for c in cats:
            sev = max([s for rx, s in HARM.get(c, []) if rx.search(text)] or [0])
            out.append({"category": c, "severity": sev})
        return {"blocklistsMatch": [], "categoriesAnalysis": out}

    def _grounded(self, r: Request) -> dict:
        body = self._auth(r)
        sources = body.get("groundingSources") or []
        check = groundedness(body.get("text", ""), [{"text": s} for s in sources])
        bad = [c for c in check["claims"] if not c["ok"]]
        return {"ungroundedDetected": not check["ok"], "ungroundedPercentage": round(len(bad) / max(len(check["claims"]), 1), 2),
                "ungroundedDetails": [{"text": c["sentence"], "reason": c["reason"]} for c in bad]}
