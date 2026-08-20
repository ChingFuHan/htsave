from __future__ import annotations

import pytest

from htsave.errors import CorruptObjectError
from htsave.hashing import sha256_id, verify_sha256


def test_hashes_raw_bytes_without_normalization() -> None:
    assert sha256_id(b"a\n") != sha256_id(b"a\r\n")


def test_verification_rejects_corruption() -> None:
    expected = sha256_id(b"original")

    with pytest.raises(CorruptObjectError):
        verify_sha256(b"changed", expected)
