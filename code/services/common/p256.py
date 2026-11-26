"""ECDSA over NIST P-256 with SHA-256 (JWS "ES256"), deterministic nonces per RFC 6979. Pure Python.

Production equivalent: the signing key lives in an Azure Key Vault Premium HSM and never leaves it; verifiers use a
vetted library (e.g. `cryptography`). This implementation exists so the offline stack can run real ES256 with the
standard library only. It is correct (checked against the RFC 6979 A.2.5 test vector in the tests) but NOT
constant-time: never use it for real keys.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
     0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)

Jacobian = tuple[int, int, int]  # (X, Y, Z); Z == 0 is the point at infinity


def _double(p: Jacobian) -> Jacobian:
    x, y, z = p
    if z == 0 or y == 0:
        return (0, 1, 0)
    delta, gamma = z * z % P, y * y % P
    beta = x * gamma % P
    alpha = 3 * (x - delta) * (x + delta) % P
    x3 = (alpha * alpha - 8 * beta) % P
    z3 = ((y + z) ** 2 - gamma - delta) % P
    y3 = (alpha * (4 * beta - x3) - 8 * gamma * gamma) % P
    return (x3, y3, z3)


def _add(p: Jacobian, q: Jacobian) -> Jacobian:
    if p[2] == 0:
        return q
    if q[2] == 0:
        return p
    x1, y1, z1 = p
    x2, y2, z2 = q
    z1z1, z2z2 = z1 * z1 % P, z2 * z2 % P
    u1, u2 = x1 * z2z2 % P, x2 * z1z1 % P
    s1, s2 = y1 * z2 * z2z2 % P, y2 * z1 * z1z1 % P
    if u1 == u2:
        return _double(p) if s1 == s2 else (0, 1, 0)
    h = (u2 - u1) % P
    i = 4 * h * h % P
    j = h * i % P
    r = 2 * (s2 - s1) % P
    v = u1 * i % P
    x3 = (r * r - j - 2 * v) % P
    y3 = (r * (v - x3) - 2 * s1 * j) % P
    z3 = ((z1 + z2) ** 2 - z1z1 - z2z2) * h % P
    return (x3, y3, z3)


def _mul(k: int, point: tuple[int, int]) -> Jacobian:
    acc: Jacobian = (0, 1, 0)
    base: Jacobian = (point[0], point[1], 1)
    for bit in bin(k)[2:]:
        acc = _double(acc)
        if bit == "1":
            acc = _add(acc, base)
    return acc


def _affine(p: Jacobian) -> tuple[int, int] | None:
    if p[2] == 0:
        return None
    zi = pow(p[2], -1, P)
    return (p[0] * zi * zi % P, p[1] * zi * zi * zi % P)


def on_curve(x: int, y: int) -> bool:
    return 0 <= x < P and 0 <= y < P and (y * y - (x * x * x + A * x + B)) % P == 0


def _rfc6979_k(d: int, digest: bytes) -> int:
    x = d.to_bytes(32, "big")
    h = (int.from_bytes(digest, "big") % N).to_bytes(32, "big")
    v, k = b"\x01" * 32, b"\x00" * 32
    k = hmac.new(k, v + b"\x00" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    k = hmac.new(k, v + b"\x01" + x + h, hashlib.sha256).digest()
    v = hmac.new(k, v, hashlib.sha256).digest()
    while True:
        v = hmac.new(k, v, hashlib.sha256).digest()
        candidate = int.from_bytes(v, "big")
        if 1 <= candidate < N:
            return candidate
        k = hmac.new(k, v + b"\x00", hashlib.sha256).digest()
        v = hmac.new(k, v, hashlib.sha256).digest()


@dataclass(frozen=True)
class PublicKey:
    x: int
    y: int

    def __post_init__(self) -> None:
        if not on_curve(self.x, self.y):
            raise ValueError("point is not on P-256")

    def verify(self, digest: bytes, signature: bytes) -> bool:
        """Verify a raw r||s (64-byte, JWS format) signature over a SHA-256 digest."""
        if len(signature) != 64 or len(digest) != 32:
            return False
        r, s = int.from_bytes(signature[:32], "big"), int.from_bytes(signature[32:], "big")
        if not (1 <= r < N and 1 <= s < N):
            return False
        w = pow(s, -1, N)
        e = int.from_bytes(digest, "big")
        point = _affine(_add(_mul(e * w % N, G), _mul(r * w % N, (self.x, self.y))))
        return point is not None and point[0] % N == r


@dataclass(frozen=True)
class PrivateKey:
    d: int

    @classmethod
    def generate(cls) -> "PrivateKey":
        return cls(secrets.randbelow(N - 1) + 1)

    def public_key(self) -> PublicKey:
        x, y = _affine(_mul(self.d, G))  # type: ignore[misc]
        return PublicKey(x, y)

    def sign(self, digest: bytes) -> bytes:
        """Sign a SHA-256 digest; returns raw r||s (64 bytes), the JWS ES256 / Key Vault format."""
        e = int.from_bytes(digest, "big")
        k = _rfc6979_k(self.d, digest)
        r = _affine(_mul(k, G))[0] % N  # type: ignore[index]
        s = pow(k, -1, N) * (e + r * self.d) % N
        if r == 0 or s == 0:  # astronomically unlikely; RFC 6979 would continue the nonce loop
            raise ValueError("degenerate signature")
        return r.to_bytes(32, "big") + s.to_bytes(32, "big")
