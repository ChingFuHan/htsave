"""Antigravity CLI (agy) integration tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from htsave.agy_install import (
    AgyIntegrationError,
    AgyIntegrationState,
    install,
    status,
    uninstall,
)
from htsave.cli import main


def test_install_creates_skill_mcp_and_hooks(tmp_path: Path) -> None:
    result = install(home=tmp_path)
    assert result.healthy
    assert result.skill_installed
    assert result.mcp_registered
    assert result.hooks_registered

    # Skill file exists with correct frontmatter.
    skill = tmp_path / ".gemini" / "config" / "skills" / "htsave" / "SKILL.md"
    assert skill.is_file()
    content = skill.read_text(encoding="utf-8")
    assert "name: htsave" in content
    assert "htsave_read" in content

    # MCP config has htsave entry.
    mcp = json.loads(
        (tmp_path / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8")
    )
    assert "htsave" in mcp["mcpServers"]
    assert mcp["mcpServers"]["htsave"]["htsaveOwned"]["owner"] == "htsave"

    # Hooks config has htsave entry.
    hooks = json.loads(
        (tmp_path / ".gemini" / "config" / "hooks.json").read_text(encoding="utf-8")
    )
    assert "htsave" in hooks
    assert hooks["htsave"]["htsaveOwned"]["owner"] == "htsave"


def test_install_is_idempotent(tmp_path: Path) -> None:
    first = install(home=tmp_path)
    assert first.healthy
    mcp_before = (tmp_path / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8")
    hooks_before = (tmp_path / ".gemini" / "config" / "hooks.json").read_text(encoding="utf-8")

    second = install(home=tmp_path)
    assert second.healthy
    mcp_after = (tmp_path / ".gemini" / "config" / "mcp_config.json").read_text(encoding="utf-8")
    hooks_after = (tmp_path / ".gemini" / "config" / "hooks.json").read_text(encoding="utf-8")

    assert mcp_before == mcp_after
    assert hooks_before == hooks_after


def test_install_refuses_foreign_mcp_server(tmp_path: Path) -> None:
    mcp_path = tmp_path / ".gemini" / "config" / "mcp_config.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(
        json.dumps({"mcpServers": {"htsave": {"command": "/other", "args": []}}}),
        encoding="utf-8",
    )
    with pytest.raises(AgyIntegrationError, match="not owned by htsave"):
        install(home=tmp_path)


def test_uninstall_previews_then_removes(tmp_path: Path) -> None:
    install(home=tmp_path)

    preview = uninstall(home=tmp_path)
    assert preview["dry_run"] is True
    assert preview["would_remove_integration"] is True
    assert status(home=tmp_path).healthy, "preview must not change anything"

    removed = uninstall(home=tmp_path, confirm=True)
    assert removed["removed"] is True
    after = status(home=tmp_path)
    assert after.state is AgyIntegrationState.NOT_INSTALLED
    assert not (tmp_path / ".gemini" / "config" / "skills" / "htsave" / "SKILL.md").exists()


def test_uninstall_preserves_foreign_entries(tmp_path: Path) -> None:
    # Pre-populate with a foreign MCP entry.
    mcp_path = tmp_path / ".gemini" / "config" / "mcp_config.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text(
        json.dumps({"mcpServers": {"other-tool": {"command": "/bin/other"}}}),
        encoding="utf-8",
    )
    # Pre-populate with a foreign hooks entry.
    hooks_path = tmp_path / ".gemini" / "config" / "hooks.json"
    hooks_path.write_text(
        json.dumps({"my-linter": {"PreToolUse": []}}),
        encoding="utf-8",
    )

    install(home=tmp_path)
    uninstall(home=tmp_path, confirm=True)

    mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
    assert "other-tool" in mcp["mcpServers"]
    assert "htsave" not in mcp.get("mcpServers", {})

    hooks = json.loads(hooks_path.read_text(encoding="utf-8"))
    assert "my-linter" in hooks
    assert "htsave" not in hooks


def test_status_reports_partial_as_drift(tmp_path: Path) -> None:
    install(home=tmp_path)
    # Remove just the skill file.
    (tmp_path / ".gemini" / "config" / "skills" / "htsave" / "SKILL.md").unlink()
    result = status(home=tmp_path)
    assert result.state is AgyIntegrationState.DRIFTED
    assert "skill-missing" in result.drifts


def test_skill_template_has_valid_frontmatter(tmp_path: Path) -> None:
    install(home=tmp_path)
    skill = tmp_path / ".gemini" / "config" / "skills" / "htsave" / "SKILL.md"
    content = skill.read_text(encoding="utf-8")
    assert content.startswith("---\n")
    assert "name: htsave" in content
    assert "description:" in content


def test_cli_agy_install_status_uninstall(tmp_path: Path) -> None:
    # CLI install
    assert main(["--json", "agy", "install", "--home", str(tmp_path)]) == 0
    # CLI status
    assert main(["--json", "agy", "status", "--home", str(tmp_path)]) == 0
    # CLI uninstall (dry run)
    assert main(["--json", "agy", "uninstall", "--home", str(tmp_path)]) == 0
    # CLI uninstall (--yes)
    assert main(["--json", "agy", "uninstall", "--home", str(tmp_path), "--yes"]) == 0
    # CLI status should now report not installed (exit code 1)
    assert main(["--json", "agy", "status", "--home", str(tmp_path)]) == 1


def test_install_handles_empty_json_files(tmp_path: Path) -> None:
    mcp_path = tmp_path / ".gemini" / "config" / "mcp_config.json"
    hooks_path = tmp_path / ".gemini" / "config" / "hooks.json"
    mcp_path.parent.mkdir(parents=True)
    mcp_path.write_text("", encoding="utf-8")
    hooks_path.write_text("   \n", encoding="utf-8")

    result = install(home=tmp_path)
    assert result.healthy

