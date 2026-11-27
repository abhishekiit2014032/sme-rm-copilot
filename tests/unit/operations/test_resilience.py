"""Diagram box: Reliability + scale (lane D).
Proves transient failures are retried with capped, jittered exponential backoff, and the circuit breaker opens
after repeated failures, fails fast, and recovers after a successful trial call.
"""
from __future__ import annotations

import random

import pytest

from copilot.contracts import Trace
from copilot.operations.resilience import (CircuitBreaker, CircuitOpen, RetryPolicy, TransientError, backoff_delay,
                                           call_with_resilience)
from helpers import GULF_STAR, ask


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now


class Flaky:
    """Fails with a transient error `failures` times, then succeeds."""

    def __init__(self, failures: int) -> None:
        self.failures, self.calls = failures, 0

    def __call__(self) -> str:
        self.calls += 1
        if self.calls <= self.failures:
            raise TransientError("HTTP 503")
        return "ok"


def resilient(fn, breaker: CircuitBreaker | None = None, trace: Trace | None = None):
    sleeps: list[float] = []
    result = call_with_resilience(fn, breaker=breaker or CircuitBreaker("crm"), trace=trace or Trace(),
                                  policy=RetryPolicy(attempts=3), sleep=sleeps.append)
    return result, sleeps


def test_transient_failures_are_retried_with_capped_jittered_backoff():
    trace = Trace()
    result, sleeps = resilient(Flaky(failures=2), trace=trace)
    assert result == "ok" and len(sleeps) == 2
    assert [(s.lane.value, s.status) for s in trace.spans] == [("D", "retry"), ("D", "retry")]
    policy, rng = RetryPolicy(base_delay_s=0.01, max_delay_s=0.05), random.Random(1)
    for attempt, ceiling in [(0, 0.01), (1, 0.02), (2, 0.04), (5, 0.05)]:
        assert all(0 <= backoff_delay(attempt, policy, rng) <= ceiling for _ in range(50))
    assert len({backoff_delay(1, policy, rng) for _ in range(5)}) > 1        # jittered, not fixed
    gives_up = Flaky(failures=99)
    with pytest.raises(TransientError):
        resilient(gives_up)
    assert gives_up.calls == 3


def test_circuit_opens_fails_fast_then_half_opens_and_recovers():
    clock = Clock()
    breaker = CircuitBreaker("core", failure_threshold=3, reset_after_s=30, clock=clock)
    with pytest.raises(TransientError):
        resilient(Flaky(failures=99), breaker=breaker)
    never_called = Flaky(failures=0)
    with pytest.raises(CircuitOpen):
        resilient(never_called, breaker=breaker)
    assert breaker.state == "open" and never_called.calls == 0
    clock.now = 31
    assert breaker.state == "half-open"
    assert resilient(Flaky(failures=0), breaker=breaker)[0] == "ok" and breaker.state == "closed"


def test_core_banking_outage_is_retried_three_times_end_to_end(copilot):
    copilot.core_banking.down = True
    trace = ask(copilot, "rm-1", action="prepare_brief", customer_id=GULF_STAR).trace
    reliability = [s.status for s in trace.spans if s.component.value == "Reliability + scale"]
    assert reliability == ["retry", "retry", "error"] and copilot.core_banking.calls == 3
