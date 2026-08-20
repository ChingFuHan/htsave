from __future__ import annotations

import os
from pathlib import Path

import pytest

from htsave.errors import SecurityBoundaryError
from htsave.workspace import WorkspaceReader


def test_reads_exact_utf8_bytes_and_line_ranges(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    target = workspace / "nested" / "source.txt"
    target.parent.mkdir()
    target.write_bytes("one\r\n雪\nlast".encode())
    reader = WorkspaceReader(workspace)

    whole = reader.read("nested/source.txt")
    subset = reader.read("nested/source.txt", start_line=2, end_line=3)

    assert whole.content == "one\r\n雪\nlast".encode()
    assert whole.text.encode() == whole.content
    assert whole.safe_label == "nested/source.txt"
    assert subset.content == "雪\nlast".encode()


@pytest.mark.parametrize("path", ["../outside.txt", "missing.txt", "", "."])
def test_rejects_escape_missing_empty_and_directory(tmp_path: Path, path: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("outside")

    with pytest.raises(SecurityBoundaryError):
        WorkspaceReader(workspace).read(path)


def test_rejects_absolute_path_outside_workspace(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")

    with pytest.raises(SecurityBoundaryError, match="escapes"):
        WorkspaceReader(workspace).read(str(outside))


def test_rejects_symlink_escape_but_allows_internal_symlink(tmp_path: Path) -> None:
    if os.name == "nt":
        pytest.skip("Windows symlink creation requires environment-specific privileges")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("outside")
    inside = workspace / "inside.txt"
    inside.write_text("inside")
    (workspace / "escape.txt").symlink_to(outside)
    (workspace / "alias.txt").symlink_to(inside)
    reader = WorkspaceReader(workspace)

    with pytest.raises(SecurityBoundaryError, match="escapes"):
        reader.read("escape.txt")
    assert reader.read("alias.txt").text == "inside"


def test_rejects_special_file_and_invalid_utf8(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    invalid = workspace / "invalid.bin"
    invalid.write_bytes(b"\xff")
    reader = WorkspaceReader(workspace)

    with pytest.raises(SecurityBoundaryError, match="UTF-8"):
        reader.read("invalid.bin")

    if hasattr(os, "mkfifo"):
        fifo = workspace / "pipe"
        os.mkfifo(fifo)
        with pytest.raises(SecurityBoundaryError, match="regular file"):
            reader.read("pipe")


@pytest.mark.parametrize(
    ("start", "end"),
    [(0, None), (True, None), (3, 2), (99, None), (None, 0)],
)
def test_rejects_invalid_line_ranges(tmp_path: Path, start: int | None, end: int | None) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "file.txt").write_text("one\ntwo\n")

    with pytest.raises(SecurityBoundaryError):
        WorkspaceReader(workspace).read("file.txt", start_line=start, end_line=end)
