from __future__ import annotations

import json
import os
import shlex
import stat
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

import htsave.plugin as plugin_module
from htsave.plugin import (
    HOOK_MODULE,
    MARKETPLACE_NAME,
    MCP_MODULE,
    PLUGIN_NAME,
    PLUGIN_SELECTOR,
    CodexPluginManager,
    PluginDrift,
    PluginIntegrationError,
    PluginOwnershipError,
    PluginState,
    build_plugin_paths,
    materialize_plugin,
)

TEMPLATE_ROOT = Path(__file__).resolve().parents[1] / "plugin" / "htsave"


class FakeCodex:
    def __init__(self, python_executable: Path) -> None:
        self.python_executable = python_executable
        self.marketplace_root: Path | None = None
        self.installed = False
        self.enabled = False
        self.version: str | None = None
        self.mcp_command: str | None = None
        self.fail_marketplace_remove = False
        self.calls: list[tuple[str, ...]] = []

    def _plugin_record(self) -> dict[str, object]:
        assert self.marketplace_root is not None
        return {
            "pluginId": PLUGIN_SELECTOR,
            "name": PLUGIN_NAME,
            "marketplaceName": MARKETPLACE_NAME,
            "version": self.version,
            "installed": self.installed,
            "enabled": self.enabled,
            "source": {
                "source": "local",
                "path": str(self.marketplace_root / "plugins" / PLUGIN_NAME),
            },
            "marketplaceSource": {
                "sourceType": "local",
                "source": str(self.marketplace_root),
            },
            "installPolicy": "AVAILABLE",
            "authPolicy": "ON_INSTALL",
        }

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        call = tuple(argv)
        self.calls.append(call)
        arguments = call[1:]
        payload: object
        if arguments == ("plugin", "marketplace", "list", "--json"):
            marketplaces = []
            if self.marketplace_root is not None:
                marketplaces.append(
                    {
                        "name": MARKETPLACE_NAME,
                        "root": str(self.marketplace_root),
                        "marketplaceSource": {
                            "sourceType": "local",
                            "source": str(self.marketplace_root),
                        },
                    }
                )
            payload = {"marketplaces": marketplaces}
        elif arguments[:3] == ("plugin", "marketplace", "add"):
            root = Path(arguments[3])
            already_added = self.marketplace_root == root
            self.marketplace_root = root
            payload = {
                "marketplaceName": MARKETPLACE_NAME,
                "installedRoot": str(root),
                "alreadyAdded": already_added,
            }
        elif arguments == ("plugin", "list", "--available", "--json"):
            installed: list[object] = []
            available: list[object] = []
            if self.marketplace_root is not None:
                (installed if self.installed else available).append(self._plugin_record())
            payload = {"installed": installed, "available": available}
        elif arguments == ("plugin", "add", PLUGIN_SELECTOR, "--json"):
            assert self.marketplace_root is not None
            manifest = json.loads(
                (
                    self.marketplace_root
                    / "plugins"
                    / PLUGIN_NAME
                    / ".codex-plugin"
                    / "plugin.json"
                ).read_text(encoding="utf-8")
            )
            self.version = manifest["version"]
            self.installed = True
            self.enabled = True
            payload = {
                "pluginId": PLUGIN_SELECTOR,
                "name": PLUGIN_NAME,
                "marketplaceName": MARKETPLACE_NAME,
                "version": self.version,
                "installedPath": "/fake/cache/htsave",
                "authPolicy": "ON_INSTALL",
            }
        elif arguments == ("mcp", "list", "--json"):
            payload = []
            if self.installed and self.enabled:
                payload = [
                    {
                        "name": PLUGIN_NAME,
                        "enabled": True,
                        "transport": {
                            "type": "stdio",
                            "command": self.mcp_command or str(self.python_executable),
                            "args": ["-m", MCP_MODULE],
                            "env": None,
                            "env_vars": [],
                            "cwd": None,
                        },
                    }
                ]
        elif arguments == ("plugin", "remove", PLUGIN_SELECTOR, "--json"):
            self.installed = False
            self.enabled = False
            payload = {
                "pluginId": PLUGIN_SELECTOR,
                "name": PLUGIN_NAME,
                "marketplaceName": MARKETPLACE_NAME,
            }
        elif arguments == (
            "plugin",
            "marketplace",
            "remove",
            MARKETPLACE_NAME,
            "--json",
        ):
            if self.fail_marketplace_remove:
                return subprocess.CompletedProcess(call, 1, "", "marketplace removal failed")
            self.marketplace_root = None
            payload = {"marketplaceName": MARKETPLACE_NAME, "installedRoot": None}
        else:  # pragma: no cover - a new argv contract must be added deliberately
            raise AssertionError(f"unexpected Codex call: {call}")
        return subprocess.CompletedProcess(call, 0, json.dumps(payload), "")


def runtime_python(tmp_path: Path) -> Path:
    executable = tmp_path / "Python Runtime" / ("python.exe" if os.name == "nt" else "python")
    executable.parent.mkdir()
    executable.touch()
    return executable.resolve()


