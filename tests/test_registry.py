from __future__ import annotations

import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace

import pytest

import htsave.registry as registry_module
from htsave.errors import CorruptObjectError
from htsave.hashing import sha256_id
from htsave.models import ContentObject, DeliveryMode
from htsave.paths import build_state_paths
from htsave.registry import SCHEMA_VERSION, Registry


def content_object(content: bytes) -> ContentObject:
    return ContentObject(
        object_hash=sha256_id(content),
        codec="raw-utf8",
        byte_size=len(content),
        estimated_tokens=max(1, len(content) // 4),
    )


def delivery_args(
    *,
    generation: int,
    target: ContentObject,
    tool_use_id: str = "tool-1",
    source_fingerprint: str = "source-a",
    mode: DeliveryMode = DeliveryMode.FULL,
) -> dict[str, object]:
    return {
        "generation": generation,
        "tool_use_id": tool_use_id,
        "source_fingerprint": source_fingerprint,
        "safe_label": "safe-label",
        "target_hash": target.object_hash,
        "base_hash": None,
        "mode": mode,
        "original_tokens": 100,
        "emitted_tokens": 80,
        "latency_ms": 1.25,
        "delta_depth": 0,
        "cumulative_delta_tokens": 0,
        "bypass_reason": None,
    }


def downgrade_registry_to_v1(database) -> None:  # type: ignore[no-untyped-def]
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TABLE compact_recovery")
        connection.execute("DROP TABLE capabilities")
        connection.execute("DROP TABLE active_agents")
        connection.execute("ALTER TABLE generations DROP COLUMN frozen")
        connection.execute("ALTER TABLE generations DROP COLUMN model")
        connection.execute("DROP TABLE registry_metadata")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 2")


def downgrade_registry_to_v2(database) -> None:  # type: ignore[no-untyped-def]
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("DROP TABLE compact_recovery")
        connection.execute("DROP TABLE capabilities")
        connection.execute("DROP TABLE active_agents")
        connection.execute("ALTER TABLE generations DROP COLUMN frozen")
        connection.execute("ALTER TABLE generations DROP COLUMN model")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 3")


def downgrade_registry_to_v3(database) -> None:  # type: ignore[no-untyped-def]
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("ALTER TABLE generations DROP COLUMN model")
        connection.execute("DELETE FROM schema_migrations WHERE version >= 4")


def test_schema_migration_is_idempotent_and_preserves_data(tmp_path) -> None:
    paths = build_state_paths("migration-session", tmp_path / "state")
    target = content_object(b"preserved")

    with Registry(paths.database, paths.session_key) as registry:
        assert registry.schema_version() == SCHEMA_VERSION
        registry.record_object(target)

    with Registry(paths.database, paths.session_key) as reopened:
        assert reopened.schema_version() == SCHEMA_VERSION
        assert reopened.object_metadata(target.object_hash) is not None
        versions = reopened.connection.execute(
            "SELECT version FROM schema_migrations ORDER BY version"
        ).fetchall()
        assert [int(row["version"]) for row in versions] == list(range(1, SCHEMA_VERSION + 1))


def test_legacy_schema_migrates_forward_and_binds_existing_session(tmp_path) -> None:
    paths = build_state_paths("legacy-session", tmp_path / "state")
    with Registry(paths.database, paths.session_key) as registry:
        generation = registry.begin_generation("legacy-startup")

    downgrade_registry_to_v1(paths.database)

    with Registry(paths.database, paths.session_key) as migrated:
        assert migrated.schema_version() == SCHEMA_VERSION
        assert migrated.active_generation() == generation
        assert (
            migrated.connection.execute(
                "SELECT value FROM registry_metadata WHERE key = 'session_key'"
            ).fetchone()[0]
            == paths.session_key
        )

    with pytest.raises(CorruptObjectError, match="different Codex session"):
        Registry(paths.database, "wrong-session")


def test_failed_migration_rolls_back_and_can_be_retried(tmp_path, monkeypatch) -> None:
    paths = build_state_paths("retry-migration", tmp_path / "state")
    with Registry(paths.database, paths.session_key):
        pass
    downgrade_registry_to_v1(paths.database)

    migration_2 = registry_module._MIGRATION_2
    monkeypatch.setattr(
        registry_module,
        "_MIGRATION_2",
        (*migration_2, "THIS IS NOT VALID SQLITE"),
    )
    with pytest.raises(sqlite3.OperationalError):
        Registry(paths.database, paths.session_key)

    with closing(sqlite3.connect(paths.database)) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'registry_metadata'"
            ).fetchone()
            is None
        )

    monkeypatch.setattr(registry_module, "_MIGRATION_2", migration_2)
    with Registry(paths.database, paths.session_key) as recovered:
        assert recovered.schema_version() == SCHEMA_VERSION
        assert recovered.integrity_check() == "ok"


