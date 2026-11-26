"""Circuit breaker shared by the APIM mock (backend pool, system API bulkheads).

Production equivalent: APIM backend `circuitBreaker` rules (failure count in an interval, trip duration,
`acceptRetryAfter`). States: closed (0) -> open (1) after `failures` failures within `window_s`, for
max(`trip_s`, Retry-After) -> half-open (2): one trial request; success closes, failure re-opens.
"""
from __future__ import annotations

import threading
import time
from collections import deque

STATE_CODE = {"closed": 0, "open": 1, "half-open": 2}


class CircuitBreaker:
    def __init__(self, failures: int = 5, window_s: float = 30.0, trip_s: float = 10.0):
        self.failures, self.window_s, self.trip_s = failures, window_s, trip_s
        self._events: deque[float] = deque()
        self._open_until = 0.0
        self._trial = False
        self._lock = threading.Lock()

    def state(self) -> str:
        with self._lock:
            return self._state()

    def _state(self) -> str:
        if self._open_until == 0.0:
            return "closed"
        return "open" if time.monotonic() < self._open_until else "half-open"

    def allow(self) -> bool:
        with self._lock:
            s = self._state()
            if s == "closed":
                return True
            if s == "half-open" and not self._trial:
                self._trial = True
                return True
            return False

    def success(self) -> None:
        with self._lock:
            self._events.clear()
            self._open_until, self._trial = 0.0, False

    def failure(self, retry_after_s: float = 0.0) -> None:
        now = time.monotonic()
        with self._lock:
            if self._state() == "half-open":
                self._open_until, self._trial = now + max(self.trip_s, retry_after_s), False
                return
            self._events.append(now)
            while self._events and now - self._events[0] > self.window_s:
                self._events.popleft()
            if len(self._events) >= self.failures:
                self._open_until, self._trial = now + max(self.trip_s, retry_after_s), False
                self._events.clear()

    def reset(self) -> None:
        self.success()
