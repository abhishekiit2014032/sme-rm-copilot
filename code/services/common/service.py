"""Base class for the deployable services: an App, a traced client, a workload identity and token validators."""
from __future__ import annotations

import threading

from .auth import Jwks, TokenProvider, TokenValidator
from .config import AUD, StackConfig
from .httpkit import App, HttpClient


class Service:
    name = "service"

    def __init__(self, cfg: StackConfig, response_headers: dict | None = None, edge: bool = False):
        self.cfg = cfg
        self.app = App(self.name, response_headers=response_headers, trust_inbound_context=not edge)
        self.telemetry = self.app.telemetry
        self.metrics = self.telemetry.metrics
        self.client = HttpClient(self.telemetry)
        self.tokens = TokenProvider(self.client, cfg.token_url, self.name, cfg.secrets[self.name])
        self.audit = None  # set by services that produce audit events (see contracts.py)
        self._jwks = Jwks(self.client, cfg.jwks_url)
        self._validators: dict[str, TokenValidator] = {}
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def validator(self, aud_key: str) -> TokenValidator:
        v = self._validators.get(aud_key)
        if v is None:
            v = self._validators[aud_key] = TokenValidator(self._jwks, self.cfg.issuer, AUD[aud_key], self.cfg.settings.tenant_id)
        return v

    def auth_headers(self, aud_key: str) -> dict:
        return {"authorization": "Bearer " + self.tokens.app_token(f"{AUD[aud_key]}/.default")}

    # background loops (workers)
    def run_loop(self, name: str, interval_s: float, fn) -> None:
        def loop() -> None:
            while not self._stop.is_set():
                try:
                    fn()
                except Exception as exc:  # noqa: BLE001 - keep the worker alive; record the type only
                    self.telemetry.log("worker_error", level="error", **{"error.type": type(exc).__name__})
                self._stop.wait(interval_s)
        t = threading.Thread(target=loop, name=f"{self.name}-{name}", daemon=True)
        self._threads.append(t)
        t.start()

    def stop(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=3)
