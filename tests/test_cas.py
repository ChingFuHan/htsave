from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor

import pytest

from htsave.cas import ContentAddressedStore
from htsave.errors import CorruptObjectError, SecurityBoundaryError
from htsave.hashing import sha256_id


def test_cas_round_trips_raw_bytes_without_normalization(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path / "objects")
    contents = (b"line one\r\nline two\n", b"line one\nline two\n", b"\x00\xff")

    object_hashes = [store.put(content) for content in contents]

    assert len(set(object_hashes)) == len(contents)
    assert [store.get(object_hash) for object_hash in object_hashes] == list(contents)
    assert set(store.iter_hashes()) == set(object_hashes)


def test_cas_rejects_a_corrupt_existing_object(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path / "objects")
    content = b"immutable"
    object_hash = store.put(content)
    store.path_for(object_hash).write_bytes(b"tampered")

    with pytest.raises(CorruptObjectError, match="content hash mismatch"):
        store.get(object_hash)
    assert not store.contains(object_hash)

    with pytest.raises(CorruptObjectError, match="content hash mismatch"):
        store.put(content)


def test_concurrent_equal_writes_publish_one_verified_object(tmp_path) -> None:
    root = tmp_path / "objects"
    payload = os.urandom(256 * 1024)

    def write_once(_: int) -> str:
        return ContentAddressedStore(root).put(payload)

    with ThreadPoolExecutor(max_workers=12) as executor:
        results = list(executor.map(write_once, range(48)))

    store = ContentAddressedStore(root)
    expected = sha256_id(payload)
    assert set(results) == {expected}
    assert store.get(expected) == payload
    assert list(store.iter_hashes()) == [expected]
    assert not list(root.rglob(".htsave-*"))


def test_writer_tolerates_recovery_publishing_its_completed_temporary(
    tmp_path, monkeypatch
) -> None:
    root = tmp_path / "objects"
    store = ContentAddressedStore(root)
    payload = b"recovery races the writer after the temporary is complete"
    original_publish = ContentAddressedStore._publish
    recovered = False

    def publish_with_recovery(self, temporary, destination, object_hash):
        nonlocal recovered
        if not recovered:
            recovered = True
            ContentAddressedStore(self.root)
        return original_publish(self, temporary, destination, object_hash)

    monkeypatch.setattr(ContentAddressedStore, "_publish", publish_with_recovery)

    object_hash = store.put(payload)

    assert ContentAddressedStore(root).get(object_hash) == payload
    assert not list(root.rglob(".htsave-*"))


def test_complete_orphan_temporary_is_recovered_atomically(tmp_path) -> None:
    root = tmp_path / "objects"
    store = ContentAddressedStore(root)
    payload = b"fully written before a simulated process crash"
    object_hash = sha256_id(payload)
    destination = store.path_for(object_hash)
    destination.parent.mkdir(parents=True, exist_ok=True)
    orphan = destination.parent / f".htsave-{object_hash.removeprefix('sha256:')}-orphan"
    orphan.write_bytes(payload)

    recovered = ContentAddressedStore(root)

    assert recovered.get(object_hash) == payload
    assert not orphan.exists()


def test_stale_incomplete_orphan_temporary_is_removed(tmp_path) -> None:
    root = tmp_path / "objects"
    store = ContentAddressedStore(root)
    object_hash = sha256_id(b"expected complete bytes")
    destination = store.path_for(object_hash)
    destination.parent.mkdir(parents=True, exist_ok=True)
    orphan = destination.parent / f".htsave-{object_hash.removeprefix('sha256:')}-orphan"
    orphan.write_bytes(b"partial")
    os.utime(orphan, (0, 0))

    recovered = ContentAddressedStore(root)

    assert not orphan.exists()
    assert not recovered.contains(object_hash)


def test_invalid_object_ids_cannot_escape_the_cas_root(tmp_path) -> None:
    store = ContentAddressedStore(tmp_path / "objects")

    for candidate in ("", "sha256:abc", "../outside", "sha256:" + "A" * 64):
        with pytest.raises(ValueError, match="invalid SHA-256 object id"):
            store.path_for(candidate)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symbolic-link fixture")
def test_cas_rejects_symbolic_linked_prefix_and_object(tmp_path) -> None:
    root = tmp_path / "objects"
    store = ContentAddressedStore(root)
    content = b"symlink-protected"
    object_hash = sha256_id(content)
    prefix = store.path_for(object_hash).parent
    prefix.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_bytes(content)
    store.path_for(object_hash).symlink_to(outside)

    with pytest.raises(SecurityBoundaryError, match="symbolic link"):
        store.get(object_hash)
    with pytest.raises(SecurityBoundaryError, match="symbolic link"):
        store.put(content)
