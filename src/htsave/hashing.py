"""Exact content addressing."""

from __future__ import annotations

import hashlib

from .errors import CorruptObjectError

HASH_PREFIX = "sha256:"


def sha256_id(content: bytes) -> str:
    return f"{HASH_PREFIX}{hashlib.sha256(content).hexdigest()}"


def verify_sha256(content: bytes, expected: str) -> None:
    actual = sha256_id(content)
    if actual != expected:
        raise CorruptObjectError(f"content hash mismatch: expected {expected}, got {actual}")
