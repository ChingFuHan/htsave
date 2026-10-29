"""Reversible htsave integration for Claude Code, delivered as a plugin.

The plugin (``plugin/claude-code``, listed by the repository's
``.claude-plugin/marketplace.json``) carries the lifecycle hooks, the MCP
server, and the skill; its commands run the htsave runtime from PATH.  This
module drives ``claude plugin`` to install it, and removes what older htsave
releases wrote directly: hook entries and an ``mcpServers`` entry in
``settings.json``, plus a skill file.  The operator's own hooks live in the
same settings file — this machine already runs `rtk` and `caveman` handlers on
most events — so cleanup touches only what it recognizes as htsave's.

``install_hooks`` still writes hook entries directly; the paired benchmark uses
it inside an isolated ``CLAUDE_CONFIG_DIR``.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from . import __version__
from .errors import HtsaveError
from .mcp_server import READ_TOOL
from .paths import atomic_write_text
from .plugin import HOOK_MODULE, MCP_MODULE, PLUGIN_NAME, CommandResult

CLAUDE_HOOK_MODULE = "htsave.claude_hooks"
MARKETPLACE_NAME = "htsave"
PLUGIN_ID = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
DEFAULT_MARKETPLACE = "ChingFuHan/htsave"
# The plugin's hooks and MCP server run these console scripts from PATH.
RUNTIME_COMMANDS: tuple[str, ...] = ("htsave-claude-hook", "htsave-mcp")
# The interpreter path is deliberately *not* resolved: a virtualenv's
# ``bin/python`` is a symlink to the base interpreter, and following it points
# the hook at a Python that cannot import htsave.
OWNER_KEY = "htsaveOwned"
CLAUDE_CONFIG_ENV = "CLAUDE_CONFIG_DIR"
# Skill files older releases wrote; the plugin now ships the skill.
LEGACY_SKILLS: tuple[Path, ...] = (
    Path("skills") / "htsave.md",
    Path("skills") / "htsave" / "SKILL.md",
)
# Every lifecycle event the adapter acts on.  Matchers stay absent so Claude
# Code runs the handler for every tool; the adapter itself decides what is
# unambiguous enough to touch.  The plugin's hooks.json lists the same events.
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
_CLAUDE_COMMAND_TIMEOUT_SECONDS = 120

ClaudeRunner = Callable[[Sequence[str], Mapping[str, str]], CommandResult]


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
    plugin_enabled: bool
    runtime_on_path: bool
    foreign_hook_handlers: int
    drifts: tuple[str, ...] = field(default_factory=tuple)

    @property
    def healthy(self) -> bool:
        return self.state is ClaudeIntegrationState.INSTALLED and not self.drifts


def _default_config_dir(home: Path | None = None) -> Path:
    configured = os.environ.get(CLAUDE_CONFIG_ENV)
    if home is None and configured:
        return Path(configured).expanduser()
    return (home or Path.home()) / ".claude"


def default_settings_path(home: Path | None = None) -> Path:
    return _default_config_dir(home) / "settings.json"


def _config_dir(settings_path: Path | None) -> Path | None:
    """The config directory a settings override belongs to; ``None`` is Claude's default."""

    if settings_path is None or settings_path == default_settings_path():
        return None
    return settings_path.parent


def _default_runner(argv: Sequence[str], env: Mapping[str, str]) -> CommandResult:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
        env=dict(env),
        timeout=_CLAUDE_COMMAND_TIMEOUT_SECONDS,
    )


class ClaudeClient:
    """Small wrapper around the ``claude plugin`` commands htsave needs."""

    def __init__(
        self,
        config_dir: Path | None = None,
        *,
        runner: ClaudeRunner | None = None,
        executable: str = "claude",
    ) -> None:
        self._config_dir = config_dir
        self._runner = runner or _default_runner
        self._executable = executable

    def plugin(self, plugin_id: str) -> Mapping[str, Any] | None:
        for record in self._json("plugin", "list", "--json"):
            if isinstance(record, Mapping) and record.get("id") == plugin_id:
                return record
        return None

    def has_marketplace(self, name: str) -> bool:
        return any(
            isinstance(record, Mapping) and record.get("name") == name
            for record in self._json("plugin", "marketplace", "list", "--json")
        )

    def add_marketplace(self, source: str) -> None:
        self._run("plugin", "marketplace", "add", source)

    def remove_marketplace(self, name: str) -> None:
        self._run("plugin", "marketplace", "remove", name)

    def install_plugin(self, plugin_id: str) -> None:
        self._run("plugin", "install", plugin_id, "--scope", "user")

    def uninstall_plugin(self, plugin_id: str) -> None:
        self._run("plugin", "uninstall", plugin_id, "--scope", "user")

    def _json(self, *arguments: str) -> list[Any]:
        stdout = self._run(*arguments)
        try:
            value = json.loads(stdout)
        except json.JSONDecodeError as error:
            raise ClaudeIntegrationError(
                f"Claude Code returned invalid JSON: claude {' '.join(arguments)}"
            ) from error
        if not isinstance(value, list):
            raise ClaudeIntegrationError(
                f"Claude Code returned an unexpected shape: claude {' '.join(arguments)}"
            )
        return value

    def _run(self, *arguments: str) -> str:
        argv = (self._executable, *arguments)
        env = dict(os.environ)
        if self._config_dir is not None:
            env[CLAUDE_CONFIG_ENV] = str(self._config_dir)
        try:
            result = self._runner(argv, env)
        except (OSError, subprocess.SubprocessError) as error:
            raise ClaudeIntegrationError(f"could not execute {self._executable}") from error
        if result.returncode != 0:
            detail = (result.stderr or "").strip() or (result.stdout or "").strip()
            raise ClaudeIntegrationError(
                f"Claude Code command failed: {' '.join(argv)}: "
                f"{detail or f'exit status {result.returncode}'}"
            )
        return result.stdout or ""


