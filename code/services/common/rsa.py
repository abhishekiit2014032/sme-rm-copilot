"""RSA (PKCS#1 v1.5 with SHA-256, JWS "RS256"). Pure Python, used by the Entra ID mock to sign tokens.

Production equivalent: Microsoft Entra ID signs tokens with keys published at its JWKS endpoint; services verify
them with a vetted library (MSAL / `cryptography`). The key is generated at stack start-up and never written to
disk. Not constant-time; never use for real keys.
"""
from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass

_SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
_SMALL_PRIMES = [p for p in range(3, 2000) if all(p % q for q in range(2, int(p ** 0.5) + 1))]
E = 65537


def _probable_prime(n: int, rounds: int = 24) -> bool:
    if any(n % p == 0 for p in _SMALL_PRIMES):
        return n in _SMALL_PRIMES
    d, s = n - 1, 0
    while d % 2 == 0:
        d, s = d // 2, s + 1
    for _ in range(rounds):
        x = pow(secrets.randbelow(n - 3) + 2, d, n)
        if x in (1, n - 1):
            continue
        for _ in range(s - 1):
            x = pow(x, 2, n)
            if x == n - 1:
                break
        else:
            return False
    return True


def _prime(bits: int) -> int:
    while True:
        c = secrets.randbits(bits) | (3 << (bits - 2)) | 1
        if (c - 1) % E and _probable_prime(c):
            return c


def _mgf1(seed: bytes, length: int) -> bytes:
    out, counter = b"", 0
    while len(out) < length:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:length]


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b))


def _emsa(digest: bytes, k: int) -> bytes:
    t = _SHA256_DIGEST_INFO + digest
    return b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t


@dataclass(frozen=True)
class PublicKey:
    n: int
    e: int = E

    def encrypt_oaep(self, message: bytes) -> bytes:
        """RSAES-OAEP with SHA-256 and MGF1-SHA-256, empty label (Key Vault `RSA-OAEP-256`)."""
        k = (self.n.bit_length() + 7) // 8
        if len(message) > k - 66:
            raise ValueError("message too long")
        db = hashlib.sha256(b"").digest() + b"\x00" * (k - len(message) - 66) + b"\x01" + message
        seed = secrets.token_bytes(32)
        masked_db = _xor(db, _mgf1(seed, k - 33))
        masked_seed = _xor(seed, _mgf1(masked_db, 32))
        em = b"\x00" + masked_seed + masked_db
        return pow(int.from_bytes(em, "big"), self.e, self.n).to_bytes(k, "big")

    def verify(self, message: bytes, signature: bytes) -> bool:
        k = (self.n.bit_length() + 7) // 8
        if len(signature) != k:
            return False
        m = pow(int.from_bytes(signature, "big"), self.e, self.n)
        return secrets.compare_digest(m.to_bytes(k, "big"), _emsa(hashlib.sha256(message).digest(), k))


@dataclass(frozen=True)
class PrivateKey:
    p: int
    q: int

    @classmethod
    def generate(cls, bits: int = 2048) -> "PrivateKey":
        while True:
            p, q = _prime(bits // 2), _prime(bits // 2)
            if p != q and (p * q).bit_length() == bits:
                return cls(p, q)

    def public_key(self) -> PublicKey:
        return PublicKey(self.p * self.q)

    def decrypt_oaep(self, ciphertext: bytes) -> bytes:
        n = self.p * self.q
        k = (n.bit_length() + 7) // 8
        d = pow(E, -1, (self.p - 1) * (self.q - 1))
        em = pow(int.from_bytes(ciphertext, "big"), d, n).to_bytes(k, "big")
        masked_seed, masked_db = em[1:33], em[33:]
        seed = _xor(masked_seed, _mgf1(masked_db, 32))
        db = _xor(masked_db, _mgf1(seed, k - 33))
        rest = db[32:].lstrip(b"\x00")
        if em[0] != 0 or db[:32] != hashlib.sha256(b"").digest() or not rest.startswith(b"\x01"):
            raise ValueError("decryption error")
        return rest[1:]

    def sign(self, message: bytes) -> bytes:
        n = self.p * self.q
        k = (n.bit_length() + 7) // 8
        m = int.from_bytes(_emsa(hashlib.sha256(message).digest(), k), "big")
        # CRT for speed
        dp, dq = pow(E, -1, self.p - 1), pow(E, -1, self.q - 1)
        m1, m2 = pow(m, dp, self.p), pow(m, dq, self.q)
        h = pow(self.q, -1, self.p) * (m1 - m2) % self.p
        return (m2 + h * self.q).to_bytes(k, "big")
