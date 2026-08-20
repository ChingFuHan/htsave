"""Reversible htsave integration for Claude Code's settings file.

Claude Code has no plugin marketplace to own, so the integration is a set of
hook entries in ``settings.json`` plus one MCP server registration.  The
operator's own hooks live in the same file — this machine already runs `rtk`
and `caveman` handlers on most events — so every write is additive and every
htsave-owned entry is tagged, letting install, status, and uninstall recognize
exactly what belongs to htsave and touch nothing else.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import __version__
from .errors import HtsaveError
from .mcp_server import READ_TOOL
from .plugin import HOOK_MODULE, MCP_MODULE, PLUGIN_NAME

CLAUDE_HOOK_MODULE = "htsave.claude_hooks"
# The interpreter path is deliberately *not* resolved: a virtualenv's
# ``bin/python`` is a symlink to the base interpreter, and following it points
# the hook at a Python that cannot import htsave.
OWNER_KEY = "htsaveOwned"
SETTINGS_RELATIVE = Path(".claude") / "settings.json"
# Every lifecycle event the adapter acts on.  Matchers stay absent so Claude
# Code runs the handler for every tool; the adapter itself decides what is
# unambiguous enough to touch.
HOOK_EVENTS: tuple[str, ...] = (
    "SessionStart",
    "PreToolUse",
    "PostToolUse",
    "PreCompact",
    "PostCompact",
    "SubagentStart",
    "SubagentStop",
    "Stop",
)
HOOK_TIMEOUT_SECONDS = 30


class ClaudeIntegrationError(HtsaveError):
    """The Claude Code integration could not be inspected or changed safely."""


class ClaudeIntegrationState(StrEnum):
    NOT_INSTALLED = "not-installed"
    INSTALLED = "installed"
    DRIFTED = "drifted"


@dataclass(frozen=True, slots=True)
class ClaudeIntegrationStatus:
    state: ClaudeIntegrationState
    settings_path: Path
    expected_version: str
    installed_version: str | None
    hook_events: tuple[str, ...]
    mcp_registered: bool
    foreign_hook_handlers: int
    drifts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        return self.state is ClaudeIntegrationState.INSTALLED and not self.drifts


def default_settings_path(home: Path | None = None) -> Path:
    return (home or Path.home()) / SETTINGS_RELATIVE


def _read_settings(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ClaudeIntegrationError(f"Claude Code settings are not valid JSON: {path}") from error
    if not isinstance(value, dict):
        raise ClaudeIntegrationError(f"Claude Code settings must be a JSON object: {path}")
    return value


def _write_settings(path: Path, payload: Mapping[str, Any]) -> None:
    """Replace the settings file atomically, preserving its permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent, prefix=".settings.", suffix=".tmp"
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, path.stat().st_mode & 0o777 if path.exists() else 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _handler(python_executable: Path) -> dict[str, Any]:
    return {
        "type": "command",
        "command": f'"{python_executable}" -m {CLAUDE_HOOK_MODULE}',
        "timeout": HOOK_TIMEOUT_SECONDS,
        OWNER_KEY: {"owner": PLUGIN_NAME, "version": __version__},
    }


def _is_owned(handler: object) -> bool:
    if not isinstance(handler, Mapping):
        return False
    marker = handler.get(OWNER_KEY)
    return isinstance(marker, Mapping) and marker.get("owner") == PLUGIN_NAME


def _owned_version(handler: Mapping[str, Any]) -> str | None:
    marker = handler.get(OWNER_KEY)
    version = marker.get("version") if isinstance(marker, Mapping) else None
    return version if isinstance(version, str) else None


def _mcp_server(python_executable: Path) -> dict[str, Any]:
    return {
        "command": str(python_executable),
        "args": ["-m", MCP_MODULE],
        OWNER_KEY: {"owner": PLUGIN_NAME, "version": __version__},
    }


