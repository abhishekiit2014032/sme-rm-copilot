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
        return {"intent": guess_intent(text), "multi_step": multi_step, "confidence": 0.93}

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
        question, clauses = v["question"], v["clauses"]
        if re.search(r"unsecured", question, re.I) and AMOUNT.search(question):
            result = _answer_unsecured_cap(question, clauses)
        elif re.search(r"collateral", question, re.I):
            result = _answer_collateral(clauses)
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
    lowered = text.lower()
    if re.search(r"draft follow|follow-up|follow up|meeting notes", lowered):
        return "followup"
    if re.search(r"\bbrief\b|prepare me|meeting prep", lowered):
        return "brief"
    if re.search(r"policy|unsecured|collateral|fee|limit|eligib|document|cap|letter of credit|\blc\b|deposit|"
                 r"financing|tenor|lending|\?", lowered):
        return "policy"
    return "unknown"


def _answer_unsecured_cap(question: str, clauses: list[dict[str, Any]]) -> dict[str, Any]:
    requested = float(AMOUNT.search(question).group(1))
    cap_clause = next((c for c in clauses if "unsecured" in c["text"].lower() and AMOUNT.search(c["text"])), None)
    if cap_clause is None:
        return {"outcome": "unknown", "answer": "The retrieved clauses do not state an unsecured cap.",
                "cited_refs": []}
    cap = float(AMOUNT.search(cap_clause["text"]).group(1))
    if requested <= cap:
        return {"outcome": "yes", "cited_refs": [cap_clause["ref"]],
                "answer": f"Yes. AED {requested:g}m is within the unsecured cap of AED {cap:g}m per SME "
                          f"borrower ({cap_clause['ref_label']}). {_other_sentences(cap_clause['text'])}".strip()}
    answer = (f"No. AED {requested:g}m is above the unsecured cap of AED {cap:g}m per SME borrower "
              f"({cap_clause['ref_label']}).")
    refs = [cap_clause["ref"]]
    above_cap = next((c for c in clauses if "above the unsecured cap" in c["text"].lower()), None)
    if above_cap:
        answer += (" Above the cap, the excess needs acceptable collateral or a Credit Committee exception "
                   f"({above_cap['ref_label']}).")
        refs.append(above_cap["ref"])
    return {"outcome": "no", "answer": answer, "cited_refs": refs}


def _answer_collateral(clauses: list[dict[str, Any]]) -> dict[str, Any]:
    clause = next((c for c in clauses if "collateral" in c["heading"].lower()), clauses[0])
    items = [line[2:].split(" (")[0] for line in clause["text"].splitlines() if line.startswith("- ")]
    if not items:
        return {"outcome": "info", "answer": f"{_first_sentence(clause['text'])} ({clause['ref_label']}).",
                "cited_refs": [clause["ref"]]}
    answer, refs = "", []
    above_cap = next((c for c in clauses if "above the unsecured cap" in c["text"].lower()), None)
    if above_cap:
        answer = ("Above the unsecured cap, the excess must be covered by acceptable collateral or approved as a "
                  f"Credit Committee exception ({above_cap['ref_label']}). ")
        refs.append(above_cap["ref"])
    listed = ", ".join(items[:-1]) + f" or {items[-1]}" if len(items) > 1 else items[0]
    answer += (f"Acceptable collateral is {listed} ({clause['ref_label']}). "
               "Personal guarantees do not count as collateral.")
    return {"outcome": "info", "answer": answer, "cited_refs": refs + [clause["ref"]]}


def _best_sentence(text: str, question: str) -> str:
    """The clause sentence sharing the most words with the question (first sentence on a tie)."""
    asked = set(re.findall(r"[a-z]{3,}", question.lower()))
    sentences = re.split(r"(?<=\.)\s", text.strip())
    best = max(sentences, key=lambda s: len(asked & set(re.findall(r"[a-z]{3,}", s.lower()))))
    return best.rstrip(".")


def _other_sentences(text: str) -> str:
    """Conditions that follow the first sentence of a clause, quoted as written."""
    parts = re.split(r"(?<=\.)\s", text.strip(), maxsplit=1)
    return parts[1] if len(parts) > 1 else ""


def _first_sentence(text: str) -> str:
    return re.split(r"(?<=\.)\s", text.strip(), maxsplit=1)[0].rstrip(".")


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
