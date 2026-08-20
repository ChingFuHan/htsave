"""SQLite ownership for generations, receipts, versions, and accounting."""

from __future__ import annotations

import math
import re
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Self

from .errors import CorruptObjectError, SecurityBoundaryError
from .models import ContentObject, DeliveryMode, ReceiptState
from .paths import ensure_private_file

SCHEMA_VERSION = 4
_OBJECT_ID = re.compile(r"^sha256:[0-9a-f]{64}$")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _require_object_id(value: object, field: str) -> str:
    if not isinstance(value, str) or _OBJECT_ID.fullmatch(value) is None:
        raise CorruptObjectError(f"{field} must be a canonical SHA-256 object id")
    return value


def _require_nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise CorruptObjectError(f"{field} must be a non-negative integer")
    return value


@dataclass(frozen=True, slots=True)
class ReceiptHead:
    receipt_id: int
    object_hash: str
    mode: DeliveryMode
    delta_depth: int
    cumulative_delta_tokens: int


@dataclass(frozen=True, slots=True)
class PendingReceipt:
    receipt_id: int
    source_fingerprint: str
    object_hash: str
    mode: DeliveryMode
    tool_use_id: str


@dataclass(frozen=True, slots=True)
class DeliveryEvent:
    tool_use_id: str
    source_fingerprint: str
    target_hash: str
    base_hash: str | None
    mode: DeliveryMode
    original_tokens: int
    emitted_tokens: int
    delta_depth: int
    cumulative_delta_tokens: int
    bypass_reason: str | None


@dataclass(frozen=True, slots=True)
class CapabilityRecord:
    token: str
    generation_id: int
    turn_id: str
    tool_use_id: str
    tool_name: str
    arguments_hash: str
    model: str
    cwd: str
    expires_at: float


@dataclass(frozen=True, slots=True)
class RecoveryTarget:
    recovery_id: int
    source_generation: int
    receipt_id: int
    source_fingerprint: str
    object_hash: str
    tool_use_id: str


@dataclass(frozen=True, slots=True)
class RegistryStats:
    events: int
    full: int
    refs: int
    deltas: int
    bypassed: int
    original_tokens: int
    emitted_tokens: int
    saved_tokens: int


_MIGRATION_1: tuple[str, ...] = (
    """
    CREATE TABLE objects (
        object_hash TEXT PRIMARY KEY,
        codec TEXT NOT NULL,
        byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
        estimated_tokens INTEGER NOT NULL CHECK (estimated_tokens >= 0),
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE sources (
        source_fingerprint TEXT PRIMARY KEY,
        safe_label TEXT NOT NULL,
        current_hash TEXT NOT NULL REFERENCES objects(object_hash),
        version INTEGER NOT NULL CHECK (version > 0),
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE source_versions (
        source_fingerprint TEXT NOT NULL REFERENCES sources(source_fingerprint),
        version INTEGER NOT NULL CHECK (version > 0),
        object_hash TEXT NOT NULL REFERENCES objects(object_hash),
        observed_at TEXT NOT NULL,
        PRIMARY KEY (source_fingerprint, version)
    )
    """,
    """
    CREATE TABLE generations (
        generation_id INTEGER PRIMARY KEY AUTOINCREMENT,
        session_key TEXT NOT NULL,
        source TEXT NOT NULL,
        active INTEGER NOT NULL CHECK (active IN (0, 1)),
        ambiguous_consumers INTEGER NOT NULL DEFAULT 0 CHECK (ambiguous_consumers >= 0),
        created_at TEXT NOT NULL
    )
    """,
    "CREATE UNIQUE INDEX one_active_generation ON generations(active) WHERE active = 1",
    """
    CREATE TABLE receipts (
        receipt_id INTEGER PRIMARY KEY AUTOINCREMENT,
        generation_id INTEGER NOT NULL REFERENCES generations(generation_id),
        source_fingerprint TEXT NOT NULL,
        object_hash TEXT NOT NULL REFERENCES objects(object_hash),
        base_hash TEXT,
        mode TEXT NOT NULL CHECK (mode IN ('full', 'ref', 'delta')),
        state TEXT NOT NULL CHECK (state IN ('pending', 'confirmed')),
        tool_use_id TEXT NOT NULL,
        delta_depth INTEGER NOT NULL CHECK (delta_depth >= 0),
        cumulative_delta_tokens INTEGER NOT NULL CHECK (cumulative_delta_tokens >= 0),
        created_at TEXT NOT NULL,
        confirmed_at TEXT,
        UNIQUE (generation_id, tool_use_id)
    )
    """,
    "CREATE INDEX receipts_by_object ON receipts(generation_id, object_hash, state)",
    """
    CREATE INDEX receipts_by_source
    ON receipts(generation_id, source_fingerprint, state, receipt_id)
    """,
    """
    CREATE TABLE events (
        event_id INTEGER PRIMARY KEY AUTOINCREMENT,
        generation_id INTEGER REFERENCES generations(generation_id),
        tool_use_id TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL,
        target_hash TEXT NOT NULL,
        base_hash TEXT,
        mode TEXT NOT NULL CHECK (mode IN ('full', 'ref', 'delta', 'bypass')),
        original_tokens INTEGER NOT NULL CHECK (original_tokens >= 0),
        emitted_tokens INTEGER NOT NULL CHECK (emitted_tokens >= 0),
        saved_tokens INTEGER NOT NULL CHECK (saved_tokens >= 0),
        latency_ms REAL NOT NULL CHECK (latency_ms >= 0),
        bypass_reason TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (generation_id, tool_use_id)
    )
    """,
)

