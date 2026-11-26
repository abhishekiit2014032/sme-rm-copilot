"""Envelope encryption for audit content (LLD §6.6, plan C21).

Producers encrypt content BEFORE it leaves them: a random 32-byte data key (DEK) encrypts the content, and the DEK
is wrapped with the RSA-OAEP-256 public key of Key Vault key `audit-transport` (distributed through App
Configuration, so producers never call Key Vault). Only `audit-writer` can unwrap (Key Vault `unwrapKey`); it then
re-encrypts with the customer's own key so erasure can crypto-shred one customer.

MOCK cipher: the content cipher below is built from the standard library (HMAC-SHA256 counter-mode keystream,
encrypt-then-MAC). It is a stand-in so the stack runs offline; production uses AES-256-GCM (A256GCM). The DEK wrap
is real RSA-OAEP-256 and matches Key Vault's `unwrapKey` contract.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from . import jose, rsa

MAGIC = b"RMCX1"
ALG = "RSA-OAEP-256+MOCK-HMAC-CTR"


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    out, counter = b"", 0
    while len(out) < n:
        out += hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        counter += 1
    return out[:n]


def seal(key: bytes, plaintext: bytes) -> bytes:
    enc, mac = hmac.new(key, b"enc", hashlib.sha256).digest(), hmac.new(key, b"mac", hashlib.sha256).digest()
    nonce = secrets.token_bytes(16)
    ct = bytes(a ^ b for a, b in zip(plaintext, _keystream(enc, nonce, len(plaintext))))
    return MAGIC + nonce + ct + hmac.new(mac, nonce + ct, hashlib.sha256).digest()


def unseal(key: bytes, blob: bytes) -> bytes:
    if not blob.startswith(MAGIC) or len(blob) < len(MAGIC) + 48:
        raise ValueError("not a sealed blob")
    nonce, ct, tag = blob[5:21], blob[21:-32], blob[-32:]
    enc, mac = hmac.new(key, b"enc", hashlib.sha256).digest(), hmac.new(key, b"mac", hashlib.sha256).digest()
    if not hmac.compare_digest(tag, hmac.new(mac, nonce + ct, hashlib.sha256).digest()):
        raise ValueError("authentication failed")
    return bytes(a ^ b for a, b in zip(ct, _keystream(enc, nonce, len(ct))))


def seal_envelope(public_jwk: dict, plaintext: bytes) -> dict:
    """Encrypt for the audit-writer only. `public_jwk` is the RSA public JWK of `audit-transport`."""
    key = rsa.PublicKey(int.from_bytes(jose.b64u_decode(public_jwk["n"]), "big"), int.from_bytes(jose.b64u_decode(public_jwk["e"]), "big"))
    dek = secrets.token_bytes(32)
    return {"alg": ALG, "kid": public_jwk["kid"], "wrapped_dek": jose.b64u(key.encrypt_oaep(dek)),
            "ciphertext": jose.b64u(seal(dek, plaintext)), "plaintext_sha256": hashlib.sha256(plaintext).hexdigest()}
