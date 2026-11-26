"""Azure App Configuration mock: feature flags and kill switches (data plane REST, api-version 2023-11-01).

Imitates:
  GET /kv?key={filter}&label={label}   list key-values (`*` suffix wildcard)
  GET /kv/{key}                         one key-value (`.appconfig.featureflag/<id>` for feature flags)
  PUT /kv/{key}                         set a key-value (operators, or a Sentinel playbook flipping a kill switch)

Feature flags use the real key prefix and content type
(`application/vnd.microsoft.appconfig.ff+json;charset=utf-8`), value `{"id", "description", "enabled", "conditions"}`.
Flags at start-up: `uc1`, `uc2`, `uc3` (per use case) and `writes` (all CRM writes), all enabled.
RBAC: orchestrator, tool-gateway, outbox-worker = App Configuration Data Reader; `ops-admin` = Data Owner.

Differences: no labels beyond the null label, no revisions, snapshots, ETag preconditions or HMAC auth.
"""
from __future__ import annotations

import json
import secrets
import threading
import time
import urllib.parse

from services.common.config import AUD, StackConfig
from services.common.httpkit import HttpError, Request

from ._base import Mock

FF_PREFIX = ".appconfig.featureflag/"
FF_CONTENT_TYPE = "application/vnd.microsoft.appconfig.ff+json;charset=utf-8"
FLAGS = {"uc1": "UC1 pre-meeting brief", "uc2": "UC2 policy copilot", "uc3": "UC3 follow-up drafting", "writes": "All CRM writes (kill switch)"}
# Data Reader for every service that enforces a flag (tool-gateway and outbox-worker enforce the `writes` kill switch).
RBAC = {"orchestrator": {"kv/read"}, "tool-gateway": {"kv/read"}, "outbox-worker": {"kv/read"}, "ops-admin": {"kv/read", "kv/write"}}


class AppConfigMock(Mock):
    name = "appconfig"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self._kv: dict[str, dict] = {}
        self._lock = threading.Lock()
        for fid, desc in FLAGS.items():
            self._put(FF_PREFIX + fid, json.dumps({"id": fid, "description": desc, "enabled": True, "conditions": {"client_filters": []}}), FF_CONTENT_TYPE)
        self.app.route("GET", "/kv")(self._list)
        self.app.route("GET", "/kv/{key:path}")(self._get)
        self.app.route("PUT", "/kv/{key:path}")(self._set)

    def _put(self, key: str, value: str, content_type: str | None) -> dict:
        item = {"key": key, "label": None, "content_type": content_type, "value": value, "etag": secrets.token_hex(8),
                "last_modified": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "locked": False, "tags": {}}
        with self._lock:
            self._kv[key] = item
        return item

    def put_value(self, key: str, value: str, content_type: str | None = None) -> None:
        """In-process helper: what a deployment pipeline writes (e.g. the audit transport public key)."""
        self._put(key, value, content_type)

    def set_flag(self, flag_id: str, enabled: bool) -> None:
        """In-process helper for tests and the stack CLI."""
        self._put(FF_PREFIX + flag_id, json.dumps({"id": flag_id, "description": FLAGS.get(flag_id, ""), "enabled": enabled,
                                                   "conditions": {"client_filters": []}}), FF_CONTENT_TYPE)

    def _auth(self, r: Request, action: str) -> None:
        self.require_api_version(r)
        self.simulate("appconfig")
        self.rbac(self.caller(r, AUD["appconfig"]), RBAC, action)

    def _list(self, r: Request) -> dict:
        self._auth(r, "kv/read")
        pattern = r.query.get("key", "*")
        with self._lock:
            items = [v for k, v in sorted(self._kv.items()) if (k.startswith(pattern[:-1]) if pattern.endswith("*") else k == pattern)]
        return {"items": items}

    def _get(self, r: Request) -> dict:
        self._auth(r, "kv/read")
        with self._lock:
            item = self._kv.get(urllib.parse.unquote(r.params["key"]))
        if not item:
            raise HttpError(404, "KeyValueNotFound", "key-value not found")
        return item

    def _set(self, r: Request) -> dict:
        self._auth(r, "kv/write")
        body = r.json()
        return self._put(urllib.parse.unquote(r.params["key"]), str(body.get("value", "")), body.get("content_type"))
