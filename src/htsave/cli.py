"""Operator CLI for htsave local state and Codex integration."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import asdict, is_dataclass
from enum import Enum
from fractions import Fraction
from pathlib import Path
from typing import Any

from .benchmark import BENCHMARK_HOSTS, BENCHMARK_PATHS, DEFAULT_HOST
from .cas import ContentAddressedStore
from .compat import probe_claude_compatibility, probe_codex_compatibility
from .engine import ContextEngine
from .errors import HtsaveError, SecurityBoundaryError
from .operator import (
    all_session_summaries,
    apply_gc,
    clear_sessions,
    configured_state_root,
    gc_candidates,
    inspect_session,
    list_session_keys,
)
from .paths import ensure_private_directory, find_state_paths
from .plugin import CodexPluginManager
from .registry import Registry
from .tokens import TokenEstimator


def _jsonable(value: Any) -> Any:
    if isinstance(value, Fraction):
        return f"{value.numerator}/{value.denominator}"
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list, set)):
        return [_jsonable(item) for item in value]
    return value


def _emit(value: Any, *, as_json: bool) -> None:
    payload = _jsonable(value)
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    elif isinstance(payload, str):
        print(payload)
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))


def _manager(args: argparse.Namespace) -> CodexPluginManager:
    return CodexPluginManager(state_root=configured_state_root(args.state_root))


def _codex_install(args: argparse.Namespace) -> int:
    status = _manager(args).install()
    _emit(status, as_json=args.json)
    return 0


def _codex_status(args: argparse.Namespace) -> int:
    status = _manager(args).status()
    _emit(status, as_json=args.json)
    return 0 if status.healthy else 1


def _codex_uninstall(args: argparse.Namespace) -> int:
    manager = _manager(args)
    if not args.yes:
        status = manager.status()
        _emit(
            {
                "dry_run": True,
                "would_remove_integration": status.state.value != "not-installed",
                "session_data_preserved": True,
                "hint": "rerun with --yes to remove the managed Codex integration",
                "status": status,
            },
            as_json=args.json,
        )
        return 0
    status = manager.uninstall()
    _emit(
        {"removed": True, "session_data_preserved": True, "status": status},
        as_json=args.json,
    )
    return 0


def _claude_settings(args: argparse.Namespace) -> Path | None:
    settings = getattr(args, "settings", None)
    return settings.expanduser() if settings is not None else None


def _claude_skill(args: argparse.Namespace) -> Path | None:
    skill = getattr(args, "skill_path", None)
    return skill.expanduser() if skill is not None else None


def _claude_install(args: argparse.Namespace) -> int:
    from .claude_install import install

    status = install(
        settings_path=_claude_settings(args), skill_path=_claude_skill(args)
    )
    _emit(status, as_json=args.json)
    return 0


def _claude_status(args: argparse.Namespace) -> int:
    from .claude_install import status as claude_status

    result = claude_status(
        settings_path=_claude_settings(args), skill_path=_claude_skill(args)
    )
    _emit(result, as_json=args.json)
    return 0 if result.healthy else 1


def _claude_uninstall(args: argparse.Namespace) -> int:
    from .claude_install import uninstall

    payload = uninstall(
        settings_path=_claude_settings(args),
        skill_path=_claude_skill(args),
        confirm=args.yes,
    )
    _emit(payload, as_json=args.json)
    return 0


def _agy_home(args: argparse.Namespace) -> Path | None:
    home = getattr(args, "home", None)
    return home.expanduser() if home is not None else None


def _agy_install(args: argparse.Namespace) -> int:
    from .agy_install import install

    status = install(home=_agy_home(args))
    _emit(status, as_json=args.json)
    return 0


def _agy_status(args: argparse.Namespace) -> int:
    from .agy_install import status as agy_status

    result = agy_status(home=_agy_home(args))
    _emit(result, as_json=args.json)
    return 0 if result.healthy else 1


def _agy_uninstall(args: argparse.Namespace) -> int:
    from .agy_install import uninstall

    payload = uninstall(home=_agy_home(args), confirm=args.yes)
    _emit(payload, as_json=args.json)
    return 0


def _doctor(args: argparse.Namespace) -> int:
    root = configured_state_root(args.state_root)
    checks: dict[str, dict[str, Any]] = {}
    checks["python"] = {
        "ok": sys.version_info >= (3, 11),
        "version": ".".join(str(part) for part in sys.version_info[:3]),
    }
    estimator = TokenEstimator("gpt-5")
    checks["tiktoken_estimator"] = {
        "ok": estimator.available,
        "backend": estimator.backend,
        "label": "estimate-only; not billed Codex usage",
    }
    try:
        ensure_private_directory(root)
        mode = None if os.name == "nt" else oct(root.stat().st_mode & 0o777)
        checks["local_state"] = {
            "ok": os.name == "nt" or mode == "0o700",
            "root": root,
            "mode": mode or "current-user ACL",
        }
    except (OSError, HtsaveError) as exc:
        checks["local_state"] = {"ok": False, "error": str(exc), "root": root}
    checks["sqlite"] = {
        "ok": sqlite3.sqlite_version_info >= (3, 35, 0),
        "version": sqlite3.sqlite_version,
        "required_features": ["WAL", "partial-index", "drop-column migration test"],
    }
    compatibility = probe_codex_compatibility()
    checks["codex_contract"] = {
        "ok": compatibility.supported,
        "detected_version": compatibility.detected_version,
        "reason": compatibility.reason,
    }
    claude = probe_claude_compatibility()
    checks["claude_contract"] = {
        "ok": claude.supported,
        "detected_version": claude.detected_version,
        "reason": claude.reason,
        "transparent_posttool_replacement": claude.posttool_result_replacement,
    }
    from .claude_install import status as claude_integration_status

    integration = claude_integration_status()
    checks["claude_integration"] = {
        "ok": integration.healthy,
        "status": integration,
    }
    from .agy_install import status as agy_integration_status

    agy_status_info = agy_integration_status()
    checks["agy_integration"] = {
        "ok": agy_status_info.healthy,
        "status": agy_status_info,
    }
    plugin_status = _manager(args).status()
    checks["plugin"] = {
        "ok": plugin_status.healthy,
        "status": plugin_status,
    }
    checks["hook_trust"] = {
        "ok": False,
        "status": "manual-review-required",
        "action": "open /hooks in Codex and trust the installed htsave hook hash",
        "reason": "Codex 0.148.0 has no stable noninteractive hook-trust query",
    }
    checks["mcp_read_path"] = {
        "ok": compatibility.mcp_tool_injection,
        "status": "supported" if compatibility.mcp_tool_injection else compatibility.reason,
        "reason": (
            "PreToolUse updatedInput injection drives htsave_read and htsave_hydrate; "
            "this is the delivery path that saves tokens today"
        ),
    }
    checks["codex_transparent_posttool_replacement"] = {
        "ok": False,
        "status": "unsupported-by-codex-0.148.0",
        "reason": (
            "PostToolUse has no supported successful arbitrary-result replacement; "
            "observer mode preserves the original result"
        ),
        "release_blocker": True,
        "blocks": (
            "the Codex shell-path benchmark only; the Codex MCP path and Claude Code are unaffected"
        ),
    }
    ok = all(check["ok"] for check in checks.values())
    _emit({"ok": ok, "checks": checks}, as_json=args.json)
    return 0 if ok else 1


def _stats(args: argparse.Namespace) -> int:
    summaries = all_session_summaries(args.state_root)
    if args.session_key:
        summaries = tuple(
            summary for summary in summaries if summary.session_key == args.session_key
        )
        if not summaries:
            raise SecurityBoundaryError("requested htsave session was not found")
    totals = {
        "sessions": len(summaries),
        "events": sum(item.stats.events for item in summaries),
        "full": sum(item.stats.full for item in summaries),
        "refs": sum(item.stats.refs for item in summaries),
        "deltas": sum(item.stats.deltas for item in summaries),
        "bypassed": sum(item.stats.bypassed for item in summaries),
        "original_tokens_estimated": sum(item.stats.original_tokens for item in summaries),
        "emitted_tokens_estimated": sum(item.stats.emitted_tokens for item in summaries),
        "saved_tokens_estimated": sum(item.stats.saved_tokens for item in summaries),
        "cas_objects": sum(item.cas_objects for item in summaries),
        "cas_bytes": sum(item.cas_bytes for item in summaries),
        "accounting": "tiktoken estimate; use codex exec --json for actual usage",
    }
    _emit({"totals": totals, "sessions": summaries}, as_json=args.json)
    return 0


def _inspect(args: argparse.Namespace) -> int:
    if args.session_key:
        payload: Any = inspect_session(args.session_key, args.state_root)
    else:
        payload = {"sessions": all_session_summaries(args.state_root)}
    _emit(payload, as_json=args.json)
    return 0


def _hydrate(args: argparse.Namespace) -> int:
    paths = find_state_paths(args.session_key, configured_state_root(args.state_root))
    registry = Registry(paths.database, paths.session_key)
    engine = ContextEngine(
        ContentAddressedStore(paths.objects),
        registry,
        TokenEstimator(args.model),
    )
    try:
        content = engine.hydrate_bytes(args.ref)
    finally:
        registry.close()
    if args.json:
        _emit(
            {
                "session_key": args.session_key,
                "ref": args.ref,
                "byte_size": len(content),
                "text": content.decode("utf-8"),
            },
            as_json=True,
        )
    else:
        sys.stdout.buffer.write(content)
        sys.stdout.buffer.flush()
    return 0


def _gc(args: argparse.Namespace) -> int:
    candidates = gc_candidates(args.state_root)
    deleted = apply_gc(candidates, args.state_root) if args.apply else ()
    _emit(
        {
            "dry_run": not args.apply,
            "candidates": candidates,
            "candidate_bytes": sum(item.byte_size for item in candidates),
            "deleted": deleted,
            "deleted_bytes": sum(item.byte_size for item in deleted),
        },
        as_json=args.json,
    )
    return 0


def _clear(args: argparse.Namespace) -> int:
    available = list_session_keys(args.state_root)
    targets = available if args.all else tuple(dict.fromkeys(args.session_key or ()))
    if args.all and not targets:
        _emit(
            {
                "dry_run": not args.yes,
                "targets": (),
                "cleared": (),
                "recoverable": None,
                "hint": None,
            },
            as_json=args.json,
        )
        return 0
    if not targets:
        raise SecurityBoundaryError("select --session-key or --all")
    missing = sorted(set(targets) - set(available))
    if missing:
        raise SecurityBoundaryError(f"unknown session key(s): {', '.join(missing)}")
    cleared = clear_sessions(targets, state_root=args.state_root) if args.yes else ()
    _emit(
        {
            "dry_run": not args.yes,
            "targets": targets,
            "cleared": cleared,
            "recoverable": False if args.yes else None,
            "hint": None if args.yes else "rerun with --yes to delete these sessions",
        },
        as_json=args.json,
    )
    return 0


def _benchmark_report_payload(manifest: Any) -> dict[str, Any]:
    report = manifest.release_report()
    return {
        "passed": report.passed,
        "missing_scenarios": report.missing_scenarios,
        "required_scenarios": report.required_scenarios,
        "scenarios": [
            {
                "scenario_id": scenario.scenario_id,
                "pairs": len(scenario.pairs),
                "required_pairs": scenario.required_pairs,
                "threshold": scenario.threshold,
                "median_input_reduction": scenario.median_input_reduction,
                "pairwise_input_reductions": scenario.pairwise_input_reductions,
                "passed": scenario.passed,
            }
            for scenario in report.scenarios
        ],
    }


def _benchmark_run(args: argparse.Namespace) -> int:
    from .benchmark_runner import resume_release_benchmark, run_release_benchmark

    output = args.output.expanduser().resolve()
    if args.resume:
        manifest = resume_release_benchmark(
            output / "manifest.json",
            confirm_paid_runs=args.confirm_paid_runs,
            max_executions=args.max_executions,
        )
    else:
        from .benchmark_runner import DEFAULT_MODEL_FOR_HOST

        manifest = run_release_benchmark(
            output,
            confirm_paid_runs=args.confirm_paid_runs,
            codex_executable=args.codex or args.host,
            model=args.model or DEFAULT_MODEL_FOR_HOST[args.host],
            reasoning_effort=args.reasoning_effort,
            payload_lines=args.payload_lines,
            timeout_seconds=args.timeout_seconds,
            host=args.host,
            path=args.path,
            max_executions=args.max_executions,
        )
    payload = {
        "manifest": output / "manifest.json",
        "host": manifest.host,
        "path": manifest.path,
        "completed": manifest.completed_count,
        "required_executions": len(manifest.executions),
        "dry_run": not args.confirm_paid_runs,
        "report": _benchmark_report_payload(manifest),
    }
    _emit(payload, as_json=args.json)
    return 0 if not args.confirm_paid_runs or payload["report"]["passed"] else 1


def _benchmark_report(args: argparse.Namespace) -> int:
    from .benchmark_runner import load_release_manifest

    manifest = load_release_manifest(args.manifest.expanduser().resolve())
    payload = {
        "manifest": args.manifest.expanduser().resolve(),
        "host": manifest.host,
        "path": manifest.path,
        "completed": manifest.completed_count,
        "required_executions": len(manifest.executions),
        "report": _benchmark_report_payload(manifest),
    }
    _emit(payload, as_json=args.json)
    return 0 if payload["report"]["passed"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="htsave")
    parser.add_argument("--state-root", type=Path, help="override local state root")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    commands = parser.add_subparsers(dest="command", required=True)

    codex = commands.add_parser("codex", help="manage the Codex plugin integration")
    codex_commands = codex.add_subparsers(dest="codex_command", required=True)
    install = codex_commands.add_parser("install")
    install.set_defaults(handler=_codex_install)
    status = codex_commands.add_parser("status")
    status.set_defaults(handler=_codex_status)
    uninstall = codex_commands.add_parser("uninstall")
    uninstall.add_argument("--yes", action="store_true", help="confirm integration removal")
    uninstall.set_defaults(handler=_codex_uninstall)

    claude = commands.add_parser("claude", help="manage the Claude Code integration")
    claude_commands = claude.add_subparsers(dest="claude_command", required=True)
    claude_install = claude_commands.add_parser("install")
    claude_install.add_argument("--settings", type=Path, help="override settings.json path")
    claude_install.add_argument("--skill-path", type=Path, help="override skill file path")
    claude_install.set_defaults(handler=_claude_install)
    claude_status = claude_commands.add_parser("status")
    claude_status.add_argument("--settings", type=Path, help="override settings.json path")
    claude_status.add_argument("--skill-path", type=Path, help="override skill file path")
    claude_status.set_defaults(handler=_claude_status)
    claude_uninstall = claude_commands.add_parser("uninstall")
    claude_uninstall.add_argument("--settings", type=Path, help="override settings.json path")
    claude_uninstall.add_argument("--skill-path", type=Path, help="override skill file path")
    claude_uninstall.add_argument("--yes", action="store_true", help="confirm integration removal")
    claude_uninstall.set_defaults(handler=_claude_uninstall)

    agy = commands.add_parser("agy", help="manage the Antigravity CLI integration")
    agy_commands = agy.add_subparsers(dest="agy_command", required=True)
    agy_install_cmd = agy_commands.add_parser("install")
    agy_install_cmd.add_argument("--home", type=Path, help="override config home directory")
    agy_install_cmd.set_defaults(handler=_agy_install)
    agy_status_cmd = agy_commands.add_parser("status")
    agy_status_cmd.add_argument("--home", type=Path, help="override config home directory")
    agy_status_cmd.set_defaults(handler=_agy_status)
    agy_uninstall_cmd = agy_commands.add_parser("uninstall")
    agy_uninstall_cmd.add_argument("--home", type=Path, help="override config home directory")
    agy_uninstall_cmd.add_argument("--yes", action="store_true", help="confirm removal")
    agy_uninstall_cmd.set_defaults(handler=_agy_uninstall)

    doctor = commands.add_parser("doctor")
    doctor.set_defaults(handler=_doctor)

    stats = commands.add_parser("stats")
    stats.add_argument("--session-key")
    stats.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    stats.set_defaults(handler=_stats)

    inspect = commands.add_parser("inspect")
    inspect.add_argument("--session-key")
    inspect.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    inspect.set_defaults(handler=_inspect)

    hydrate = commands.add_parser("hydrate")
    hydrate.add_argument("ref")
    hydrate.add_argument("--session-key", required=True)
    hydrate.add_argument("--model", default="gpt-5")
    hydrate.set_defaults(handler=_hydrate)

    gc = commands.add_parser("gc")
    gc.add_argument("--apply", action="store_true", help="delete listed orphan objects")
    gc.set_defaults(handler=_gc)

    clear = commands.add_parser("clear")
    targets = clear.add_mutually_exclusive_group(required=True)
    targets.add_argument("--session-key", action="append")
    targets.add_argument("--all", action="store_true")
    clear.add_argument("--yes", action="store_true", help="confirm irreversible deletion")
    clear.set_defaults(handler=_clear)

    benchmark = commands.add_parser("benchmark", help="run or inspect the paired Codex benchmark")
    benchmark_commands = benchmark.add_subparsers(dest="benchmark_command", required=True)
    benchmark_run = benchmark_commands.add_parser("run")
    benchmark_run.add_argument("--output", type=Path, required=True)
    benchmark_run.add_argument("--resume", action="store_true", help="resume an existing manifest")
    benchmark_run.add_argument(
        "--confirm-paid-runs",
        action="store_true",
        help="explicitly authorize the 80 host exec calls",
    )
    benchmark_run.add_argument(
        "--host",
        choices=BENCHMARK_HOSTS,
        default=DEFAULT_HOST,
        help="agent CLI under test (default: claude)",
    )
    benchmark_run.add_argument(
        "--path",
        choices=BENCHMARK_PATHS,
        default=None,
        help="delivery path under test; defaults to shell on claude and mcp on codex",
    )
    benchmark_run.add_argument(
        "--executable",
        "--codex",
        dest="codex",
        default=None,
        help="host executable to spawn (default: the host name)",
    )
    benchmark_run.add_argument("--model", default=None)
    benchmark_run.add_argument("--reasoning-effort", default="low")
    benchmark_run.add_argument(
        "--max-executions",
        type=int,
        default=None,
        help="execute at most N slots this invocation, then stop; --resume continues",
    )
    benchmark_run.add_argument("--payload-lines", type=int, default=1024)
    benchmark_run.add_argument("--timeout-seconds", type=int, default=900)
    benchmark_run.set_defaults(handler=_benchmark_run)

    benchmark_report = benchmark_commands.add_parser("report")
    benchmark_report.add_argument("manifest", type=Path)
    benchmark_report.set_defaults(handler=_benchmark_report)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.handler(args))
    except (HtsaveError, OSError, sqlite3.Error, UnicodeError) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"htsave: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
