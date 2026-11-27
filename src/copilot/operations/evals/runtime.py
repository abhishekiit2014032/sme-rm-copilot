"""Diagram box: Evaluation (lane D), runtime scoring.
Scores each policy answer for faithfulness (supported by the evidence) and relevancy (addresses the question).
Mocked: a deterministic lexical judge stands in for the judge model; thresholds are the production ones.
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from copilot.execution.retrieval import tokenize

FAITHFULNESS_MIN = 0.9
RELEVANCY_MIN = 0.3
SUPPORTED_SENTENCE = 0.6   # share of a sentence's content words that must appear in the evidence
MIN_TERMS = 3              # shorter sentences ("No.") carry no claim worth scoring


class RuntimeScores(BaseModel):
    faithfulness: float
    answer_relevancy: float

    @property
    def passed(self) -> bool:
        return self.faithfulness >= FAITHFULNESS_MIN and self.answer_relevancy >= RELEVANCY_MIN


CITATION = re.compile(r"\s?\(§[\d.]+(?:, §[\d.]+)*\)")


def score_answer(question: str, answer: str, evidence: list[str]) -> RuntimeScores:
    answer = CITATION.sub("", answer)    # citation markers are checked by output guardrails, not scored here
    evidence_terms = set(tokenize(" ".join(evidence + [question])))
    sentences = [s for s in re.split(r"(?<=[.!?])\s+", answer.strip()) if len(tokenize(s)) >= MIN_TERMS]
    supported = [s for s in sentences if _support(s, evidence_terms) >= SUPPORTED_SENTENCE]
    faithfulness = len(supported) / len(sentences) if sentences else 0.0
    question_terms = set(tokenize(question))
    relevancy = len(question_terms & set(tokenize(answer))) / len(question_terms) if question_terms else 0.0
    return RuntimeScores(faithfulness=round(faithfulness, 3), answer_relevancy=round(relevancy, 3))


def _support(sentence: str, evidence_terms: set[str]) -> float:
    terms = tokenize(sentence)
    return sum(t in evidence_terms for t in terms) / len(terms)