def test_lifecycle_migration_rolls_back_and_can_be_retried(tmp_path, monkeypatch) -> None:
    paths = build_state_paths("retry-lifecycle-migration", tmp_path / "state")
    with Registry(paths.database, paths.session_key):
        pass
    downgrade_registry_to_v2(paths.database)

    migration_3 = registry_module._MIGRATION_3
    monkeypatch.setattr(
        registry_module,
        "_MIGRATION_3",
        (*migration_3, "THIS IS NOT VALID SQLITE"),
    )
    with pytest.raises(sqlite3.OperationalError):
        Registry(paths.database, paths.session_key)

    with closing(sqlite3.connect(paths.database)) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 2
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'capabilities'"
            ).fetchone()
            is None
        )
        columns = connection.execute("PRAGMA table_info(generations)").fetchall()
        assert "frozen" not in {str(column[1]) for column in columns}

    monkeypatch.setattr(registry_module, "_MIGRATION_3", migration_3)
    with Registry(paths.database, paths.session_key) as recovered:
        assert recovered.schema_version() == SCHEMA_VERSION
        assert recovered.integrity_check() == "ok"


def test_newer_schema_is_rejected_without_mutating_it(tmp_path) -> None:
    paths = build_state_paths("future-session", tmp_path / "state")
    future = SCHEMA_VERSION + 1
    with closing(sqlite3.connect(paths.database)) as connection, connection:
        connection.execute(
            "CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO schema_migrations(version, applied_at) VALUES (?, 'future')",
            (future,),
        )

    with pytest.raises(CorruptObjectError, match="newer than supported"):
        Registry(paths.database, paths.session_key)

    with closing(sqlite3.connect(paths.database)) as connection:
        assert (
            connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == future
        )
        assert (
            connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'objects'"
            ).fetchone()
            is None
        )


def test_registry_uses_wal_full_sync_foreign_keys_and_passes_integrity_check(tmp_path) -> None:
    paths = build_state_paths("durable-session", tmp_path / "state")

    with Registry(paths.database, paths.session_key) as registry:
        assert registry.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert registry.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert registry.connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert registry.integrity_check() == "ok"


def test_database_is_bound_to_one_session_key(tmp_path) -> None:
    paths = build_state_paths("bound-session", tmp_path / "state")
    with Registry(paths.database, paths.session_key) as registry:
        registry.begin_generation("startup")

    with pytest.raises(CorruptObjectError, match="session"):
        Registry(paths.database, "different-session-key")


def test_object_retry_rejects_identity_metadata_conflicts(tmp_path) -> None:
    paths = build_state_paths("object-retry", tmp_path / "state")
    target = content_object(b"same hash")

    with Registry(paths.database, paths.session_key) as registry:
        registry.record_object(target)
        registry.record_object(replace(target, estimated_tokens=target.estimated_tokens + 5))

        metadata = registry.object_metadata(target.object_hash)
        assert metadata is not None
        assert int(metadata["estimated_tokens"]) == target.estimated_tokens

        for conflict in (
            replace(target, codec="different-codec"),
            replace(target, byte_size=target.byte_size + 1),
        ):
            with pytest.raises(CorruptObjectError, match="metadata"):
                registry.record_object(conflict)


