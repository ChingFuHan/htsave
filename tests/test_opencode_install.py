"""OpenCode CLI integration tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from htsave.cli import main
from htsave.opencode_install import (
    OpenCodeIntegrationError,
    OpenCodeIntegrationState,
    install,
    status,
    uninstall,
)


def test_install_creates_plugin_with_ownership_tag(tmp_path: Path) -> None:
    result = install(project_dir=tmp_path)
    assert result.healthy
    assert result.plugin_installed

    plugin = tmp_path / ".opencode" / "plugins" / "htsave.js"
    assert plugin.is_file()
    content = plugin.read_text(encoding="utf-8")
    assert "htsaveOwned" in content
    assert '"owner": "htsave"' in content
    assert "export default async function htsave" in content
    assert "execute.before" in content
    assert "execute.after" in content


def test_install_is_idempotent(tmp_path: Path) -> None:
    first = install(project_dir=tmp_path)
    assert first.healthy
    plugin = tmp_path / ".opencode" / "plugins" / "htsave.js"
    content_before = plugin.read_text(encoding="utf-8")

    second = install(project_dir=tmp_path)
    assert second.healthy
    content_after = plugin.read_text(encoding="utf-8")
    assert content_before == content_after


def test_install_refuses_foreign_plugin(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".opencode" / "plugins"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "htsave.js").write_text(
        "// Someone else's plugin\nexport default {};\n",
        encoding="utf-8",
    )
    with pytest.raises(OpenCodeIntegrationError, match="not owned by htsave"):
        install(project_dir=tmp_path)


def test_uninstall_previews_then_removes(tmp_path: Path) -> None:
    install(project_dir=tmp_path)

    preview = uninstall(project_dir=tmp_path)
    assert preview["dry_run"] is True
    assert preview["would_remove_integration"] is True
    assert status(project_dir=tmp_path).healthy

    removed = uninstall(project_dir=tmp_path, confirm=True)
    assert removed["removed"] is True
    after = status(project_dir=tmp_path)
    assert after.state is OpenCodeIntegrationState.NOT_INSTALLED
    assert not (tmp_path / ".opencode" / "plugins" / "htsave.js").exists()


def test_uninstall_preserves_foreign_files(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".opencode" / "plugins"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "other.mjs").write_text("// other plugin\n", encoding="utf-8")

    install(project_dir=tmp_path)
    uninstall(project_dir=tmp_path, confirm=True)

    assert (plugin_dir / "other.mjs").is_file()
    assert not (plugin_dir / "htsave.js").exists()


def test_status_reports_version_drift(tmp_path: Path) -> None:
    install(project_dir=tmp_path)
    plugin = tmp_path / ".opencode" / "plugins" / "htsave.js"
    content = plugin.read_text(encoding="utf-8")
    content = content.replace('"version": "', '"version": "0.0.0-old')
    plugin.write_text(content, encoding="utf-8")

    result = status(project_dir=tmp_path)
    assert result.state is OpenCodeIntegrationState.DRIFTED
    assert "version-mismatch" in result.drifts


def test_absent_directory_reports_not_installed(tmp_path: Path) -> None:
    result = status(project_dir=tmp_path)
    assert result.state is OpenCodeIntegrationState.NOT_INSTALLED
    assert not result.plugin_installed


def test_plugin_template_has_valid_structure(tmp_path: Path) -> None:
    install(project_dir=tmp_path)
    plugin = tmp_path / ".opencode" / "plugins" / "htsave.js"
    content = plugin.read_text(encoding="utf-8")
    assert "Plugin.define" not in content
    assert "import { execFileSync }" in content
    assert "async config(config)" in content
    assert "htsaveOwned" in content


def test_installed_python_path_matches_running_interpreter(
    tmp_path: Path,
) -> None:
    install(project_dir=tmp_path)
    result = status(project_dir=tmp_path)
    assert result.python_executable == sys.executable


def test_cli_opencode_install_status_uninstall(
    tmp_path: Path, capsys
) -> None:
    assert main(["--json", "opencode", "install", "--project-dir", str(tmp_path)]) == 0
    assert main(["--json", "opencode", "status", "--project-dir", str(tmp_path)]) == 0
    assert main(["--json", "opencode", "uninstall", "--project-dir", str(tmp_path)]) == 0
    assert main(["--json", "opencode", "uninstall", "--project-dir", str(tmp_path), "--yes"]) == 0
    assert main(["--json", "opencode", "status", "--project-dir", str(tmp_path)]) == 1


def test_install_handles_empty_plugins_directory(tmp_path: Path) -> None:
    plugin_dir = tmp_path / ".opencode" / "plugins"
    plugin_dir.mkdir(parents=True)
    result = install(project_dir=tmp_path)
    assert result.healthy


def test_foreign_skill_blocks_install_before_plugin_write(tmp_path: Path) -> None:
    skill = tmp_path / ".opencode/skills/htsave/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("foreign skill")
    with pytest.raises(OpenCodeIntegrationError, match="not owned"):
        install(project_dir=tmp_path)
    assert not (tmp_path / ".opencode/plugins/htsave.js").exists()
    assert skill.read_text() == "foreign skill"


def test_upgrade_owned_legacy_plugin_and_detect_same_version_drift(tmp_path: Path) -> None:
    install(project_dir=tmp_path)
    plugin = tmp_path / ".opencode/plugins/htsave.js"
    current = plugin.read_text()
    plugin.write_text(current.splitlines()[0] + "\nexport default Plugin.define({});\n")
    assert not status(project_dir=tmp_path).healthy
    install(project_dir=tmp_path)
    assert plugin.read_text() == current
    plugin.write_text(current + "\n// drift\n")
    assert "plugin-content-mismatch" in status(project_dir=tmp_path).drifts


def test_skill_drift_and_foreign_skill_survive_uninstall(tmp_path: Path) -> None:
    install(project_dir=tmp_path)
    skill = tmp_path / ".opencode/skills/htsave/SKILL.md"
    skill.write_text("foreign replacement")
    assert "skill-content-mismatch" in status(project_dir=tmp_path).drifts
    uninstall(project_dir=tmp_path, confirm=True)
    assert skill.read_text() == "foreign replacement"


def test_unavailable_python_does_not_install(tmp_path: Path) -> None:
    with pytest.raises(OpenCodeIntegrationError, match="cannot import"):
        install(project_dir=tmp_path, python_executable=tmp_path / "missing-python")
    assert not (tmp_path / ".opencode/plugins/htsave.js").exists()


def test_migrate_owned_mjs_and_preserve_foreign_mjs(tmp_path: Path) -> None:
    legacy = tmp_path / ".opencode/plugins/htsave.mjs"
    legacy.parent.mkdir(parents=True)
    legacy.write_text('// htsaveOwned: {"owner": "htsave", "version": "1.0.0"}\n')
    assert "legacy-plugin-not-discoverable" in status(project_dir=tmp_path).drifts
    install(project_dir=tmp_path)
    assert not legacy.exists()
    legacy.write_text("foreign legacy plugin")
    install(project_dir=tmp_path)
    uninstall(project_dir=tmp_path, confirm=True)
    assert legacy.read_text() == "foreign legacy plugin"


def test_partial_update_rolls_back_owned_files(tmp_path: Path, monkeypatch) -> None:
    import htsave.opencode_install as integration

    install(project_dir=tmp_path)
    plugin = tmp_path / integration.PLUGIN_FILE
    skill = tmp_path / integration.SKILL_FILE
    old_plugin, old_skill = plugin.read_bytes(), skill.read_bytes()
    original_write = integration.atomic_write_text
    original_skill = integration._skill_content()
    monkeypatch.setattr(
        integration, "_skill_content", lambda: original_skill + "\nNew instructions\n",
    )

    def fail_plugin_write(path, content, *, mode):
        if path == plugin:
            raise OSError("injected write failure")
        original_write(path, content, mode=mode)

    monkeypatch.setattr(integration, "atomic_write_text", fail_plugin_write)
    with pytest.raises(OSError, match="injected"):
        install(project_dir=tmp_path)
    assert plugin.read_bytes() == old_plugin
    assert skill.read_bytes() == old_skill
