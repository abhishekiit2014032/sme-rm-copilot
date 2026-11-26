"""Shared plumbing for the Azure mocks: token checks, server-side RBAC, latency profiles and fault injection.

Fault injection (per mock, per target such as `crm` or `aoai-primary`):
* environment, read at start-up: `AZMOCK_FAULTS="crm:outage=1;aoai-primary:status=429,error_rate=1,retry_after_ms=2000"`
* admin endpoint on the mock: `POST /admin/faults {"target": "crm", "latency_ms": 300, "error_rate": 0.2,
  "outage": false, "status": 503, "slow_ms": 0}`; `DELETE /admin/faults` resets.
`latency_ms` delays before the request is processed; `slow_ms` delays the response after processing (a write
that succeeded but whose response arrives too late, the case that causes double posts).
"""
from __future__ import annotations

import os
import random
import threading
import time

from services.common.auth import Jwks, TokenValidator
from services.common.config import StackConfig
from services.common.httpkit import App, HttpClient, HttpError, Request

# Typical p50 service latencies in ms for the "realistic" profile (illustrative, in-region, private endpoints).
REALISTIC_MS = {
    "entra": 40, "keyvault": 25, "appconfig": 15, "cosmos": 8, "eventhubs": 12, "content-safety": 120, "language": 140,
    "search": 160, "apim": 4, "crm": 180, "ods": 320, "los": 220, "kyc": 150, "news": 420, "graph": 180,
}
# Azure OpenAI: time to first token + per output token (ms), per deployment.
REALISTIC_AOAI = {"gen-large": (900, 16.0), "gen-small": (450, 7.0), "embed": (90, 0.0)}


def realistic(cfg: StackConfig) -> bool:
    return cfg.settings.latency == "realistic"


def sleep_ms(ms: float, jitter: float = 0.2) -> None:
    if ms > 0:
        time.sleep(ms * (1 + random.uniform(-jitter, jitter)) / 1000)


class Faults:
    FIELDS = {"latency_ms": 0.0, "error_rate": 0.0, "outage": False, "status": 503, "retry_after_ms": 0, "slow_ms": 0.0}

    def __init__(self, targets: tuple[str, ...]):
        self.targets = targets
        self._state: dict[str, dict] = {}
        self._lock = threading.Lock()
        self.reset()
        self._load_env(os.environ.get("AZMOCK_FAULTS", ""))

    def _load_env(self, spec: str) -> None:
        for part in filter(None, (p.strip() for p in spec.split(";"))):
            target, _, kvs = part.partition(":")
            if target in self.targets:
                self.configure(target, **{k: v for k, _, v in (kv.partition("=") for kv in kvs.split(","))})

    def reset(self) -> None:
        with self._lock:
            self._state = {t: dict(self.FIELDS) for t in self.targets}

    def configure(self, target: str, **kw) -> dict:
        if target not in self.targets:
            raise HttpError(400, "UnknownTarget", f"targets: {', '.join(self.targets)}")
        with self._lock:
            s = self._state[target]
            for k, v in kw.items():
                if k in self.FIELDS:
                    default = self.FIELDS[k]
                    s[k] = (str(v).lower() in ("1", "true", "yes")) if isinstance(default, bool) else type(default)(v)
            return dict(s)

    def get(self, target: str) -> dict:
        with self._lock:
            return dict(self._state[target])

    def before(self, target: str) -> None:
        s = self.get(target)
        sleep_ms(s["latency_ms"], 0.0)
        if s["outage"] or (s["error_rate"] > 0 and random.random() < s["error_rate"]):
            headers = {}
            if s["retry_after_ms"]:
                headers = {"retry-after-ms": str(s["retry_after_ms"]), "retry-after": str(max(1, round(s["retry_after_ms"] / 1000)))}
            raise HttpError(int(s["status"]), "InjectedFault", f"injected fault on {target}", headers=headers)

    def after(self, target: str) -> None:
        sleep_ms(self.get(target)["slow_ms"], 0.0)


class Mock:
    """Base for every mock: an `App`, a traced client, token validation and the admin fault endpoints."""

    name = "mock"
    targets: tuple[str, ...] = ()
    cors = False

    def __init__(self, cfg: StackConfig, name: str | None = None):
        self.cfg = cfg
        self.name = name or self.name
        self.app = App(self.name, cors=self.cors)
        self.client = HttpClient(self.app.telemetry)
        self.faults = Faults(self.targets or (self.name,))
        self._validators: dict[str, TokenValidator] = {}
        self._jwks: Jwks | None = None
        self.app.route("POST", "/admin/faults")(self._set_fault)
        self.app.route("GET", "/admin/faults")(lambda r: {t: self.faults.get(t) for t in self.faults.targets})
        self.app.route("DELETE", "/admin/faults")(lambda r: (self.faults.reset(), {"status": "reset"})[1])

    def _set_fault(self, r: Request) -> dict:
        body = r.json()
        return self.faults.configure(body.pop("target", self.faults.targets[0]), **body)

    def simulate(self, key: str) -> None:
        """Realistic base latency (if enabled) followed by any injected faults for `key`."""
        if realistic(self.cfg):
            sleep_ms(REALISTIC_MS.get(key, 0))
        if key in self.faults.targets:
            self.faults.before(key)

    def caller(self, r: Request, audience: str, token: str | None = None, **kw) -> dict:
        """Validate the bearer token (or `token`) for this resource. Returns its claims (azp = calling workload)."""
        if self._jwks is None:
            self._jwks = Jwks(self.client, self.cfg.jwks_url)
        v = self._validators.get(audience)
        if v is None:
            v = self._validators[audience] = TokenValidator(self._jwks, self.cfg.issuer, audience, self.cfg.settings.tenant_id)
        return v.validate(token if token is not None else r.bearer(), **kw)

    @staticmethod
    def require_api_version(r: Request) -> None:
        if not r.query.get("api-version"):
            raise HttpError(400, "MissingApiVersionParameter", "The api-version query parameter (?api-version=) is required for all requests.")

    @staticmethod
    def rbac(claims: dict, allowed: dict[str, set[str]], action: str) -> None:
        """Server-side Azure RBAC: the principal (azp) must hold a role that grants `action`."""
        if action not in allowed.get(claims.get("azp", ""), set()):
            raise HttpError(403, "Forbidden", f"Caller is not authorized to perform action '{action}'.", details={"innererror": {"code": "ForbiddenByRbac"}})
