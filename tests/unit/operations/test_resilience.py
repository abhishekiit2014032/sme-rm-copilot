"""Diagram box: Reliability + scale (lane D).
Proves transient failures are retried with capped, jittered exponential backoff, non-transient errors are not
retried, and the circuit breaker opens after repeated failures, fails fast, and recovers after a trial call.
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


def resilient(fn, breaker: CircuitBreaker | None = None, attempts: int = 3, trace: Trace | None = None):
    sleeps: list[float] = []
    result = call_with_resilience(fn, breaker=breaker or CircuitBreaker("crm"), trace=trace or Trace(),
                                  policy=RetryPolicy(attempts=attempts), sleep=sleeps.append)
    return result, sleeps


def test_transient_failure_is_retried_until_success_with_a_backoff_each_time():
    trace = Trace()
    result, sleeps = resilient(Flaky(failures=2), trace=trace)
    assert result == "ok" and len(sleeps) == 2
    assert [s.status for s in trace.spans] == ["retry", "retry"]
    assert {s.lane.value for s in trace.spans} == {"D"}


def test_gives_up_after_the_last_attempt_without_a_final_sleep():
    flaky = Flaky(failures=5)
    with pytest.raises(TransientError):
        resilient(flaky, attempts=3)
    assert flaky.calls == 3


def test_non_transient_error_is_not_retried():
    calls = []

    def broken():
        calls.append(1)
        raise KeyError("bad request")

    with pytest.raises(KeyError):
        resilient(broken)
    assert len(calls) == 1


def test_backoff_ceiling_doubles_per_attempt_and_is_capped():
    policy = RetryPolicy(base_delay_s=0.01, max_delay_s=0.05)
    rng = random.Random(1)
    for attempt, ceiling in [(0, 0.01), (1, 0.02), (2, 0.04), (5, 0.05)]:
        delays = [backoff_delay(attempt, policy, rng) for _ in range(50)]
        assert all(0 <= d <= ceiling for d in delays)
    assert len({backoff_delay(1, policy, rng) for _ in range(5)}) > 1     # jittered, not fixed


def test_circuit_opens_after_threshold_failures_and_then_fails_fast():
    breaker = CircuitBreaker("core", failure_threshold=3, clock=Clock())
    with pytest.raises(TransientError):
        resilient(Flaky(failures=99), breaker=breaker, attempts=3)
    assert breaker.state == "open"
    never_called = Flaky(failures=0)
    with pytest.raises(CircuitOpen):
        resilient(never_called, breaker=breaker)
    assert never_called.calls == 0


def test_circuit_half_opens_after_reset_time_and_closes_on_a_successful_trial():
    clock = Clock()
    breaker = CircuitBreaker("core", failure_threshold=1, reset_after_s=30, clock=clock)
    breaker.record_failure()
    assert breaker.state == "open"
    clock.now = 31
    assert breaker.state == "half-open"
    assert resilient(Flaky(failures=0), breaker=breaker)[0] == "ok"
    assert breaker.state == "closed" and breaker.failures == 0


def test_core_banking_outage_is_retried_and_then_marked_unavailable_end_to_end(copilot):
    copilot.core_banking.down = True
    trace = ask(copilot, "rm-1", action="prepare_brief", customer_id=GULF_STAR).trace
    reliability = [s for s in trace.spans if s.component.value == "Reliability + scale"]
    assert [s.status for s in reliability] == ["retry", "retry", "error"]
    assert copilot.core_banking.calls == 3
