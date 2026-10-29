"""Reversible htsave integration for OpenCode CLI (opencode.ai).

OpenCode discovers plugins from ``.opencode/plugins/`` at project level.
This module manages its project plugin and native skill:

  .opencode/plugins/htsave.js
  .opencode/skills/htsave/SKILL.md

The plugin bridge registers the htsave MCP server, skill, and lifecycle
hooks.  Ownership is tracked via a JS comment marker in the plugin file.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import __version__
from .errors import HtsaveError
from .paths import atomic_write_text
from .plugin import PLUGIN_NAME

OWNER_KEY = "htsaveOwned"
OPENCODE_HOOK_MODULE = "htsave.opencode_hooks"
PLUGIN_DIR = Path(".opencode") / "plugins"
PLUGIN_FILE = PLUGIN_DIR / "htsave.js"
LEGACY_PLUGIN_FILE = PLUGIN_DIR / "htsave.mjs"
SKILL_FILE = Path(".opencode/skills/htsave/SKILL.md")
SKILL_MARKER = "<!-- htsaveOwned: opencode-skill-v1 -->"
_OWNER_PATTERN = re.compile(
    r'//\s*htsaveOwned:\s*(\{[^}]+\})'
)


class OpenCodeIntegrationError(HtsaveError):
    """The OpenCode integration could not be inspected or changed safely."""


class OpenCodeIntegrationState(StrEnum):
    NOT_INSTALLED = "not-installed"
    INSTALLED = "installed"
    DRIFTED = "drifted"


@dataclass(frozen=True, slots=True)
class OpenCodeIntegrationStatus:
    state: OpenCodeIntegrationState
    plugin_installed: bool
    python_executable: str | None = None
    installed_version: str | None = None
    drifts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        return self.state is OpenCodeIntegrationState.INSTALLED and not self.drifts


def _ownership_tag() -> dict[str, str]:
    return {"owner": PLUGIN_NAME, "version": __version__}


def _parse_ownership(content: str) -> dict[str, Any] | None:
    match = _OWNER_PATTERN.search(content)
    if match is None:
        return None
    try:
        tag = json.loads(match.group(1))
        if isinstance(tag, dict) and tag.get("owner") == PLUGIN_NAME:
            return tag
    except (json.JSONDecodeError, ValueError):
        pass
    return None


def _is_owned_file(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return _parse_ownership(content) is not None


def _skill_content() -> str:
    """Load the OpenCode skill template from the packaged plugin tree."""
    packaged = (
        Path(__file__).resolve().parent
        / "_plugin"
        / "htsave"
        / "skills"
        / "opencode"
        / "SKILL.md"
    )
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    source = (
        Path(__file__).resolve().parents[2]
        / "plugin"
        / "htsave"
        / "skills"
        / "opencode"
        / "SKILL.md"
    )
    if source.is_file():
        return source.read_text(encoding="utf-8")
    raise OpenCodeIntegrationError("packaged OpenCode skill template is missing")


def _plugin_asset(relative: str) -> str:
    packaged = Path(__file__).resolve().parent / "_plugin" / "htsave" / relative
    source = Path(__file__).resolve().parents[2] / "plugin" / "htsave" / relative
    return (packaged if packaged.is_file() else source).read_text(encoding="utf-8")


def _render_skill() -> str:
    return _skill_content().rstrip() + "\n\n" + SKILL_MARKER + "\n"


def _render_plugin(python_executable: str) -> str:
    return (
        f"// htsaveOwned: {json.dumps(_ownership_tag())}\n"
        f"const PYTHON = {json.dumps(python_executable)};\n"
        f"const HTSAVE_INSTRUCTIONS = {json.dumps(_skill_content())};\n"
        + _plugin_asset("opencode/bridge.mjs")
    )


def _python_ready(interpreter: str) -> bool:
    try:
        result = subprocess.run(
            [interpreter, "-c", "import htsave.mcp_server; import htsave.opencode_hooks"],
            capture_output=True, timeout=10, check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# ── Top-level operations ───────────────────────────────────────────────────


def install(
    *,
    project_dir: Path | None = None,
    python_executable: Path | None = None,
) -> OpenCodeIntegrationStatus:
    """Install the htsave plugin for OpenCode in the project."""
    root = project_dir or Path.cwd()
    target = root / PLUGIN_FILE
    interpreter = str((python_executable or Path(sys.executable)).expanduser().absolute())

    if target.is_file():
        content = target.read_text(encoding="utf-8")
        if _parse_ownership(content) is None:
            raise OpenCodeIntegrationError(
                f"plugin file '{target}' already exists and is not owned by htsave"
            )

    skill = root / SKILL_FILE
    if skill.exists() and SKILL_MARKER not in skill.read_text(encoding="utf-8"):
        raise OpenCodeIntegrationError(f"skill file '{skill}' is not owned by htsave")
    if not _python_ready(interpreter):
        raise OpenCodeIntegrationError("Python cannot import the htsave runtime")
    previous = {
        path: path.read_text(encoding="utf-8") if path.is_file() else None
        for path in (skill, target)
    }
    written: list[Path] = []
    try:
        for path, content in ((skill, _render_skill()), (target, _render_plugin(interpreter))):
            atomic_write_text(path, content, mode=0o644)
            written.append(path)
        result = status(project_dir=root)
        if not result.healthy:
            raise OpenCodeIntegrationError(
                f"OpenCode install verification failed: {result.state.value}"
            )
    except (OSError, HtsaveError):
        for path in reversed(written):
            if previous[path] is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write_text(path, previous[path], mode=0o644)
        raise
    legacy = root / LEGACY_PLUGIN_FILE
    if _is_owned_file(legacy):
        legacy.unlink()
    return result


def uninstall(
    *,
    project_dir: Path | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Remove only htsave-owned plugin file."""
    root = project_dir or Path.cwd()
    before = status(project_dir=root)
    if not confirm:
        return {
            "dry_run": True,
            "would_remove_integration": (
                before.state is not OpenCodeIntegrationState.NOT_INSTALLED
            ),
            "session_data_preserved": True,
            "hint": "rerun with --yes to remove the managed OpenCode integration",
            "status": before,
        }
    legacy = root / LEGACY_PLUGIN_FILE
    if _is_owned_file(legacy):
        legacy.unlink()
    target = root / PLUGIN_FILE
    if target.is_file() and _is_owned_file(target):
        target.unlink()
        plugin_dir = target.parent
        if plugin_dir.is_dir() and not any(plugin_dir.iterdir()):
            plugin_dir.rmdir()
    skill = root / SKILL_FILE
    if skill.is_file() and SKILL_MARKER in skill.read_text(encoding="utf-8"):
        skill.unlink()
        if not any(skill.parent.iterdir()):
            skill.parent.rmdir()
    return {
        "removed": True,
        "session_data_preserved": True,
        "status": status(project_dir=root),
    }


