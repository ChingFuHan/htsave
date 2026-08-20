"""Cross-platform, session-isolated state paths and permissions."""

from __future__ import annotations

import getpass
import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path

from .errors import SecurityBoundaryError

_SESSION_KEY = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class StatePaths:
    root: Path
    session_root: Path
    objects: Path
    database: Path
    session_key: str


def default_state_root() -> Path:
    """Resolve the state root every entry point must agree on.

    Hooks, the MCP server, and the CLI all land here, so ``HTSAVE_STATE_DIR``
    has to be honored in one place: otherwise a redirected MCP server and a
    default-rooted hook would split one session across two registries.
    """

    configured = os.environ.get("HTSAVE_STATE_DIR")
    if configured:
        return Path(configured).expanduser()
    return Path(user_data_path("htsave", appauthor=False, roaming=False))


def session_key(session_id: str) -> str:
    if not session_id:
        raise SecurityBoundaryError("session id must not be empty")
    return hashlib.sha256(session_id.encode("utf-8")).hexdigest()


def _secure_windows_directory(path: Path) -> None:
    user = getpass.getuser()
    if not user:
        raise SecurityBoundaryError("could not determine current Windows user")
    result = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{user}:(OI)(CI)F",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise SecurityBoundaryError(
            f"could not restrict Windows state ACL: {result.stderr.strip()}"
        )


def ensure_private_directory(path: Path) -> None:
    if path.is_symlink():
        raise SecurityBoundaryError(f"refusing symbolic-link state directory: {path}")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "nt":
        _secure_windows_directory(path)
    else:
        os.chmod(path, 0o700)


def ensure_private_file(path: Path) -> None:
    if path.is_symlink():
        raise SecurityBoundaryError(f"refusing symbolic-link state file: {path}")
    if os.name != "nt" and path.exists():
        os.chmod(path, 0o600)


def build_state_paths(session_id: str, root: Path | None = None) -> StatePaths:
    requested_root = (root or default_state_root()).expanduser()
    # Canonicalising a configured root is useful for stable diagnostics, but
    # following a symlink at the trust boundary would let an attacker redirect
    # all session state elsewhere.  Parent-directory symlinks are allowed (the
    # caller may intentionally place state below a symlinked home), while the
    # configured state directory itself is not.
    if requested_root.is_symlink():
        raise SecurityBoundaryError("refusing symbolic-link state root")
    state_root = requested_root.resolve()
    key = session_key(session_id)
    session_root = state_root / "sessions" / key
    objects = session_root / "objects"
    ensure_private_directory(state_root)
    ensure_private_directory(state_root / "sessions")
    ensure_private_directory(session_root)
    ensure_private_directory(objects)
    return StatePaths(
        root=state_root,
        session_root=session_root,
        objects=objects,
        database=session_root / "registry.sqlite3",
        session_key=key,
    )


def find_state_paths(session_key_value: str, root: Path | None = None) -> StatePaths:
    """Resolve an existing session directory without creating attacker-chosen state."""

    if _SESSION_KEY.fullmatch(session_key_value) is None:
        raise SecurityBoundaryError("invalid htsave session key")
    requested_root = (root or default_state_root()).expanduser()
    if requested_root.is_symlink():
        raise SecurityBoundaryError("refusing symbolic-link state root")
    state_root = requested_root.resolve()
    sessions_path = state_root / "sessions"
    if sessions_path.is_symlink():
        raise SecurityBoundaryError("refusing symbolic-link sessions directory")
    sessions_root = sessions_path.resolve(strict=True)
    try:
        sessions_root.relative_to(state_root)
    except ValueError as exc:
        raise SecurityBoundaryError("htsave sessions directory escaped its state root") from exc
    session_path = sessions_root / session_key_value
    if session_path.is_symlink():
        raise SecurityBoundaryError("refusing symbolic-link session directory")
    session_root = session_path.resolve(strict=True)
    try:
        session_root.relative_to(sessions_root)
    except ValueError as exc:
        raise SecurityBoundaryError("htsave session directory escaped its state root") from exc
    objects_path = session_root / "objects"
    database_path = session_root / "registry.sqlite3"
    if objects_path.is_symlink():
        raise SecurityBoundaryError("refusing symbolic-link CAS directory")
    if database_path.is_symlink():
        raise SecurityBoundaryError("refusing symbolic-link registry database")
    objects = objects_path.resolve(strict=True)
    database = database_path.resolve(strict=True)
    try:
        objects.relative_to(session_root)
        database.relative_to(session_root)
    except ValueError as exc:
        raise SecurityBoundaryError("htsave session files escaped their session directory") from exc
    if not objects.is_dir() or not database.is_file():
        raise SecurityBoundaryError("htsave session state is incomplete")
    ensure_private_directory(sessions_root)
    ensure_private_directory(session_root)
    ensure_private_directory(objects)
    ensure_private_file(database)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = database.with_name(database.name + suffix)
        if sidecar.is_symlink():
            raise SecurityBoundaryError("refusing symbolic-link registry sidecar")
        if sidecar.exists():
            ensure_private_file(sidecar)
    return StatePaths(
        root=state_root,
        session_root=session_root,
        objects=objects,
        database=database,
        session_key=session_key_value,
    )
