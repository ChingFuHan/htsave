"""Claude Code plugin integration tests.

The fixture mirrors a real, busy ``settings.json``: several unrelated handlers
per event (this machine runs `rtk` and `caveman` on most events) plus unrelated
top-level keys.  Preserving all of that is the whole point of the module.
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace

import pytest

from htsave import __version__, claude_install
from htsave.claude_install import (
    HOOK_EVENTS,
    PLUGIN_ID,
    RUNTIME_COMMANDS,
    ClaudeIntegrationError,
    ClaudeIntegrationState,
    install,
    install_hooks,
    status,
    uninstall,
)

REPOSITORY = Path(__file__).resolve().parents[1]


class FakeClaudeCli:
    """Stand-in for the ``claude plugin`` commands, idempotent where the real CLI is."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], str | None]] = []
        self.marketplaces: list[dict[str, object]] = []
        self.plugins: list[dict[str, object]] = []

    def __call__(self, argv: Sequence[str], env: Mapping[str, str]) -> SimpleNamespace:
        self.calls.append((tuple(argv), env.get("CLAUDE_CONFIG_DIR")))
        names = [record["name"] for record in self.marketplaces]
        ids = [record["id"] for record in self.plugins]
        match tuple(argv[1:]):
            case ("plugin", "list", "--json"):
                return _ok(json.dumps(self.plugins))
            case ("plugin", "marketplace", "list", "--json"):
                return _ok(json.dumps(self.marketplaces))
            case ("plugin", "marketplace", "add", source):
                if "htsave" not in names:
                    self.marketplaces.append({"name": "htsave", "path": source})
                return _ok()
            case ("plugin", "marketplace", "remove", name):
                if name not in names:
                    return _failed(f"Marketplace '{name}' not found")
                self.marketplaces = [m for m in self.marketplaces if m["name"] != name]
                return _ok()
            case ("plugin", "install", plugin_id, "--scope", "user"):
                if "htsave" not in names:
                    return _failed("marketplace not found")
                if plugin_id not in ids:
                    self.plugins.append({"id": plugin_id, "version": __version__, "enabled": True})
                return _ok()
            case ("plugin", "uninstall", plugin_id, "--scope", "user"):
                if plugin_id not in ids:
                    return _failed("not found in installed plugins")
                self.plugins = [p for p in self.plugins if p["id"] != plugin_id]
                return _ok()
        raise AssertionError(f"unexpected command: {argv}")

    def commands(self) -> list[tuple[str, ...]]:
        return [argv[1:] for argv, _ in self.calls if "--json" not in argv]


def _ok(stdout: str = "") -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout=stdout, stderr="")


def _failed(stderr: str) -> SimpleNamespace:
    return SimpleNamespace(returncode=1, stdout="", stderr=stderr)


@pytest.fixture(autouse=True)
def claude_cli(monkeypatch: pytest.MonkeyPatch) -> FakeClaudeCli:
    fake = FakeClaudeCli()
    monkeypatch.setattr(claude_install, "_default_runner", fake)
    return fake


@pytest.fixture(autouse=True)
def runtime_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_install.shutil, "which", lambda command: f"/bin/{command}")


def _foreign(command: str) -> dict[str, object]:
    return {"type": "command", "command": command}


def _busy_settings() -> dict[str, object]:
    return {
        "model": "opus",
        "theme": "dark",
        "env": {"SOME_VAR": "1"},
        "enabledPlugins": {"caveman@local": True},
        "hooks": {
            "PreToolUse": [
                {"matcher": "Bash", "hooks": [_foreign("rtk hook claude")]},
                {"hooks": [_foreign("caveman-proxy native-hook claude")]},
            ],
            "PostToolUse": [{"hooks": [_foreign("caveman-proxy native-hook claude")]}],
            "SessionStart": [{"hooks": [_foreign("caveman-proxy native-hook claude")]}],
            "UserPromptSubmit": [{"hooks": [_foreign("caveman shrink-hook")]}],
            "SessionEnd": [{"hooks": [_foreign("caveman-proxy native-hook claude")]}],
        },
    }


@pytest.fixture
def settings_path(tmp_path: Path) -> Path:
    path = tmp_path / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_busy_settings(), indent=2), encoding="utf-8")
    return path


def _load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _commands(settings: dict[str, object], event: str) -> list[str]:
    return [
        handler["command"]
        for group in settings["hooks"].get(event, [])
        for handler in group["hooks"]
    ]


