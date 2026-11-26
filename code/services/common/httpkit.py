"""A small HTTP kit on the standard library: routing, JSON, errors, request context, traced client, servers.

Every service and mock is an `App` served by its own `Server` (its own port), like separate pods. The kit gives
each of them the same cross-cutting behaviour, which in production comes from the platform and OpenTelemetry:

* W3C `traceparent` in and out on every hop, plus `x-request-id`;
* a request deadline carried as `x-deadline-ms` (milliseconds left); each client call's timeout is
  min(own cap, remaining deadline - reserve), and a call with no time left fails fast (plan §5);
* server and client spans and RED metrics per route (`http_requests_total`, `http_request_duration_seconds`);
* Azure-style JSON errors: `{"error": {"code": ..., "message": ...}}`;
* built-in `/healthz`, `/metrics` (Prometheus text) and `/telemetry/spans|logs` (local stand-in for App Insights).
"""
from __future__ import annotations

import contextvars
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .telemetry import Telemetry, current_span, parse_traceparent

DEADLINE_HEADER = "x-deadline-ms"
HOP_RESERVE_S = 0.05  # time kept back at each hop to turn a downstream timeout into a clean error


RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


class HttpError(Exception):
    """Rendered as the common error envelope (LLD §1.4):
    `{"error": {"code", "message", "request_id", "retryable", ...details}}`."""

    def __init__(self, status: int, code: str, message: str = "", headers: dict | None = None, details: dict | None = None,
                 retryable: bool | None = None):
        super().__init__(message or code)
        self.status, self.code, self.message = status, code, message or code
        self.headers, self.details = headers or {}, details or {}
        self.retryable = (status in RETRYABLE_STATUSES) if retryable is None else retryable


# --------------------------------------------------------------------------- request context
@dataclass
class RequestContext:
    request_id: str
    deadline: float | None = None  # time.monotonic() value
    data: dict = field(default_factory=dict)  # per-service request state (claims, tokens, budgets, lineage)

    def remaining(self) -> float:
        return float("inf") if self.deadline is None else self.deadline - time.monotonic()


_ctx: contextvars.ContextVar[RequestContext | None] = contextvars.ContextVar("rmc_request_ctx", default=None)


def request_context() -> RequestContext:
    c = _ctx.get()
    if c is None:  # background work (workers): a context without a deadline
        c = RequestContext(request_id="bg-" + str(int(time.time() * 1000)))
        _ctx.set(c)
    return c


def set_request_context(c: RequestContext) -> contextvars.Token:
    return _ctx.set(c)


def time_left() -> float:
    return request_context().remaining()


def run_parallel(fns: dict[str, Callable[[], Any]], max_workers: int = 8) -> dict[str, tuple[Any, BaseException | None]]:
    """Run callables concurrently, each in a copy of the caller's context (trace, deadline, identity)."""
    with ThreadPoolExecutor(max_workers=min(max_workers, max(1, len(fns)))) as ex:
        futures = {k: ex.submit(contextvars.copy_context().run, fn) for k, fn in fns.items()}
        out = {}
        for k, f in futures.items():
            try:
                out[k] = (f.result(), None)
            except BaseException as exc:  # noqa: BLE001 - returned to the caller to decide
                out[k] = (None, exc)
        return out


# --------------------------------------------------------------------------- request / response
@dataclass
class Request:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    body: bytes
    params: dict[str, str] = field(default_factory=dict)
    route: str = ""
    state: dict = field(default_factory=dict)

    def header(self, name: str, default: str | None = None) -> str | None:
        return self.headers.get(name.lower(), default)

    def bearer(self, header: str = "authorization") -> str | None:
        value = self.header(header) or ""
        return value[7:].strip() if value[:7].lower() == "bearer " else None

    def json(self) -> Any:
        if not self.body:
            return {}
        try:
            return json.loads(self.body)
        except ValueError as exc:
            raise HttpError(400, "InvalidJson", "request body is not valid JSON") from exc

    def form(self) -> dict[str, str]:
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.body.decode()).items()}


@dataclass
class Response:
    status: int = 200
    body: Any = None
    headers: dict = field(default_factory=dict)
    content_type: str | None = None

    def encode(self) -> tuple[bytes, str]:
        if isinstance(self.body, bytes):
            return self.body, self.content_type or "application/octet-stream"
        if isinstance(self.body, str):
            return self.body.encode(), self.content_type or "text/plain; charset=utf-8"
        if self.body is None:
            return b"", self.content_type or "application/json"
        return json.dumps(self.body).encode(), self.content_type or "application/json"


Handler = Callable[[Request], "Response | dict | list | None"]


def _compile(pattern: str) -> re.Pattern:
    out, pos = "", 0
    for m in re.finditer(r"\{(\w+)(:path)?\}", pattern):
        out += re.escape(pattern[pos:m.start()]) + (f"(?P<{m.group(1)}>.+)" if m.group(2) else f"(?P<{m.group(1)}>[^/]+)")
        pos = m.end()
    return re.compile("^" + out + re.escape(pattern[pos:]) + "$")


