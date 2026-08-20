"""Workspace-contained, symlink-safe-enough text reads for the MCP boundary."""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import SecurityBoundaryError


@dataclass(frozen=True, slots=True)
class WorkspaceText:
    text: str
    content: bytes
    resolved_path: Path
    safe_label: str


class WorkspaceReader:
    def __init__(self, workspace: Path) -> None:
        try:
            self.workspace = workspace.expanduser().resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise SecurityBoundaryError("workspace does not resolve safely") from exc
        if not self.workspace.is_dir():
            raise SecurityBoundaryError("workspace root is not a directory")

    @staticmethod
    def _line_number(value: int | None, name: str) -> int | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise SecurityBoundaryError(f"{name} must be a positive integer")
        return value

    def read(
        self,
        path: str,
        *,
        start_line: int | None = None,
        end_line: int | None = None,
    ) -> WorkspaceText:
        if not isinstance(path, str) or not path or "\x00" in path:
            raise SecurityBoundaryError("path must be a non-empty text path")
        start = self._line_number(start_line, "start_line")
        end = self._line_number(end_line, "end_line")
        if end is not None and start is None:
            start = 1
        if start is not None and end is not None and end < start:
            raise SecurityBoundaryError("end_line must not precede start_line")

        requested = Path(path).expanduser()
        candidate = requested if requested.is_absolute() else self.workspace / requested
        try:
            resolved = candidate.resolve(strict=True)
            relative = resolved.relative_to(self.workspace)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SecurityBoundaryError("path escapes the active workspace") from exc
        if resolved == self.workspace:
            raise SecurityBoundaryError("path must identify a regular file")
        try:
            if not stat.S_ISREG(resolved.stat().st_mode):
                raise SecurityBoundaryError("workspace path is not a regular file")
        except OSError as exc:
            raise SecurityBoundaryError("workspace file could not be inspected safely") from exc

        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(resolved, flags)
        except OSError as exc:
            raise SecurityBoundaryError("workspace file could not be opened safely") from exc
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise SecurityBoundaryError("workspace path is not a regular file")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                content = stream.read()
        finally:
            os.close(descriptor)

        # Detect a path swap across the validation/read window. This complements
        # O_NOFOLLOW on platforms that provide it and remains useful on Windows.
        try:
            after = candidate.resolve(strict=True)
            after.relative_to(self.workspace)
        except (OSError, RuntimeError, ValueError) as exc:
            raise SecurityBoundaryError("workspace path changed during the read") from exc
        if after != resolved:
            raise SecurityBoundaryError("workspace path changed during the read")

        if start is not None:
            lines = content.splitlines(keepends=True)
            if start > max(1, len(lines)):
                raise SecurityBoundaryError("start_line is beyond the end of the file")
            content = b"".join(lines[start - 1 : end])
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SecurityBoundaryError("workspace file is not valid UTF-8 text") from exc
        return WorkspaceText(
            text=text,
            content=content,
            resolved_path=resolved,
            safe_label=relative.as_posix(),
        )