def test_registry_rejects_noncanonical_object_ids_and_delivery_metadata(tmp_path) -> None:
    paths = build_state_paths("registry-validation", tmp_path / "state")
    with Registry(paths.database, paths.session_key) as registry:
        with pytest.raises(CorruptObjectError, match="canonical SHA-256"):
            registry.record_object(
                ContentObject(
                    object_hash="not-an-object-id",
                    codec="raw-utf8",
                    byte_size=0,
                    estimated_tokens=0,
                )
            )

        target = content_object(b"validated")
        registry.record_object(target)
        generation = registry.begin_generation("startup")
        with pytest.raises(CorruptObjectError, match="latency"):
            registry.record_delivery(
                **(
                    delivery_args(generation=generation, target=target)
                    | {"latency_ms": float("inf")}
                )
            )


def test_separate_session_registries_share_no_state(tmp_path) -> None:
    first_paths = build_state_paths("first-session", tmp_path / "state")
    second_paths = build_state_paths("second-session", tmp_path / "state")
    target = content_object(b"session private")

    with Registry(first_paths.database, first_paths.session_key) as first:
        first.record_object(target)
        first_generation = first.begin_generation("startup")
        first.record_delivery(**delivery_args(generation=first_generation, target=target))
        first.confirm_pending()

    with Registry(second_paths.database, second_paths.session_key) as second:
        assert second.object_metadata(target.object_hash) is None
        assert second.active_generation() is None
        assert second.pending_receipts() == ()
        assert second.stats().events == 0


def test_receipts_are_pending_until_confirmation_and_generation_isolated(tmp_path) -> None:
    paths = build_state_paths("receipt-session", tmp_path / "state")
    target = content_object(b"target")

    with Registry(paths.database, paths.session_key) as registry:
        registry.record_object(target)
        first_generation = registry.begin_generation("startup")
        registry.record_delivery(**delivery_args(generation=first_generation, target=target))

        assert not registry.confirmed_object_exists(first_generation, target.object_hash)
        assert [receipt.tool_use_id for receipt in registry.pending_receipts()] == ["tool-1"]
        assert registry.confirm_pending() == 1
        assert registry.confirm_pending() == 0
        assert registry.confirmed_object_exists(first_generation, target.object_hash)
        assert (
            registry.confirmed_head(first_generation, "source-a").object_hash == target.object_hash
        )

        next_generation = registry.begin_generation("compact")
        assert registry.active_generation() == next_generation
        assert not registry.confirmed_object_exists(next_generation, target.object_hash)
        assert registry.confirmed_head(next_generation, "source-a") is None
        assert registry.pending_receipts() == ()


def test_confirmation_only_affects_the_selected_generation(tmp_path) -> None:
    paths = build_state_paths("selective-confirm", tmp_path / "state")
    target = content_object(b"same target")

    with Registry(paths.database, paths.session_key) as registry:
        registry.record_object(target)
        first = registry.begin_generation("startup")
        registry.record_delivery(
            **delivery_args(generation=first, target=target, tool_use_id="one")
        )
        second = registry.begin_generation("resume")
        registry.record_delivery(
            **delivery_args(generation=second, target=target, tool_use_id="two")
        )

        assert registry.confirm_pending(second) == 1
        assert [receipt.tool_use_id for receipt in registry.pending_receipts(first)] == ["one"]
        assert not registry.confirmed_object_exists(first, target.object_hash)
        assert registry.confirmed_object_exists(second, target.object_hash)


def test_compaction_freezes_generation_and_queues_only_transformed_pending(tmp_path) -> None:
    paths = build_state_paths("compact-session", tmp_path / "state")
    full = content_object(b"full")
    transformed = content_object(b"transformed")

    with Registry(paths.database, paths.session_key) as registry:
        registry.record_object(full)
        registry.record_object(transformed)
        generation = registry.begin_generation("startup")
        registry.record_delivery(
            **delivery_args(generation=generation, target=full, tool_use_id="full")
        )
        registry.record_delivery(
            **delivery_args(
                generation=generation,
                target=transformed,
                tool_use_id="ref",
                source_fingerprint="source-ref",
                mode=DeliveryMode.REF,
            )
        )

        queued = registry.prepare_compaction()
        assert registry.active_is_frozen()
        assert [item.tool_use_id for item in queued] == ["ref"]
        assert registry.prepare_compaction() == queued

        next_generation = registry.begin_generation("compact")
        assert registry.recovery_targets(generation) == queued
        registry.mark_recovery_consumed(queued[0].recovery_id, next_generation)
        assert registry.recovery_targets(generation) == ()


