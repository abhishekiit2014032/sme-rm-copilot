"""Telemetry shared by every service and mock: W3C trace context, spans, structured logs, Prometheus metrics.

Production equivalent: the OpenTelemetry SDK exporting to Azure Monitor Application Insights (traces, logs) and a
`/metrics` endpoint scraped by Azure Monitor managed service for Prometheus (PodMonitor).

Metadata only (plan C5): span attributes and log fields pass through an allow-list of keys and scalar values, so
prompt text, notes, customer names or exception messages cannot be recorded by accident. Exceptions are recorded
by type name only. Each service owns its own `Telemetry` instance, as separate pods would; the web-bff trace view
queries every component's `/telemetry/spans`, the local stand-in for an Application Insights query by operation id.
"""
from __future__ import annotations

import contextvars
import re
import secrets
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Iterator

_TRACEPARENT = re.compile(r"^00-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")

# Keys that may appear in span attributes and log fields. Values must be scalars; strings are truncated.
SAFE_KEYS = frozenset({
    "http.method", "http.route", "http.status_code", "peer.service", "error.type", "net.peer.port",
    "rmc.use_case", "rmc.request_id", "rmc.decision", "rmc.reason", "rmc.backend", "rmc.breaker", "rmc.outcome",
    "rmc.flag", "rmc.items", "rmc.count", "rmc.attempt", "rmc.degraded", "rmc.partial", "rmc.tool", "rmc.op",
    "rmc.retry_after_ms", "rmc.remaining_ms", "rmc.index", "rmc.offset", "rmc.event", "rmc.queue_depth",
    "gen_ai.system", "gen_ai.operation.name", "gen_ai.request.model", "gen_ai.response.model",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens", "service", "level", "event", "status",
    "rmc.agent", "rmc.node", "rmc.hop", "rmc.server", "rmc.step", "rmc.memory", "rmc.rpc_method", "rmc.graph", "rmc.resumed",
})
_MAX_STR = 80


def safe_fields(fields: dict) -> dict:
    """Drop anything that is not an allow-listed key with a scalar value."""
    out = {}
    for k, v in fields.items():
        if k not in SAFE_KEYS or v is None:
            continue
        if isinstance(v, bool) or isinstance(v, (int, float)):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:_MAX_STR]
    return out


@dataclass(frozen=True)
class SpanContext:
    trace_id: str
    span_id: str

    def traceparent(self) -> str:
        return f"00-{self.trace_id}-{self.span_id}-01"


def parse_traceparent(value: str | None) -> SpanContext | None:
    m = _TRACEPARENT.match((value or "").strip().lower())
    if not m or m.group(1) == "0" * 32 or m.group(2) == "0" * 16:
        return None
    return SpanContext(m.group(1), m.group(2))


def new_trace_id() -> str:
    return secrets.token_hex(16)


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str | None
    service: str
    name: str
    kind: str
    start: float
    duration_ms: float = 0.0
    status: str = "ok"
    attributes: dict = field(default_factory=dict)

    @property
    def context(self) -> SpanContext:
        return SpanContext(self.trace_id, self.span_id)

    def set(self, **attrs) -> None:
        self.attributes.update(safe_fields({k.replace("__", "."): v for k, v in attrs.items()}))

    def set_attr(self, key: str, value: object) -> None:
        self.attributes.update(safe_fields({key: value}))


_current_span: contextvars.ContextVar[Span | None] = contextvars.ContextVar("rmc_current_span", default=None)


def current_span() -> Span | None:
    return _current_span.get()


# --------------------------------------------------------------------------- metrics
def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(names: tuple[str, ...], values: tuple[str, ...], extra: str = "") -> str:
    parts = [f'{n}="{_escape(str(v))}"' for n, v in zip(names, values)]
    if extra:
        parts.append(extra)
    return "{" + ",".join(parts) + "}" if parts else ""


class _Metric:
    kind = ""

    def __init__(self, name: str, help_text: str, labels: tuple[str, ...]):
        self.name, self.help, self.labelnames = name, help_text, labels
        self._lock = threading.Lock()

    def _key(self, labels: dict) -> tuple[str, ...]:
        return tuple(str(labels.get(n, "")) for n in self.labelnames)


class Counter(_Metric):
    kind = "counter"

    def __init__(self, *a):
        super().__init__(*a)
        self.values: dict[tuple[str, ...], float] = {}

    def inc(self, amount: float = 1.0, **labels) -> None:
        k = self._key(labels)
        with self._lock:
            self.values[k] = self.values.get(k, 0.0) + amount

    def render(self) -> list[str]:
        with self._lock:
            return [f"{self.name}{_labels(self.labelnames, k)} {v:g}" for k, v in sorted(self.values.items())]


