"""Diagram box: Reliability + scale (lane D).
Retry with exponential backoff and jitter, circuit breakers per downstream system, and transient error typing.
Mocked: nothing; delays are kept tiny so demos run fast. Production adds HTTP client timeouts and bulkheads.
"""
from __future__ import annotations

import random
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from copilot.contracts.trace import Component, Trace

T = TypeVar("T")


class TransientError(Exception):
    """A failure worth retrying: timeout, 503, lost reply. Mocks raise subclasses of this."""


class CircuitOpen(Exception):
    pass


@dataclass
class RetryPolicy:
    attempts: int = 3
    base_delay_s: float = 0.005
    max_delay_s: float = 0.05


def backoff_delay(attempt: int, policy: RetryPolicy, rng: random.Random) -> float:
    """Full jitter: a random delay between 0 and base * 2^attempt, capped."""
    ceiling = min(policy.max_delay_s, policy.base_delay_s * (2 ** attempt))
    return rng.uniform(0, ceiling)


class CircuitBreaker:
    """Opens after `failure_threshold` consecutive failures; lets one trial call through after `reset_after_s`."""

    def __init__(self, name: str, failure_threshold: int = 5, reset_after_s: float = 30.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.name = name
        self.failure_threshold = failure_threshold
        self.reset_after_s = reset_after_s
        self._clock = clock
        self.failures = 0
        self.opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if self._clock() - self.opened_at >= self.reset_after_s else "open"

    def before_call(self) -> None:
        if self.state == "open":
            raise CircuitOpen(f"circuit for {self.name} is open")

    def record_success(self) -> None:
        self.failures, self.opened_at = 0, None

    def record_failure(self) -> None:
        self.failures += 1
        if self.failures >= self.failure_threshold:
            self.opened_at = self._clock()


def call_with_resilience(fn: Callable[[], T], *, breaker: CircuitBreaker, trace: Trace,
                         policy: RetryPolicy | None = None, rng: random.Random | None = None,
                         sleep: Callable[[float], None] = time.sleep) -> T:
    """Call `fn`, retrying transient errors with backoff and jitter. Each retry is a lane D span."""
    policy = policy or RetryPolicy()
    rng = rng or random.Random(7)
    for attempt in range(policy.attempts):
        try:
            breaker.before_call()
        except CircuitOpen:
            trace.event(Component.RELIABILITY, f"circuit open: {breaker.name}", status="error",
                        detail="call skipped, failing fast")
            raise
        try:
            result = fn()
        except TransientError as exc:
            breaker.record_failure()
            last_attempt = attempt == policy.attempts - 1
            trace.event(Component.RELIABILITY, f"{'gave up' if last_attempt else 'retry'}: {breaker.name}",
                        status="error" if last_attempt else "retry",
                        detail=f"attempt {attempt + 1}/{policy.attempts}: {exc}",
                        breaker=breaker.state)
            if last_attempt:
                raise
            sleep(backoff_delay(attempt, policy, rng))
        else:
            breaker.record_success()
            return result
    raise AssertionError("unreachable")