def test_frozen_generation_accepts_only_fail_open_bypass(tmp_path) -> None:
    paths = build_state_paths("frozen-session", tmp_path / "state")
    target = content_object(b"target")
    with Registry(paths.database, paths.session_key) as registry:
        registry.record_object(target)
        generation = registry.begin_generation("startup")
        registry.freeze_active_generation()

        with pytest.raises(CorruptObjectError, match="only BYPASS"):
            registry.record_delivery(**delivery_args(generation=generation, target=target))

        registry.record_delivery(
            **delivery_args(
                generation=generation,
                target=target,
                mode=DeliveryMode.BYPASS,
            )
        )
        assert registry.stats().bypassed == 1


def test_subagent_tracking_is_idempotent_and_generation_safe(tmp_path) -> None:
    paths = build_state_paths("agent-session", tmp_path / "state")
    with Registry(paths.database, paths.session_key) as registry:
        first = registry.begin_generation("startup")
        assert registry.subagent_started("agent-1", "investigator") == 1
        assert registry.subagent_started("agent-1", "investigator") == 1
        assert registry.active_is_ambiguous()
        with pytest.raises(CorruptObjectError, match="agent type"):
            registry.subagent_started("agent-1", "builder")

        assert registry.subagent_stopped("unknown") == 1
        assert registry.subagent_stopped("agent-1") == 0
        second = registry.begin_generation("subagents-finished")
        assert second != first
        assert not registry.active_is_ambiguous()


def test_delivery_retry_is_idempotent_only_for_the_same_semantics(tmp_path) -> None:
    paths = build_state_paths("retry-session", tmp_path / "state")
    target = content_object(b"target")

    with Registry(paths.database, paths.session_key) as registry:
        registry.record_object(target)
        generation = registry.begin_generation("startup")
        original = delivery_args(generation=generation, target=target)
        registry.record_delivery(**original)
        registry.record_delivery(**original)

        assert registry.stats().events == 1
        assert len(registry.pending_receipts()) == 1
        version = registry.connection.execute(
            "SELECT version FROM sources WHERE source_fingerprint = 'source-a'"
        ).fetchone()[0]
        assert version == 1

        semantic_conflicts = (
            {"source_fingerprint": "source-b"},
            {"base_hash": sha256_id(b"different-base")},
            {"original_tokens": 101},
            {"emitted_tokens": 79},
            {"delta_depth": 1},
            {"cumulative_delta_tokens": 5},
            {"bypass_reason": "changed"},
        )
        for changes in semantic_conflicts:
            conflicting = original | changes
            with pytest.raises(CorruptObjectError, match="retry changed"):
                registry.record_delivery(**conflicting)

        assert registry.stats().events == 1
        assert len(registry.pending_receipts()) == 1


def test_concurrent_delivery_retries_commit_exactly_once(tmp_path) -> None:
    paths = build_state_paths("concurrent-session", tmp_path / "state")
    target = content_object(b"concurrent target")
    with Registry(paths.database, paths.session_key) as registry:
        registry.record_object(target)
        generation = registry.begin_generation("startup")

    def retry(_: int) -> None:
        with Registry(paths.database, paths.session_key) as registry:
            registry.record_delivery(**delivery_args(generation=generation, target=target))

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(retry, range(24)))

    with Registry(paths.database, paths.session_key) as registry:
        assert registry.stats().events == 1
        assert len(registry.pending_receipts()) == 1
        assert registry.integrity_check() == "ok"


