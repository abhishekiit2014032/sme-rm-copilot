"""Diagram box: Primary / fallback LLM (lane B), the scripted mock behind the model gateway.
Returns deterministic JSON for each prompt id, plus a hashing "embedding", so demos and tests repeat exactly.
Mocked: the whole model. It reads the prompt variables, never real customer data, and has fault switches.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import date, timedelta
from typing import Any

from copilot.contracts.ports import LlmCompletion, RenderedPrompt
from copilot.operations.resilience import TransientError

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}
AMOUNT = re.compile(r"AED\s?(\d+(?:\.\d+)?)\s?m\b", re.IGNORECASE)
CAP = re.compile(r"(?:must not exceed|not more than|at most|a maximum of)\s+" + AMOUNT.pattern, re.IGNORECASE)
CLAUSE_MENTION = re.compile(r"\s(?:in|under)\s(§\d+(?:\.\d+)*)")
# Intent -> cue pattern. More distinct cues in the ask give a more confident classification.
INTENT_CUES = {
    "followup": r"draft follow|follow-up|follow up|meeting notes",
    "brief": r"\bbrief\b|prepare me|meeting prep",
    "policy": r"policy|unsecured|collateral|fee|limit|eligib|document|cap|letter of credit|\blc\b|deposit|"
              r"financing|tenor|lending|\?",
}


class LlmUnavailable(TransientError):
    pass


class ScriptedLlm:
    """Implements the LlmClient port. One small, readable rule set per prompt id."""

    def __init__(self) -> None:
        self.down_models: set[str] = set()      # fault switch: these model names fail
        self.unsupported_number_once = False    # fault switch: next policy answer invents a figure
        self.calls: list[str] = []

    # ---- port methods -------------------------------------------------------------------
    def complete(self, model: str, prompt: RenderedPrompt, params: dict[str, Any]) -> LlmCompletion:
        self.calls.append(f"{model}:{prompt.prompt_id}")
        if model in self.down_models:
            raise LlmUnavailable(f"{model} returned HTTP 503")
        handler = {
            "router.classify_intent": self._classify_intent,
            "planner.plan": self._plan,
            "policy.answer": self._policy_answer,
            "brief.summarise": self._brief_summary,
            "followup.extract": self._followup_extract,
        }[prompt.prompt_id]
        output = json.dumps(handler(prompt.variables))
        return LlmCompletion(text=output, tokens_in=_count_tokens(prompt.system + prompt.user),
                             tokens_out=_count_tokens(output))

    def embed(self, model: str, text: str) -> list[float]:
        if model in self.down_models:
            raise LlmUnavailable(f"{model} returned HTTP 503")
        return hash_embedding(text)

    # ---- router ------------------------------------------------------------------------
    def _classify_intent(self, v: dict[str, Any]) -> dict[str, Any]:
        text = v["text"]
        parts = split_question(text)
        multi_step = len(parts) > 1 or bool(re.search(r"\bif not\b", text, re.I))
        intent, cues = _intent_cues(text)
        confidence = round(1 - 0.5 ** (cues + 1), 2) if cues else 0.5    # 1 cue 0.75, 2 cues 0.88, 3 cues 0.94
        return {"intent": intent, "multi_step": multi_step, "confidence": confidence}

    def _plan(self, v: dict[str, Any]) -> dict[str, Any]:
        steps = []
        for index, part in enumerate(split_question(v["text"]), start=1):
            conditional = re.match(r"^\s*if not,?\s*", part, re.I)
            instruction = part[conditional.end():] if conditional else part
            agent = guess_intent(part)
            step: dict[str, Any] = {
                "step_id": index,
                "task": {"agent": agent if agent != "unknown" else "policy",
                         "instruction": instruction[:1].upper() + instruction[1:],
                         "customer_id": v.get("customer_id")},
            }
            if conditional and index > 1:
                step["run_if"] = {"step_id": index - 1, "outcome": "no"}
            steps.append(step)
        return {"goal": v["text"], "steps": steps}

    # ---- policy agent ------------------------------------------------------------------
    def _policy_answer(self, v: dict[str, Any]) -> dict[str, Any]:
        """Generic rules over the retrieved clauses: an amount checked against a stated maximum, a listed clause,
        or the top clause's best-matching sentence. Every sentence is quoted or built from clause text."""
        question, clauses = v["question"], v["clauses"]
        asked = AMOUNT.search(question)
        cap_clause = next((c for c in clauses if CAP.search(c["text"])), None)
        if asked and cap_clause:
            result = _limit_check(float(asked.group(1)), cap_clause, clauses)
        elif _list_items(clauses[0]["text"]):
            result = _list_answer(clauses[0], clauses)
        else:
            top = clauses[0]
            result = {"outcome": "info", "answer": f"{_best_sentence(top['text'], question)} ({top['ref_label']}).",
                      "cited_refs": [top["ref"]]}
        if self.unsupported_number_once and not v.get("strict"):
            self.unsupported_number_once = False
            result["answer"] += " A processing fee of AED 9,999 also applies."
        return result

    # ---- brief agent -------------------------------------------------------------------
    def _brief_summary(self, v: dict[str, Any]) -> dict[str, Any]:
        keys = set(v["fact_keys"])
        summary = "{{customer_name}} meets you on {{meeting_date}} for: {{meeting_purpose}}."
        if {"wc_limit", "wc_utilisation_pct"} <= keys:
            summary += " The working capital limit of {{wc_limit}} is {{wc_utilisation_pct}} utilised"
            summary += " and the customer has asked for {{requested_limit}}." if "requested_limit" in keys else "."
        points = []
        if "requested_limit" in keys:
            points.append("Test the business case for {{requested_limit}} and check what is still pending "
                          "before the request can move.")
        if "open_complaint" in keys:
            points.append("Acknowledge the open complaint ({{open_complaint}}) before discussing new business.")
        for i in range(1, 3):
            if f"product_{i}" in keys:
                points.append(f"Explore {{{{product_{i}}}}}: {{{{product_{i}_reason}}}}.")
        if v.get("unavailable"):
            points.append("Some data was unavailable; confirm it in the source system before the meeting.")
        return {"summary": summary, "talking_points": points}

    # ---- follow-up agent ---------------------------------------------------------------
    def _followup_extract(self, v: dict[str, Any]) -> dict[str, Any]:
        notes, today = v["notes"], date.fromisoformat(v["today"])
        match = re.search(r"(?P<who>owner|customer|client)\s+will\s+(?P<what>.+?)\s+by\s+(?P<day>\d{1,2})\s+"
                          r"(?P<month>[A-Za-z]{3})", notes, re.I)
        if not match:
            due = today + timedelta(days=7)
            return {"note": f"Meeting note: {notes.strip()}",
                    "tasks": [{"title": "Follow up on the meeting notes", "due_date": due.isoformat(),
                               "owner": "rm"}],
                    "email_subject": "Thank you for meeting us",
                    "email_body": "Dear {{contact_first_name}},\n\nThank you for your time today. "
                                  "I will follow up on the points we discussed.\n\nKind regards,\n{{rm_name}}"}
        what = match["what"].strip()
        due = _next_date(int(match["day"]), MONTHS[match["month"].lower()], today)
        reminder = due + timedelta(days=1)
        return {
            "note": f"Meeting note: the {match['who'].lower()} will {what} by {due:%d %b %Y}.",
            "tasks": [
                {"title": what[:1].upper() + what[1:], "due_date": due.isoformat(), "owner": "customer"},
                {"title": f"Chase if not received: {what}", "due_date": reminder.isoformat(), "owner": "rm"},
            ],
            "email_subject": "Next steps from our meeting",
            "email_body": "Dear {{contact_first_name}},\n\nThank you for your time today. As agreed, "
                          f"you will {what} by {due:%d %B %Y}. Once received, we will continue with your "
                          "request.\n\nKind regards,\n{{rm_name}}",
        }


