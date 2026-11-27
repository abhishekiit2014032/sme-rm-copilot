"""Diagram box: Router + orchestrator (lane A), the per-request run context.
Carries identity, trace, token and time budgets, and the PII vault through every step of one request.
Nothing is mocked here.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from copilot.contracts.identity import Identity
from copilot.contracts.trace import Trace


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


@dataclass
class RunContext:
    request_id: str
    identity: Identity
    trace: Trace
    customer_id: str | None = None
    tokens_left: int = 6000                 # per-request token budget (model gateway)
    time_budget_s: float = 8.0              # p95 target from the review document
    strict: bool = False                    # set for the one stricter retry after a failed check
    pii_vault: dict[str, str] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    def time_left(self) -> float:
        return self.time_budget_s - (time.monotonic() - self.started)
