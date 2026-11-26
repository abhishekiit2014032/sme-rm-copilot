"""Start the whole local stack: every Azure mock and every service, each on its own localhost port.

    python3 stack.py                       # start, print URLs, open the console URL in a browser, Ctrl+C to stop
    python3 stack.py --latency realistic   # mock services take roughly realistic time
    python3 stack.py --port-base 0         # ephemeral ports
    python3 stack.py --smoke               # start, run UC1/UC2/UC3 once end to end, verify the audit chain, stop

One config maps each logical dependency to a base URL (services/common/config.py). To use a real Azure service,
set `RMC_URL_<NAME>` (e.g. `RMC_URL_SEARCH=https://<name>.search.windows.net`): that mock is not started and
every service calls the URL you gave. See README.md for the table and what changes beyond the URL.
"""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from azmock.apim import ApimMock
from azmock.appconfig import AppConfigMock
from azmock.aoai import AzureOpenAIMock
from azmock.content_safety import ContentSafetyMock
from azmock.cosmos import CosmosMock
from azmock.entra import WORKLOADS, EntraMock
from azmock.eventhubs import EventHubsMock
from azmock.keyvault import KeyVaultMock
from azmock.language import LanguageMock
from azmock.search import SearchMock
from azmock.systems import SystemsOfRecordMock
from services.audit_writer import AuditWriter
from services.common.config import SOR_SYSTEMS, Settings, StackConfig, env_url_overrides
from services.common.httpkit import Server
from services.ingestion_worker import IngestionWorker
from services.orchestrator import Orchestrator
from services.outbox_worker import OutboxWorker
from services.tool_gateway import ToolGateway
from services.web_bff import WebBff

# component -> factory; order = port offset from --port-base
MOCKS = {
    "entra": EntraMock, "keyvault": KeyVaultMock, "appconfig": AppConfigMock, "cosmos": CosmosMock, "eventhubs": EventHubsMock,
    "aoai-primary": lambda c: AzureOpenAIMock(c, "aoai-primary"), "aoai-secondary": lambda c: AzureOpenAIMock(c, "aoai-secondary"),
    "apim": ApimMock, "content-safety": ContentSafetyMock, "language": LanguageMock, "search": SearchMock, "systems": SystemsOfRecordMock,
}
SERVICES = {"web-bff": WebBff, "orchestrator": Orchestrator, "tool-gateway": ToolGateway, "outbox-worker": OutboxWorker, "audit-writer": AuditWriter}
SYSTEM_URLS = (*SOR_SYSTEMS, "graph")


@dataclass
class Stack:
    settings: Settings
    host: str = "127.0.0.1"
    port_base: int = 0
    components: dict = field(default_factory=dict)  # name -> mock or service object
    servers: dict = field(default_factory=dict)
    cfg: StackConfig | None = None
    ingestion: dict | None = None

    def start(self) -> "Stack":
        overrides = env_url_overrides()
        names = [n for n in MOCKS if n not in overrides and not (n == "systems" and all(s in overrides for s in SYSTEM_URLS))]
        names += [n for n in SERVICES if n not in overrides]
        for i, n in enumerate(list(MOCKS) + list(SERVICES)):
            if n in names:
                self.servers[n] = Server(n, self.host, self.port_base + i if self.port_base else 0)
        urls = {n: s.url for n, s in self.servers.items()}
        for sysname in SYSTEM_URLS:
            urls.setdefault(sysname, overrides.get(sysname) or f"{urls['systems']}/{sysname}")
        urls.update(overrides)
        urls.pop("systems", None)
        secret_names = [*WORKLOADS, "apim-backend-signing"]
        self.cfg = StackConfig(self.settings, urls, {n: secrets.token_urlsafe(32) for n in secret_names})
        self.cfg.urls["systems"] = self.servers["systems"].url if "systems" in self.servers else urls["crm"]
        for n in names:
            factory = MOCKS.get(n) or SERVICES[n]
            self.components[n] = factory(self.cfg)
        for n in names:
            self.servers[n].start(self.components[n].app)
        if "keyvault" in self.components and "appconfig" in self.components:
            # Pipeline step in production: publish the audit transport public key where producers can read it.
            jwk = self.components["keyvault"].public_jwk("audit-transport")
            self.components["appconfig"].put_value("audit/transport-public-key", json.dumps(jwk), "application/json")
        self.ingestion = IngestionWorker(self.cfg).run()
        for n in ("audit-writer", "outbox-worker"):
            if n in self.components:
                self.components[n].start()
        return self

    def stop(self) -> None:
        for n in ("outbox-worker", "audit-writer"):
            if n in self.components:
                self.components[n].stop()
        for s in self.servers.values():
            s.stop()

    def __enter__(self) -> "Stack":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    def url(self, name: str) -> str:
        return self.cfg.url(name)  # type: ignore[union-attr]

    def describe(self) -> list[tuple[str, str, str]]:
        rows = []
        for n in [*MOCKS, *SERVICES]:
            if n in self.servers:
                rows.append((n, "service" if n in SERVICES else "mock", self.servers[n].url))
            elif n in self.cfg.urls:  # type: ignore[union-attr]
                rows.append((n, "external", self.cfg.urls[n]))  # type: ignore[union-attr]
        return rows