# ---- helpers (shared with tests) ----------------------------------------------------------
def split_question(text: str) -> list[str]:
    """Split an ask into parts at '?' and ';' so each part can become one plan step."""
    parts = re.split(r"(?<=\?)\s+|;\s*", text.strip())
    return [p.strip() for p in parts if p.strip()]


def guess_intent(text: str) -> str:
    return _intent_cues(text)[0]


def _intent_cues(text: str) -> tuple[str, int]:
    """The first intent whose cues appear in the ask, and how many distinct cues matched."""
    lowered = text.lower()
    for intent, cues in INTENT_CUES.items():
        found = set(re.findall(cues, lowered))
        if found:
            return intent, len(found)
    return "unknown", 0


def _limit_check(requested: float, clause: dict[str, Any], clauses: list[dict[str, Any]]) -> dict[str, Any]:
    """Compare the amount asked for with the maximum the clause states, quoting the rule itself."""
    rule = next(s for s in _sentences(clause["text"]) if CAP.search(s))
    cap = float(CAP.search(rule).group(1))
    within = requested <= cap
    answer = (f"{'Yes' if within else 'No'}. AED {requested:g}m is {'within' if within else 'above'} the limit: "
              f"{rule[:1].lower()}{rule[1:].rstrip('.')} ({clause['ref_label']}).")
    if within:   # the clause's other conditions still apply
        others = " ".join(s for s in _sentences(clause["text"]) if s != rule)
        return {"outcome": "yes", "answer": f"{answer} {others}".strip(), "cited_refs": [clause["ref"]]}
    related = _referring(clause, clauses)    # e.g. what applies above the limit
    cited = [clause] + related
    answer = " ".join([answer] + [_quote(c, cited) for c in related])
    return {"outcome": "no", "answer": answer, "cited_refs": [c["ref"] for c in cited]}