def status(*, project_dir: Path | None = None) -> OpenCodeIntegrationStatus:
    """Report what htsave owns in the OpenCode project without changing anything."""
    root = project_dir or Path.cwd()
    target = root / PLUGIN_FILE

    if not target.is_file():
        if _is_owned_file(root / LEGACY_PLUGIN_FILE):
            return OpenCodeIntegrationStatus(
                state=OpenCodeIntegrationState.DRIFTED, plugin_installed=True,
                drifts=("legacy-plugin-not-discoverable",),
            )
        skill = root / SKILL_FILE
        if skill.is_file() and SKILL_MARKER in skill.read_text(encoding="utf-8"):
            return OpenCodeIntegrationStatus(
                state=OpenCodeIntegrationState.DRIFTED, plugin_installed=False,
                drifts=("owned-skill-without-plugin",),
            )
        return OpenCodeIntegrationStatus(
            state=OpenCodeIntegrationState.NOT_INSTALLED,
            plugin_installed=False,
        )

    try:
        content = target.read_text(encoding="utf-8")
    except OSError:
        return OpenCodeIntegrationStatus(
            state=OpenCodeIntegrationState.NOT_INSTALLED,
            plugin_installed=False,
        )

    tag = _parse_ownership(content)
    if tag is None:
        return OpenCodeIntegrationStatus(
            state=OpenCodeIntegrationState.NOT_INSTALLED,
            plugin_installed=False,
        )

    installed_version = tag.get("version")
    drifts: list[str] = []
    if installed_version != __version__:
        drifts.append("version-mismatch")

    python_match = re.search(r'const PYTHON = ("(?:[^"\\]|\\.)*");', content)
    python_path = json.loads(python_match.group(1)) if python_match else None
    if not python_path or not _python_ready(python_path):
        drifts.append("python-runtime-unavailable")
    if python_path and content != _render_plugin(python_path):
        drifts.append("plugin-content-mismatch")
    skill = root / SKILL_FILE
    if not skill.is_file() or skill.read_text(encoding="utf-8") != _render_skill():
        drifts.append("skill-content-mismatch")

    state = (
        OpenCodeIntegrationState.DRIFTED
        if drifts
        else OpenCodeIntegrationState.INSTALLED
    )
    return OpenCodeIntegrationStatus(
        state=state,
        plugin_installed=True,
        python_executable=python_path,
        installed_version=installed_version,
        drifts=tuple(drifts),
    )