class App:
    def __init__(self, name: str, telemetry: Telemetry | None = None, cors: bool = False, response_headers: dict | None = None,
                 trust_inbound_context: bool = True):
        self.name = name
        # False at the internet-facing edge (web-bff): a client's `x-request-id` / `traceparent` are not adopted, so a
        # client cannot put arbitrary text into telemetry, collide with another RM's request id or join their trace.
        self.trust_inbound_context = trust_inbound_context
        self.telemetry = telemetry or Telemetry(name)
        self.cors = cors
        self.response_headers = response_headers or {}
        self._routes: list[tuple[str, str, re.Pattern, Handler]] = []
        self.route("GET", "/healthz")(lambda r: {"status": "ok", "service": self.name})
        self.route("GET", "/metrics")(lambda r: Response(200, self.telemetry.metrics.render(), content_type="text/plain; version=0.0.4; charset=utf-8"))
        self.route("GET", "/telemetry/spans")(self._spans)
        self.route("GET", "/telemetry/logs")(lambda r: {"value": self.telemetry.logs()})
        m = self.telemetry.metrics
        self._req_total = m.counter("http_requests_total", "HTTP requests handled", ("service", "method", "route", "status"))
        self._req_seconds = m.histogram("http_request_duration_seconds", "HTTP request duration", ("service", "route"))

    def _spans(self, r: Request) -> dict:
        tid = r.query.get("trace_id")
        return {"service": self.name, "value": self.telemetry.spans_for(tid) if tid else self.telemetry.all_spans()[-500:]}

    def route(self, method: str, pattern: str) -> Callable[[Handler], Handler]:
        def deco(fn: Handler) -> Handler:
            self._routes.append((method.upper(), pattern, _compile(pattern), fn))
            return fn
        return deco

    def _match(self, method: str, path: str) -> tuple[Handler | None, str, dict, bool]:
        path_known = False
        for m, pattern, rx, fn in self._routes:
            hit = rx.match(path)
            if hit:
                path_known = True
                if m == method:
                    return fn, pattern, {k: urllib.parse.unquote(v) for k, v in hit.groupdict().items()}, True
        return None, "unmatched", {}, path_known

    def dispatch(self, method: str, raw_path: str, headers: dict[str, str], body: bytes) -> Response:
        method = method.upper()
        url = urllib.parse.urlsplit(raw_path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()}
        headers = {k.lower(): v for k, v in headers.items()}
        if method == "OPTIONS" and self.cors:
            return self._finish(Response(204, None), headers.get("x-request-id", "") if self.trust_inbound_context else "", None)
        fn, route, params, path_known = self._match(method, url.path)
        req = Request(method, url.path, query, headers, body, params, route)
        trusted = self.trust_inbound_context
        request_id = (headers.get("x-request-id") if trusted else None) or ("req-" + _short_id())
        deadline_ms = headers.get(DEADLINE_HEADER)
        ctx = RequestContext(request_id, time.monotonic() + max(0.0, float(deadline_ms)) / 1000 if _is_number(deadline_ms) else None)
        token = set_request_context(ctx)
        t0 = time.perf_counter()
        status = 500
        try:
            with self.telemetry.span(f"{method} {route}", kind="server", parent=parse_traceparent(headers.get("traceparent")) if trusted else None,
                                     **{"http.method": method, "http.route": route, "rmc.request_id": request_id}) as sp:
                resp = self._invoke(fn, req, path_known)
                status = resp.status
                sp.set_attr("http.status_code", status)
                if status >= 500:
                    sp.status = "error"
                return self._finish(resp, request_id, sp)
        finally:
            if route not in ("/metrics", "/telemetry/spans", "/telemetry/logs", "/healthz"):
                self._req_total.inc(service=self.name, method=method, route=route, status=str(status))
                self._req_seconds.observe(time.perf_counter() - t0, service=self.name, route=route)
            _ctx.reset(token)

    def _invoke(self, fn: Handler | None, req: Request, path_known: bool) -> Response:
        try:
            if fn is None:
                raise HttpError(405 if path_known else 404, "MethodNotAllowed" if path_known else "NotFound", "no such route")
            result = fn(req)
            return result if isinstance(result, Response) else Response(200, result)
        except HttpError as e:
            return Response(e.status, {"error": {"code": e.code, "message": e.message, "request_id": request_context().request_id,
                                                 "retryable": e.retryable, **e.details}}, dict(e.headers))
        except Exception as exc:  # noqa: BLE001 - never leak internals; record the type only
            self.telemetry.log("unhandled_exception", level="error", **{"error.type": type(exc).__name__})
            return Response(500, {"error": {"code": "INTERNAL", "message": "internal error", "request_id": request_context().request_id,
                                            "retryable": True}})

    def _finish(self, resp: Response, request_id: str, span) -> Response:
        resp.headers.setdefault("x-request-id", request_id)
        if span is not None:
            resp.headers.setdefault("traceparent", span.context.traceparent())
        for k, v in self.response_headers.items():
            resp.headers.setdefault(k, v)
        if self.cors:
            resp.headers.update({"Access-Control-Allow-Origin": "*", "Access-Control-Allow-Headers": "authorization, content-type, x-request-id",
                                 "Access-Control-Allow-Methods": "GET, POST, OPTIONS"})
        return resp


