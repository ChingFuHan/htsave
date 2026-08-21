"""Claude Code settings integration tests.

The fixture mirrors a real, busy ``settings.json``: several unrelated handlers
per event (this machine runs `rtk` and `caveman` on most events) plus unrelated
top-level keys.  Preserving all of that is the whole point of the module.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from htsave.claude_install import (
    HOOK_EVENTS,
    ClaudeIntegrationError,
    ClaudeIntegrationState,
    install,
    status,
    uninstall,
)


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


def test_install_is_idempotent_and_preserves_foreign_hooks(settings_path: Path) -> None:
    before = status(settings_path=settings_path)
    assert before.state is ClaudeIntegrationState.NOT_INSTALLED
    assert before.foreign_hook_handlers == 6

    first = install(settings_path=settings_path)
    after_first = _load(settings_path)
    second = install(settings_path=settings_path)
    after_second = _load(settings_path)

    assert first.healthy and second.healthy
    assert after_first == after_second, "a second install must not accumulate handlers"
    assert sorted(first.hook_events) == sorted(HOOK_EVENTS)
    assert first.foreign_hook_handlers == 6

    # Every unrelated handler and top-level key survives untouched.
    assert "rtk hook claude" in _commands(after_first, "PreToolUse")
    assert "caveman-proxy native-hook claude" in _commands(after_first, "PostToolUse")
    assert _commands(after_first, "UserPromptSubmit") == ["caveman shrink-hook"]
    assert _commands(after_first, "SessionEnd") == ["caveman-proxy native-hook claude"]
    for key in ("model", "theme", "env", "enabledPlugins"):
        assert after_first[key] == _busy_settings()[key]


def test_install_registers_the_mcp_server_for_the_running_interpreter(
    settings_path: Path,
) -> None:
    install(settings_path=settings_path)
    settings = _load(settings_path)

    server = settings["mcpServers"]["htsave"]
    assert server["args"] == ["-m", "htsave.mcp_server"]
    assert Path(server["command"]).exists()
    assert server["htsaveOwned"]["owner"] == "htsave"


def test_uninstall_previews_then_removes_only_owned_entries(settings_path: Path) -> None:
    install(settings_path=settings_path)

    preview = uninstall(settings_path=settings_path)
    assert preview["dry_run"] is True
    assert preview["would_remove_integration"] is True
    assert status(settings_path=settings_path).healthy, "a preview must change nothing"

    removed = uninstall(settings_path=settings_path, confirm=True)
    assert removed["removed"] is True
    assert removed["session_data_preserved"] is True

    after = _load(settings_path)
    assert after == _busy_settings(), "settings must return to their original content"

    # Removal is idempotent.
    again = uninstall(settings_path=settings_path, confirm=True)
    assert again["removed"] is True
    assert _load(settings_path) == _busy_settings()


def test_install_refuses_a_foreign_mcp_server_of_the_same_name(settings_path: Path) -> None:
    settings = _busy_settings()
    settings["mcpServers"] = {"htsave": {"command": "/somewhere/else", "args": []}}
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    with pytest.raises(ClaudeIntegrationError, match="not owned by htsave"):
        install(settings_path=settings_path)

    assert _load(settings_path)["mcpServers"]["htsave"]["command"] == "/somewhere/else"


def test_interpreter_drift_is_reported_rather_than_silently_accepted(
    settings_path: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other-python"
    other.write_text("", encoding="utf-8")

    install(settings_path=settings_path, python_executable=other)
    drifted = status(settings_path=settings_path)

    assert not drifted.healthy
    assert drifted.state is ClaudeIntegrationState.DRIFTED
    assert any(item.startswith("interpreter-mismatch:") for item in drifted.drifts)


def test_partial_installation_is_reported_as_drift(settings_path: Path) -> None:
    install(settings_path=settings_path)
    settings = _load(settings_path)
    del settings["hooks"]["Stop"]
    settings_path.write_text(json.dumps(settings), encoding="utf-8")

    result = status(settings_path=settings_path)

    assert result.state is ClaudeIntegrationState.DRIFTED
    assert "missing-event:Stop" in result.drifts


def test_absent_and_malformed_settings_are_handled_distinctly(tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "settings.json"
    assert status(settings_path=missing).state is ClaudeIntegrationState.NOT_INSTALLED

    installed = install(settings_path=missing)
    assert installed.healthy
    assert missing.is_file()

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    with pytest.raises(ClaudeIntegrationError, match="not valid JSON"):
        status(settings_path=broken)


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


def test_installed_command_can_actually_import_htsave(settings_path: Path) -> None:
    """A venv's bin/python is a symlink; resolving it breaks the hook import."""

    import subprocess
    import sys

    install(settings_path=settings_path)
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


def test_install_creates_skill_file(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    skill = tmp_path / ".claude" / "skills" / "htsave.md"
    install(settings_path=settings, skill_path=skill)
    assert skill.is_file()
    content = skill.read_text(encoding="utf-8")
    assert "htsave-managed: true" in content
    assert "HTSAVE/1" in content


def test_install_is_idempotent_for_skill(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    skill = tmp_path / ".claude" / "skills" / "htsave.md"
    install(settings_path=settings, skill_path=skill)
    first = skill.read_text(encoding="utf-8")
    install(settings_path=settings, skill_path=skill)
    assert skill.read_text(encoding="utf-8") == first


def test_install_refuses_foreign_skill(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    skill = tmp_path / ".claude" / "skills" / "htsave.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("my custom htsave notes", encoding="utf-8")
    with pytest.raises(ClaudeIntegrationError, match="not owned by htsave"):
        install(settings_path=settings, skill_path=skill)


def test_uninstall_removes_owned_skill(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    skill = tmp_path / ".claude" / "skills" / "htsave.md"
    install(settings_path=settings, skill_path=skill)
    assert skill.is_file()
    uninstall(settings_path=settings, skill_path=skill, confirm=True)
    assert not skill.exists()


def test_uninstall_preserves_foreign_skill(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    foreign_skill = tmp_path / ".claude" / "skills" / "other.md"
    foreign_skill.parent.mkdir(parents=True)
    foreign_skill.write_text("other custom notes", encoding="utf-8")
    install(settings_path=settings)
    uninstall(settings_path=settings, confirm=True)
    assert foreign_skill.is_file()


def test_status_reports_skill_drift(tmp_path: Path) -> None:
    settings = tmp_path / ".claude" / "settings.json"
    skill = tmp_path / ".claude" / "skills" / "htsave.md"
    install(settings_path=settings, skill_path=skill)
    skill.unlink()
    result = status(settings_path=settings, skill_path=skill)
    assert not result.healthy
    assert "skill-not-installed" in result.drifts

