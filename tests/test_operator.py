from __future__ import annotations

from pathlib import Path

import pytest

from htsave.cas import ContentAddressedStore
from htsave.engine import ContextEngine
from htsave.errors import SecurityBoundaryError
from htsave.operator import (
    all_session_summaries,
    apply_gc,
    clear_sessions,
    gc_candidates,
    inspect_session,
    list_session_keys,
)
from htsave.paths import build_state_paths
from htsave.registry import Registry
from htsave.tokens import TokenEstimator


def _seed(tmp_path: Path, session_id: str = "session-one") -> tuple[Path, str]:
    root = tmp_path / "state"
    paths = build_state_paths(session_id, root)
    engine = ContextEngine(
        ContentAddressedStore(paths.objects),
        Registry(paths.database, paths.session_key),
        TokenEstimator("gpt-5"),
    )
    try:
        engine.start_generation("startup")
        engine.decide(
            text="registered context\n" * 200,
            source_fingerprint="source",
            safe_label="fixture",
            tool_use_id="tool-1",
        )
    finally:
        engine.registry.close()
    return root, paths.session_key


def test_summary_and_inspect_are_session_scoped(tmp_path: Path) -> None:
    root, key = _seed(tmp_path)

    assert list_session_keys(root) == (key,)
    summaries = all_session_summaries(root)
    assert summaries[0].stats.full == 1
    details = inspect_session(key, root)
    assert len(details["generations"]) == 1
    assert details["events"][0]["tool_use_id"] == "tool-1"


def test_gc_is_explicit_and_rechecks_references(tmp_path: Path) -> None:
    root, key = _seed(tmp_path)
    paths = build_state_paths("session-one", root)
    cas = ContentAddressedStore(paths.objects)
    orphan_hash = cas.put(b"crash orphan")

    candidates = gc_candidates(root)
    assert [(item.session_key, item.object_hash) for item in candidates] == [(key, orphan_hash)]
    assert cas.contains(orphan_hash)

    deleted = apply_gc(candidates, root)
    assert deleted == candidates
    assert not cas.contains(orphan_hash)


def test_clear_targets_only_explicit_session_and_preserves_root(tmp_path: Path) -> None:
    root, first = _seed(tmp_path, "session-one")
    _, second = _seed(tmp_path, "session-two")

    assert clear_sessions((first,), state_root=root) == (first,)
    assert list_session_keys(root) == (second,)
    assert root.exists()


def test_session_symlink_is_rejected(tmp_path: Path) -> None:
    root = tmp_path / "state"
    sessions = root / "sessions"
    sessions.mkdir(parents=True)
    target = tmp_path / "outside"
    target.mkdir()
    symlink = sessions / ("a" * 64)
    try:
        symlink.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available")

    with pytest.raises(SecurityBoundaryError, match="symbolic"):
        list_session_keys(root)


def test_configured_state_root_rejects_root_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation is not available")

    with pytest.raises(SecurityBoundaryError, match="symbolic"):
        list_session_keys(alias)