def _groups(settings: Mapping[str, Any], event: str) -> list[Any]:
    hooks = settings.get("hooks")
    if hooks is None:
        return []
    if not isinstance(hooks, Mapping):
        raise ClaudeIntegrationError("settings 'hooks' must be an object")
    groups = hooks.get(event, [])
    if groups is None:
        return []
    if not isinstance(groups, list):
        raise ClaudeIntegrationError(f"settings hook event '{event}' must be an array")
    return list(groups)


def _owned_handlers(settings: Mapping[str, Any], event: str) -> list[Mapping[str, Any]]:
    found: list[Mapping[str, Any]] = []
    for group in _groups(settings, event):
        if not isinstance(group, Mapping):
            continue
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            continue
        found.extend(handler for handler in handlers if _is_owned(handler))
    return found


def _count_foreign_handlers(settings: Mapping[str, Any]) -> int:
    hooks = settings.get("hooks")
    if not isinstance(hooks, Mapping):
        return 0
    total = 0
    for event in hooks:
        for group in _groups(settings, event):
            if not isinstance(group, Mapping):
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                continue
            total += sum(1 for handler in handlers if not _is_owned(handler))
    return total


def _strip_owned(settings: dict[str, Any]) -> dict[str, Any]:
    """Remove every htsave-owned handler, leaving foreign entries in place."""

    hooks = settings.get("hooks")
    if not isinstance(hooks, Mapping):
        return settings
    remaining_events: dict[str, Any] = {}
    for event in hooks:
        groups: list[Any] = []
        for group in _groups(settings, event):
            if not isinstance(group, Mapping):
                groups.append(group)
                continue
            handlers = group.get("hooks")
            if not isinstance(handlers, list):
                groups.append(group)
                continue
            kept = [handler for handler in handlers if not _is_owned(handler)]
            if not kept:
                # The group existed only to carry htsave; drop it entirely
                # rather than leaving an empty matcher behind.
                continue
            groups.append({**group, "hooks": kept})
        if groups:
            remaining_events[event] = groups
    if remaining_events:
        settings["hooks"] = remaining_events
    else:
        settings.pop("hooks", None)

    servers = settings.get("mcpServers")
    if isinstance(servers, Mapping):
        kept_servers = {name: value for name, value in servers.items() if not _is_owned(value)}
        if kept_servers:
            settings["mcpServers"] = kept_servers
        else:
            settings.pop("mcpServers", None)
    return settings


def status(
    *,
    settings_path: Path | None = None,
    python_executable: Path | None = None,
) -> ClaudeIntegrationStatus:
    """Report what htsave owns in the settings file without changing anything."""

    path = settings_path or default_settings_path()
    interpreter = (python_executable or Path(sys.executable)).expanduser()
    settings = _read_settings(path)

    installed_events: list[str] = []
    versions: set[str] = set()
    drifts: list[str] = []
    expected_command = _handler(interpreter)["command"]
    for event in HOOK_EVENTS:
        handlers = _owned_handlers(settings, event)
        if not handlers:
            continue
        installed_events.append(event)
        if len(handlers) > 1:
            drifts.append(f"duplicate-handler:{event}")
        for handler in handlers:
            version = _owned_version(handler)
            if version is not None:
                versions.add(version)
            if handler.get("command") != expected_command:
                drifts.append(f"interpreter-mismatch:{event}")

    servers = settings.get("mcpServers")
    server = servers.get(PLUGIN_NAME) if isinstance(servers, Mapping) else None
    mcp_registered = _is_owned(server)
    if mcp_registered and isinstance(server, Mapping):
        if server.get("command") != str(interpreter) or server.get("args") != ["-m", MCP_MODULE]:
            drifts.append("mcp-server-mismatch")
    elif server is not None and not mcp_registered:
        drifts.append("mcp-server-not-owned")

    if not installed_events and not mcp_registered:
        state = ClaudeIntegrationState.NOT_INSTALLED
    elif sorted(installed_events) == sorted(HOOK_EVENTS) and mcp_registered and not drifts:
        state = ClaudeIntegrationState.INSTALLED
    else:
        state = ClaudeIntegrationState.DRIFTED
        missing = sorted(set(HOOK_EVENTS) - set(installed_events))
        drifts.extend(f"missing-event:{event}" for event in missing)
        if not mcp_registered:
            drifts.append("mcp-server-not-registered")

    installed_version = versions.pop() if len(versions) == 1 else None
    return ClaudeIntegrationStatus(
        state=state,
        settings_path=path,
        expected_version=__version__,
        installed_version=installed_version,
        hook_events=tuple(sorted(installed_events)),
        mcp_registered=mcp_registered,
        foreign_hook_handlers=_count_foreign_handlers(settings),
        drifts=tuple(dict.fromkeys(drifts)),
    )


