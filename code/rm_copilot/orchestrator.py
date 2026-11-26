"""The copilot orchestrator: one agent, a typed tool catalogue, explicit budgets.

UC1 brief      -> fixed workflow: fan out read tools, model writes talking points with placeholders.
UC2 policy Q&A -> RAG: filtered retrieval, cited answer, groundedness check, retry once, else decline.
UC3 follow-up  -> extract drafts; writes need a signed RM approval (see tools.ApprovalService).
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta

from . import guardrails as g
from . import tools as t
from .llm import BRIEF_SYSTEM, EXTRACT_SYSTEM, POLICY_QA_SYSTEM, STRICT_SUFFIX, LLMClient

MIN_RETRIEVAL_SCORE = 2.0   # below this, decline instead of generating
MAX_TOOL_STEPS = 6          # per-request budget for open-ended planning (unused by fixed workflows)
TODAY = date(2026, 11, 26)  # fixed for reproducible demos
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _aed(n: float) -> str:
    return f"AED {n / 1e6:.1f}m".replace(".0m", "m")


def _fmt(d: date) -> str:
    return f"{d.day:02d} {MONTHS[d.month - 1]} {d.year}"


class Copilot:
    def __init__(self, llm: LLMClient):
        self.llm = llm

    # ------------------------------------------------------------------ UC1
    def brief(self, user: t.User, customer_id: str) -> dict:
        # Deterministic fan-out (production: asyncio.gather). Raises NotEntitled before any data moves.
        profile = t.get_customer_profile(user, customer_id)
        facilities = t.get_facilities(user, customer_id)
        kyc = t.get_kyc_status(user, customer_id)
        signals = t.get_transaction_signals(user, customer_id)

        # FACTS: every value the brief may show, formatted once, from tool output only.
        facts: dict = {"watch_facilities": [], "signals": [s["text"] for s in signals]}
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
        for i, s in enumerate(signals):
            facts[f"nba_{i}"] = s["nba"]

        raw = self.llm.complete(BRIEF_SYSTEM, "FACTS:" + json.dumps(facts))
        points, rejected = [], []
        for p in json.loads(raw):
            keys = re.findall(r"\{([^}]+)\}", p)
            if g.free_numbers(p) or any(k not in facts for k in keys):
                rejected.append(p)  # the model invented a figure or a fact key
                continue
            points.append(re.sub(r"\{([^}]+)\}", lambda m: str(facts[m.group(1)]), p))
        t.audit("brief_built", user=user.id, customer=customer_id, points=len(points), rejected=len(rejected))
        return {
            "customer": profile["name"],
            "snapshot": f"Customer since {profile['since']} · turnover {_aed(profile['turnover_aed'])} · revenue to bank {profile['revenue_yoy_pct']:+d}% YoY [CRM #{profile['crm_id']}]",
            "watch": watch or ["No limit or KYC flags"],
            "opportunities": [s["text"] for s in signals],
            "talking_points": points,
            "rejected_model_output": rejected,
        }

    # ------------------------------------------------------------------ UC2
    def ask(self, user: t.User, question: str) -> dict:
        if g.detect_injection(question):
            t.audit("injection_flagged", user=user.id, where="question")
        hits = t.search_policies(user, question, k=3)
        if not hits or hits[0]["score"] < MIN_RETRIEVAL_SCORE:
            return self._decline(user, question, hits, "no approved document answers this")
        hits = [h for h in hits if h["score"] >= hits[0]["score"] * 0.45]  # keep only passages close to the best
        context = "\n".join(f"[{i}] {h['doc']} {h['ref']} :: {h['text']}" for i, h in enumerate(hits, 1))
        prompt = f"PASSAGES:\n{context}\n\nQUESTION: {question}"

        for attempt in (1, 2):
            answer = self.llm.complete(POLICY_QA_SYSTEM + (STRICT_SUFFIX if attempt == 2 else ""), prompt).strip()
            if answer == "INSUFFICIENT_EVIDENCE":
                return self._decline(user, question, hits, "model found the passages insufficient")
            check = g.groundedness(answer, hits)
            t.audit("groundedness", user=user.id, attempt=attempt, score=check["score"])
            if check["ok"]:
                return {"status": "answered", "answer": answer, "grounded": check["score"],
                        "sources": [f"[{i}] {h['doc']} {h['ref']}" for i, h in enumerate(hits, 1)], "source_ids": [h["id"] for h in hits]}
        return self._decline(user, question, hits, "answer failed verification twice", check)

    @staticmethod
    def _decline(user, question, hits, reason, check=None) -> dict:
        t.audit("declined", user=user.id, reason=reason)
        return {"status": "declined", "reason": reason, "message": "I can't confirm this from approved documents. I can route it to the credit or product desk.",
                "closest_sources": [f"{h['doc']} {h['ref']}" for h in hits], "source_ids": [h["id"] for h in hits], "check": check}

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
        profile = t.get_customer_profile(user, customer_id)  # entitlement check
        notes, removed = g.quarantine_quotes(notes)
        notes, pii = g.mask_pii(notes)
        raw = self.llm.complete(EXTRACT_SYSTEM, "NOTES:\n" + self._resolve_dates(notes))
        draft = json.loads(raw)
        # Schema and policy validation of model output before anything is shown.
        draft["tasks"] = [x for x in draft.get("tasks", []) if isinstance(x, dict) and not re.search(r"approve|increase the limit|pricing", x.get("title", ""), re.I)]
        lines = "\n".join(f"- {x['title']} (by {x['due']})" for x in draft["tasks"])
        email = t.draft_email(user, customer_id, f"Dear {profile['contact']},\n\nThank you for your time today. I will follow up on:\n{lines}\n\nKind regards,\n{user.name}")
        t.audit("followup_drafted", user=user.id, customer=customer_id, quarantined=len(removed), pii=pii)
        return {"customer_id": customer_id, "call_report": draft["call_report"], "tasks": draft["tasks"],
                "signals": draft.get("signals", []), "email_draft": email["body"], "quarantined": removed, "pii_masked": pii}

    def commit(self, user: t.User, draft: dict, approval_token: str | None) -> dict:
        payload = {"call_report": draft["call_report"], "tasks": draft["tasks"]}
        return t.create_call_report(user, draft["customer_id"], payload, approval_token=approval_token)