class Gauge(Counter):
    kind = "gauge"

    def set(self, value: float, **labels) -> None:
        with self._lock:
            self.values[self._key(labels)] = float(value)


class Histogram(_Metric):
    kind = "histogram"
    BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 16.0)

    def __init__(self, *a):
        super().__init__(*a)
        self.series: dict[tuple[str, ...], list[float]] = {}  # bucket counts + [sum, count]

    def observe(self, value: float, **labels) -> None:
        k = self._key(labels)
        with self._lock:
            s = self.series.setdefault(k, [0.0] * (len(self.BUCKETS) + 2))
            for i, b in enumerate(self.BUCKETS):
                if value <= b:
                    s[i] += 1
            s[-2] += value
            s[-1] += 1

    def render(self) -> list[str]:
        lines = []
        with self._lock:
            for k, s in sorted(self.series.items()):
                for i, b in enumerate(self.BUCKETS):
                    le = 'le="%g"' % b
                    lines.append(f"{self.name}_bucket{_labels(self.labelnames, k, le)} {s[i]:g}")
                inf = 'le="+Inf"'
                lines.append(f"{self.name}_bucket{_labels(self.labelnames, k, inf)} {s[-1]:g}")
                lines.append(f"{self.name}_sum{_labels(self.labelnames, k)} {s[-2]:.6f}")
                lines.append(f"{self.name}_count{_labels(self.labelnames, k)} {s[-1]:g}")
        return lines


class Metrics:
    """A tiny Prometheus registry. Metric families are created on first use and rendered in text format 0.0.4."""

    def __init__(self) -> None:
        self._families: dict[str, _Metric] = {}
        self._lock = threading.Lock()

    def _get(self, cls, name: str, help_text: str, labels: tuple[str, ...]):
        with self._lock:
            m = self._families.get(name)
            if m is None:
                m = self._families[name] = cls(name, help_text, labels)
            return m

    def counter(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Counter:
        return self._get(Counter, name, help_text, labels)

    def gauge(self, name: str, help_text: str, labels: tuple[str, ...] = ()) -> Gauge:
        return self._get(Gauge, name, help_text, labels)

    def histogram(self, name: str, help_text: str, labels: tuple[str, ...] = (), buckets: tuple[float, ...] | None = None) -> Histogram:
        h = self._get(Histogram, name, help_text, labels)
        if buckets and not h.series:
            h.BUCKETS = tuple(buckets)
        return h

    def render(self) -> str:
        out = []
        with self._lock:
            families = list(self._families.values())
        for m in sorted(families, key=lambda f: f.name):
            out += [f"# HELP {m.name} {m.help}", f"# TYPE {m.name} {m.kind}", *m.render()]
        return "\n".join(out) + "\n"


# --------------------------------------------------------------------------- per-service telemetry
class Telemetry:
    def __init__(self, service: str, max_spans: int = 20000):
        self.service = service
        self.metrics = Metrics()
        self._spans: deque[Span] = deque(maxlen=max_spans)
        self._logs: deque[dict] = deque(maxlen=5000)
        self._lock = threading.Lock()

    @contextmanager
    def span(self, name: str, kind: str = "internal", parent: SpanContext | None = None, **attrs) -> Iterator[Span]:
        cur = current_span()
        ctx = parent or (cur.context if cur else None)
        sp = Span(ctx.trace_id if ctx else new_trace_id(), secrets.token_hex(8), ctx.span_id if ctx else None,
                  self.service, name, kind, time.time())
        sp.set(**attrs)
        token = _current_span.set(sp)
        t0 = time.perf_counter()
        try:
            yield sp
        except BaseException as exc:
            sp.status = "error"
            sp.set_attr("error.type", type(exc).__name__)
            raise
        finally:
            sp.duration_ms = round((time.perf_counter() - t0) * 1000, 2)
            _current_span.reset(token)
            with self._lock:
                self._spans.append(sp)

    def log(self, event: str, level: str = "info", **fields) -> None:
        cur = current_span()
        rec = {"ts": round(time.time(), 3), "service": self.service, "level": level, "event": event,
               "trace_id": cur.trace_id if cur else None, **safe_fields(fields)}
        with self._lock:
            self._logs.append(rec)

    def spans_for(self, trace_id: str) -> list[dict]:
        with self._lock:
            return [asdict(s) for s in self._spans if s.trace_id == trace_id]

    def all_spans(self) -> list[dict]:
        with self._lock:
            return [asdict(s) for s in self._spans]

    def logs(self) -> list[dict]:
        with self._lock:
            return list(self._logs)