def install(
    *,
    settings_path: Path | None = None,
    python_executable: Path | None = None,
) -> ClaudeIntegrationStatus:
    """Add htsave's hooks and MCP server, leaving every foreign entry intact."""

    path = settings_path or default_settings_path()
    interpreter = (python_executable or Path(sys.executable)).expanduser()
    settings = _read_settings(path)
    foreign_before = _count_foreign_handlers(settings)

    # Removing first makes a re-install idempotent instead of accumulating.
    settings = _strip_owned(settings)
    hooks = dict(settings.get("hooks") or {})
    for event in HOOK_EVENTS:
        groups = list(hooks.get(event) or [])
        groups.append({"hooks": [_handler(interpreter)]})
        hooks[event] = groups
    settings["hooks"] = hooks

    servers = dict(settings.get("mcpServers") or {})
    existing = servers.get(PLUGIN_NAME)
    if existing is not None and not _is_owned(existing):
        raise ClaudeIntegrationError(
            f"MCP server '{PLUGIN_NAME}' already exists and is not owned by htsave"
        )
    servers[PLUGIN_NAME] = _mcp_server(interpreter)
    settings["mcpServers"] = servers

    _write_settings(path, settings)
    result = status(settings_path=path, python_executable=interpreter)
    if result.foreign_hook_handlers != foreign_before:
        raise ClaudeIntegrationError(
            "install changed the operator's own hook handlers; settings were not preserved"
        )
    if not result.healthy:
        raise ClaudeIntegrationError(
            f"Claude Code install verification failed: {result.state.value}"
        )
    return result


def uninstall(
    *,
    settings_path: Path | None = None,
    python_executable: Path | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Remove only htsave-owned entries.  Session data is never touched."""

    path = settings_path or default_settings_path()
    interpreter = (python_executable or Path(sys.executable)).expanduser()
    before = status(settings_path=path, python_executable=interpreter)
    if not confirm:
        return {
            "dry_run": True,
            "would_remove_integration": before.state is not ClaudeIntegrationState.NOT_INSTALLED,
            "session_data_preserved": True,
            "hint": "rerun with --yes to remove the managed Claude Code integration",
            "status": before,
        }

    settings = _strip_owned(_read_settings(path))
    _write_settings(path, settings)
    after = status(settings_path=path, python_executable=interpreter)
    if after.foreign_hook_handlers != before.foreign_hook_handlers:
        raise ClaudeIntegrationError(
            "uninstall changed the operator's own hook handlers; settings were not preserved"
        )
    return {
        "removed": True,
        "session_data_preserved": True,
        "status": after,
    }


def integration_summary(python_executable: Path | None = None) -> dict[str, Any]:
    """Small description used by ``htsave doctor``."""

    interpreter = (python_executable or Path(sys.executable)).expanduser()
    return {
        "hook_module": CLAUDE_HOOK_MODULE,
        "mcp_module": MCP_MODULE,
        "mcp_tool": READ_TOOL,
        "codex_hook_module": HOOK_MODULE,
        "python": str(interpreter),
        "events": list(HOOK_EVENTS),
    }
