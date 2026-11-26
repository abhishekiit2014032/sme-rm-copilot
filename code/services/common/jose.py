"""Minimal JOSE: base64url, JWK conversion, compact JWS encode/decode/verify for RS256 and ES256.

Production equivalent: MSAL / `azure-identity` to obtain tokens and a vetted JOSE library to validate them.
Deliberately strict: only RS256 and ES256 are accepted; `alg: none` and HMAC algorithms are always rejected, so a
token can never be "verified" with a public key used as an HMAC secret.
"""
from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from typing import Callable

from . import p256, rsa

ALLOWED_ALGS = ("RS256", "ES256")


class JoseError(ValueError):
    """Malformed or unverifiable token. `code` is a short machine-readable reason."""

    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def b64u_decode(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _int_b64u(n: int, length: int | None = None) -> str:
    return b64u(n.to_bytes(length or (n.bit_length() + 7) // 8, "big"))


def _b64u_int(text: str) -> int:
    return int.from_bytes(b64u_decode(text), "big")


def canonical_json(value: object) -> bytes:
    """RFC 8785 (JCS) subset used for hashing drafts: sorted keys, no insignificant whitespace, UTF-8.
    Sufficient for our payloads (strings, integers, booleans, lists, objects; no floats)."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# --------------------------------------------------------------------------- JWK
def rsa_jwk(key: rsa.PublicKey, kid: str) -> dict:
    return {"kty": "RSA", "use": "sig", "alg": "RS256", "kid": kid, "n": _int_b64u(key.n), "e": _int_b64u(key.e)}


def ec_jwk(key: p256.PublicKey, kid: str) -> dict:
    return {"kty": "EC", "crv": "P-256", "kid": kid, "x": _int_b64u(key.x, 32), "y": _int_b64u(key.y, 32)}


@dataclass(frozen=True)
class VerifyingKey:
    kid: str
    alg: str
    key: rsa.PublicKey | p256.PublicKey

    @classmethod
    def from_jwk(cls, jwk: dict) -> "VerifyingKey":
        if jwk.get("kty") == "RSA":
            return cls(jwk["kid"], "RS256", rsa.PublicKey(_b64u_int(jwk["n"]), _b64u_int(jwk["e"])))
        if jwk.get("kty") == "EC" and jwk.get("crv") == "P-256":
            return cls(jwk["kid"], "ES256", p256.PublicKey(_b64u_int(jwk["x"]), _b64u_int(jwk["y"])))
        raise JoseError("unsupported_key", f"unsupported JWK {jwk.get('kty')}")

    def verify(self, signing_input: bytes, signature: bytes) -> bool:
        if isinstance(self.key, rsa.PublicKey):
            return self.key.verify(signing_input, signature)
        return self.key.verify(hashlib.sha256(signing_input).digest(), signature)


# --------------------------------------------------------------------------- JWS
def signing_input(header: dict, claims: dict) -> bytes:
    return (b64u(json.dumps(header, separators=(",", ":")).encode()) + "." + b64u(json.dumps(claims, separators=(",", ":")).encode())).encode()


def encode(header: dict, claims: dict, sign: Callable[[bytes], bytes]) -> str:
    """`sign` receives the JWS signing input and returns the raw signature (RS256: PKCS#1 v1.5; ES256: r||s)."""
    si = signing_input(header, claims)
    return si.decode() + "." + b64u(sign(si))


@dataclass(frozen=True)
class Decoded:
    header: dict
    claims: dict
    signing_input: bytes
    signature: bytes


def decode_unverified(token: str) -> Decoded:
    parts = token.split(".") if isinstance(token, str) else []
    if len(parts) != 3:
        raise JoseError("malformed", "not a compact JWS")
    try:
        header, claims = json.loads(b64u_decode(parts[0])), json.loads(b64u_decode(parts[1]))
        signature = b64u_decode(parts[2])
    except (ValueError, UnicodeDecodeError) as exc:
        raise JoseError("malformed", "token is not valid base64url JSON") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise JoseError("malformed")
    return Decoded(header, claims, f"{parts[0]}.{parts[1]}".encode(), signature)


def verify(token: str, keys: dict[str, VerifyingKey]) -> Decoded:
    """Check structure, algorithm allow-list, key id and signature. Claim checks are the caller's job."""
    d = decode_unverified(token)
    alg, kid = d.header.get("alg"), d.header.get("kid")
    if alg not in ALLOWED_ALGS:
        raise JoseError("bad_alg", f"algorithm {alg!r} not allowed")
    key = keys.get(kid or "")
    if key is None:
        raise JoseError("unknown_kid", "unknown key id")
    if key.alg != alg:
        raise JoseError("bad_alg", "algorithm does not match the key")
    if not key.verify(d.signing_input, d.signature):
        raise JoseError("bad_signature", "signature verification failed")
    return d