def test_delivery_failure_rolls_back_source_event_and_receipt(tmp_path) -> None:
    paths = build_state_paths("rollback-session", tmp_path / "state")
    target = content_object(b"target")

    with Registry(paths.database, paths.session_key) as registry:
        registry.record_object(target)
        generation = registry.begin_generation("startup")
        registry.connection.execute(
            """
            CREATE TRIGGER fail_receipt BEFORE INSERT ON receipts
            WHEN NEW.tool_use_id = 'crash-me'
            BEGIN
                SELECT RAISE(ABORT, 'simulated crash');
            END
            """
        )

        with pytest.raises(sqlite3.IntegrityError, match="simulated crash"):
            registry.record_delivery(
                **delivery_args(generation=generation, target=target, tool_use_id="crash-me")
            )

        assert registry.connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 0
        assert (
            registry.connection.execute("SELECT COUNT(*) FROM source_versions").fetchone()[0] == 0
        )
        assert registry.connection.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
        assert registry.connection.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
        assert registry.integrity_check() == "ok"


def test_abrupt_process_exit_rolls_back_an_open_transaction(tmp_path) -> None:
    paths = build_state_paths("process-crash", tmp_path / "state")
    target = content_object(b"never committed")
    with Registry(paths.database, paths.session_key):
        pass

    child_code = """
import os
import sqlite3
import sys

database, object_hash = sys.argv[1:]
connection = sqlite3.connect(database, isolation_level=None)
connection.execute('PRAGMA journal_mode = WAL')
connection.execute('BEGIN IMMEDIATE')
connection.execute(
    '''INSERT INTO objects(object_hash, codec, byte_size, estimated_tokens, created_at)
       VALUES (?, 'raw-utf8', 15, 4, 'crash')''',
    (object_hash,),
)
os._exit(17)
"""
    crashed = subprocess.run(
        [sys.executable, "-c", child_code, str(paths.database), target.object_hash],
        check=False,
    )
    assert crashed.returncode == 17

    with Registry(paths.database, paths.session_key) as recovered:
        assert recovered.object_metadata(target.object_hash) is None
        assert recovered.integrity_check() == "ok"


def test_session_model_migration_preserves_generations_and_rolls_back(
    tmp_path, monkeypatch
) -> None:
    paths = build_state_paths("retry-model-migration", tmp_path / "state")
    with Registry(paths.database, paths.session_key) as registry:
        registry.begin_generation("startup", model="claude-opus-5")
        assert registry.active_model() == "claude-opus-5"
    downgrade_registry_to_v3(paths.database)

    migration_4 = registry_module._MIGRATION_4
    monkeypatch.setattr(
        registry_module,
        "_MIGRATION_4",
        (*migration_4, "THIS IS NOT VALID SQLITE"),
    )
    with pytest.raises(sqlite3.OperationalError):
        Registry(paths.database, paths.session_key)

    with closing(sqlite3.connect(paths.database)) as connection:
        assert connection.execute("SELECT MAX(version) FROM schema_migrations").fetchone()[0] == 3
        columns = connection.execute("PRAGMA table_info(generations)").fetchall()
        assert "model" not in {str(column[1]) for column in columns}
        assert connection.execute("SELECT COUNT(*) FROM generations").fetchone()[0] == 1

    monkeypatch.setattr(registry_module, "_MIGRATION_4", migration_4)
    with Registry(paths.database, paths.session_key) as recovered:
        assert recovered.schema_version() == SCHEMA_VERSION
        assert recovered.integrity_check() == "ok"
        # The pre-migration generation survives; its model is simply unknown.
        assert recovered.active_model() is None


def test_new_generation_carries_the_session_model_forward(tmp_path) -> None:
    paths = build_state_paths("model-carry-forward", tmp_path / "state")
    with Registry(paths.database, paths.session_key) as registry:
        registry.begin_generation("startup", model="claude-opus-5")
        registry.begin_generation("compact")

        assert registry.active_model() == "claude-opus-5"

        registry.begin_generation("resume", model="claude-sonnet-5")
        assert registry.active_model() == "claude-sonnet-5"