def test_materialize_renders_direct_mcp_hooks_marketplace_and_ownership(tmp_path) -> None:
    executable = runtime_python(tmp_path)
    paths = build_plugin_paths(tmp_path / "state")

    materialize_plugin(
        paths=paths,
        template_root=TEMPLATE_ROOT,
        python_executable=executable,
        package_version="1.2.3",
    )

    manifest = json.loads(
        (paths.plugin_root / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
    )
    assert manifest["name"] == PLUGIN_NAME
    assert manifest["version"] == "1.2.3"
    assert "hooks" not in manifest  # default hooks/hooks.json discovery

    mcp = json.loads((paths.plugin_root / ".mcp.json").read_text(encoding="utf-8"))
    assert set(mcp) == {PLUGIN_NAME}  # Runtime direct-map contract, not stale wrapper validation.
    assert mcp[PLUGIN_NAME]["command"] == str(executable)
    assert mcp[PLUGIN_NAME]["args"] == ["-m", MCP_MODULE]

    hooks = json.loads((paths.plugin_root / "hooks" / "hooks.json").read_text(encoding="utf-8"))
    handlers = [
        handler
        for groups in hooks["hooks"].values()
        for group in groups
        for handler in group["hooks"]
    ]
    expected_argv = [str(executable), "-m", HOOK_MODULE]
    assert handlers
    assert {handler["command"] for handler in handlers} == {shlex.join(expected_argv)}
    assert {handler["commandWindows"] for handler in handlers} == {
        subprocess.list2cmdline(expected_argv)
    }
    session_start_handler = hooks["hooks"]["SessionStart"][0]["hooks"][0]
    assert session_start_handler["additionalContextLimit"] == 0

    marketplace = json.loads(paths.marketplace_manifest.read_text(encoding="utf-8"))
    assert marketplace["name"] == MARKETPLACE_NAME
    assert marketplace["plugins"][0]["source"]["path"] == "./plugins/htsave"
    marker = json.loads(paths.ownership_marker.read_text(encoding="utf-8"))
    assert marker == {
        "schemaVersion": 1,
        "owner": "htsave",
        "kind": "codex-managed-marketplace",
        "marketplaceName": MARKETPLACE_NAME,
        "pluginName": PLUGIN_NAME,
    }

    if os.name != "nt":
        assert stat.S_IMODE(paths.marketplace_root.stat().st_mode) == 0o700
        for file_path in paths.marketplace_root.rglob("*"):
            expected = 0o700 if file_path.is_dir() else 0o600
            assert stat.S_IMODE(file_path.stat().st_mode) == expected


def test_materialize_rolls_back_when_rendering_fails(tmp_path, monkeypatch) -> None:
    executable = runtime_python(tmp_path)
    paths = build_plugin_paths(tmp_path / "state")
    materialize_plugin(
        paths=paths,
        template_root=TEMPLATE_ROOT,
        python_executable=executable,
        package_version="1.0.0",
    )
    original = (paths.plugin_root / ".codex-plugin" / "plugin.json").read_bytes()

    def fail_render(*args, **kwargs) -> None:
        raise RuntimeError("render failed")

    monkeypatch.setattr(plugin_module, "_render_plugin_tree", fail_render)
    with pytest.raises(RuntimeError, match="render failed"):
        materialize_plugin(
            paths=paths,
            template_root=TEMPLATE_ROOT,
            python_executable=executable,
            package_version="2.0.0",
        )

    assert (paths.plugin_root / ".codex-plugin" / "plugin.json").read_bytes() == original
    assert not list(paths.state_root.glob(".codex-marketplace.stage-*"))


def test_materialize_refuses_unowned_existing_tree(tmp_path) -> None:
    paths = build_plugin_paths(tmp_path / "state")
    paths.marketplace_root.mkdir(parents=True)
    sentinel = paths.marketplace_root / "keep.txt"
    sentinel.write_text("user-owned", encoding="utf-8")

    with pytest.raises(PluginOwnershipError, match="unowned path"):
        materialize_plugin(
            paths=paths,
            template_root=TEMPLATE_ROOT,
            python_executable=runtime_python(tmp_path),
        )

    assert sentinel.read_text(encoding="utf-8") == "user-owned"


def test_install_is_idempotent_and_uses_only_argv_runner(tmp_path) -> None:
    executable = runtime_python(tmp_path)
    fake = FakeCodex(executable)
    manager = CodexPluginManager(
        state_root=tmp_path / "state",
        template_root=TEMPLATE_ROOT,
        python_executable=executable,
        package_version="1.2.3",
        runner=fake,
    )

    first = manager.install()
    second = manager.install()

    assert first.state is PluginState.INSTALLED_ENABLED
    assert second.healthy
    assert (
        fake.calls.count(
            ("codex", "plugin", "marketplace", "add", str(manager.paths.marketplace_root), "--json")
        )
        == 2
    )
    assert fake.calls.count(("codex", "plugin", "add", PLUGIN_SELECTOR, "--json")) == 2
    assert fake.version == "1.2.3"


def test_status_reports_disabled_and_drift_states(tmp_path) -> None:
    executable = runtime_python(tmp_path)
    fake = FakeCodex(executable)
    manager = CodexPluginManager(
        state_root=tmp_path / "state",
        template_root=TEMPLATE_ROOT,
        python_executable=executable,
        package_version="1.2.3",
        runner=fake,
    )
    materialize_plugin(
        paths=manager.paths,
        template_root=TEMPLATE_ROOT,
        python_executable=executable,
        package_version="1.2.3",
    )
    fake.marketplace_root = manager.paths.marketplace_root
    fake.installed = True
    fake.enabled = False
    fake.version = "1.2.3"

    assert manager.status().state is PluginState.INSTALLED_DISABLED

    fake.enabled = True
    fake.version = "0.9.0"
    fake.mcp_command = str(tmp_path / "wrong-python")
    status = manager.status()
    assert status.state is PluginState.DRIFTED
    assert PluginDrift.PLUGIN_VERSION in status.drifts
    assert PluginDrift.MCP_COMMAND in status.drifts


def test_status_reports_marketplace_name_conflict(tmp_path) -> None:
    executable = runtime_python(tmp_path)
    fake = FakeCodex(executable)
    fake.marketplace_root = tmp_path / "someone-elses-marketplace"
    fake.version = "1.2.3"
    manager = CodexPluginManager(
        state_root=tmp_path / "state",
        template_root=TEMPLATE_ROOT,
        python_executable=executable,
        package_version="1.2.3",
        runner=fake,
    )

    status = manager.status()

    assert status.state is PluginState.CONFLICT
    assert PluginDrift.MARKETPLACE_ROOT in status.drifts


def test_uninstall_removes_only_owned_marketplace_and_preserves_sessions(tmp_path) -> None:
    executable = runtime_python(tmp_path)
    fake = FakeCodex(executable)
    state_root = tmp_path / "state"
    session_file = state_root / "sessions" / "session-key" / "registry.sqlite3"
    session_file.parent.mkdir(parents=True)
    session_file.write_bytes(b"session-data")
    manager = CodexPluginManager(
        state_root=state_root,
        template_root=TEMPLATE_ROOT,
        python_executable=executable,
        package_version="1.2.3",
        runner=fake,
    )
    manager.install()

    first = manager.uninstall()
    second = manager.uninstall()

    assert first.state is PluginState.NOT_INSTALLED
    assert second.state is PluginState.NOT_INSTALLED
    assert not manager.paths.marketplace_root.exists()
    assert session_file.read_bytes() == b"session-data"
    assert fake.calls.count(("codex", "plugin", "remove", PLUGIN_SELECTOR, "--json")) == 1


def test_uninstall_never_deletes_unowned_tree(tmp_path) -> None:
    executable = runtime_python(tmp_path)
    fake = FakeCodex(executable)
    manager = CodexPluginManager(
        state_root=tmp_path / "state",
        template_root=TEMPLATE_ROOT,
        python_executable=executable,
        runner=fake,
    )
    manager.paths.marketplace_root.mkdir(parents=True)
    sentinel = manager.paths.marketplace_root / "keep.txt"
    sentinel.write_text("keep", encoding="utf-8")

    with pytest.raises(PluginOwnershipError, match="unowned path"):
        manager.uninstall()

    assert sentinel.read_text(encoding="utf-8") == "keep"


def test_uninstall_keeps_owned_tree_when_codex_removal_partially_fails(tmp_path) -> None:
    executable = runtime_python(tmp_path)
    fake = FakeCodex(executable)
    manager = CodexPluginManager(
        state_root=tmp_path / "state",
        template_root=TEMPLATE_ROOT,
        python_executable=executable,
        package_version="1.2.3",
        runner=fake,
    )
    manager.install()
    fake.fail_marketplace_remove = True

    with pytest.raises(PluginIntegrationError, match="marketplace removal failed"):
        manager.uninstall()

    assert manager.paths.marketplace_root.is_dir()
    assert manager.paths.ownership_marker.is_file()


def test_install_refuses_registered_marketplace_name_collision(tmp_path) -> None:
    executable = runtime_python(tmp_path)
    fake = FakeCodex(executable)
    fake.marketplace_root = tmp_path / "foreign-root"
    manager = CodexPluginManager(
        state_root=tmp_path / "state",
        template_root=TEMPLATE_ROOT,
        python_executable=executable,
        runner=fake,
    )

    with pytest.raises(PluginOwnershipError, match="owned by another path"):
        manager.install()

    assert not manager.paths.marketplace_root.exists()


def test_failed_codex_json_is_observable_without_writing_config(tmp_path) -> None:
    executable = runtime_python(tmp_path)

    def failing_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(argv, 1, "", "config unavailable")

    manager = CodexPluginManager(
        state_root=tmp_path / "state",
        template_root=TEMPLATE_ROOT,
        python_executable=executable,
        runner=failing_runner,
    )

    status = manager.status()
    assert status.state is PluginState.CODEX_UNAVAILABLE
    assert "config unavailable" in status.notes[0]
    with pytest.raises(PluginIntegrationError, match="config unavailable"):
        manager.install()
    assert not manager.paths.marketplace_root.exists()