def _write_legacy_installation(settings_path: Path) -> tuple[Path, Path]:
    """What older releases left behind: hooks, a settings MCP entry, and skill files."""

    install_hooks(settings_path=settings_path, python_executable=Path("/old/python"))
    settings = _load(settings_path)
    # Claude Code can drop the ownership marker when it rewrites settings.json.
    for handler in settings["hooks"]["Stop"][-1]["hooks"]:
        handler.pop("htsaveOwned")
    settings["mcpServers"] = {
        "htsave": {"command": "/old/python", "args": ["-m", "htsave.mcp_server"]},
        "other": {"command": "other-server"},
    }
    settings_path.write_text(json.dumps(settings), encoding="utf-8")
    skills = settings_path.parent / "skills"
    flat = skills / "htsave.md"
    nested = skills / "htsave" / "SKILL.md"
    nested.parent.mkdir(parents=True)
    for skill in (flat, nested):
        skill.write_text("---\nname: htsave\nhtsave-managed: true\n---\n", encoding="utf-8")
    return flat, nested


def test_install_adds_the_marketplace_and_plugin_idempotently(
    settings_path: Path, claude_cli: FakeClaudeCli
) -> None:
    before = status(settings_path=settings_path)
    assert before.state is ClaudeIntegrationState.NOT_INSTALLED
    assert before.foreign_hook_handlers == 6

    first = install(settings_path=settings_path, marketplace="/repo/htsave")
    second = install(settings_path=settings_path, marketplace="/repo/htsave")

    assert first.healthy and second.healthy
    assert first.installed_version == __version__
    assert claude_cli.commands() == [
        ("plugin", "marketplace", "add", "/repo/htsave"),
        ("plugin", "install", PLUGIN_ID, "--scope", "user"),
        ("plugin", "install", PLUGIN_ID, "--scope", "user"),
    ]
    assert _load(settings_path) == _busy_settings(), "a clean settings file is never rewritten"


def test_install_removes_legacy_entries_and_preserves_foreign_ones(settings_path: Path) -> None:
    flat, nested = _write_legacy_installation(settings_path)
    foreign_skill = settings_path.parent / "skills" / "other.md"
    foreign_skill.write_text("other notes", encoding="utf-8")

    before = status(settings_path=settings_path)
    assert before.state is ClaudeIntegrationState.DRIFTED
    assert before.foreign_hook_handlers == 6
    assert set(before.drifts) == {
        "legacy-settings-hooks",
        "legacy-settings-mcp-server",
        "legacy-skill",
        "plugin-not-installed",
    }

    assert install(settings_path=settings_path, marketplace="/repo/htsave").healthy

    expected = _busy_settings()
    expected["mcpServers"] = {"other": {"command": "other-server"}}
    assert _load(settings_path) == expected
    assert not flat.exists() and not nested.exists() and not nested.parent.exists()
    assert foreign_skill.is_file()


def test_a_foreign_skill_at_the_legacy_path_is_kept(settings_path: Path) -> None:
    skill = settings_path.parent / "skills" / "htsave.md"
    skill.parent.mkdir()
    skill.write_text("my own htsave notes", encoding="utf-8")

    install(settings_path=settings_path, marketplace="/repo/htsave")

    assert skill.read_text(encoding="utf-8") == "my own htsave notes"


def test_install_without_the_runtime_on_path_explains_how_to_get_it(
    settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude_install.shutil, "which", lambda command: None)

    with pytest.raises(ClaudeIntegrationError, match="uv tool install htsave"):
        install(settings_path=settings_path, marketplace="/repo/htsave")
    assert "runtime-not-on-path" in status(settings_path=settings_path).drifts


def test_disabled_or_stale_plugin_is_reported_as_drift(
    settings_path: Path, claude_cli: FakeClaudeCli
) -> None:
    install(settings_path=settings_path, marketplace="/repo/htsave")
    claude_cli.plugins[0].update(enabled=False, version="0.9.0")

    result = status(settings_path=settings_path)

    assert result.state is ClaudeIntegrationState.DRIFTED
    assert {"plugin-disabled", "plugin-version-mismatch"} <= set(result.drifts)


def test_uninstall_previews_then_removes_plugin_marketplace_and_legacy(
    settings_path: Path, claude_cli: FakeClaudeCli
) -> None:
    install(settings_path=settings_path, marketplace="/repo/htsave")
    _write_legacy_installation(settings_path)

    preview = uninstall(settings_path=settings_path)
    assert preview["dry_run"] is True
    assert preview["would_remove_integration"] is True
    assert claude_cli.plugins, "a preview must change nothing"

    removed = uninstall(settings_path=settings_path, confirm=True)
    assert removed["removed"] is True
    assert removed["session_data_preserved"] is True
    assert removed["status"].state is ClaudeIntegrationState.NOT_INSTALLED
    assert claude_cli.plugins == [] and claude_cli.marketplaces == []
    expected = _busy_settings()
    expected["mcpServers"] = {"other": {"command": "other-server"}}
    assert _load(settings_path) == expected

    again = uninstall(settings_path=settings_path, confirm=True)
    assert again["removed"] is True


