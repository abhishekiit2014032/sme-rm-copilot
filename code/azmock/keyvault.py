"""Azure Key Vault mock (keys and secrets, data plane REST, api-version 7.4).

Imitates:
  GET    /keys/{name}[/{version}]               public part of the key as a JWK (EC P-256)
  POST   /keys/{name}/{version}/sign            {"alg": "ES256", "value": b64url(SHA-256 digest)} -> {"kid", "value": b64url(r||s)}
  POST   /keys/{name}/{version}/verify          {"alg", "digest", "value"} -> {"value": bool}
  POST   /keys/{name}/rotate                    new version; old versions stay valid for verification (overlap)
  PUT    /secrets/{name}   GET /secrets/{name}   DELETE /secrets/{name}

The approval-signing key is an EC P-256 key generated in memory at start-up; it is never exported or written to
disk (Key Vault Premium keeps it in an HSM). Server-side RBAC by caller identity mirrors plan §3.3:
`web-bff` may sign, `tool-gateway` may only read the public key, the orchestrator has no access at all;
`audit-writer` manages per-customer content keys (secrets), crypto-shreds them on erasure, and unwraps content
keys wrapped with the RSA-OAEP-256 key `audit-transport` (`POST /keys/{name}/{version}/unwrapkey`).

Differences: no soft-delete/purge protection (DELETE purges immediately), no key attestation, no HSM, no
private endpoint, simplified error bodies.
"""
from __future__ import annotations

import secrets
import threading
import time

from services.common import jose, p256, rsa
from services.common.config import AUD, StackConfig
from services.common.httpkit import HttpError, Request

from ._base import Mock

APPROVAL_KEY = "approval-signing"
TRANSPORT_KEY = "audit-transport"  # RSA-OAEP-256: producers wrap content keys with its public half
RBAC = {
    "web-bff": {"keys/sign", "keys/get"},              # Key Vault Crypto User (sign) scoped to the approval key
    "tool-gateway": {"keys/get"},                      # public key only (Key Vault Crypto Service Encryption User-like read)
    "audit-writer": {"secrets/get", "secrets/set", "secrets/delete", "keys/unwrap"},  # content keys; unwraps transport DEKs
}