_MIGRATION_2: tuple[str, ...] = (
    """
    CREATE TABLE registry_metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
)

# The Claude Code hook contract reports ``model`` on SessionStart only, so the
# generation carries it forward for every later event in the same generation.
_MIGRATION_4: tuple[str, ...] = ("ALTER TABLE generations ADD COLUMN model TEXT",)

_MIGRATION_3: tuple[str, ...] = (
    "ALTER TABLE generations ADD COLUMN frozen INTEGER NOT NULL DEFAULT 0 CHECK (frozen IN (0, 1))",
    """
    CREATE TABLE active_agents (
        agent_id TEXT PRIMARY KEY,
        generation_id INTEGER NOT NULL REFERENCES generations(generation_id),
        agent_type TEXT NOT NULL,
        started_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE capabilities (
        token TEXT PRIMARY KEY,
        generation_id INTEGER NOT NULL REFERENCES generations(generation_id),
        turn_id TEXT NOT NULL,
        tool_use_id TEXT NOT NULL,
        tool_name TEXT NOT NULL,
        arguments_hash TEXT NOT NULL,
        model TEXT NOT NULL,
        cwd TEXT NOT NULL,
        expires_at REAL NOT NULL,
        consumed_at TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (generation_id, tool_use_id, tool_name)
    )
    """,
    """
    CREATE TABLE compact_recovery (
        recovery_id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_generation INTEGER NOT NULL REFERENCES generations(generation_id),
        receipt_id INTEGER NOT NULL UNIQUE REFERENCES receipts(receipt_id),
        source_fingerprint TEXT NOT NULL,
        object_hash TEXT NOT NULL REFERENCES objects(object_hash),
        tool_use_id TEXT NOT NULL,
        consumed_generation INTEGER REFERENCES generations(generation_id),
        created_at TEXT NOT NULL
    )
    """,
    "CREATE INDEX recovery_pending ON compact_recovery(source_generation, consumed_generation)",
)


class Registry:
    def __init__(self, database: Path, session_key: str) -> None:
        self.database = database
        self.session_key = session_key
        if database.is_symlink():
            raise SecurityBoundaryError("registry database must not be a symbolic link")
        for suffix in ("-wal", "-shm", "-journal"):
            sidecar = database.with_name(database.name + suffix)
            if sidecar.is_symlink():
                raise SecurityBoundaryError("registry sidecars must not be symbolic links")
        self.connection = sqlite3.connect(
            database,
            timeout=5,
            isolation_level=None,
        )
        try:
            self.connection.row_factory = sqlite3.Row
            self.connection.execute("PRAGMA foreign_keys = ON")
            self.connection.execute("PRAGMA busy_timeout = 5000")
            self.connection.execute("PRAGMA journal_mode = WAL")
            self.connection.execute("PRAGMA synchronous = FULL")
            ensure_private_file(database)
            self._migrate()
            self._bind_session_key()
        except Exception:
            self.connection.close()
            raise

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield self.connection
        except Exception:
            self.connection.execute("ROLLBACK")
            raise
        else:
            self.connection.execute("COMMIT")
            self._secure_database_files()

    def _secure_database_files(self) -> None:
        """Keep SQLite's database and any journal sidecars private."""

        for suffix in ("", "-wal", "-shm", "-journal"):
            candidate = self.database.with_name(self.database.name + suffix)
            if candidate.exists():
                ensure_private_file(candidate)

    def _migrate(self) -> None:
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            )
            """
        )
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        current = int(row["version"])
        if current > SCHEMA_VERSION:
            raise CorruptObjectError(
                f"registry schema {current} is newer than supported {SCHEMA_VERSION}"
            )
        if current < 1:
            with self.transaction() as connection:
                for statement in _MIGRATION_1:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (1, _now()),
                )
            current = 1
        if current < 2:
            with self.transaction() as connection:
                for statement in _MIGRATION_2:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (2, _now()),
                )
            current = 2
        if current < 3:
            with self.transaction() as connection:
                for statement in _MIGRATION_3:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (3, _now()),
                )
            current = 3
        if current < 4:
            with self.transaction() as connection:
                for statement in _MIGRATION_4:
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (4, _now()),
                )

    def _bind_session_key(self) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT value FROM registry_metadata WHERE key = 'session_key'"
            ).fetchone()
            if row is None:
                legacy_rows = connection.execute(
                    "SELECT DISTINCT session_key FROM generations"
                ).fetchall()
                legacy_keys = {str(item["session_key"]) for item in legacy_rows}
                if legacy_keys and legacy_keys != {self.session_key}:
                    raise CorruptObjectError(
                        "registry session key does not match its legacy generations"
                    )
                connection.execute(
                    "INSERT INTO registry_metadata(key, value) VALUES ('session_key', ?)",
                    (self.session_key,),
                )
            elif str(row["value"]) != self.session_key:
                raise CorruptObjectError("registry belongs to a different Codex session")

    def schema_version(self) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(version), 0) AS version FROM schema_migrations"
        ).fetchone()
        return int(row["version"])

    def integrity_check(self) -> str:
        return str(self.connection.execute("PRAGMA integrity_check").fetchone()[0])

    def begin_generation(
        self,
        source: str,
        *,
        preserve_agents: bool = False,
        model: str | None = None,
    ) -> int:
        with self.transaction() as connection:
            if not preserve_agents:
                connection.execute("DELETE FROM active_agents")
            agent_count = int(
                connection.execute("SELECT COUNT(*) FROM active_agents").fetchone()[0]
            )
            carried = model
            if carried is None:
                row = connection.execute(
                    "SELECT model FROM generations WHERE active = 1"
                ).fetchone()
                carried = None if row is None else row["model"]
            connection.execute("UPDATE generations SET active = 0 WHERE active = 1")
            cursor = connection.execute(
                """
                INSERT INTO generations(
                    session_key, source, active, ambiguous_consumers, created_at, model
                )
                VALUES (?, ?, 1, ?, ?, ?)
                """,
                (self.session_key, source, agent_count, _now(), carried),
            )
            return int(cursor.lastrowid)

    def active_model(self) -> str | None:
        """Return the model recorded when the active generation began."""

        row = self.connection.execute("SELECT model FROM generations WHERE active = 1").fetchone()
        if row is None:
            return None
        model = row["model"]
        return model if isinstance(model, str) and model else None

    def active_generation(self) -> int | None:
        row = self.connection.execute(
            "SELECT generation_id FROM generations WHERE active = 1"
        ).fetchone()
        return None if row is None else int(row["generation_id"])

    def ensure_generation(self) -> int:
        generation = self.active_generation()
        return generation if generation is not None else self.begin_generation("implicit")

    def active_is_ambiguous(self) -> bool:
        row = self.connection.execute(
            "SELECT ambiguous_consumers FROM generations WHERE active = 1"
        ).fetchone()
        return row is not None and int(row["ambiguous_consumers"]) > 0

    def active_is_frozen(self) -> bool:
        row = self.connection.execute("SELECT frozen FROM generations WHERE active = 1").fetchone()
        return row is not None and int(row["frozen"]) == 1

    def freeze_active_generation(self) -> int | None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT generation_id FROM generations WHERE active = 1"
            ).fetchone()
            if row is None:
                return None
            generation = int(row["generation_id"])
            connection.execute(
                "UPDATE generations SET frozen = 1 WHERE generation_id = ?",
                (generation,),
            )
            return generation

    def set_subagent_active(self, active: bool) -> int:
        generation = self.ensure_generation()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT ambiguous_consumers FROM generations WHERE generation_id = ?",
                (generation,),
            ).fetchone()
            count = int(row["ambiguous_consumers"])
            count = count + 1 if active else max(0, count - 1)
            connection.execute(
                "UPDATE generations SET ambiguous_consumers = ? WHERE generation_id = ?",
                (count, generation),
            )
        return count

    def subagent_started(self, agent_id: str, agent_type: str) -> int:
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        generation = self.ensure_generation()
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT agent_type FROM active_agents WHERE agent_id = ?",
                (agent_id,),
            ).fetchone()
            if existing is not None and str(existing["agent_type"]) != agent_type:
                raise CorruptObjectError("subagent retry changed its agent type")
            connection.execute(
                """
                INSERT INTO active_agents(agent_id, generation_id, agent_type, started_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(agent_id) DO NOTHING
                """,
                (agent_id, generation, agent_type, _now()),
            )
            count = int(connection.execute("SELECT COUNT(*) FROM active_agents").fetchone()[0])
            connection.execute(
                "UPDATE generations SET ambiguous_consumers = ? WHERE generation_id = ?",
                (count, generation),
            )
            return count

    def subagent_stopped(self, agent_id: str) -> int:
        _, remaining = self.finish_subagent(agent_id)
        return remaining

    def finish_subagent(self, agent_id: str) -> tuple[bool, int]:
        if not agent_id:
            raise ValueError("agent_id must not be empty")
        generation = self.active_generation()
        if generation is None:
            return False, 0
        with self.transaction() as connection:
            removed = connection.execute(
                "DELETE FROM active_agents WHERE agent_id = ?", (agent_id,)
            )
            count = int(connection.execute("SELECT COUNT(*) FROM active_agents").fetchone()[0])
            connection.execute(
                "UPDATE generations SET ambiguous_consumers = ? WHERE generation_id = ?",
                (count, generation),
            )
            return int(removed.rowcount) == 1, count

    def record_object(self, content: ContentObject) -> None:
        object_hash = _require_object_id(content.object_hash, "object_hash")
        if not isinstance(content.codec, str) or not content.codec:
            raise CorruptObjectError("object codec must be a non-empty string")
        byte_size = _require_nonnegative_int(content.byte_size, "object byte size")
        estimated_tokens = _require_nonnegative_int(
            content.estimated_tokens, "object token estimate"
        )
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT codec, byte_size FROM objects WHERE object_hash = ?",
                (object_hash,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["codec"]) != content.codec
                    or int(existing["byte_size"]) != byte_size
                ):
                    raise CorruptObjectError(
                        "content hash was reused with conflicting object metadata"
                    )
                # Token estimates can legitimately change when a session switches
                # models. Per-delivery accounting remains authoritative.
                return
            connection.execute(
                """
                INSERT INTO objects(object_hash, codec, byte_size, estimated_tokens, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    object_hash,
                    content.codec,
                    byte_size,
                    estimated_tokens,
                    _now(),
                ),
            )

    def confirmed_object_exists(self, generation: int, object_hash: str) -> bool:
        row = self.connection.execute(
            """
            SELECT 1 FROM receipts
            WHERE generation_id = ? AND object_hash = ? AND state = 'confirmed'
            LIMIT 1
            """,
            (generation, object_hash),
        ).fetchone()
        return row is not None

    def confirmed_object_head(self, generation: int, object_hash: str) -> ReceiptHead | None:
        row = self.connection.execute(
            """
            SELECT receipt_id, object_hash, mode, delta_depth, cumulative_delta_tokens
            FROM receipts
            WHERE generation_id = ? AND object_hash = ? AND state = 'confirmed'
            ORDER BY receipt_id DESC
            LIMIT 1
            """,
            (generation, object_hash),
        ).fetchone()
        if row is None:
            return None
        return ReceiptHead(
            receipt_id=int(row["receipt_id"]),
            object_hash=str(row["object_hash"]),
            mode=DeliveryMode(str(row["mode"])),
            delta_depth=int(row["delta_depth"]),
            cumulative_delta_tokens=int(row["cumulative_delta_tokens"]),
        )

    def confirmed_head(self, generation: int, source_fingerprint: str) -> ReceiptHead | None:
        row = self.connection.execute(
            """
            SELECT receipt_id, object_hash, mode, delta_depth, cumulative_delta_tokens
            FROM receipts
            WHERE generation_id = ? AND source_fingerprint = ? AND state = 'confirmed'
            ORDER BY receipt_id DESC
            LIMIT 1
            """,
            (generation, source_fingerprint),
        ).fetchone()
        if row is None:
            return None
        return ReceiptHead(
            receipt_id=int(row["receipt_id"]),
            object_hash=str(row["object_hash"]),
            mode=DeliveryMode(str(row["mode"])),
            delta_depth=int(row["delta_depth"]),
            cumulative_delta_tokens=int(row["cumulative_delta_tokens"]),
        )

    def delivery_event(self, generation: int, tool_use_id: str) -> DeliveryEvent | None:
        row = self.connection.execute(
            """
            SELECT
                e.tool_use_id,
                e.source_fingerprint,
                e.target_hash,
                e.base_hash,
                e.mode,
                e.original_tokens,
                e.emitted_tokens,
                e.bypass_reason,
                COALESCE(r.delta_depth, 0) AS delta_depth,
                COALESCE(r.cumulative_delta_tokens, 0) AS cumulative_delta_tokens
            FROM events AS e
            LEFT JOIN receipts AS r
              ON r.generation_id = e.generation_id
             AND r.tool_use_id = e.tool_use_id
            WHERE e.generation_id = ? AND e.tool_use_id = ?
            """,
            (generation, tool_use_id),
        ).fetchone()
        if row is None:
            return None
        return DeliveryEvent(
            tool_use_id=str(row["tool_use_id"]),
            source_fingerprint=str(row["source_fingerprint"]),
            target_hash=str(row["target_hash"]),
            base_hash=None if row["base_hash"] is None else str(row["base_hash"]),
            mode=DeliveryMode(str(row["mode"])),
            original_tokens=int(row["original_tokens"]),
            emitted_tokens=int(row["emitted_tokens"]),
            delta_depth=int(row["delta_depth"]),
            cumulative_delta_tokens=int(row["cumulative_delta_tokens"]),
            bypass_reason=(None if row["bypass_reason"] is None else str(row["bypass_reason"])),
        )

    def record_delivery(
        self,
        *,
        generation: int,
        tool_use_id: str,
        source_fingerprint: str,
        safe_label: str,
        target_hash: str,
        base_hash: str | None,
        mode: DeliveryMode,
        original_tokens: int,
        emitted_tokens: int,
        latency_ms: float,
        delta_depth: int,
        cumulative_delta_tokens: int,
        bypass_reason: str | None = None,
    ) -> None:
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            raise CorruptObjectError("delivery generation must be a positive integer")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            raise CorruptObjectError("delivery tool_use_id must be non-empty")
        if not isinstance(source_fingerprint, str) or not source_fingerprint:
            raise CorruptObjectError("delivery source fingerprint must be non-empty")
        if not isinstance(safe_label, str) or not safe_label:
            raise CorruptObjectError("delivery safe label must be non-empty")
        target_hash = _require_object_id(target_hash, "target_hash")
        if base_hash is not None:
            base_hash = _require_object_id(base_hash, "base_hash")
        if not isinstance(mode, DeliveryMode):
            raise CorruptObjectError("delivery mode is unsupported")
        original_tokens = _require_nonnegative_int(original_tokens, "original token count")
        emitted_tokens = _require_nonnegative_int(emitted_tokens, "emitted token count")
        delta_depth = _require_nonnegative_int(delta_depth, "delta depth")
        cumulative_delta_tokens = _require_nonnegative_int(
            cumulative_delta_tokens, "cumulative delta token count"
        )
        if isinstance(latency_ms, bool) or not isinstance(latency_ms, (int, float)):
            raise CorruptObjectError("delivery latency must be a finite number")
        if not math.isfinite(float(latency_ms)) or float(latency_ms) < 0:
            raise CorruptObjectError("delivery latency must be a finite number")
        if bypass_reason is not None and not isinstance(bypass_reason, str):
            raise CorruptObjectError("delivery bypass reason must be text or null")
        with self.transaction() as connection:
            existing = connection.execute(
                """
                SELECT
                    source_fingerprint,
                    target_hash,
                    base_hash,
                    mode,
                    original_tokens,
                    emitted_tokens,
                    bypass_reason
                FROM events
                WHERE generation_id = ? AND tool_use_id = ?
                """,
                (generation, tool_use_id),
            ).fetchone()
            if existing is not None:
                if (
                    existing["source_fingerprint"] != source_fingerprint
                    or existing["target_hash"] != target_hash
                    or existing["base_hash"] != base_hash
                    or existing["mode"] != mode.value
                    or int(existing["original_tokens"]) != original_tokens
                    or int(existing["emitted_tokens"]) != emitted_tokens
                    or existing["bypass_reason"] != bypass_reason
                ):
                    raise CorruptObjectError("tool-use retry changed its delivery result")
                receipt = connection.execute(
                    """
                    SELECT delta_depth, cumulative_delta_tokens
                    FROM receipts
                    WHERE generation_id = ? AND tool_use_id = ?
                    """,
                    (generation, tool_use_id),
                ).fetchone()
                if mode is DeliveryMode.BYPASS:
                    if receipt is not None:
                        raise CorruptObjectError("bypass delivery unexpectedly has a receipt")
                elif receipt is None or (
                    int(receipt["delta_depth"]) != delta_depth
                    or int(receipt["cumulative_delta_tokens"]) != cumulative_delta_tokens
                ):
                    raise CorruptObjectError("tool-use retry changed its receipt metadata")
                return

            if mode is DeliveryMode.DELTA and base_hash is None:
                raise CorruptObjectError("DELTA delivery requires a base object")
            if mode is not DeliveryMode.DELTA and base_hash is not None:
                raise CorruptObjectError("only DELTA delivery may name a base object")
            if mode in {DeliveryMode.FULL, DeliveryMode.BYPASS} and (
                delta_depth != 0 or cumulative_delta_tokens != 0
            ):
                raise CorruptObjectError("FULL/BYPASS delivery cannot retain delta-chain state")

            generation_row = connection.execute(
                """
                SELECT session_key, active, frozen
                FROM generations WHERE generation_id = ?
                """,
                (generation,),
            ).fetchone()
            if (
                generation_row is None
                or str(generation_row["session_key"]) != self.session_key
                or int(generation_row["active"]) != 1
            ):
                raise CorruptObjectError("delivery generation is not active for this session")
            if int(generation_row["frozen"]) == 1 and mode is not DeliveryMode.BYPASS:
                raise CorruptObjectError("only BYPASS is allowed in a frozen generation")
            target = connection.execute(
                "SELECT 1 FROM objects WHERE object_hash = ?", (target_hash,)
            ).fetchone()
            if target is None:
                raise CorruptObjectError("delivery target object is not registered")
            if base_hash is not None:
                base = connection.execute(
                    "SELECT 1 FROM objects WHERE object_hash = ?", (base_hash,)
                ).fetchone()
                if base is None:
                    raise CorruptObjectError("DELTA base object is not registered")

            source = connection.execute(
                "SELECT current_hash, version FROM sources WHERE source_fingerprint = ?",
                (source_fingerprint,),
            ).fetchone()
            if source is None:
                version = 1
                connection.execute(
                    """
                    INSERT INTO sources(
                        source_fingerprint, safe_label, current_hash, version, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (source_fingerprint, safe_label, target_hash, version, _now()),
                )
                connection.execute(
                    """
                    INSERT INTO source_versions(
                        source_fingerprint, version, object_hash, observed_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (source_fingerprint, version, target_hash, _now()),
                )
            elif source["current_hash"] != target_hash:
                version = int(source["version"]) + 1
                connection.execute(
                    """
                    UPDATE sources SET current_hash = ?, version = ?, updated_at = ?
                    WHERE source_fingerprint = ?
                    """,
                    (target_hash, version, _now(), source_fingerprint),
                )
                connection.execute(
                    """
                    INSERT INTO source_versions(
                        source_fingerprint, version, object_hash, observed_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (source_fingerprint, version, target_hash, _now()),
                )

            saved_tokens = max(0, original_tokens - emitted_tokens)
            connection.execute(
                """
                INSERT INTO events(
                    generation_id, tool_use_id, source_fingerprint, target_hash, base_hash,
                    mode, original_tokens, emitted_tokens, saved_tokens, latency_ms,
                    bypass_reason, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    generation,
                    tool_use_id,
                    source_fingerprint,
                    target_hash,
                    base_hash,
                    mode.value,
                    original_tokens,
                    emitted_tokens,
                    saved_tokens,
                    latency_ms,
                    bypass_reason,
                    _now(),
                ),
            )
            if mode is not DeliveryMode.BYPASS:
                connection.execute(
                    """
                    INSERT INTO receipts(
                        generation_id, source_fingerprint, object_hash, base_hash, mode,
                        state, tool_use_id, delta_depth, cumulative_delta_tokens,
                        created_at, confirmed_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (
                        generation,
                        source_fingerprint,
                        target_hash,
                        base_hash,
                        mode.value,
                        ReceiptState.PENDING.value,
                        tool_use_id,
                        delta_depth,
                        cumulative_delta_tokens,
                        _now(),
                    ),
                )

    def confirm_pending(self, generation: int | None = None) -> int:
        generation = generation or self.active_generation()
        if generation is None:
            return 0
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE receipts
                SET state = 'confirmed', confirmed_at = ?
                WHERE generation_id = ? AND state = 'pending'
                """,
                (_now(), generation),
            )
            return int(cursor.rowcount)

    def pending_receipts(self, generation: int | None = None) -> tuple[PendingReceipt, ...]:
        generation = generation or self.active_generation()
        if generation is None:
            return ()
        rows = self.connection.execute(
            """
            SELECT receipt_id, source_fingerprint, object_hash, mode, tool_use_id
            FROM receipts
            WHERE generation_id = ? AND state = 'pending'
            ORDER BY receipt_id
            """,
            (generation,),
        ).fetchall()
        return tuple(
            PendingReceipt(
                receipt_id=int(row["receipt_id"]),
                source_fingerprint=str(row["source_fingerprint"]),
                object_hash=str(row["object_hash"]),
                mode=DeliveryMode(str(row["mode"])),
                tool_use_id=str(row["tool_use_id"]),
            )
            for row in rows
        )

    def issue_capability(
        self,
        *,
        token: str,
        generation: int,
        turn_id: str,
        tool_use_id: str,
        tool_name: str,
        arguments_hash: str,
        model: str,
        cwd: str,
        expires_at: float,
    ) -> str:
        with self.transaction() as connection:
            active = connection.execute(
                "SELECT generation_id, frozen FROM generations WHERE active = 1"
            ).fetchone()
            if active is None or int(active["generation_id"]) != generation:
                raise SecurityBoundaryError("capability generation is not active")
            if int(active["frozen"]) == 1:
                raise SecurityBoundaryError("capabilities cannot be issued while compacting")
            existing = connection.execute(
                """
                SELECT * FROM capabilities
                WHERE generation_id = ? AND tool_use_id = ? AND tool_name = ?
                """,
                (generation, tool_use_id, tool_name),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["turn_id"]) != turn_id
                    or str(existing["arguments_hash"]) != arguments_hash
                    or str(existing["model"]) != model
                    or str(existing["cwd"]) != cwd
                ):
                    raise CorruptObjectError("tool-use retry changed its capability scope")
                return str(existing["token"])
            connection.execute(
                """
                INSERT INTO capabilities(
                    token, generation_id, turn_id, tool_use_id, tool_name,
                    arguments_hash, model, cwd, expires_at, consumed_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)
                """,
                (
                    token,
                    generation,
                    turn_id,
                    tool_use_id,
                    tool_name,
                    arguments_hash,
                    model,
                    cwd,
                    expires_at,
                    _now(),
                ),
            )
            return token

    def consume_capability(
        self,
        *,
        token: str,
        tool_name: str,
        arguments_hash: str,
        now: float,
    ) -> CapabilityRecord:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM capabilities WHERE token = ?",
                (token,),
            ).fetchone()
            if row is None:
                raise SecurityBoundaryError("unknown htsave session capability")
            if row["consumed_at"] is not None:
                raise SecurityBoundaryError("htsave session capability was already used")
            if float(row["expires_at"]) < now:
                raise SecurityBoundaryError("htsave session capability expired")
            if str(row["tool_name"]) != tool_name:
                raise SecurityBoundaryError("htsave capability tool mismatch")
            if str(row["arguments_hash"]) != arguments_hash:
                raise SecurityBoundaryError("htsave capability argument mismatch")
            active = connection.execute(
                "SELECT generation_id FROM generations WHERE active = 1"
            ).fetchone()
            if active is None or int(active["generation_id"]) != int(row["generation_id"]):
                raise SecurityBoundaryError("htsave capability generation is no longer active")
            cursor = connection.execute(
                """
                UPDATE capabilities SET consumed_at = ?
                WHERE token = ? AND consumed_at IS NULL
                """,
                (_now(), token),
            )
            if int(cursor.rowcount) != 1:
                raise SecurityBoundaryError("htsave capability replay was rejected")
            return CapabilityRecord(
                token=str(row["token"]),
                generation_id=int(row["generation_id"]),
                turn_id=str(row["turn_id"]),
                tool_use_id=str(row["tool_use_id"]),
                tool_name=str(row["tool_name"]),
                arguments_hash=str(row["arguments_hash"]),
                model=str(row["model"]),
                cwd=str(row["cwd"]),
                expires_at=float(row["expires_at"]),
            )

    def prepare_compaction(self) -> tuple[RecoveryTarget, ...]:
        with self.transaction() as connection:
            active = connection.execute(
                "SELECT generation_id FROM generations WHERE active = 1"
            ).fetchone()
            if active is None:
                return ()
            generation = int(active["generation_id"])
            connection.execute(
                "UPDATE generations SET frozen = 1 WHERE generation_id = ?",
                (generation,),
            )
            receipts = connection.execute(
                """
                SELECT receipt_id, source_fingerprint, object_hash, tool_use_id
                FROM receipts
                WHERE generation_id = ? AND state = 'pending'
                  AND mode IN ('ref', 'delta')
                ORDER BY receipt_id
                """,
                (generation,),
            ).fetchall()
            for receipt in receipts:
                connection.execute(
                    """
                    INSERT INTO compact_recovery(
                        source_generation, receipt_id, source_fingerprint,
                        object_hash, tool_use_id, consumed_generation, created_at
                    ) VALUES (?, ?, ?, ?, ?, NULL, ?)
                    ON CONFLICT(receipt_id) DO NOTHING
                    """,
                    (
                        generation,
                        int(receipt["receipt_id"]),
                        str(receipt["source_fingerprint"]),
                        str(receipt["object_hash"]),
                        str(receipt["tool_use_id"]),
                        _now(),
                    ),
                )
            rows = connection.execute(
                """
                SELECT * FROM compact_recovery
                WHERE source_generation = ? AND consumed_generation IS NULL
                ORDER BY recovery_id
                """,
                (generation,),
            ).fetchall()
            return tuple(self._recovery_target(row) for row in rows)

    def recovery_targets(self, source_generation: int) -> tuple[RecoveryTarget, ...]:
        rows = self.connection.execute(
            """
            SELECT * FROM compact_recovery
            WHERE source_generation = ? AND consumed_generation IS NULL
            ORDER BY recovery_id
            """,
            (source_generation,),
        ).fetchall()
        return tuple(self._recovery_target(row) for row in rows)

    @staticmethod
    def _recovery_target(row: sqlite3.Row) -> RecoveryTarget:
        return RecoveryTarget(
            recovery_id=int(row["recovery_id"]),
            source_generation=int(row["source_generation"]),
            receipt_id=int(row["receipt_id"]),
            source_fingerprint=str(row["source_fingerprint"]),
            object_hash=str(row["object_hash"]),
            tool_use_id=str(row["tool_use_id"]),
        )

    def mark_recovery_consumed(self, recovery_id: int, generation: int) -> None:
        with self.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE compact_recovery SET consumed_generation = ?
                WHERE recovery_id = ? AND consumed_generation IS NULL
                """,
                (generation, recovery_id),
            )
            if int(cursor.rowcount) != 1:
                raise CorruptObjectError("compact recovery was already consumed or missing")

    def stats(self) -> RegistryStats:
        row = self.connection.execute(
            """
            SELECT
                COUNT(*) AS events,
                SUM(CASE WHEN mode = 'full' THEN 1 ELSE 0 END) AS full,
                SUM(CASE WHEN mode = 'ref' THEN 1 ELSE 0 END) AS refs,
                SUM(CASE WHEN mode = 'delta' THEN 1 ELSE 0 END) AS deltas,
                SUM(CASE WHEN mode = 'bypass' THEN 1 ELSE 0 END) AS bypassed,
                COALESCE(SUM(original_tokens), 0) AS original_tokens,
                COALESCE(SUM(emitted_tokens), 0) AS emitted_tokens,
                COALESCE(SUM(saved_tokens), 0) AS saved_tokens
            FROM events
            """
        ).fetchone()
        return RegistryStats(
            events=int(row["events"] or 0),
            full=int(row["full"] or 0),
            refs=int(row["refs"] or 0),
            deltas=int(row["deltas"] or 0),
            bypassed=int(row["bypassed"] or 0),
            original_tokens=int(row["original_tokens"]),
            emitted_tokens=int(row["emitted_tokens"]),
            saved_tokens=int(row["saved_tokens"]),
        )

    def object_metadata(self, object_hash: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM objects WHERE object_hash = ?",
            (object_hash,),
        ).fetchone()

    def referenced_hashes(self) -> set[str]:
        rows = self.connection.execute(
            """
            SELECT current_hash AS object_hash FROM sources
            UNION
            SELECT object_hash FROM receipts
            """
        ).fetchall()
        return {str(row["object_hash"]) for row in rows}

    def delete_objects(self, object_hashes: Sequence[str]) -> int:
        if not object_hashes:
            return 0
        placeholders = ",".join("?" for _ in object_hashes)
        with self.transaction() as connection:
            connection.execute(
                f"DELETE FROM source_versions WHERE object_hash IN ({placeholders})",
                tuple(object_hashes),
            )
            cursor = connection.execute(
                f"DELETE FROM objects WHERE object_hash IN ({placeholders})",
                tuple(object_hashes),
            )
            return int(cursor.rowcount)