# --------------------------------------------------------------------------- a console-like client
class ApiError(Exception):
    def __init__(self, status: int, body: dict, headers: dict):
        super().__init__(f"{status} {body}")
        self.status, self.body, self.headers = status, body, headers


class ConsoleClient:
    """What the browser console does, from Python: sign in, open a session, call the BFF. Used by tests and loadtest."""

    def __init__(self, stack: Stack | None, upn: str = "layla@bank.example", *, base: str = "", token_url: str = "", password: str = ""):
        if stack is not None:
            base, token_url, password = stack.url("web-bff"), stack.cfg.token_url, stack.settings.dev_password  # type: ignore[union-attr]
        self.base, self.token_url, self.password = base.rstrip("/"), token_url, password
        self.upn = upn
        self.token: str | None = None
        self.session: dict = {}
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    @classmethod
    def for_url(cls, web_bff_url: str, upn: str) -> "ConsoleClient":
        """For a stack started elsewhere: read the sign-in settings from web-bff, as the browser console does."""
        c = cls(None, upn, base=web_bff_url)
        status, conf, _ = c._http("GET", c.base + "/api/config", None, {})
        if status != 200:
            raise ApiError(status, conf, {})
        c.token_url, c.password = conf["token_url"], conf["mock_password"]
        return c

    def _http(self, method: str, url: str, body: bytes | None, headers: dict, timeout: float = 30.0) -> tuple[int, dict, dict]:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=timeout) as r:
                raw, status, hdrs = r.read(), r.status, dict(r.headers.items())
        except urllib.error.HTTPError as e:
            raw, status, hdrs = e.read(), e.code, dict(e.headers.items())
        try:
            return status, json.loads(raw) if raw else {}, {k.lower(): v for k, v in hdrs.items()}
        except ValueError:
            return status, {"raw": raw.decode(errors="replace")}, {k.lower(): v for k, v in hdrs.items()}

    def sign_in(self) -> "ConsoleClient":
        form = urllib.parse.urlencode({"grant_type": "password", "client_id": "rm-copilot-console", "username": self.upn,
                                       "password": self.password, "scope": "api://rm-copilot-web-bff/access_as_user"}).encode()
        status, body, _ = self._http("POST", self.token_url, form, {"content-type": "application/x-www-form-urlencoded"})
        if status != 200:
            raise ApiError(status, body, {})
        self.token = body["access_token"]
        self.session = self.call("POST", "/api/session")
        return self

    def call(self, method: str, path: str, body: dict | None = None, *, headers: dict | None = None, check: bool = True) -> dict:
        status, out, hdrs = self.raw(method, path, body, headers=headers)
        if check and status >= 400:
            raise ApiError(status, out, hdrs)
        out["_status"] = status
        return out

    def raw(self, method: str, path: str, body: dict | None = None, *, headers: dict | None = None) -> tuple[int, dict, dict]:
        h = {"content-type": "application/json", **({"authorization": "Bearer " + self.token} if self.token else {}), **(headers or {})}
        return self._http(method, self.base + path, json.dumps(body).encode() if body is not None else None, h)

    def approve_headers(self) -> dict:
        return {"origin": self.base, "x-session-id": self.session.get("session_id", ""), "x-csrf-token": self.session.get("csrf_token", "")}

    # convenience wrappers
    def brief(self, customer_id: str, **kw) -> dict:
        return self.call("POST", "/api/uc1/brief", {"customer_id": customer_id}, **kw)

    def ask(self, question: str, **kw) -> dict:
        return self.call("POST", "/api/uc2/ask", {"question": question}, **kw)

    def draft(self, customer_id: str, notes: str, **kw) -> dict:
        return self.call("POST", "/api/uc3/draft", {"customer_id": customer_id, "notes": notes}, **kw)

    def approve(self, draft: dict, **kw) -> dict:
        body = {"customer_id": draft["customer_id"], "draft_id": draft.get("draft_id"),
                "draft": {"call_report": draft["call_report"], "tasks": draft["tasks"], "email": draft.get("email_draft", "")}}
        return self.call("POST", "/api/uc3/approve", body, headers=self.approve_headers(), **kw)

    def trace(self, request_id: str) -> dict:
        return self.call("GET", f"/api/trace/{request_id}")


