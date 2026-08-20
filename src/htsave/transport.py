"""Versioned, length-framed model transport for REF, DELTA, and recovery FULL."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .delta import UnifiedDelta
from .errors import TransportError
from .hashing import sha256_id, verify_sha256
from .models import DeliveryMode

_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAGIC = "HTSAVE/1"


@dataclass(frozen=True, slots=True)
class TransportFrame:
    mode: DeliveryMode
    target_hash: str
    target_bytes: int
    payload: bytes
    base_hash: str | None = None
    codec: str | None = None

    def delta(self) -> UnifiedDelta:
        if self.mode is not DeliveryMode.DELTA or self.codec != "unified-diff-v1":
            raise TransportError("transport frame does not contain a supported delta")
        try:
            parsed = UnifiedDelta.parse(self.payload.decode("utf-8"))
        except UnicodeDecodeError as exc:
            raise TransportError("delta payload is not UTF-8") from exc
        if parsed.base_hash != self.base_hash or parsed.target_hash != self.target_hash:
            raise TransportError("delta hashes disagree with the transport header")
        return parsed


def _require_hash(value: str, field: str) -> str:
    if _HASH_RE.fullmatch(value) is None:
        raise TransportError(f"invalid {field} hash")
    return value


def _require_byte_count(value: int, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise TransportError(f"{field} byte count must be a non-negative integer")
    return value


def _render(mode: DeliveryMode, fields: list[tuple[str, str]], payload: bytes) -> str:
    lines = [f"{_MAGIC} {mode.value.upper()}"]
    lines.extend(f"{key}={value}" for key, value in fields)
    header = "\n".join(lines).encode("ascii") + b"\n\n"
    try:
        return (header + payload).decode("utf-8")
    except UnicodeDecodeError as exc:  # pragma: no cover - callers provide UTF-8
        raise TransportError("transport payload is not UTF-8") from exc


def render_full_frame(content: bytes) -> str:
    target_hash = sha256_id(content)
    return _render(
        DeliveryMode.FULL,
        [
            ("target", target_hash),
            ("target-bytes", str(len(content))),
            ("payload-bytes", str(len(content))),
            ("codec", "identity-utf8"),
        ],
        content,
    )


def render_ref(target_hash: str, target_bytes: int) -> str:
    _require_hash(target_hash, "target")
    _require_byte_count(target_bytes, "target")
    return _render(
        DeliveryMode.REF,
        [
            ("target", target_hash),
            ("target-bytes", str(target_bytes)),
            ("payload-bytes", "0"),
        ],
        b"",
    )


def render_delta(delta: UnifiedDelta, target_bytes: int) -> str:
    _require_byte_count(target_bytes, "target")
    payload = delta.serialize().encode("utf-8")
    return _render(
        DeliveryMode.DELTA,
        [
            ("base", _require_hash(delta.base_hash, "base")),
            ("target", _require_hash(delta.target_hash, "target")),
            ("target-bytes", str(target_bytes)),
            ("payload-bytes", str(len(payload))),
            ("codec", "unified-diff-v1"),
        ],
        payload,
    )


def parse_transport(value: str | bytes) -> TransportFrame:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    header, separator, payload = raw.partition(b"\n\n")
    if not separator:
        raise TransportError("transport frame is missing its header terminator")
    try:
        header_text = header.decode("ascii")
    except UnicodeDecodeError as exc:
        raise TransportError("transport header must be ASCII") from exc
    if "\r" in header_text:
        raise TransportError("transport header must use LF line endings")

    lines = header_text.split("\n")
    if not lines or not lines[0].startswith(f"{_MAGIC} "):
        raise TransportError("unsupported transport version")
    mode_name = lines[0][len(_MAGIC) + 1 :].lower()
    try:
        mode = DeliveryMode(mode_name)
    except ValueError as exc:
        raise TransportError("unsupported transport mode") from exc
    if mode is DeliveryMode.BYPASS:
        raise TransportError("BYPASS is not a transport frame")

    fields: dict[str, str] = {}
    for line in lines[1:]:
        key, marker, field_value = line.partition("=")
        if not marker or not key or key in fields:
            raise TransportError("malformed or duplicate transport header field")
        fields[key] = field_value

    required = {"target", "target-bytes", "payload-bytes"}
    if mode is DeliveryMode.FULL:
        required.add("codec")
        allowed = required
    elif mode is DeliveryMode.REF:
        allowed = required
    else:
        required.update({"base", "codec"})
        allowed = required
    if fields.keys() != allowed:
        raise TransportError("transport header fields do not match its mode")

    try:
        target_bytes = int(fields["target-bytes"])
        payload_bytes = int(fields["payload-bytes"])
    except ValueError as exc:
        raise TransportError("transport byte counts must be decimal integers") from exc
    if target_bytes < 0 or payload_bytes < 0 or payload_bytes != len(payload):
        raise TransportError("transport byte count mismatch")

    target_hash = _require_hash(fields["target"], "target")
    base_hash = fields.get("base")
    if base_hash is not None:
        _require_hash(base_hash, "base")
    codec = fields.get("codec")
    frame = TransportFrame(
        mode=mode,
        target_hash=target_hash,
        target_bytes=target_bytes,
        payload=payload,
        base_hash=base_hash,
        codec=codec,
    )

    if mode is DeliveryMode.FULL:
        if codec != "identity-utf8" or len(payload) != target_bytes:
            raise TransportError("invalid FULL transport payload")
        try:
            payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TransportError("FULL payload is not UTF-8") from exc
        verify_sha256(payload, target_hash)
    elif mode is DeliveryMode.REF:
        if payload:
            raise TransportError("REF transport must not contain a payload")
    else:
        if codec != "unified-diff-v1":
            raise TransportError("unsupported DELTA codec")
        frame.delta()
    return frame
