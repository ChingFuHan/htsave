from __future__ import annotations

import pytest

from htsave.delta import create_unified_delta
from htsave.errors import CorruptObjectError, TransportError
from htsave.hashing import sha256_id
from htsave.models import DeliveryMode
from htsave.transport import (
    parse_transport,
    render_delta,
    render_full_frame,
    render_ref,
)


def test_full_frame_preserves_exact_utf8_bytes() -> None:
    content = "alpha\r\n雪\nno-final-newline".encode()

    frame = parse_transport(render_full_frame(content))

    assert frame.mode is DeliveryMode.FULL
    assert frame.target_hash == sha256_id(content)
    assert frame.target_bytes == len(content)
    assert frame.payload == content


def test_ref_frame_is_length_framed_and_has_no_payload() -> None:
    target = b"payload"

    frame = parse_transport(render_ref(sha256_id(target), len(target)))

    assert frame.mode is DeliveryMode.REF
    assert frame.payload == b""
    assert frame.target_bytes == len(target)


def test_delta_frame_carries_a_verified_unified_diff() -> None:
    base = "one\r\ntwo\nlast"
    target = "one\r\nchanged\nlast"
    delta = create_unified_delta(base, target)

    frame = parse_transport(render_delta(delta, len(target.encode())))

    assert frame.mode is DeliveryMode.DELTA
    assert frame.delta().apply(base) == target


@pytest.mark.parametrize(
    "mutate",
    [
        lambda frame: frame.replace("HTSAVE/1 REF", "HTSAVE/2 REF", 1),
        lambda frame: frame.replace("payload-bytes=0", "payload-bytes=1", 1),
        lambda frame: frame.replace("target-bytes=7", "target-bytes=-1", 1),
        lambda frame: frame.replace("target=sha256:", "target=sha1:", 1),
        lambda frame: frame.replace("\n\n", "\r\n\r\n", 1),
    ],
)
def test_transport_rejects_malformed_headers(mutate) -> None:  # type: ignore[no-untyped-def]
    valid = render_ref(sha256_id(b"payload"), 7)

    with pytest.raises((TransportError, CorruptObjectError)):
        parse_transport(mutate(valid))


def test_full_frame_rejects_payload_corruption() -> None:
    frame = render_full_frame(b"original")

    with pytest.raises(CorruptObjectError):
        parse_transport(frame[:-1] + "X")