def smoke(stack: Stack) -> int:
    c = ConsoleClient(stack).sign_in()
    b = c.brief("harbourline")
    print(f"UC1 brief: {len(b['talking_points'])} talking points, {len(b['watch'])} watch items · trace {b['trace_id'][:8]}")
    a = c.ask("Can a company trading for 2 years get an unsecured working capital line?")
    print(f"UC2 answer: {a['status']} · grounded {a.get('grounded')} · sources {a.get('source_ids')}")
    d = c.draft("harbourline", "Met Mr Haddad. I promised to send the limit increase checklist by Friday. His ID is 784-1985-1234567-1.")
    w = c.approve(d)
    print(f"UC3 commit: {w['status']} · items {[i['status'] for i in w['items']]}")
    t = c.trace(b["request_id"])
    print(f"UC1 trace: {len(t['spans'])} spans across {', '.join(t['components'])}")
    time.sleep(0.5)
    stack.components["audit-writer"].flush()
    v = stack.components["audit-writer"].chain_path
    from services.audit_writer import verify_chain
    print("Audit chain:", verify_chain(v))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Run the SME RM Copilot local stack (Azure mocks + services).")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port-base", type=int, default=8700, help="first port; 0 = ephemeral ports")
    ap.add_argument("--latency", choices=("none", "realistic"), default=None, help="mock latency profile (default: RMC_LATENCY or none)")
    ap.add_argument("--smoke", action="store_true", help="run UC1/UC2/UC3 once, print results, stop")
    args = ap.parse_args(argv)
    settings = Settings.from_env(**({"latency": args.latency} if args.latency else {}))
    stack = Stack(settings, host=args.host, port_base=args.port_base)
    t0 = time.time()
    stack.start()
    print(f"SME RM Copilot local stack up in {time.time() - t0:.1f}s · latency profile: {settings.latency} · run dir: {settings.run_dir}")
    for name, kind, url in stack.describe():
        print(f"  {name:<15} {kind:<8} {url}")
    print(f"Index {stack.ingestion['index']}: {stack.ingestion['indexed']} clauses indexed")
    print(f"\nConsole: {stack.url('web-bff')}/   (sign in as any fictional RM; mock password is filled in)")
    if args.smoke:
        try:
            return smoke(stack)
        finally:
            stack.stop()
    stop = threading.Event()
    try:
        while not stop.wait(1.0):
            pass
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        stack.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
