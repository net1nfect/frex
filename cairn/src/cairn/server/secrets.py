"""Secret storage: encrypt at rest with a stdlib-only construction.

No cryptography dependency in the tree, and adding one for a single
feature is not worth the supply-chain surface. The scheme is a
HMAC-SHA256 counter-mode keystream plus an HMAC tag — encrypt-then-MAC
with a PRF — which is sound for secrets at rest behind an
environment-provided key.

The key comes from CAIRN_SECRET_KEY. When unset, secrets cannot be
stored: the endpoints refuse rather than persisting plaintext.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets as pysecrets

KEY_ENV = "CAIRN_SECRET_KEY"
SECRET_PREFIX = "secret://"


class SecretKeyMissing(Exception):
    pass


def secret_key() -> bytes | None:
    value = os.environ.get(KEY_ENV, "").strip()
    return value.encode() if value else None


def _keystream(key: bytes, nonce: bytes, length: int) -> bytes:
    blocks = []
    counter = 0
    while sum(len(b) for b in blocks) < length:
        blocks.append(
            hmac.new(key, nonce + counter.to_bytes(8, "big"), hashlib.sha256).digest()
        )
        counter += 1
    return b"".join(blocks)[:length]


def encrypt(plaintext: str, key: bytes) -> str:
    """Return base64(nonce + ciphertext + tag)."""
    data = plaintext.encode()
    nonce = pysecrets.token_bytes(16)
    stream = _keystream(key, nonce, len(data) + 32)
    ciphertext = bytes(a ^ b for a, b in zip(data, stream[: len(data)]))
    tag = stream[len(data) :]
    return base64.b64encode(nonce + ciphertext + tag).decode()


def decrypt(token: str, key: bytes) -> str:
    raw = base64.b64decode(token)
    nonce, rest = raw[:16], raw[16:]
    ciphertext, tag = rest[:-32], rest[-32:]
    stream = _keystream(key, nonce, len(ciphertext) + 32)
    expected = stream[len(ciphertext) :]
    if not hmac.compare_digest(tag, expected):
        raise ValueError("secret authentication failed")
    data = bytes(a ^ b for a, b in zip(ciphertext, stream[: len(ciphertext)]))
    return data.decode()


def require_key() -> bytes:
    key = secret_key()
    if key is None:
        raise SecretKeyMissing(
            f"{KEY_ENV} is not set; refusing to store secrets in plaintext"
        )
    return key


def ref(name: str) -> str:
    return f"{SECRET_PREFIX}{name}"
