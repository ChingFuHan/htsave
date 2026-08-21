"""Reversible htsave integration for Antigravity CLI (agy).

agy discovers customizations from ~/.gemini/config/ (global) and
.agents/ (project-level). This module manages global installation:
1. Skill:  ~/.gemini/config/skills/htsave/SKILL.md
2. MCP:    htsave entry in ~/.gemini/config/mcp_config.json
3. Hooks:  htsave entry in ~/.gemini/config/hooks.json

All owned entries are tagged with an htsaveOwned marker for safe
identification during status checks and uninstall.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import __version__
from .errors import HtsaveError
from .plugin import MCP_MODULE, PLUGIN_NAME

OWNER_KEY = "htsaveOwned"
AGY_HOOK_MODULE = "htsave.agy_hooks"
GEMINI_CONFIG = Path(".gemini") / "config"
SKILL_RELATIVE = GEMINI_CONFIG / "skills" / "htsave" / "SKILL.md"
MCP_CONFIG_RELATIVE = GEMINI_CONFIG / "mcp_config.json"
HOOKS_CONFIG_RELATIVE = GEMINI_CONFIG / "hooks.json"


class AgyIntegrationError(HtsaveError):
    """The agy integration could not be inspected or changed safely."""


class AgyIntegrationState(StrEnum):
    NOT_INSTALLED = "not-installed"
    INSTALLED = "installed"
    DRIFTED = "drifted"


@dataclass(frozen=True, slots=True)
class AgyIntegrationStatus:
    state: AgyIntegrationState
    skill_installed: bool
    mcp_registered: bool
    hooks_registered: bool
    drifts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        return self.state is AgyIntegrationState.INSTALLED and not self.drifts


# ── Atomic I/O helpers ──────────────────────────────────────────────────────


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, tmp_name = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        content = path.read_text(encoding="utf-8")
        if not content.strip():
            return {}
        data = json.loads(content)
        if not isinstance(data, dict):
            raise AgyIntegrationError(f"config file '{path}' must be a JSON object")
        return data
    except OSError as error:
        raise AgyIntegrationError(f"cannot read config file: {path}") from error
    except json.JSONDecodeError as error:
        raise AgyIntegrationError(f"invalid JSON file: {path}") from error


def _write_json(path: Path, payload: Any) -> None:
    _atomic_write(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _is_owned_entry(entry: object) -> bool:
    if not isinstance(entry, dict):
        return False
    marker = entry.get(OWNER_KEY)
    return isinstance(marker, dict) and marker.get("owner") == PLUGIN_NAME


def _ownership_tag() -> dict[str, str]:
    return {"owner": PLUGIN_NAME, "version": __version__}


# ── Skill management ────────────────────────────────────────────────────────


def _skill_template() -> str:
    """Load the agy skill template from the packaged plugin tree."""
    packaged = (
        Path(__file__).resolve().parent / "_plugin" / "htsave" / "skills" / "agy" / "SKILL.md"
    )
    if packaged.is_file():
        return packaged.read_text(encoding="utf-8")
    source = (
        Path(__file__).resolve().parents[2] / "plugin" / "htsave" / "skills" / "agy" / "SKILL.md"
    )
    if source.is_file():
        return source.read_text(encoding="utf-8")
    raise AgyIntegrationError("packaged agy skill template is missing")


def _install_skill(home: Path | None = None) -> Path:
    target = (home or Path.home()) / SKILL_RELATIVE
    target.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(target, _skill_template())
    return target


def _uninstall_skill(home: Path | None = None) -> None:
    target = (home or Path.home()) / SKILL_RELATIVE
    if target.is_file():
        target.unlink()
    # Clean up empty skill directory.
    skill_dir = target.parent
    if skill_dir.is_dir() and not any(skill_dir.iterdir()):
        skill_dir.rmdir()


# ── MCP config management ───────────────────────────────────────────────────


def _install_mcp(home: Path | None = None) -> None:
    """Add htsave to ~/.gemini/config/mcp_config.json."""
    path = (home or Path.home()) / MCP_CONFIG_RELATIVE
    config = _read_json(path) if path.is_file() else {}
    servers = config.setdefault("mcpServers", {})

    existing = servers.get(PLUGIN_NAME)
    if existing is not None and not _is_owned_entry(existing):
        raise AgyIntegrationError(
            f"MCP server '{PLUGIN_NAME}' in {path} already exists and is not owned by htsave"
        )

    interpreter = Path(sys.executable).expanduser()
    servers[PLUGIN_NAME] = {
        "command": str(interpreter),
        "args": ["-m", MCP_MODULE],
        OWNER_KEY: _ownership_tag(),
    }
    _write_json(path, config)


def _uninstall_mcp(home: Path | None = None) -> None:
    path = (home or Path.home()) / MCP_CONFIG_RELATIVE
    if not path.is_file():
        return
    config = _read_json(path)
    servers = config.get("mcpServers", {})
    entry = servers.get(PLUGIN_NAME)
    if entry is not None and _is_owned_entry(entry):
        del servers[PLUGIN_NAME]
        if not servers:
            config.pop("mcpServers", None)
        _write_json(path, config)


# ── Hooks config management ─────────────────────────────────────────────────


def _hook_command() -> str:
    interpreter = Path(sys.executable).expanduser()
    return f'"{interpreter}" -m {AGY_HOOK_MODULE}'


def _install_hooks(home: Path | None = None) -> None:
    """Add htsave hook entries to ~/.gemini/config/hooks.json."""
    path = (home or Path.home()) / HOOKS_CONFIG_RELATIVE
    config = _read_json(path) if path.is_file() else {}

    command = _hook_command()
    config["htsave"] = {
        OWNER_KEY: _ownership_tag(),
        "PreToolUse": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": command, "timeout": 30}],
            }
        ],
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": command, "timeout": 30}],
            }
        ],
        "Stop": [{"type": "command", "command": command, "timeout": 30}],
    }
    _write_json(path, config)


def _uninstall_hooks(home: Path | None = None) -> None:
    path = (home or Path.home()) / HOOKS_CONFIG_RELATIVE
    if not path.is_file():
        return
    config = _read_json(path)
    entry = config.get("htsave")
    if isinstance(entry, dict) and _is_owned_entry(entry):
        del config["htsave"]
        _write_json(path, config)


# ── Top-level operations ────────────────────────────────────────────────────


def install(*, home: Path | None = None) -> AgyIntegrationStatus:
    """Install skill, MCP server, and hooks for agy CLI."""
    _install_skill(home)
    _install_mcp(home)
    _install_hooks(home)
    result = status(home=home)
    if not result.healthy:
        raise AgyIntegrationError(f"agy install verification failed: {result.state.value}")
    return result


def uninstall(
    *, home: Path | None = None, confirm: bool = False
) -> dict[str, Any]:
    """Remove only htsave-owned entries. Session data is never touched."""
    before = status(home=home)
    if not confirm:
        return {
            "dry_run": True,
            "would_remove_integration": (before.state is not AgyIntegrationState.NOT_INSTALLED),
            "session_data_preserved": True,
            "hint": "rerun with --yes to remove the managed agy integration",
            "status": before,
        }
    _uninstall_hooks(home)
    _uninstall_mcp(home)
    _uninstall_skill(home)
    return {
        "removed": True,
        "session_data_preserved": True,
        "status": status(home=home),
    }


def status(*, home: Path | None = None) -> AgyIntegrationStatus:
    """Report what htsave owns in agy config without changing anything."""
    root = home or Path.home()

    # Skill
    skill_installed = (root / SKILL_RELATIVE).is_file()

    # MCP
    mcp_path = root / MCP_CONFIG_RELATIVE
    mcp_registered = False
    if mcp_path.is_file():
        try:
            config = _read_json(mcp_path)
        except AgyIntegrationError:
            config = {}
        servers = config.get("mcpServers", {})
        entry = servers.get(PLUGIN_NAME)
        mcp_registered = entry is not None and _is_owned_entry(entry)

    # Hooks
    hooks_path = root / HOOKS_CONFIG_RELATIVE
    hooks_registered = False
    if hooks_path.is_file():
        try:
            config = _read_json(hooks_path)
        except AgyIntegrationError:
            config = {}
        entry = config.get("htsave")
        hooks_registered = isinstance(entry, dict) and _is_owned_entry(entry)

    # State determination
    drifts: list[str] = []
    all_installed = skill_installed and mcp_registered and hooks_registered
    any_installed = skill_installed or mcp_registered or hooks_registered

    if not any_installed:
        state = AgyIntegrationState.NOT_INSTALLED
    elif all_installed:
        state = AgyIntegrationState.INSTALLED
    else:
        state = AgyIntegrationState.DRIFTED
        if not skill_installed:
            drifts.append("skill-missing")
        if not mcp_registered:
            drifts.append("mcp-missing")
        if not hooks_registered:
            drifts.append("hooks-missing")

    return AgyIntegrationStatus(
        state=state,
        skill_installed=skill_installed,
        mcp_registered=mcp_registered,
        hooks_registered=hooks_registered,
        drifts=tuple(drifts),
    )
