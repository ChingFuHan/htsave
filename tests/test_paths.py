from __future__ import annotations

import os
import stat

import pytest

from htsave.cas import ContentAddressedStore
from htsave.errors import SecurityBoundaryError
from htsave.paths import build_state_paths, session_key
from htsave.registry import Registry


def test_session_paths_are_deterministic_and_isolated(tmp_path) -> None:
    first = build_state_paths("session-one", tmp_path / "state")
    repeated = build_state_paths("session-one", tmp_path / "state")
    second = build_state_paths("session-two", tmp_path / "state")

    assert first == repeated
    assert first.session_key == session_key("session-one")
    assert first.session_root != second.session_root
    assert first.database != second.database
    assert first.objects != second.objects

    first_store = ContentAddressedStore(first.objects)
    second_store = ContentAddressedStore(second.objects)
    object_hash = first_store.put(b"session-private")

    assert first_store.get(object_hash) == b"session-private"
    assert not second_store.contains(object_hash)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits only")
def test_state_directories_and_files_are_private_on_posix(tmp_path) -> None:
    root = tmp_path / "state"
    root.mkdir(mode=0o777)
    root.chmod(0o777)

    paths = build_state_paths("private-session", root)
    store = ContentAddressedStore(paths.objects)
    object_hash = store.put(b"private bytes")

    with Registry(paths.database, paths.session_key) as registry:
        registry.begin_generation("startup")
        sidecars = (
            paths.database.with_name(paths.database.name + "-wal"),
            paths.database.with_name(paths.database.name + "-shm"),
        )
        assert all(candidate.exists() for candidate in sidecars)

        for directory in (
            paths.root,
            paths.root / "sessions",
            paths.session_root,
            paths.objects,
            store.path_for(object_hash).parent,
        ):
            assert stat.S_IMODE(directory.stat().st_mode) == 0o700

        for file_path in (paths.database, store.path_for(object_hash), *sidecars):
            assert stat.S_IMODE(file_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name == "nt", reason="POSIX symbolic-link fixture")
def test_session_container_symbolic_link_is_rejected(tmp_path) -> None:
    root = tmp_path / "state"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "sessions").symlink_to(outside, target_is_directory=True)

    with pytest.raises(SecurityBoundaryError, match="symbolic-link"):
        build_state_paths("symlink-session", root)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symbolic-link fixture")
def test_existing_session_lookup_rejects_internal_symlink_redirects(tmp_path) -> None:
    root = tmp_path / "state"
    paths = build_state_paths("real-session", root)
    with Registry(paths.database, paths.session_key) as registry:
        registry.begin_generation("startup")

    sessions = root / "sessions"
    alias = sessions / ("a" * 64)
    alias.symlink_to(paths.session_root, target_is_directory=True)
    with pytest.raises(SecurityBoundaryError, match="symbolic-link"):
        from htsave.paths import find_state_paths

        find_state_paths("a" * 64, root)
