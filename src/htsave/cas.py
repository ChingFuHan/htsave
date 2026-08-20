"""Immutable content-addressed object storage."""

from __future__ import annotations

import errno
import os
import re
import stat
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

from .errors import CorruptObjectError, SecurityBoundaryError
from .hashing import HASH_PREFIX, sha256_id, verify_sha256
from .paths import ensure_private_directory, ensure_private_file

_OBJECT_ID = re.compile(r"^sha256:([0-9a-f]{64})$")
_TEMPORARY = re.compile(r"^\.htsave-([0-9a-f]{64})-[^/]+$")
_INCOMPLETE_ORPHAN_GRACE_SECONDS = 24 * 60 * 60


class ContentAddressedStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        ensure_private_directory(root)
        self._recover_temporaries()

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            try:
                os.fsync(descriptor)
            except OSError as exc:
                if exc.errno not in (errno.EINVAL, errno.ENOTSUP):
                    raise
        finally:
            os.close(descriptor)

    def _publish(
        self,
        temporary: Path,
        destination: Path,
        object_hash: str,
    ) -> None:
        if destination.is_symlink():
            raise SecurityBoundaryError(f"CAS object must not be a symbolic link: {destination}")
        if destination.exists():
            if not destination.is_file():
                raise CorruptObjectError(f"CAS object is not a regular file: {object_hash}")
            verify_sha256(destination.read_bytes(), object_hash)
            temporary.unlink(missing_ok=True)
            return

        # A concurrent recovery or equal writer may already have published this
        # temporary. The destination remains the source of truth.  Recovery can
        # race the writer between the existence check above and ``os.replace``;
        # in that case the temporary disappears while the destination is now
        # valid, so re-check it instead of leaking a transient ENOENT.
        try:
            os.replace(temporary, destination)
        except FileNotFoundError:
            if not destination.exists():
                raise
        ensure_private_file(destination)
        verify_sha256(destination.read_bytes(), object_hash)
        self._sync_directory(destination.parent)

    def _recover_temporaries(self) -> None:
        cutoff = time.time() - _INCOMPLETE_ORPHAN_GRACE_SECONDS
        for prefix in self.root.iterdir():
            if prefix.is_symlink():
                raise SecurityBoundaryError(f"CAS prefix must not be a symbolic link: {prefix}")
            if not prefix.is_dir() or len(prefix.name) != 2:
                continue
            for temporary in prefix.glob(".htsave-*"):
                try:
                    metadata = temporary.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if not stat.S_ISREG(metadata.st_mode):
                    continue

                match = _TEMPORARY.fullmatch(temporary.name)
                if match is not None and match.group(1).startswith(prefix.name):
                    object_hash = f"{HASH_PREFIX}{match.group(1)}"
                    try:
                        verify_sha256(temporary.read_bytes(), object_hash)
                    except (CorruptObjectError, FileNotFoundError):
                        pass
                    else:
                        self._publish(temporary, self.path_for(object_hash), object_hash)
                        continue

                if metadata.st_mtime <= cutoff:
                    temporary.unlink(missing_ok=True)

    def path_for(self, object_hash: str) -> Path:
        match = _OBJECT_ID.fullmatch(object_hash)
        if match is None:
            raise ValueError("invalid SHA-256 object id")
        digest = match.group(1)
        return self.root / digest[:2] / digest[2:]

    def put(self, content: bytes) -> str:
        object_hash = sha256_id(content)
        destination = self.path_for(object_hash)
        ensure_private_directory(destination.parent)
        if destination.is_symlink():
            raise SecurityBoundaryError(f"CAS object must not be a symbolic link: {destination}")
        if destination.exists():
            if not destination.is_file():
                raise CorruptObjectError(f"CAS object is not a regular file: {object_hash}")
            verify_sha256(destination.read_bytes(), object_hash)
            return object_hash

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".htsave-{object_hash.removeprefix(HASH_PREFIX)}-",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            self._publish(temporary, destination, object_hash)
        finally:
            temporary.unlink(missing_ok=True)

        return object_hash

    def get(self, object_hash: str) -> bytes:
        path = self.path_for(object_hash)
        if path.parent.is_symlink():
            raise SecurityBoundaryError(f"CAS prefix must not be a symbolic link: {path.parent}")
        if path.is_symlink():
            raise SecurityBoundaryError(f"CAS object must not be a symbolic link: {path}")
        try:
            content = path.read_bytes()
        except (FileNotFoundError, IsADirectoryError) as exc:
            raise CorruptObjectError(f"missing CAS object: {object_hash}") from exc
        verify_sha256(content, object_hash)
        return content

    def contains(self, object_hash: str) -> bool:
        try:
            self.get(object_hash)
        except CorruptObjectError:
            return False
        return True

    def iter_hashes(self) -> Iterator[str]:
        if not self.root.exists():
            return
        for prefix in sorted(self.root.iterdir()):
            if prefix.is_symlink():
                raise SecurityBoundaryError(f"CAS prefix must not be a symbolic link: {prefix}")
            if not prefix.is_dir() or len(prefix.name) != 2:
                continue
            for item in sorted(prefix.iterdir()):
                if item.is_symlink():
                    raise SecurityBoundaryError(f"CAS object must not be a symbolic link: {item}")
                candidate = f"{HASH_PREFIX}{prefix.name}{item.name}"
                if item.is_file() and _OBJECT_ID.fullmatch(candidate):
                    yield candidate

    def delete(self, object_hash: str) -> None:
        path = self.path_for(object_hash)
        if path.parent.is_symlink() or path.is_symlink():
            raise SecurityBoundaryError("refusing to delete a symbolic-link CAS path")
        path.unlink(missing_ok=True)
