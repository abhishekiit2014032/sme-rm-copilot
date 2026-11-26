"""Feature flags and kill switches from App Configuration, shared by orchestrator, tool-gateway and outbox-worker.

Cached `flags_ttl_s`. On an App Configuration outage the last known value is used, except for `writes`: a
`writes = on` value older than `writes_flag_max_stale_s` (LLD §4.4 step 3: "fresh within 15 min") is treated as off,
so a kill switch can never be bypassed by the flag store being unreachable. Never reached at all -> defaults
(`writes` off).
"""
from __future__ import annotations

import json
import threading
import time
import urllib.parse
from typing import TYPE_CHECKING

from .httpkit import TransportError

if TYPE_CHECKING:
    from .service import Service

FLAG_DEFAULTS = {"uc1": True, "uc2": True, "uc3": True, "writes": False}  # used only if App Configuration was never reachable
FAIL_CLOSED = ("writes",)


class FeatureFlags:
    def __init__(self, svc: "Service"):
        self.svc = svc
        self._cache: dict[str, tuple[float, float, bool]] = {}  # flag -> (fresh until, fetched at, value)
        self._lock = threading.Lock()

    def enabled(self, flag: str) -> bool:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(flag)
        if hit and hit[0] > now:
            return hit[2]
        key = urllib.parse.quote(f".appconfig.featureflag/{flag}", safe="")
        try:
            r = self.svc.client.request("GET", f"{self.svc.cfg.url('appconfig')}/kv/{key}?api-version=2023-11-01",
                                        headers=self.svc.auth_headers("appconfig"), timeout=1.0, peer="appconfig")
            value = bool(json.loads(r.json()["value"]).get("enabled")) if r.ok else None
        except (TransportError, KeyError, ValueError, TypeError):
            value = None
        if value is None:
            if not hit:
                return FLAG_DEFAULTS.get(flag, False)
            if flag in FAIL_CLOSED and now - hit[1] > self.svc.cfg.settings.writes_flag_max_stale_s:
                return False
            return hit[2]
        with self._lock:
            self._cache[flag] = (now + self.svc.cfg.settings.flags_ttl_s, now, value)
        return value


class ConfigValues:
    """Plain JSON key-values from App Configuration (e.g. `rmc:tool-gateway:approval:allowed_kids`), cached
    `flags_ttl_s`; on an outage the last known value is used; never read -> None (the caller fails closed)."""

    def __init__(self, svc: "Service"):
        self.svc = svc
        self._cache: dict[str, tuple[float, object]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> object | None:
        now = time.monotonic()
        with self._lock:
            hit = self._cache.get(key)
        if hit and hit[0] > now:
            return hit[1]
        try:
            r = self.svc.client.request("GET", f"{self.svc.cfg.url('appconfig')}/kv/{urllib.parse.quote(key, safe='')}?api-version=2023-11-01",
                                        headers=self.svc.auth_headers("appconfig"), timeout=1.0, peer="appconfig")
            ok = r.ok
            value = json.loads(r.json()["value"]) if ok else None
        except (TransportError, KeyError, ValueError, TypeError):
            ok, value = False, None
        if not ok:
            return hit[1] if hit else None
        with self._lock:
            self._cache[key] = (now + self.svc.cfg.settings.flags_ttl_s, value)
        return value
