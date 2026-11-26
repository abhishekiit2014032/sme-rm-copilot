"""Azure AI Language mock: PII entity recognition.

Imitates (api-version 2024-11-01):
  POST /language/:analyze-text   {"kind": "PiiEntityRecognition", "parameters": {"redactionPolicy": {"policyKind":
      "characterMask"|"entityMask"|"noMask"}, "piiCategories"?}, "analysisInput": {"documents": [{"id", "language", "text"}]}}
  -> {"kind": "PiiEntityRecognitionResults", "results": {"documents": [{"id", "redactedText", "entities":
      [{"text", "category", "offset", "length", "confidenceScore"}], "warnings": []}], "errors": [], "modelVersion"}}

Detection uses the same patterns as the local floor (`rm_copilot.guardrails.PII_PATTERNS`), mapped to service
categories: InternationalBankingAccountNumber, CreditCardNumber, PhoneNumber, Email, plus the Bank's custom
regex entities EmiratesIdNumber and TradeLicenceNumber (plan §3.3). Differences: regex only (no names,
addresses or ML), no Arabic model; offsets are Python string indexes (the service defaults to UTF-16 code units).
"""
from __future__ import annotations

from rm_copilot.guardrails import PII_PATTERNS
from services.common.config import AUD, StackConfig
from services.common.httpkit import HttpError, Request

from ._base import Mock

CATEGORY = {"national_id": "EmiratesIdNumber", "iban": "InternationalBankingAccountNumber", "card": "CreditCardNumber",
            "trade_licence": "TradeLicenceNumber", "email": "Email", "phone": "PhoneNumber"}
LOCAL_NAME = {v: k for k, v in CATEGORY.items()}
RBAC = {"orchestrator": {"use"}, "ingestion-worker": {"use"}}


def find_entities(text: str) -> list[dict]:
    """Non-overlapping entities in pattern priority order (IDs and IBANs before generic digit runs)."""
    taken: list[tuple[int, int]] = []
    out = []
    for name, rx in PII_PATTERNS.items():
        for m in rx.finditer(text):
            if any(m.start() < e and s < m.end() for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            out.append({"text": m.group(0), "category": CATEGORY[name], "offset": m.start(), "length": m.end() - m.start(), "confidenceScore": 0.85})
    return sorted(out, key=lambda e: e["offset"])


def redact(text: str, entities: list[dict], policy: str) -> str:
    for e in sorted(entities, key=lambda e: e["offset"], reverse=True):
        repl = {"entityMask": f"[{e['category']}]", "noMask": e["text"]}.get(policy, "*" * e["length"])
        text = text[:e["offset"]] + repl + text[e["offset"] + e["length"]:]
    return text


class LanguageMock(Mock):
    name = "language"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self.app.route("POST", "/language/:analyze-text")(self._analyze)

    def _analyze(self, r: Request) -> dict:
        self.require_api_version(r)
        self.simulate("language")
        self.rbac(self.caller(r, AUD["cognitive"]), RBAC, "use")
        body = r.json()
        if body.get("kind") != "PiiEntityRecognition":
            raise HttpError(400, "InvalidArgument", "mock supports kind=PiiEntityRecognition only")
        params = body.get("parameters") or {}
        policy = (params.get("redactionPolicy") or {}).get("policyKind", "characterMask")
        wanted = set(params.get("piiCategories") or CATEGORY.values())
        docs = (body.get("analysisInput") or {}).get("documents") or []
        if len(docs) > 5:
            raise HttpError(400, "InvalidDocumentBatch", "Batch request contains too many records. Max 5 records are permitted.")
        results = []
        for d in docs:
            ents = [e for e in find_entities(d.get("text", "")) if e["category"] in wanted]
            results.append({"id": d.get("id"), "redactedText": redact(d.get("text", ""), ents, policy), "entities": ents, "warnings": []})
        return {"kind": "PiiEntityRecognitionResults", "results": {"documents": results, "errors": [], "modelVersion": "2025-01-01-mock"}}
