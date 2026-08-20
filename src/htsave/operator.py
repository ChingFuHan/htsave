"""Read-only inspection and explicitly destructive local-state operations."""

from __future__ import annotations

import os
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .cas import ContentAddressedStore
from .errors import SecurityBoundaryError
from .paths import default_state_root, find_state_paths
from .registry import Registry, RegistryStats

_SESSION_KEY = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_key: str
    schema_version: int
    integrity: str
    active_generation: int | None
    stats: RegistryStats
    cas_objects: int
    cas_bytes: int


@dataclass(frozen=True, slots=True)
class GcCandidate:
    session_key: str
    object_hash: str
    byte_size: int


def configured_state_root(value: Path | None = None) -> Path:
    requested = value.expanduser() if value is not None else None
    if requested is None:
        configured = os.environ.get("HTSAVE_STATE_DIR")
        requested = Path(configured).expanduser() if configured else default_state_root()
    if requested.is_symlink():
        raise SecurityBoundaryError("refusing symbolic-link state root")
    return requested.resolve()


def list_session_keys(state_root: Path | None = None) -> tuple[str, ...]:
    root = configured_state_root(state_root)
    sessions = root / "sessions"
    if not sessions.exists():
        return ()
    if sessions.is_symlink() or not sessions.is_dir():
        raise SecurityBoundaryError("htsave sessions path is not a safe directory")
    keys: list[str] = []
    for candidate in sessions.iterdir():
        if candidate.is_symlink():
            raise SecurityBoundaryError("htsave session path must not be a symbolic link")
        if candidate.is_dir() and _SESSION_KEY.fullmatch(candidate.name):
            keys.append(candidate.name)
    return tuple(sorted(keys))


def session_summary(session_key: str, state_root: Path | None = None) -> SessionSummary:
    paths = find_state_paths(session_key, configured_state_root(state_root))
    with Registry(paths.database, paths.session_key) as registry:
        stats = registry.stats()
        schema = registry.schema_version()
        integrity = registry.integrity_check()
        active = registry.active_generation()
    cas = ContentAddressedStore(paths.objects)
    hashes = tuple(cas.iter_hashes())
    sizes = [cas.path_for(object_hash).stat().st_size for object_hash in hashes]
    return SessionSummary(
        session_key=session_key,
        schema_version=schema,
        integrity=integrity,
        active_generation=active,
        stats=stats,
        cas_objects=len(hashes),
        cas_bytes=sum(sizes),
    )


def all_session_summaries(state_root: Path | None = None) -> tuple[SessionSummary, ...]:
    return tuple(session_summary(key, state_root) for key in list_session_keys(state_root))


def inspect_session(session_key: str, state_root: Path | None = None) -> dict[str, Any]:
    paths = find_state_paths(session_key, configured_state_root(state_root))
    with Registry(paths.database, paths.session_key) as registry:
        generations = [
            dict(row)
            for row in registry.connection.execute(
                """
                SELECT generation_id, source, active, frozen, ambiguous_consumers, created_at
                FROM generations ORDER BY generation_id
                """
            ).fetchall()
        ]
        receipts = [
            dict(row)
            for row in registry.connection.execute(
                """
                SELECT receipt_id, generation_id, source_fingerprint, object_hash,
                       base_hash, mode, state, tool_use_id, delta_depth,
                       cumulative_delta_tokens, created_at, confirmed_at
                FROM receipts ORDER BY receipt_id
                """
            ).fetchall()
        ]
        events = [
            dict(row)
            for row in registry.connection.execute(
                """
                SELECT event_id, generation_id, tool_use_id, source_fingerprint,
                       target_hash, base_hash, mode, original_tokens, emitted_tokens,
                       saved_tokens, latency_ms, bypass_reason, created_at
                FROM events ORDER BY event_id
                """
            ).fetchall()
        ]
        summary = session_summary(session_key, state_root)
    return {
        "summary": asdict(summary),
        "generations": generations,
        "receipts": receipts,
        "events": events,
    }


def gc_candidates(state_root: Path | None = None) -> tuple[GcCandidate, ...]:
    root = configured_state_root(state_root)
    candidates: list[GcCandidate] = []
    for key in list_session_keys(root):
        paths = find_state_paths(key, root)
        cas = ContentAddressedStore(paths.objects)
        with Registry(paths.database, paths.session_key) as registry:
            referenced = registry.referenced_hashes()
        for object_hash in sorted(set(cas.iter_hashes()) - referenced):
            candidates.append(
                GcCandidate(
                    session_key=key,
                    object_hash=object_hash,
                    byte_size=cas.path_for(object_hash).stat().st_size,
                )
            )
    return tuple(candidates)


def apply_gc(
    candidates: tuple[GcCandidate, ...], state_root: Path | None = None
) -> tuple[GcCandidate, ...]:
    root = configured_state_root(state_root)
    by_session: dict[str, list[GcCandidate]] = {}
    for candidate in candidates:
        by_session.setdefault(candidate.session_key, []).append(candidate)
    deleted: list[GcCandidate] = []
    for session_key, session_candidates in by_session.items():
        paths = find_state_paths(session_key, root)
        cas = ContentAddressedStore(paths.objects)
        hashes = [candidate.object_hash for candidate in session_candidates]
        with Registry(paths.database, paths.session_key) as registry:
            still_unreferenced = set(hashes) - registry.referenced_hashes()
            registry.delete_objects(sorted(still_unreferenced))
        for candidate in session_candidates:
            if candidate.object_hash in still_unreferenced:
                cas.delete(candidate.object_hash)
                deleted.append(candidate)
    return tuple(deleted)


def clear_sessions(
    session_keys: tuple[str, ...],
    *,
    state_root: Path | None = None,
) -> tuple[str, ...]:
    root = configured_state_root(state_root)
    sessions_root = (root / "sessions").resolve(strict=True)
    cleared: list[str] = []
    for key in session_keys:
        if _SESSION_KEY.fullmatch(key) is None:
            raise SecurityBoundaryError("invalid session key")
        candidate = sessions_root / key
        if not candidate.exists():
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise SecurityBoundaryError("refusing to clear an unsafe session path")
        resolved = candidate.resolve(strict=True)
        try:
            resolved.relative_to(sessions_root)
        except ValueError as exc:
            raise SecurityBoundaryError("session clear target escaped state root") from exc
        quarantine = sessions_root / f".htsave-clear-{key}-{uuid.uuid4().hex}"
        os.replace(resolved, quarantine)
        shutil.rmtree(quarantine)
        cleared.append(key)
    return tuple(cleared)
