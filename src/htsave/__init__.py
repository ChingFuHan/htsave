"""Lossless repeated-context saving for Codex CLI."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("htsave")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "1.0.0"

__all__ = ["__version__"]