class KeyVaultMock(Mock):
    name = "keyvault"

    def __init__(self, cfg: StackConfig):
        super().__init__(cfg)
        self._keys: dict[str, dict[str, p256.PrivateKey | rsa.PrivateKey]] = {}
        self._current: dict[str, str] = {}
        self._secrets: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._create(APPROVAL_KEY)
        self._create(TRANSPORT_KEY, kty="RSA")
        a = self.app.route
        a("GET", "/keys/{name}")(lambda r: self._get_key(r, None))
        a("GET", "/keys/{name}/{version}")(lambda r: self._get_key(r, r.params["version"]))
        a("POST", "/keys/{name}/{version}/sign")(self._sign)
        a("POST", "/keys/{name}/{version}/verify")(self._verify)
        a("POST", "/keys/{name}/rotate")(self._rotate)
        a("POST", "/keys/{name}/{version}/unwrapkey")(self._unwrap)
        a("PUT", "/secrets/{name}")(self._set_secret)
        a("GET", "/secrets/{name}")(self._get_secret)
        a("DELETE", "/secrets/{name}")(self._delete_secret)

    def _create(self, name: str, kty: str = "EC") -> str:
        version = secrets.token_hex(16)
        key = p256.PrivateKey.generate() if kty == "EC" else rsa.PrivateKey.generate(2048)
        with self._lock:
            self._keys.setdefault(name, {})[version] = key
            self._current[name] = version
        return version

    def _kid(self, name: str, version: str) -> str:
        return f"{self.cfg.url('keyvault')}/keys/{name}/{version}"

    def _auth(self, r: Request, action: str) -> None:
        self.require_api_version(r)
        self.simulate("keyvault")
        self.rbac(self.caller(r, AUD["vault"]), RBAC, action)

    def _lookup(self, name: str, version: str | None) -> tuple[str, p256.PrivateKey]:
        with self._lock:
            versions = self._keys.get(name)
            if not versions:
                raise HttpError(404, "KeyNotFound", f"A key with (name/id) {name} was not found in this key vault.")
            version = version or self._current[name]
            if version not in versions:
                raise HttpError(404, "KeyNotFound", "key version not found")
            return version, versions[version]

    def _get_key(self, r: Request, version: str | None) -> dict:
        self._auth(r, "keys/get")
        version, key = self._lookup(r.params["name"], version)
        jwk = self.public_jwk(r.params["name"], version)
        jwk["key_ops"] = ["sign", "verify"] if isinstance(key, p256.PrivateKey) else ["wrapKey", "unwrapKey"]
        return {"key": jwk, "attributes": {"enabled": True, "exportable": False, "recoveryLevel": "Recoverable+Purgeable"}}

    def public_jwk(self, name: str, version: str | None = None) -> dict:
        version, key = self._lookup(name, version)
        kid = self._kid(name, version)
        if isinstance(key, rsa.PrivateKey):
            return {**jose.rsa_jwk(key.public_key(), kid), "use": "enc", "alg": "RSA-OAEP-256"}
        return jose.ec_jwk(key.public_key(), kid)

    def _unwrap(self, r: Request) -> dict:
        self._auth(r, "keys/unwrap")
        body = r.json()
        version, key = self._lookup(r.params["name"], r.params["version"])
        if not isinstance(key, rsa.PrivateKey) or body.get("alg") != "RSA-OAEP-256":
            raise HttpError(400, "BadParameter", "unwrapKey needs an RSA key and alg RSA-OAEP-256")
        try:
            dek = key.decrypt_oaep(jose.b64u_decode(body.get("value", "")))
        except ValueError:
            raise HttpError(400, "BadParameter", "unwrap failed") from None
        return {"kid": self._kid(r.params["name"], version), "value": jose.b64u(dek)}

    def _sign(self, r: Request) -> dict:
        self._auth(r, "keys/sign")
        body = r.json()
        if body.get("alg") != "ES256":
            raise HttpError(400, "BadParameter", "alg must be ES256 for an EC P-256 key")
        digest = jose.b64u_decode(body.get("value", ""))
        if len(digest) != 32:
            raise HttpError(400, "BadParameter", "value must be a SHA-256 digest")
        version, key = self._lookup(r.params["name"], r.params["version"])
        if not isinstance(key, p256.PrivateKey):
            raise HttpError(400, "BadParameter", "this key cannot sign")
        return {"kid": self._kid(r.params["name"], version), "value": jose.b64u(key.sign(digest))}

    def _verify(self, r: Request) -> dict:
        self._auth(r, "keys/get")
        body = r.json()
        version, key = self._lookup(r.params["name"], r.params["version"])
        ok = isinstance(key, p256.PrivateKey) and body.get("alg") == "ES256" and key.public_key().verify(jose.b64u_decode(body.get("digest", "")), jose.b64u_decode(body.get("value", "")))
        return {"value": ok}

    def _rotate(self, r: Request) -> dict:
        self._auth(r, "keys/sign")
        return {"key": {"kid": self._kid(r.params["name"], self._create(r.params["name"]))}}

    # ------------------------------------------------------------------ secrets (per-customer content keys)
    def _secret_doc(self, name: str, s: dict, with_value: bool = True) -> dict:
        doc = {"id": f"{self.cfg.url('keyvault')}/secrets/{name}/{s['version']}", "attributes": {"enabled": True, "created": s["created"]}}
        if with_value:
            doc["value"] = s["value"]
        return doc

    def _set_secret(self, r: Request) -> dict:
        self._auth(r, "secrets/set")
        value = r.json().get("value")
        if not isinstance(value, str):
            raise HttpError(400, "BadParameter", "value is required")
        s = {"value": value, "version": secrets.token_hex(16), "created": int(time.time())}
        with self._lock:
            self._secrets[r.params["name"]] = s
        return self._secret_doc(r.params["name"], s)

    def _get_secret(self, r: Request) -> dict:
        self._auth(r, "secrets/get")
        with self._lock:
            s = self._secrets.get(r.params["name"])
        if not s:
            raise HttpError(404, "SecretNotFound", f"A secret with (name/id) {r.params['name']} was not found in this key vault.")
        return self._secret_doc(r.params["name"], s)

    def _delete_secret(self, r: Request) -> dict:
        self._auth(r, "secrets/delete")
        with self._lock:
            s = self._secrets.pop(r.params["name"], None)
        if not s:
            raise HttpError(404, "SecretNotFound", "secret not found")
        return {**self._secret_doc(r.params["name"], s, with_value=False), "deletedDate": int(time.time()), "purged": True}
