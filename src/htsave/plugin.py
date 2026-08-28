"""Package and manage the local Codex plugin without touching session data."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from . import __version__
from .errors import HtsaveError, SecurityBoundaryError
from .paths import default_state_root, ensure_private_directory

PLUGIN_NAME = "htsave"
MARKETPLACE_NAME = "htsave-local"
PLUGIN_SELECTOR = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"
OWNERSHIP_FILE = ".htsave-owned.json"
MCP_MODULE = "htsave.mcp_server"
HOOK_MODULE = "htsave.codex_hooks"
_MCP_RUNTIME_IMPORT = "import htsave.mcp_server"
_MCP_RUNTIME_TIMEOUT_SECONDS = 5


class PluginIntegrationError(HtsaveError):
    """The managed plugin installation could not be inspected or changed safely."""


class PluginOwnershipError(PluginIntegrationError):
    """A path or marketplace name is present but is not owned by htsave."""


class CommandResult(Protocol):
    returncode: int
    stdout: str | None
    stderr: str | None


Runner = Callable[[Sequence[str]], CommandResult]


class PluginState(StrEnum):
    NOT_INSTALLED = "not-installed"
    INSTALLED_DISABLED = "installed-disabled"
    INSTALLED_ENABLED = "installed-enabled"
    DRIFTED = "drifted"
    CONFLICT = "conflict"
    CODEX_UNAVAILABLE = "codex-unavailable"


class PluginDrift(StrEnum):
    MANAGED_ROOT_MISSING = "managed-root-missing"
    OWNERSHIP_MISMATCH = "ownership-mismatch"
    MATERIALIZED_MANIFEST = "materialized-manifest-mismatch"
    MATERIALIZED_MCP = "materialized-mcp-mismatch"
    MATERIALIZED_HOOKS = "materialized-hooks-mismatch"
    MARKETPLACE_NOT_REGISTERED = "marketplace-not-registered"
    MARKETPLACE_ROOT = "marketplace-root-mismatch"
    PLUGIN_NOT_DISCOVERABLE = "plugin-not-discoverable"
    PLUGIN_VERSION = "plugin-version-mismatch"
    PLUGIN_SOURCE = "plugin-source-mismatch"
    MCP_SERVER_MISSING = "mcp-server-missing"
    MCP_COMMAND = "mcp-command-mismatch"
    MCP_ARGS = "mcp-args-mismatch"
    MCP_RUNTIME = "mcp-runtime-unavailable"


@dataclass(frozen=True, slots=True)
class PluginPaths:
    state_root: Path
    marketplace_root: Path
    marketplace_manifest: Path
    plugin_root: Path
    ownership_marker: Path


@dataclass(frozen=True, slots=True)
class PluginStatus:
    state: PluginState
    expected_version: str
    installed_version: str | None
    enabled: bool
    marketplace_registered: bool
    marketplace_root: Path
    drifts: tuple[PluginDrift, ...] = ()
    notes: tuple[str, ...] = ()

    @property
    def healthy(self) -> bool:
        return self.state is PluginState.INSTALLED_ENABLED and not self.drifts


def build_plugin_paths(state_root: Path | None = None) -> PluginPaths:
    requested_root = (state_root or default_state_root()).expanduser()
    if requested_root.is_symlink():
        raise SecurityBoundaryError("refusing symbolic-link plugin state root")
    root = requested_root.resolve()
    marketplace_root = root / "codex-marketplace"
    return PluginPaths(
        state_root=root,
        marketplace_root=marketplace_root,
        marketplace_manifest=marketplace_root / ".agents" / "plugins" / "marketplace.json",
        plugin_root=marketplace_root / "plugins" / PLUGIN_NAME,
        ownership_marker=marketplace_root / OWNERSHIP_FILE,
    )


def default_template_root() -> Path:
    packaged = Path(__file__).resolve().parent / "_plugin" / PLUGIN_NAME
    if packaged.is_dir():
        return packaged
    source_tree = Path(__file__).resolve().parents[2] / "plugin" / PLUGIN_NAME
    if source_tree.is_dir():
        return source_tree
    raise PluginIntegrationError("packaged Codex plugin template is missing")


def _ownership_payload() -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "owner": "htsave",
        "kind": "codex-managed-marketplace",
        "marketplaceName": MARKETPLACE_NAME,
        "pluginName": PLUGIN_NAME,
    }


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PluginIntegrationError(f"invalid JSON file: {path}") from error


def _is_owned(root: Path) -> bool:
    marker = root / OWNERSHIP_FILE
    if not marker.is_file():
        return False
    try:
        return _read_json(marker) == _ownership_payload()
    except PluginIntegrationError:
        return False


def _require_owned(root: Path) -> None:
    if root.exists() and not _is_owned(root):
        raise PluginOwnershipError(f"refusing to replace or remove unowned path: {root}")


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if os.name != "nt":
            os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _secure_tree(root: Path) -> None:
    ensure_private_directory(root)
    if os.name == "nt":
        return
    for candidate in root.rglob("*"):
        os.chmod(candidate, 0o700 if candidate.is_dir() else 0o600)


def _command_lines(python_executable: Path) -> tuple[str, str]:
    argv = [str(python_executable), "-m", HOOK_MODULE]
    return shlex.join(argv), subprocess.list2cmdline(argv)


def _absolute_lexical_path(path: Path) -> Path:
    """Make a path absolute without resolving symbolic links."""

    return Path(os.path.abspath(os.fspath(path.expanduser())))


def _runtime_python_path(python_executable: Path | None = None) -> Path:
    """Select the interpreter path that Codex should use to run htsave."""

    if python_executable is not None:
        return _absolute_lexical_path(python_executable)

    scripts_directory = "Scripts" if os.name == "nt" else "bin"
    launcher_name = "python.exe" if os.name == "nt" else "python"
    environment_roots: list[Path] = []
    if sys.prefix != sys.base_prefix:
        environment_roots.append(Path(sys.prefix))
    for variable in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        value = os.environ.get(variable)
        if value:
            environment_roots.append(Path(value))

    for root in environment_roots:
        candidate = _absolute_lexical_path(root / scripts_directory / launcher_name)
        if candidate.is_file():
            return candidate
    return _absolute_lexical_path(Path(sys.executable))


def _mcp_runtime_importable(python_executable: Path) -> bool:
    """Check that the configured interpreter can import the MCP server."""

    try:
        result = subprocess.run(
            [str(python_executable), "-c", _MCP_RUNTIME_IMPORT],
            capture_output=True,
            text=True,
            check=False,
            timeout=_MCP_RUNTIME_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def render_hooks_document(document: Any, python_executable: Path) -> dict[str, Any]:
    """Bind every command handler in a hooks template to one Python interpreter."""

    hooks = document.get("hooks") if isinstance(document, dict) else None
    if not isinstance(hooks, dict):
        raise PluginIntegrationError("hooks template must contain a hooks object")
    posix_command, windows_command = _command_lines(python_executable)
    rendered_handlers = 0
    for groups in hooks.values():
        if not isinstance(groups, list):
            raise PluginIntegrationError("hook event groups must be arrays")
        for group in groups:
            handlers = group.get("hooks") if isinstance(group, dict) else None
            if not isinstance(handlers, list):
                raise PluginIntegrationError("hook matcher groups must contain hook arrays")
            for handler in handlers:
                if not isinstance(handler, dict) or handler.get("type") != "command":
                    raise PluginIntegrationError("only command hook templates are supported")
                handler["command"] = posix_command
                handler["commandWindows"] = windows_command
                rendered_handlers += 1
    if rendered_handlers == 0:
        raise PluginIntegrationError("hooks template contains no command handlers")
    return document


def load_rendered_hooks(
    python_executable: Path, *, template_root: Path | None = None
) -> dict[str, Any]:
    """Read the packaged hooks template and bind it to ``python_executable``."""

    root = template_root or default_template_root()
    return render_hooks_document(_read_json(root / "hooks" / "hooks.json"), python_executable)


def _render_plugin_tree(
    stage: Path,
    *,
    template_root: Path,
    python_executable: Path,
    package_version: str,
) -> None:
    if not template_root.is_dir():
        raise PluginIntegrationError(f"plugin template is missing: {template_root}")
    if any(candidate.is_symlink() for candidate in template_root.rglob("*")):
        raise PluginIntegrationError("plugin template must not contain symbolic links")

    plugin_root = stage / "plugins" / PLUGIN_NAME
    shutil.copytree(template_root, plugin_root)

    manifest_path = plugin_root / ".codex-plugin" / "plugin.json"
    manifest = _read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("name") != PLUGIN_NAME:
        raise PluginIntegrationError("plugin manifest identity does not match htsave")
    manifest["version"] = package_version
    _write_json(manifest_path, manifest)

    mcp_path = plugin_root / ".mcp.json"
    mcp = _read_json(mcp_path)
    if not isinstance(mcp, dict) or set(mcp) != {PLUGIN_NAME}:
        raise PluginIntegrationError(".mcp.json must be a direct htsave server map")
    server = mcp[PLUGIN_NAME]
    if not isinstance(server, dict):
        raise PluginIntegrationError("htsave MCP server config must be an object")
    server["command"] = str(python_executable)
    server["args"] = ["-m", MCP_MODULE]
    _write_json(mcp_path, mcp)

    hooks_path = plugin_root / "hooks" / "hooks.json"
    _write_json(hooks_path, render_hooks_document(_read_json(hooks_path), python_executable))

    marketplace = {
        "name": MARKETPLACE_NAME,
        "interface": {"displayName": "htsave local"},
        "plugins": [
            {
                "name": PLUGIN_NAME,
                "source": {"source": "local", "path": f"./plugins/{PLUGIN_NAME}"},
                "policy": {
                    "installation": "AVAILABLE",
                    "authentication": "ON_INSTALL",
                },
                "category": "Productivity",
            }
        ],
    }
    _write_json(stage / ".agents" / "plugins" / "marketplace.json", marketplace)
    _write_json(stage / OWNERSHIP_FILE, _ownership_payload())
    _secure_tree(stage)


def _recover_interrupted_swap(root: Path) -> None:
    backup = root.with_name(f".{root.name}.backup")
    if not backup.exists():
        return
    _require_owned(backup)
    if root.exists():
        _require_owned(root)
        shutil.rmtree(backup)
    else:
        os.replace(backup, root)


def materialize_plugin(
    *,
    paths: PluginPaths | None = None,
    template_root: Path | None = None,
    python_executable: Path | None = None,
    package_version: str = __version__,
) -> PluginPaths:
    """Render and transactionally replace the htsave-owned marketplace tree."""

    target = paths or build_plugin_paths()
    template = (template_root or default_template_root()).expanduser().resolve()
    executable = _runtime_python_path(python_executable)
    if not package_version:
        raise PluginIntegrationError("plugin version must not be empty")

    ensure_private_directory(target.state_root)
    _recover_interrupted_swap(target.marketplace_root)
    _require_owned(target.marketplace_root)
    stage = Path(
        tempfile.mkdtemp(
            dir=target.state_root,
            prefix=f".{target.marketplace_root.name}.stage-",
        )
    )
    backup = target.marketplace_root.with_name(f".{target.marketplace_root.name}.backup")
    try:
        _render_plugin_tree(
            stage,
            template_root=template,
            python_executable=executable,
            package_version=package_version,
        )
        if target.marketplace_root.exists():
            os.replace(target.marketplace_root, backup)
        try:
            os.replace(stage, target.marketplace_root)
        except BaseException:
            if backup.exists() and not target.marketplace_root.exists():
                os.replace(backup, target.marketplace_root)
            raise
        if backup.exists():
            shutil.rmtree(backup)
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return target


def _default_runner(argv: Sequence[str]) -> CommandResult:
    return subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        check=False,
    )


class CodexClient:
    """Small JSON-only wrapper around stable Codex plugin commands."""

    def __init__(self, runner: Runner | None = None, executable: str = "codex") -> None:
        self._runner = runner or _default_runner
        self._executable = executable

    def _json(self, *arguments: str) -> Any:
        argv = (self._executable, *arguments)
        try:
            result = self._runner(argv)
        except OSError as error:
            raise PluginIntegrationError(f"could not execute {self._executable}") from error
        stdout = result.stdout or ""
        stderr = (result.stderr or "").strip()
        if result.returncode != 0:
            detail = stderr or stdout.strip() or f"exit status {result.returncode}"
            raise PluginIntegrationError(f"Codex command failed: {' '.join(argv)}: {detail}")
        try:
            return json.loads(stdout)
        except json.JSONDecodeError as error:
            raise PluginIntegrationError(
                f"Codex command returned invalid JSON: {' '.join(argv)}"
            ) from error

    def marketplaces(self) -> list[Mapping[str, Any]]:
        payload = self._json("plugin", "marketplace", "list", "--json")
        values = payload.get("marketplaces") if isinstance(payload, dict) else None
        if not isinstance(values, list) or not all(isinstance(item, dict) for item in values):
            raise PluginIntegrationError("unexpected Codex marketplace list response")
        return values

    def plugins(self) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        payload = self._json("plugin", "list", "--available", "--json")
        if not isinstance(payload, dict):
            raise PluginIntegrationError("unexpected Codex plugin list response")
        installed = payload.get("installed")
        available = payload.get("available")
        if not isinstance(installed, list) or not isinstance(available, list):
            raise PluginIntegrationError("unexpected Codex plugin list response")
        if not all(isinstance(item, dict) for item in (*installed, *available)):
            raise PluginIntegrationError("unexpected Codex plugin list entries")
        return installed, available

    def mcp_servers(self) -> list[Mapping[str, Any]]:
        payload = self._json("mcp", "list", "--json")
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise PluginIntegrationError("unexpected Codex MCP list response")
        return payload

    def add_marketplace(self, root: Path) -> Mapping[str, Any]:
        payload = self._json("plugin", "marketplace", "add", str(root), "--json")
        if not isinstance(payload, dict):
            raise PluginIntegrationError("unexpected Codex marketplace add response")
        return payload

    def add_plugin(self) -> Mapping[str, Any]:
        payload = self._json("plugin", "add", PLUGIN_SELECTOR, "--json")
        if not isinstance(payload, dict):
            raise PluginIntegrationError("unexpected Codex plugin add response")
        return payload

    def remove_plugin(self) -> Mapping[str, Any]:
        payload = self._json("plugin", "remove", PLUGIN_SELECTOR, "--json")
        if not isinstance(payload, dict):
            raise PluginIntegrationError("unexpected Codex plugin remove response")
        return payload

    def remove_marketplace(self) -> Mapping[str, Any]:
        payload = self._json("plugin", "marketplace", "remove", MARKETPLACE_NAME, "--json")
        if not isinstance(payload, dict):
            raise PluginIntegrationError("unexpected Codex marketplace remove response")
        return payload


def _normalized_path(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return os.path.normcase(os.path.abspath(os.path.expanduser(value)))


def _same_path(left: object, right: Path) -> bool:
    return _normalized_path(left) == os.path.normcase(os.path.abspath(right))


def _find(records: Sequence[Mapping[str, Any]], key: str, value: str) -> Mapping[str, Any] | None:
    return next((record for record in records if record.get(key) == value), None)


class CodexPluginManager:
    """Own materialization and the reversible Codex marketplace lifecycle."""

    def __init__(
        self,
        *,
        state_root: Path | None = None,
        template_root: Path | None = None,
        python_executable: Path | None = None,
        package_version: str = __version__,
        runner: Runner | None = None,
        codex_executable: str = "codex",
    ) -> None:
        self.paths = build_plugin_paths(state_root)
        self.template_root = template_root
        self.python_executable = _runtime_python_path(python_executable)
        self.package_version = package_version
        self.client = CodexClient(runner, codex_executable)

    def _marketplace(self, marketplaces: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
        return _find(marketplaces, "name", MARKETPLACE_NAME)

    def _assert_no_marketplace_conflict(
        self, marketplaces: Sequence[Mapping[str, Any]]
    ) -> Mapping[str, Any] | None:
        marketplace = self._marketplace(marketplaces)
        if marketplace is not None and not _same_path(
            marketplace.get("root"), self.paths.marketplace_root
        ):
            raise PluginOwnershipError(
                f"marketplace name {MARKETPLACE_NAME!r} is owned by another path"
            )
        return marketplace

    def install(self) -> PluginStatus:
        marketplaces = self.client.marketplaces()
        self._assert_no_marketplace_conflict(marketplaces)
        materialize_plugin(
            paths=self.paths,
            template_root=self.template_root,
            python_executable=self.python_executable,
            package_version=self.package_version,
        )
        self.client.add_marketplace(self.paths.marketplace_root)
        self.client.add_plugin()
        status = self.status()
        if not status.healthy:
            raise PluginIntegrationError(
                f"Codex plugin install verification failed: {status.state.value}"
            )
        return status

    def _materialized_drifts(self) -> list[PluginDrift]:
        root = self.paths.marketplace_root
        if not root.exists():
            return []
        if not _is_owned(root):
            return [PluginDrift.OWNERSHIP_MISMATCH]

        drifts: list[PluginDrift] = []
        try:
            manifest = _read_json(self.paths.plugin_root / ".codex-plugin" / "plugin.json")
            if not isinstance(manifest, dict) or manifest.get("version") != self.package_version:
                drifts.append(PluginDrift.MATERIALIZED_MANIFEST)
        except PluginIntegrationError:
            drifts.append(PluginDrift.MATERIALIZED_MANIFEST)

        try:
            mcp = _read_json(self.paths.plugin_root / ".mcp.json")
            server = mcp.get(PLUGIN_NAME) if isinstance(mcp, dict) else None
            if (
                set(mcp) != {PLUGIN_NAME}
                or not isinstance(server, dict)
                or not _same_path(server.get("command"), self.python_executable)
                or server.get("args") != ["-m", MCP_MODULE]
            ):
                drifts.append(PluginDrift.MATERIALIZED_MCP)
        except (PluginIntegrationError, TypeError):
            drifts.append(PluginDrift.MATERIALIZED_MCP)

        try:
            document = _read_json(self.paths.plugin_root / "hooks" / "hooks.json")
            hooks = document.get("hooks") if isinstance(document, dict) else None
            expected_posix, expected_windows = _command_lines(self.python_executable)
            handlers = (
                [
                    handler
                    for groups in hooks.values()
                    for group in groups
                    for handler in group["hooks"]
                ]
                if isinstance(hooks, dict)
                else []
            )
            if not handlers or any(
                handler.get("command") != expected_posix
                or handler.get("commandWindows") != expected_windows
                for handler in handlers
            ):
                drifts.append(PluginDrift.MATERIALIZED_HOOKS)
        except (PluginIntegrationError, KeyError, TypeError):
            drifts.append(PluginDrift.MATERIALIZED_HOOKS)
        return drifts

    def status(self) -> PluginStatus:
        local_drifts = self._materialized_drifts()
        try:
            marketplaces = self.client.marketplaces()
            installed, available = self.client.plugins()
            mcp_servers = self.client.mcp_servers()
        except PluginIntegrationError as error:
            return PluginStatus(
                state=PluginState.CODEX_UNAVAILABLE,
                expected_version=self.package_version,
                installed_version=None,
                enabled=False,
                marketplace_registered=False,
                marketplace_root=self.paths.marketplace_root,
                drifts=tuple(dict.fromkeys(local_drifts)),
                notes=(str(error),),
            )

        drifts = list(local_drifts)
        notes: list[str] = []
        marketplace = self._marketplace(marketplaces)
        marketplace_registered = marketplace is not None
        conflict = False
        if marketplace is None:
            if self.paths.marketplace_root.exists():
                drifts.append(PluginDrift.MARKETPLACE_NOT_REGISTERED)
        elif not _same_path(marketplace.get("root"), self.paths.marketplace_root):
            drifts.append(PluginDrift.MARKETPLACE_ROOT)
            conflict = True

        installed_record = _find(installed, "pluginId", PLUGIN_SELECTOR)
        available_record = _find(available, "pluginId", PLUGIN_SELECTOR)
        plugin_record = installed_record or available_record
        if marketplace_registered and not conflict and plugin_record is None:
            drifts.append(PluginDrift.PLUGIN_NOT_DISCOVERABLE)

        installed_version = None
        enabled = False
        if plugin_record is not None:
            version = plugin_record.get("version")
            installed_version = version if isinstance(version, str) else None
            if installed_version != self.package_version:
                drifts.append(PluginDrift.PLUGIN_VERSION)
            source = plugin_record.get("source")
            source_path = source.get("path") if isinstance(source, dict) else None
            if not _same_path(source_path, self.paths.plugin_root):
                drifts.append(PluginDrift.PLUGIN_SOURCE)
        if installed_record is not None:
            enabled = installed_record.get("enabled") is True

        if (
            marketplace_registered or installed_record is not None
        ) and not self.paths.marketplace_root.exists():
            drifts.append(PluginDrift.MANAGED_ROOT_MISSING)

        if installed_record is not None and enabled:
            mcp = _find(mcp_servers, "name", PLUGIN_NAME)
            if mcp is None:
                drifts.append(PluginDrift.MCP_SERVER_MISSING)
            else:
                transport = mcp.get("transport")
                command_matches = isinstance(transport, dict) and _same_path(
                    transport.get("command"), self.python_executable
                )
                if not command_matches:
                    drifts.append(PluginDrift.MCP_COMMAND)
                if not isinstance(transport, dict) or transport.get("args") != ["-m", MCP_MODULE]:
                    drifts.append(PluginDrift.MCP_ARGS)
                if command_matches and not _mcp_runtime_importable(self.python_executable):
                    drifts.append(PluginDrift.MCP_RUNTIME)

        unique_drifts = tuple(dict.fromkeys(drifts))
        if conflict or PluginDrift.OWNERSHIP_MISMATCH in unique_drifts:
            state = PluginState.CONFLICT
        elif unique_drifts:
            state = PluginState.DRIFTED
        elif installed_record is None:
            state = PluginState.NOT_INSTALLED
        elif enabled:
            state = PluginState.INSTALLED_ENABLED
        else:
            state = PluginState.INSTALLED_DISABLED
        return PluginStatus(
            state=state,
            expected_version=self.package_version,
            installed_version=installed_version,
            enabled=enabled,
            marketplace_registered=marketplace_registered,
            marketplace_root=self.paths.marketplace_root,
            drifts=unique_drifts,
            notes=tuple(notes),
        )

    def uninstall(self) -> PluginStatus:
        marketplaces = self.client.marketplaces()
        marketplace = self._assert_no_marketplace_conflict(marketplaces)
        _require_owned(self.paths.marketplace_root)
        installed, _ = self.client.plugins()
        installed_record = _find(installed, "pluginId", PLUGIN_SELECTOR)
        if installed_record is not None:
            source = installed_record.get("source")
            source_path = source.get("path") if isinstance(source, dict) else None
            if not _same_path(source_path, self.paths.plugin_root):
                raise PluginOwnershipError(
                    f"plugin selector {PLUGIN_SELECTOR!r} is owned by another source"
                )
            self.client.remove_plugin()
        elif marketplace is not None:
            # Codex 0.148 makes removal idempotent while the marketplace exists.
            self.client.remove_plugin()
        if marketplace is not None:
            self.client.remove_marketplace()
        if self.paths.marketplace_root.exists():
            shutil.rmtree(self.paths.marketplace_root)
        return self.status()


__all__ = [
    "MARKETPLACE_NAME",
    "PLUGIN_NAME",
    "PLUGIN_SELECTOR",
    "CodexClient",
    "CodexPluginManager",
    "PluginDrift",
    "PluginIntegrationError",
    "PluginOwnershipError",
    "PluginPaths",
    "PluginState",
    "PluginStatus",
    "build_plugin_paths",
    "default_template_root",
    "materialize_plugin",
]
