from __future__ import annotations

import json
from pathlib import Path

import htsave.cli as cli_module
from htsave.cas import ContentAddressedStore
from htsave.cli import main
from htsave.compat import CodexCompatibility
from htsave.engine import ContextEngine
from htsave.paths import build_state_paths
from htsave.plugin import PluginState, PluginStatus
from htsave.registry import Registry
from htsave.tokens import TokenEstimator


def _seed(tmp_path: Path) -> tuple[Path, str, str, bytes]:
    root = tmp_path / "state"
    paths = build_state_paths("session-one", root)
    registry = Registry(paths.database, paths.session_key)
    engine = ContextEngine(ContentAddressedStore(paths.objects), registry, TokenEstimator("gpt-5"))
    content = "exact\r\n雪\nlast".encode()
    try:
        engine.start_generation("startup")
        decision = engine.decide(
            text=content.decode(),
            source_fingerprint="source",
            safe_label="fixture",
            tool_use_id="tool-1",
        )
    finally:
        registry.close()
    return root, paths.session_key, decision.target_hash, content


def test_stats_and_inspect_json(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root, key, _, _ = _seed(tmp_path)

    assert main(["--state-root", str(root), "--json", "stats"]) == 0
    stats = json.loads(capsys.readouterr().out)
    assert stats["totals"]["sessions"] == 1
    assert stats["totals"]["accounting"].startswith("tiktoken estimate")

    assert main(["--state-root", str(root), "--json", "inspect", "--session-key", key]) == 0
    details = json.loads(capsys.readouterr().out)
    assert details["events"][0]["mode"] == "full"


def test_hydrate_writes_exact_bytes(tmp_path: Path, capsysbinary) -> None:  # type: ignore[no-untyped-def]
    root, key, object_hash, content = _seed(tmp_path)

    assert (
        main(
            [
                "--state-root",
                str(root),
                "hydrate",
                object_hash,
                "--session-key",
                key,
            ]
        )
        == 0
    )
    assert capsysbinary.readouterr().out == content


def test_codex_status_reports_integration_and_capabilities(
    tmp_path: Path, monkeypatch, capsys
) -> None:  # type: ignore[no-untyped-def]
    integration = PluginStatus(
        state=PluginState.INSTALLED_ENABLED,
        expected_version="1.0.0",
        installed_version="1.0.0",
        enabled=True,
        marketplace_registered=True,
        marketplace_root=tmp_path,
    )

    class FakeManager:
        def status(self) -> PluginStatus:
            return integration

    monkeypatch.setattr(cli_module, "_manager", lambda _args: FakeManager())
    monkeypatch.setattr(
        cli_module,
        "probe_codex_compatibility",
        lambda: CodexCompatibility(
            "0.150.1",
            True,
            "capability-compatible",
            mcp_tool_injection=True,
        ),
    )

    assert main(["--json", "codex", "status"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["integration"]["state"] == "installed-enabled"
    assert payload["codex_contract"] == {
        "detected_version": "0.150.1",
        "mcp_tool_injection": True,
        "posttool_result_replacement": False,
        "reason": "capability-compatible",
        "supported": True,
    }


def test_gc_and_clear_are_dry_run_by_default(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root, key, _, _ = _seed(tmp_path)
    paths = build_state_paths("session-one", root)
    orphan = ContentAddressedStore(paths.objects).put(b"orphan")

    assert main(["--state-root", str(root), "--json", "gc"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert ContentAddressedStore(paths.objects).contains(orphan)

    assert (
        main(
            [
                "--state-root",
                str(root),
                "--json",
                "clear",
                "--session-key",
                key,
            ]
        )
        == 0
    )
    preview = json.loads(capsys.readouterr().out)
    assert preview["dry_run"] is True
    assert paths.session_root.exists()

    assert (
        main(
            [
                "--state-root",
                str(root),
                "--json",
                "clear",
                "--session-key",
                key,
                "--yes",
            ]
        )
        == 0
    )
    applied = json.loads(capsys.readouterr().out)
    assert applied["cleared"] == [key]
    assert not paths.session_root.exists()


def test_benchmark_run_is_paid_call_gated_and_report_is_explicit(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    output = tmp_path / "benchmark"

    assert (
        main(
            [
                "--json",
                "benchmark",
                "run",
                "--output",
                str(output),
                "--payload-lines",
                "3",
            ]
        )
        == 0
    )
    dry_run = json.loads(capsys.readouterr().out)
    assert dry_run["dry_run"] is True
    assert dry_run["completed"] == 0
    assert dry_run["required_executions"] == 80
    assert dry_run["report"]["passed"] is False

    assert (
        main(
            [
                "--json",
                "benchmark",
                "report",
                str(output / "manifest.json"),
            ]
        )
        == 1
    )
    report = json.loads(capsys.readouterr().out)
    assert report["completed"] == 0
    assert report["report"]["missing_scenarios"] == []
    assert all(not scenario["passed"] for scenario in report["report"]["scenarios"])
