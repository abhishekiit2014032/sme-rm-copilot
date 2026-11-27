"""Diagram box: Output guardrails (lane A).
Releases an answer only if it is grounded: citations valid, every figure matches a source, runtime scores pass.
Also unmasks PII for the RM. Mocked: the judge model (see operations/evals/runtime.py).
"""
from __future__ import annotations

import re

from pydantic import BaseModel

from copilot.contracts.context import RunContext
from copilot.contracts.money import figures_in_answer, numbers_in_evidence
from copilot.contracts.results import AgentResult
from copilot.contracts.trace import Component
from copilot.operations.evals.runtime import RuntimeScores, score_answer

CLAUSE_MENTION = re.compile(r"§(\d+(?:\.\d+)*)")


class Verdict(BaseModel):
    passed: bool
    failures: list[str]
    scores: RuntimeScores | None = None


class OutputGuardrails:
    def check(self, result: AgentResult, question: str, ctx: RunContext) -> Verdict:
        if result.status == "declined":
            return Verdict(passed=True, failures=[])       # a decline is always safe to release
        with ctx.trace.span(Component.OUTPUT_GUARDRAILS, "verify answer") as span:
            failures = self._citation_failures(result) + self._number_failures(result, question)
            scores = None
            if result.citations:
                scores = self._runtime_scores(result, question, ctx)
                if not scores.passed:
                    failures.append(f"runtime scores below threshold ({scores.faithfulness:.2f} faithfulness)")
            span.status = "ok" if not failures else "failed"
            span.detail = "grounded, figures match, citations valid" if not failures else "; ".join(failures)
            return Verdict(passed=not failures, failures=failures, scores=scores)

    def _citation_failures(self, result: AgentResult) -> list[str]:
        """Policy answers must cite, and every § mentioned must be a retrieved, cited clause."""
        if result.agent != "policy":
            return []
        if not result.citations:
            return ["policy answer has no citation"]
        cited = {c.clause for c in result.citations}
        unknown = [m for m in CLAUSE_MENTION.findall(result.text) if m not in cited]
        return [f"mentions §{m} which was not retrieved" for m in unknown]

    def _number_failures(self, result: AgentResult, question: str) -> list[str]:
        """Every AED amount and percentage in the answer must appear in the tool data, clauses or question."""
        allowed = numbers_in_evidence(" ".join(result.evidence + [question]))
        texts = [result.text] + [d.body for d in result.drafts]
        return [f"figure '{shown}' not found in any source"
                for text in texts for shown, value in figures_in_answer(text)
                if not any(abs(value - a) < 0.5 for a in allowed)]

    def _runtime_scores(self, result: AgentResult, question: str, ctx: RunContext) -> RuntimeScores:
        scores = score_answer(question, result.text, result.evidence)
        ctx.trace.event(Component.EVALUATION, "runtime scores",
                        detail=f"faithfulness {scores.faithfulness:.2f}, relevancy {scores.answer_relevancy:.2f}",
                        status="ok" if scores.passed else "failed", judge="lexical mock judge",
                        faithfulness=scores.faithfulness, answer_relevancy=scores.answer_relevancy)
        return scores
