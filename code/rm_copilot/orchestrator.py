"""The copilot orchestrator: one agent, a typed tool catalogue, explicit budgets.

UC1 brief      -> fixed workflow: fan out read tools, model writes talking points with placeholders.
UC2 policy Q&A -> RAG: rewrite, filtered retrieval, cited answer, groundedness check, retry once, else decline.
UC3 follow-up  -> extract drafts; writes need a signed RM approval (see tools.ApprovalService).

The workflows depend only on three interfaces: an `LLMClient`, a `ToolPort` and a `GuardPort` (ports.py). The
defaults run everything in-process; the `orchestrator` service of the local stack plugs in HTTP adapters.
When the model is unavailable every use case answers in a clearly flagged degraded mode (plan C3).
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, timedelta
from typing import Callable

from . import data
from . import guardrails as g
from . import tools as t
from .llm import BRIEF_SYSTEM, EXTRACT_SYSTEM, POLICY_QA_SYSTEM, QUERY_REWRITE_SYSTEM, STRICT_SUFFIX, LLMClient, ModelUnavailable
from .ports import GuardPort, LocalGuard, LocalTools, ToolPort

MIN_RETRIEVAL_SCORE = 2.0   # below this, decline instead of generating
MAX_TOOL_STEPS = 6          # per-request budget for open-ended planning (unused by fixed workflows)
MIN_RETRY_SECONDS = 4.0     # UC2 retries once only if at least this much of the deadline remains (plan §4.3)
TODAY = data.AS_OF          # fixed for reproducible demos
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
DECLINE_MESSAGE = "I can't confirm this from approved documents. I can route it to the credit or product desk."


def _aed(n: float) -> str:
    return f"AED {n / 1e6:.1f}m".replace(".0m", "m")


def _fmt(d: date) -> str:
    return f"{d.day:02d} {MONTHS[d.month - 1]} {d.year}"


class Copilot:
    def __init__(self, llm: LLMClient, tools: ToolPort | None = None, guard: GuardPort | None = None,
                 time_left: Callable[[], float] | None = None):
        self.llm = llm
        self.tools: ToolPort = tools or LocalTools()
        self.guard: GuardPort = guard or LocalGuard()
        self.time_left = time_left or (lambda: math.inf)  # seconds left of the request deadline

    # ------------------------------------------------------------------ UC1
    def brief(self, user: t.User, customer_id: str) -> dict:
        # The tool layer fans out to the systems of record; it raises NotEntitled before any data moves.
        inputs = self.tools.brief_inputs(user, customer_id)
        profile, facilities = inputs["profile"], inputs.get("facilities") or []
        kyc = inputs.get("kyc") or {"docs": []}
        signals, news = inputs.get("signals") or [], inputs.get("news") or []

        # Free text from source systems is untrusted: screen it for document attacks before it can reach the model.
        flags = self.guard.shield_documents([s["text"] for s in signals] + [n["headline"] for n in news]) if (signals or news) else []
        kept_signals = [s for s, bad in zip(signals, flags[:len(signals)]) if not bad]
        kept_news = [n for n, bad in zip(news, flags[len(signals):]) if not bad]

        # FACTS: every value the brief may show, formatted once, from tool output only.
        facts: dict = {"watch_facilities": [], "signals": [s["text"] for s in kept_signals]}
        watch = []
        for f in facilities:
            util = round(f["used"] / f["limit"] * 100)
            facts.update({f"type_{f['id']}": f["type"].lower(), f"util_{f['id']}": f"{util}%", f"days_{f['id']}": f"{f['days_above_90']} days"})
            if util >= 90:
                facts["watch_facilities"].append(f["id"])
                watch.append(f"{f['type']} {util}% utilised for {f['days_above_90']} days ({_aed(f['used'])} of {_aed(f['limit'])}) [LOS {f['id']}]")
        for i, d in enumerate(kyc["docs"]):
            exp = date.fromisoformat(d["expires"])
            if (exp - TODAY).days <= 30:
                facts.update({f"doc_{i}_name": d["name"].lower(), f"doc_{i}_expiry": _fmt(exp)})
                watch.append(f"{d['name']} expires {_fmt(exp)} [KYC]")
        for i, s in enumerate(kept_signals):
            facts[f"nba_{i}"] = s["nba"]
        partial = inputs.get("partial", [])
        for section, note in (("facilities", "Facility data unavailable right now: check the lending system before the meeting [LOS]"),
                              ("signals", "Transaction signals unavailable right now [ODS]"),
                              ("kyc", "KYC status unavailable right now: check the KYC system before the meeting [KYC]")):
            if section in partial:
                watch.append(note)

        degraded = None
        masked_facts, _ = self.guard.mask_pii(json.dumps(facts))
        try:
            raw = self.llm.complete(BRIEF_SYSTEM, "FACTS:" + masked_facts)
        except ModelUnavailable as e:
            raw, degraded = "[]", str(e) or "model unavailable"
        points, rejected = [], []
        for p in json.loads(raw):
            keys = re.findall(r"\{([^}]+)\}", p)
            if g.free_numbers(p) or g.policy_violations(p) or any(k not in facts for k in keys):
                rejected.append(p)  # the model invented a figure, a fact key or a commitment
                continue
            points.append(re.sub(r"\{([^}]+)\}", lambda m: str(facts[m.group(1)]), p))
        self.tools.audit("brief_built", user=user.id, customer=customer_id, points=len(points), rejected=len(rejected),
                         degraded=bool(degraded), partial=list(inputs.get("partial", [])))
        return {
            "customer": profile["name"],
            "snapshot": f"Customer since {profile['since']} · turnover {_aed(profile['turnover_aed'])} · revenue to bank {profile['revenue_yoy_pct']:+d}% YoY [CRM #{profile['crm_id']}]",
            "watch": watch or ["No limit or KYC flags"],
            "opportunities": [s["text"] for s in kept_signals],
            "talking_points": points,
            "rejected_model_output": rejected,
            "news": [f"{n['headline']} [News {n['date']}]" for n in kept_news],
            "quarantined_documents": len(flags) - sum(not f for f in flags),
            "partial": list(inputs.get("partial", [])),
            "degraded": degraded is not None,
            "degraded_reason": degraded,
        }

    # ------------------------------------------------------------------ UC2
    def _rewrite(self, question: str) -> str:
        """`gen-small` intent and query rewrite. Falls back to the question itself if the model is unavailable."""
        try:
            out = json.loads(self.llm.complete(QUERY_REWRITE_SYSTEM, question))
            return out.get("query") or question
        except (ModelUnavailable, ValueError, AttributeError):
            return question

    def ask(self, user: t.User, question: str) -> dict:
        injection = self.guard.prompt_attack(question)
        if injection:
            self.tools.audit("injection_flagged", user=user.id, where="question")
        question, _ = self.guard.mask_pii(question)
        hits = self.tools.search_policies(user, self._rewrite(question), k=3)
        if not hits or hits[0]["score"] < MIN_RETRIEVAL_SCORE:
            return self._decline(user, hits, "no approved document answers this")
        hits = [h for h in hits if h["score"] >= hits[0]["score"] * 0.45]  # keep only passages close to the best
        context = "\n".join(f"[{i}] {h['doc']} {h['ref']} :: {h['text']}" for i, h in enumerate(hits, 1))
        prompt = f"PASSAGES:\n{context}\n\nQUESTION: {question}"

        check = None
        for attempt in (1, 2):
            if attempt == 2 and self.time_left() < MIN_RETRY_SECONDS:
                return self._decline(user, hits, "answer failed verification and there is no time to retry", check)
            try:
                answer = self.llm.complete(POLICY_QA_SYSTEM + (STRICT_SUFFIX if attempt == 2 else ""), prompt).strip()
            except ModelUnavailable as e:
                return self._passages_only(user, hits, str(e) or "model unavailable")
            if answer == "INSUFFICIENT_EVIDENCE":
                return self._decline(user, hits, "model found the passages insufficient")
            check = self.guard.groundedness(answer, hits)
            self.tools.audit("groundedness", user=user.id, attempt=attempt, score=check["score"])
            if check["ok"]:
                if self.guard.harmful(answer):
                    return self._decline(user, hits, "answer blocked by content safety")
                return {"status": "answered", "answer": answer, "grounded": check["score"], "injection_flagged": injection,
                        "sources": [f"[{i}] {h['doc']} {h['ref']}" for i, h in enumerate(hits, 1)], "source_ids": [h["id"] for h in hits]}
        return self._decline(user, hits, "answer failed verification twice", check)

    def _decline(self, user: t.User, hits: list[dict], reason: str, check: dict | None = None) -> dict:
        self.tools.audit("declined", user=user.id, reason=reason)
        return {"status": "declined", "reason": reason, "message": DECLINE_MESSAGE,
                "closest_sources": [f"{h['doc']} {h['ref']}" for h in hits], "source_ids": [h["id"] for h in hits], "check": check}

    def _passages_only(self, user: t.User, hits: list[dict], reason: str) -> dict:
        """Degraded UC2: ranked approved passages, no generated answer (plan C3)."""
        self.tools.audit("degraded", user=user.id, use_case="uc2", reason=reason)
        return {"status": "degraded", "reason": reason, "message": "The assistant cannot write an answer right now. These are the most relevant approved passages.",
                "passages": [{"n": i, "source": f"{h['doc']} {h['ref']}", "text": h["text"]} for i, h in enumerate(hits, 1)],
                "source_ids": [h["id"] for h in hits]}

    # ------------------------------------------------------------------ UC3
    @staticmethod
    def _resolve_dates(notes: str) -> str:
        """Deterministic date resolution, so the model never computes a date."""
        def friday() -> date:
            d = TODAY
            while d.weekday() != 4:
                d += timedelta(days=1)
            return d
        notes = re.sub(r"\bby Friday\b", lambda m: f"{m.group(0)} ({friday().isoformat()})", notes, flags=re.I)
        notes = re.sub(r"\bnext week\b", lambda m: f"{m.group(0)} ({(TODAY + timedelta(days=7)).isoformat()})", notes, flags=re.I)

        def explicit(m: re.Match) -> str:
            d = date(TODAY.year, MONTHS.index(m.group(2).title()) + 1, int(m.group(1)))
            if d < TODAY:
                d = d.replace(year=d.year + 1)
            return f"{m.group(0)} ({d.isoformat()})"
        return re.sub(r"\b(\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\b", explicit, notes, flags=re.I)

    def draft_followup(self, user: t.User, customer_id: str, notes: str) -> dict:
        profile = self.tools.get_customer_profile(user, customer_id)  # entitlement check
        injection = self.guard.prompt_attack(notes)
        notes, removed = self.guard.quarantine_quotes(notes)
        notes, pii = self.guard.mask_pii(notes)
        degraded = None
        try:
            draft = json.loads(self.llm.complete(EXTRACT_SYSTEM, "NOTES:\n" + self._resolve_dates(notes)))
        except ModelUnavailable as e:
            draft, degraded = {"call_report": "", "tasks": [], "signals": []}, str(e) or "model unavailable"
        # Schema and policy validation of model output before anything is shown.
        draft["tasks"] = [x for x in draft.get("tasks", []) if isinstance(x, dict) and not g.policy_violations(x.get("title", ""))]
        lines = "\n".join(f"- {x['title']} (by {x['due']})" for x in draft["tasks"])
        email = self.tools.draft_email(user, customer_id, f"Dear {profile['contact']},\n\nThank you for your time today. I will follow up on:\n{lines}\n\nKind regards,\n{user.name}")
        self.tools.audit("followup_drafted", user=user.id, customer=customer_id, quarantined=len(removed), pii=pii, degraded=bool(degraded))
        return {"customer_id": customer_id, "call_report": draft["call_report"], "tasks": draft["tasks"],
                "signals": draft.get("signals", []), "email_draft": email["body"], "email_compose_link": email.get("compose_link"),
                "quarantined": removed, "injection_flagged": injection, "pii_masked": pii,
                "degraded": degraded is not None, "degraded_reason": degraded}

    def commit(self, user: t.User, draft: dict, approval_token: str | None) -> dict:
        payload = {"call_report": draft["call_report"], "tasks": draft["tasks"]}
        return self.tools.create_call_report(user, draft["customer_id"], payload, approval_token=approval_token)