def _list_answer(clause: dict[str, Any], clauses: list[dict[str, Any]]) -> dict[str, Any]:
    """A clause that lists options: name them, keep the clause's other sentences, and quote clauses referring to
    it first (they say when the list applies)."""
    lines = [line.strip() for line in clause["text"].splitlines() if line.strip()]
    items = _list_items(clause["text"])
    intro = next(line for line in lines if not line.startswith("- "))
    rest = [line for line in lines if not line.startswith("- ") and line != intro]
    listed = ", ".join(items[:-1]) + f" or {items[-1]}" if len(items) > 1 else items[0]
    related = _referring(clause, clauses)
    cited = related + [clause]
    answer = " ".join([_quote(c, cited) for c in related]
                      + [f"{intro.rstrip(':')} {listed} ({clause['ref_label']})."] + rest)
    return {"outcome": "info", "answer": answer, "cited_refs": [c["ref"] for c in cited]}


def _list_items(text: str) -> list[str]:
    return [line.strip()[2:] for line in text.splitlines() if line.strip().startswith("- ")]


def _referring(clause: dict[str, Any], clauses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Retrieved clauses of the same document that refer to this clause by number."""
    doc = clause["ref"].split("@")[0]
    return [c for c in clauses if c is not clause and c["ref"].startswith(doc + "@")
            and re.search(re.escape(clause["ref_label"]) + r"\b", c["text"])]


def _quote(clause: dict[str, Any], cited: list[dict[str, Any]]) -> str:
    """Quote a clause; a reference to a clause that is not cited is dropped, since it cannot be checked."""
    labels = {c["ref_label"] for c in cited}
    text = CLAUSE_MENTION.sub(lambda m: m.group(0) if m.group(1) in labels else "", clause["text"].strip())
    return f"{text.rstrip('.')} ({clause['ref_label']})."


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=\.)\s", text.strip()) if s.strip()]


def _best_sentence(text: str, question: str) -> str:
    """The clause sentence sharing the most words with the question (first sentence on a tie)."""
    asked = set(re.findall(r"[a-z]{3,}", question.lower()))
    sentences = re.split(r"(?<=\.)\s", text.strip())
    best = max(sentences, key=lambda s: len(asked & set(re.findall(r"[a-z]{3,}", s.lower()))))
    return best.rstrip(".")


def _next_date(day: int, month: int, today: date) -> date:
    candidate = date(today.year, month, day)
    return candidate if candidate >= today else date(today.year + 1, month, day)


def _count_tokens(text: str) -> int:
    return max(1, math.ceil(len(text.split()) * 1.3))


def _stem(word: str) -> str:
    for suffix in ("ing", "es", "ed", "s"):
        if word.endswith(suffix) and len(word) > len(suffix) + 2:
            return word[: -len(suffix)]
    return word


def hash_embedding(text: str, dims: int = 256) -> list[float]:
    """Deterministic stand-in for an embedding model: signed feature hashing of stemmed words."""
    vector = [0.0] * dims
    for word in re.findall(r"[a-z0-9]+", text.lower()):
        digest = hashlib.sha256(_stem(word).encode()).digest()
        index = int.from_bytes(digest[:4], "big") % dims
        vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
    norm = math.sqrt(sum(x * x for x in vector)) or 1.0
    return [x / norm for x in vector]