def test_a_failing_claude_command_leaves_settings_untouched(
    settings_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_legacy_installation(settings_path)
    legacy = _load(settings_path)

    def missing_claude(argv: Sequence[str], env: Mapping[str, str]) -> SimpleNamespace:
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(claude_install, "_default_runner", missing_claude)
    with pytest.raises(ClaudeIntegrationError, match="could not execute claude"):
        install(settings_path=settings_path, marketplace="/repo/htsave")
    assert _load(settings_path) == legacy


def test_a_settings_override_targets_its_own_claude_config_dir(
    settings_path: Path, claude_cli: FakeClaudeCli
) -> None:
    install(settings_path=settings_path, marketplace="/repo/htsave")

    assert {config_dir for _, config_dir in claude_cli.calls} == {str(settings_path.parent)}


def test_absent_and_malformed_settings_are_handled_distinctly(tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "settings.json"
    assert status(settings_path=missing).state is ClaudeIntegrationState.NOT_INSTALLED
    assert install(settings_path=missing, marketplace="/repo/htsave").healthy
    assert not missing.exists(), "the plugin needs no settings entries of its own"

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ClaudeIntegrationError, match="not valid JSON"):
        status(settings_path=broken)


def test_install_hooks_is_idempotent_and_recognizes_handlers_without_a_marker(
    settings_path: Path,
) -> None:
    install_hooks(settings_path=settings_path)
    settings = _load(settings_path)
    for groups in settings["hooks"].values():
        for group in groups:
            for handler in group["hooks"]:
                handler.pop("htsaveOwned", None)
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    install_hooks(settings_path=settings_path)

    reinstalled = _load(settings_path)
    for event in HOOK_EVENTS:
        owned = [c for c in _commands(reinstalled, event) if c.endswith("-m htsave.claude_hooks")]
        assert len(owned) == 1, f"{event} must not accumulate a second htsave handler"
    assert "rtk hook claude" in _commands(reinstalled, "PreToolUse")
    assert _commands(reinstalled, "UserPromptSubmit") == ["caveman shrink-hook"]


def test_installed_hook_command_can_actually_import_htsave(settings_path: Path) -> None:
    """A venv's bin/python is a symlink; resolving it breaks the hook import."""

    import subprocess
    import sys

    install_hooks(settings_path=settings_path)
    owned = [
        handler
        for group in _load(settings_path)["hooks"]["PostToolUse"]
        for handler in group["hooks"]
        if "htsaveOwned" in handler
    ]
    assert len(owned) == 1
    interpreter = owned[0]["command"].split('"')[1]

    assert interpreter == sys.executable
    probe = subprocess.run(
        (interpreter, "-c", "import htsave.claude_hooks"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr


def test_hooks_and_cli_resolve_one_state_root(tmp_path, monkeypatch) -> None:
    """A redirected MCP server and a default-rooted hook must not split a session."""

    from htsave.operator import configured_state_root
    from htsave.paths import default_state_root

    redirected = tmp_path / "redirected-state"
    redirected.mkdir()
    monkeypatch.setenv("HTSAVE_STATE_DIR", str(redirected))

    assert default_state_root() == redirected
    assert configured_state_root() == redirected.resolve()

    monkeypatch.delenv("HTSAVE_STATE_DIR")
    assert default_state_root() != redirected


def test_the_plugin_package_matches_the_runtime() -> None:
    marketplace = _load(REPOSITORY / ".claude-plugin" / "marketplace.json")
    assert marketplace["name"] == claude_install.MARKETPLACE_NAME
    (entry,) = marketplace["plugins"]
    plugin_root = (REPOSITORY / entry["source"]).resolve()

    manifest = _load(plugin_root / ".claude-plugin" / "plugin.json")
    assert f"{manifest['name']}@{marketplace['name']}" == PLUGIN_ID == f"{entry['name']}@htsave"
    assert manifest["version"] == __version__

    scripts = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]["scripts"]
    assert set(RUNTIME_COMMANDS) <= set(scripts)
    assert scripts["htsave-claude-hook"] == "htsave.claude_hooks:main"
    assert scripts["htsave-mcp"] == "htsave.mcp_server:main"

    hooks = _load(plugin_root / "hooks" / "hooks.json")["hooks"]
    assert sorted(hooks) == sorted(HOOK_EVENTS)
    assert {
        handler["command"] for groups in hooks.values() for g in groups for handler in g["hooks"]
    } == {"htsave-claude-hook"}
    servers = _load(plugin_root / ".mcp.json")["mcpServers"]
    assert servers == {"htsave": {"command": "htsave-mcp", "args": []}}

    skill = (plugin_root / "skills" / "htsave" / "SKILL.md").read_text(encoding="utf-8")
    assert skill.startswith("---\nname: htsave\n")
    assert "htsave-managed" not in skill, "the plugin owns its skill; no marker is needed"