def _is_number(v: str | None) -> bool:
    try:
        float(v)  # type: ignore[arg-type]
        return True
    except (TypeError, ValueError):
        return False


def _short_id() -> str:
    import secrets
    return secrets.token_hex(8)


# --------------------------------------------------------------------------- server
class _Handler(BaseHTTPRequestHandler):
    server_version = "rmc"
    sys_version = ""

    def _handle(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        body = self.rfile.read(length) if length else b""
        app: App = self.server.app  # type: ignore[attr-defined]
        resp = app.dispatch(self.command, self.path, dict(self.headers.items()), body)
        payload, ctype = resp.encode()
        try:
            self.send_response(resp.status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            for k, v in resp.headers.items():
                self.send_header(k, str(v))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the caller gave up (its deadline expired); nothing to send it

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = do_OPTIONS = _handle

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - silence default access log
        pass


class _ThreadingServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 256

    def handle_error(self, request, client_address) -> None:  # noqa: ANN001 - socketserver signature
        import sys
        if not isinstance(sys.exc_info()[1], ConnectionError):
            super().handle_error(request, client_address)


class Server:
    """Binds first (so ephemeral ports are known before apps are built), serves once an app is attached."""

    def __init__(self, name: str, host: str = "127.0.0.1", port: int = 0):
        self.name = name
        self._httpd = _ThreadingServer((host, port), _Handler)
        self._httpd.app = None  # type: ignore[attr-defined]
        self.host, self.port = self._httpd.server_address[0], self._httpd.server_address[1]
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, app: App) -> "Server":
        self._httpd.app = app  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.2}, name=f"srv-{self.name}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._thread:
            self._httpd.shutdown()
        self._httpd.server_close()


# --------------------------------------------------------------------------- client
class TransportError(Exception):
    """The call did not produce an HTTP response: timeout, refused connection, or no deadline left."""

    def __init__(self, kind: str, message: str = ""):
        super().__init__(message or kind)
        self.kind = kind


@dataclass
class ClientResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        try:
            return json.loads(self.body) if self.body else {}
        except ValueError:
            return {}

    def error_code(self) -> str:
        err = self.json().get("error") if isinstance(self.json(), dict) else None
        return str(err.get("code", "")) if isinstance(err, dict) else ""


class HttpClient:
    """Traced, deadline-aware HTTP client. Never follows proxies for local calls (set use_proxy for real Azure)."""

    def __init__(self, telemetry: Telemetry, use_proxy: bool = False):
        self.telemetry = telemetry
        self._opener = urllib.request.build_opener() if use_proxy else urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._calls = telemetry.metrics.counter("http_client_requests_total", "Outbound HTTP calls", ("service", "peer", "status"))

    def request(self, method: str, url: str, *, json_body: Any = None, data: bytes | None = None, headers: dict | None = None,
                timeout: float = 5.0, peer: str = "") -> ClientResponse:
        ctx = request_context()
        remaining = ctx.remaining()
        effective = min(timeout, remaining - HOP_RESERVE_S)
        peer = peer or urllib.parse.urlsplit(url).netloc
        if effective <= 0:
            self._calls.inc(service=self.telemetry.service, peer=peer, status="deadline")
            raise TransportError("deadline", "no time left before the request deadline")
        hdrs = {"content-type": "application/json"} if json_body is not None else {}
        hdrs.update({k.lower(): v for k, v in (headers or {}).items()})
        body = json.dumps(json_body).encode() if json_body is not None else data
        with self.telemetry.span(f"{method} {peer}", kind="client", **{"http.method": method, "peer.service": peer}) as sp:
            hdrs["traceparent"] = sp.context.traceparent()
            hdrs["x-request-id"] = ctx.request_id
            if remaining != float("inf"):
                hdrs[DEADLINE_HEADER] = str(int(effective * 1000))
            req = urllib.request.Request(url, data=body, method=method, headers=hdrs)
            try:
                with self._opener.open(req, timeout=effective) as r:
                    resp = ClientResponse(r.status, {k.lower(): v for k, v in r.headers.items()}, r.read())
            except urllib.error.HTTPError as e:
                resp = ClientResponse(e.code, {k.lower(): v for k, v in e.headers.items()}, e.read())
            except (TimeoutError, urllib.error.URLError, ConnectionError, OSError) as e:
                timed_out = isinstance(e, TimeoutError) or "timed out" in str(e)
                sp.status = "error"
                sp.set_attr("error.type", "Timeout" if timed_out else "ConnectError")
                self._calls.inc(service=self.telemetry.service, peer=peer, status="timeout" if timed_out else "connect_error")
                raise TransportError("timeout" if timed_out else "connect") from None
            sp.set_attr("http.status_code", resp.status)
            if resp.status >= 500:
                sp.status = "error"
            self._calls.inc(service=self.telemetry.service, peer=peer, status=str(resp.status))
            return resp


def current_trace_id() -> str | None:
    s = current_span()
    return s.trace_id if s else None
