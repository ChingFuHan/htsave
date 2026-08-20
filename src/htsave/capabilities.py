"""One-use, session-scoped capabilities for the Codex hook-to-MCP boundary."""

from __future__ import annotations

import json
import re
import secrets
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cas import ContentAddressedStore
from .engine import ContextEngine
from .errors import SecurityBoundaryError
from .hashing import sha256_id
from .paths import StatePaths, find_state_paths
from .registry import CapabilityRecord, Registry
from .tokens import TokenEstimator

CAPABILITY_TTL_SECONDS = 60.0
_TOKEN = re.compile(r"^v1\.([0-9a-f]{64})\.([A-Za-z0-9_-]{43})$")
_INTERNAL_FIELDS = frozenset({"_htsave_context", "_htsave_ack"})


def canonical_arguments_hash(arguments: Mapping[str, Any]) -> str:
    public_arguments = {
        key: value for key, value in arguments.items() if key not in _INTERNAL_FIELDS
    }
    try:
        encoded = json.dumps(
            public_arguments,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SecurityBoundaryError("tool arguments are not canonical JSON") from exc
    return sha256_id(encoded)


def issue_session_capability(
    registry: Registry,
    *,
    turn_id: str,
    tool_use_id: str,
    tool_name: str,
    arguments: Mapping[str, Any],
    model: str,
    cwd: str,
    now: float | None = None,
    ttl_seconds: float = CAPABILITY_TTL_SECONDS,
) -> str:
    if ttl_seconds <= 0:
        raise ValueError("capability TTL must be positive")
    generation = registry.ensure_generation()
    secret = secrets.token_urlsafe(32)
    if len(secret) != 43:  # pragma: no cover - CPython token_urlsafe contract
        raise SecurityBoundaryError("could not create a canonical capability secret")
    token = f"v1.{registry.session_key}.{secret}"
    issued_at = time.time() if now is None else now
    return registry.issue_capability(
        token=token,
        generation=generation,
        turn_id=turn_id,
        tool_use_id=tool_use_id,
        tool_name=tool_name,
        arguments_hash=canonical_arguments_hash(arguments),
        model=model,
        cwd=cwd,
        expires_at=issued_at + ttl_seconds,
    )


def session_key_from_token(token: str) -> str:
    match = _TOKEN.fullmatch(token)
    if match is None:
        raise SecurityBoundaryError("invalid htsave session capability format")
    return match.group(1)


def token_from_context(value: object) -> str:
    if not isinstance(value, Mapping) or set(value) != {"token"}:
        raise SecurityBoundaryError("invalid _htsave_context metadata")
    token = value.get("token")
    if not isinstance(token, str):
        raise SecurityBoundaryError("invalid _htsave_context token")
    session_key_from_token(token)
    return token


@dataclass(slots=True)
class ConsumedSession:
    record: CapabilityRecord
    paths: StatePaths
    registry: Registry
    engine: ContextEngine


@contextmanager
def consume_session_capability(
    context: object,
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    state_root: Path | None = None,
    now: float | None = None,
) -> Iterator[ConsumedSession]:
    token = token_from_context(context)
    paths = find_state_paths(session_key_from_token(token), state_root)
    registry = Registry(paths.database, paths.session_key)
    try:
        record = registry.consume_capability(
            token=token,
            tool_name=tool_name,
            arguments_hash=canonical_arguments_hash(arguments),
            now=time.time() if now is None else now,
        )
        engine = ContextEngine(
            ContentAddressedStore(paths.objects),
            registry,
            TokenEstimator(record.model),
        )
        yield ConsumedSession(
            record=record,
            paths=paths,
            registry=registry,
            engine=engine,
        )
    finally:
        registry.close()