def _is_owned_skill(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        content = path.read_text(encoding="utf-8")
        return "htsave-managed: true" in content
    except OSError:
        return False


def _remove_owned_skill(path: Path) -> None:
    if not _is_owned_skill(path):
        return
    path.unlink()
    skill_dir = path.parent
    if skill_dir.name == PLUGIN_NAME and not any(skill_dir.iterdir()):
        skill_dir.rmdir()


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

    atomic_write_text(path, json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def _handler(python_executable: Path) -> dict[str, Any]:
    return {
        "type": "command",
        "command": f'"{python_executable}" -m {CLAUDE_HOOK_MODULE}',
        "timeout": HOOK_TIMEOUT_SECONDS,
        OWNER_KEY: {"owner": PLUGIN_NAME, "version": __version__},
    }


def _has_owner_marker(value: Mapping[str, Any]) -> bool:
    marker = value.get(OWNER_KEY)
    return isinstance(marker, Mapping) and marker.get("owner") == PLUGIN_NAME


def _is_owned(handler: object) -> bool:
    """Recognize htsave's hook handler by its marker or its htsave-only command.

    Claude Code can drop unknown handler keys when it rewrites settings.json,
    which loses the marker; the hook module in the command is htsave's alone.
    """

    if not isinstance(handler, Mapping):
        return False
    command = handler.get("command")
    return _has_owner_marker(handler) or (
        isinstance(command, str) and command.endswith(f" -m {CLAUDE_HOOK_MODULE}")
    )


def _is_owned_mcp(server: object) -> bool:
    if not isinstance(server, Mapping):
        return False
    return _has_owner_marker(server) or server.get("args") == ["-m", MCP_MODULE]


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


def _handlers(settings: Mapping[str, Any]) -> list[object]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, Mapping):
        return []
    found: list[object] = []
    for event in hooks:
        for group in _groups(settings, event):
            if isinstance(group, Mapping) and isinstance(group.get("hooks"), list):
                found.extend(group["hooks"])
    return found


def _count_foreign_handlers(settings: Mapping[str, Any]) -> int:
    return sum(1 for handler in _handlers(settings) if not _is_owned(handler))


def _legacy_settings_hooks(settings: Mapping[str, Any]) -> bool:
    return any(_is_owned(handler) for handler in _handlers(settings))


def _legacy_settings_server(settings: Mapping[str, Any]) -> bool:
    """Older releases put the MCP server in settings.json, which Claude Code ignores."""

    servers = settings.get("mcpServers")
    return isinstance(servers, Mapping) and _is_owned_mcp(servers.get(PLUGIN_NAME))


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
        kept_servers = {
            name: value for name, value in servers.items() if not _is_owned_mcp(value)
        }
        if kept_servers:
            settings["mcpServers"] = kept_servers
        else:
            settings.pop("mcpServers", None)
    return settings


def _legacy_drifts(settings: Mapping[str, Any], config_dir: Path) -> list[str]:
    drifts: list[str] = []
    if _legacy_settings_hooks(settings):
        drifts.append("legacy-settings-hooks")
    if _legacy_settings_server(settings):
        drifts.append("legacy-settings-mcp-server")
    if any(_is_owned_skill(config_dir / relative) for relative in LEGACY_SKILLS):
        drifts.append("legacy-skill")
    return drifts


def _remove_legacy(path: Path) -> None:
    """Remove what older releases wrote, which would duplicate the plugin's hooks."""

    settings = _read_settings(path)
    if _legacy_settings_hooks(settings) or _legacy_settings_server(settings):
        _write_settings(path, _strip_owned(settings))
    for relative in LEGACY_SKILLS:
        _remove_owned_skill(path.parent / relative)


def _with_owned_hooks(settings: dict[str, Any], interpreter: Path) -> dict[str, Any]:
    # Removing first makes a re-install idempotent instead of accumulating.
    settings = _strip_owned(settings)
    hooks = dict(settings.get("hooks") or {})
    for event in HOOK_EVENTS:
        groups = list(hooks.get(event) or [])
        groups.append({"hooks": [_handler(interpreter)]})
        hooks[event] = groups
    settings["hooks"] = hooks
    return settings


def status(
    *,
    settings_path: Path | None = None,
    claude: ClaudeClient | None = None,
) -> ClaudeIntegrationStatus:
    """Report the plugin, its runtime, and any legacy leftovers without changing anything."""

    path = settings_path or default_settings_path()
    client = claude or ClaudeClient(_config_dir(settings_path))
    settings = _read_settings(path)
    drifts = _legacy_drifts(settings, path.parent)
    plugin = client.plugin(PLUGIN_ID)
    runtime_on_path = all(shutil.which(command) for command in RUNTIME_COMMANDS)
    enabled = plugin is not None and plugin.get("enabled") is True
    version = plugin.get("version") if plugin is not None else None
    installed_version = version if isinstance(version, str) else None

    if plugin is None:
        state = (
            ClaudeIntegrationState.DRIFTED if drifts else ClaudeIntegrationState.NOT_INSTALLED
        )
        if drifts:
            drifts.append("plugin-not-installed")
    else:
        if not enabled:
            drifts.append("plugin-disabled")
        if installed_version != __version__:
            drifts.append("plugin-version-mismatch")
        if not runtime_on_path:
            drifts.append("runtime-not-on-path")
        state = ClaudeIntegrationState.DRIFTED if drifts else ClaudeIntegrationState.INSTALLED

    return ClaudeIntegrationStatus(
        state=state,
        settings_path=path,
        expected_version=__version__,
        installed_version=installed_version,
        plugin_enabled=enabled,
        runtime_on_path=runtime_on_path,
        foreign_hook_handlers=_count_foreign_handlers(settings),
        drifts=tuple(drifts),
    )


def install_hooks(*, settings_path: Path, python_executable: Path | None = None) -> None:
    """Add only htsave's lifecycle hooks to a settings file.

    The paired benchmark uses this inside an isolated ``CLAUDE_CONFIG_DIR``: it
    measures the transparent replacement path alone, with ``--strict-mcp-config``
    keeping every MCP server out of both arms.
    """

    interpreter = (python_executable or Path(sys.executable)).expanduser()
    _write_settings(settings_path, _with_owned_hooks(_read_settings(settings_path), interpreter))


def install(
    *,
    marketplace: str = DEFAULT_MARKETPLACE,
    settings_path: Path | None = None,
    claude: ClaudeClient | None = None,
) -> ClaudeIntegrationStatus:
    """Install the htsave plugin and remove legacy entries that would duplicate it."""

    path = settings_path or default_settings_path()
    client = claude or ClaudeClient(_config_dir(settings_path))
    foreign_before = _count_foreign_handlers(_read_settings(path))

    # The external commands are the likeliest step to fail, so they run before
    # any local cleanup.
    if not client.has_marketplace(MARKETPLACE_NAME):
        client.add_marketplace(marketplace)
    client.install_plugin(PLUGIN_ID)
    _remove_legacy(path)

    result = status(settings_path=path, claude=client)
    if result.foreign_hook_handlers != foreign_before:
        raise ClaudeIntegrationError(
            "install changed the operator's own hook handlers; settings were not preserved"
        )
    if not result.healthy:
        hint = (
            "; install the runtime with `uv tool install htsave`"
            if "runtime-not-on-path" in result.drifts
            else ""
        )
        raise ClaudeIntegrationError(
            f"Claude Code install verification failed: {', '.join(result.drifts)}{hint}"
        )
    return result


def uninstall(
    *,
    settings_path: Path | None = None,
    confirm: bool = False,
    claude: ClaudeClient | None = None,
) -> dict[str, Any]:
    """Remove the plugin, its marketplace, and legacy entries.  Session data is never touched."""

    path = settings_path or default_settings_path()
    client = claude or ClaudeClient(_config_dir(settings_path))
    before = status(settings_path=path, claude=client)
    if not confirm:
        return {
            "dry_run": True,
            "would_remove_integration": before.state is not ClaudeIntegrationState.NOT_INSTALLED,
            "session_data_preserved": True,
            "hint": "rerun with --yes to remove the Claude Code plugin",
            "status": before,
        }

    if client.plugin(PLUGIN_ID) is not None:
        client.uninstall_plugin(PLUGIN_ID)
    if client.has_marketplace(MARKETPLACE_NAME):
        client.remove_marketplace(MARKETPLACE_NAME)
    _remove_legacy(path)
    after = status(settings_path=path, claude=client)
    if after.foreign_hook_handlers != before.foreign_hook_handlers:
        raise ClaudeIntegrationError(
            "uninstall changed the operator's own hook handlers; settings were not preserved"
        )
    return {
        "removed": True,
        "session_data_preserved": True,
        "status": after,
    }


def integration_summary() -> dict[str, Any]:
    """Small description used by ``htsave doctor``."""

    return {
        "plugin": PLUGIN_ID,
        "runtime_commands": list(RUNTIME_COMMANDS),
        "hook_module": CLAUDE_HOOK_MODULE,
        "mcp_module": MCP_MODULE,
        "mcp_tool": READ_TOOL,
        "codex_hook_module": HOOK_MODULE,
        "events": list(HOOK_EVENTS),
    }
