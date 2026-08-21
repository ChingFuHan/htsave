from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import htsave.benchmark_runner as runner_module
from htsave.benchmark import BENCHMARK_PATHS, REQUIRED_SCENARIOS
from htsave.benchmark_runner import (
    EXECUTION_COUNT,
    PAIR_COUNT,
    ProcessOutput,
    ProcessRequest,
    build_release_manifest,
    generate_scenario_fixture,
    load_release_manifest,
    resume_release_benchmark,
    run_release_benchmark,
    tree_digest,
)
from htsave.compat import CodexCompatibility
from htsave.errors import CompatibilityError


@pytest.mark.parametrize("scenario_id", REQUIRED_SCENARIOS)
@pytest.mark.parametrize("path", BENCHMARK_PATHS)
def test_scenario_generators_and_emit_driver_are_deterministic(
    tmp_path: Path, scenario_id: str, path: str
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    generate_scenario_fixture(first, scenario_id, payload_lines=9, path=path)
    generate_scenario_fixture(second, scenario_id, payload_lines=9, path=path)

    first_output = _driver(first, "emit").stdout
    second_output = _driver(second, "emit").stdout

    assert tree_digest(first) == tree_digest(second)
    assert first_output == second_output
    assert first_output
    assert (first / "answer.schema.json").is_file()
    assert (first / "prompt.txt").is_file()


def test_source_driver_changes_exactly_three_lines_and_is_idempotent(tmp_path: Path) -> None:
    workspace = tmp_path / "source"
    generate_scenario_fixture(
        workspace,
        "source_three_line_delta",
        payload_lines=9,
    )
    before = _driver(workspace, "emit").stdout.splitlines()

    first_mutation = _driver(workspace, "mutate")
    after = _driver(workspace, "emit").stdout.splitlines()
    second_mutation = _driver(workspace, "mutate")

    assert sum(left != right for left, right in zip(before, after, strict=True)) == 3
    assert first_mutation.stdout == b"mutated_lines=3\n"
    assert second_mutation.stdout == b"mutated_lines=0\n"


def test_release_manifest_has_80_alternating_executions_and_fixed_argv() -> None:
    manifest = build_release_manifest(
        codex_executable="fake-codex",
        payload_lines=6,
        host="codex",
    )

    assert manifest.host == "codex"
    assert manifest.path == "mcp"
    assert manifest.python_executable == sys.executable
    assert len(manifest.executions) == EXECUTION_COUNT == 80
    assert set(manifest.fixture_digests) == set(REQUIRED_SCENARIOS)
    assert manifest.argv[0:2] == ("fake-codex", "exec")
    for required in (
        "--json",
        "--ephemeral",
        "--approve-for-me",
        "--add-dir",
        "--output-schema",
        "--output-last-message",
    ):
        assert required in manifest.argv
    assert manifest.argv[manifest.argv.index("--cd") + 1] == "."
    assert (
        manifest.argv[manifest.argv.index("--output-last-message") + 1]
        == "../artifacts/answer.json"
    )

    for scenario_index, scenario_id in enumerate(REQUIRED_SCENARIOS):
        for pair_index in range(PAIR_COUNT):
            pair = [
                execution.arm
                for execution in manifest.executions
                if execution.scenario_id == scenario_id and execution.pair_index == pair_index
            ]
            expected = (
                ["baseline", "treatment"]
                if (scenario_index + pair_index) % 2 == 0
                else ["treatment", "baseline"]
            )
            assert pair == expected


def test_mcp_fixture_drives_htsave_tools_and_shell_fixture_does_not(tmp_path: Path) -> None:
    mcp = tmp_path / "mcp"
    shell = tmp_path / "shell"
    generate_scenario_fixture(mcp, "source_three_line_delta", payload_lines=9, path="mcp")
    generate_scenario_fixture(shell, "source_three_line_delta", payload_lines=9, path="shell")

    mcp_prompt = (mcp / "prompt.txt").read_text(encoding="utf-8")
    shell_prompt = (shell / "prompt.txt").read_text(encoding="utf-8")

    assert "htsave_hydrate" in mcp_prompt
    assert '"rate_limit"' in mcp_prompt
    assert '"rate_limit": 640' not in mcp_prompt
    assert "Every listed step is mandatory" in mcp_prompt
    assert "four explicit rounds" in mcp_prompt
    assert "exactly 4 required htsave_read calls" in mcp_prompt
    assert mcp_prompt.count('Call the htsave_read tool with path "src/large_module.py".') == 4
    assert mcp_prompt.count("python benchmark_driver.py mutate") == 1
    assert "python benchmark_driver.py emit" not in mcp_prompt

    assert "htsave_read" not in shell_prompt
    assert "MCP tools" in shell_prompt
    assert shell_prompt.count("python benchmark_driver.py emit") == 4


def test_multi_round_mcp_fixture_reads_every_context_file_each_round(tmp_path: Path) -> None:
    workspace = tmp_path / "multi"
    generate_scenario_fixture(workspace, "multi_round_context", payload_lines=12, path="mcp")

    prompt = (workspace / "prompt.txt").read_text(encoding="utf-8")
    scenario = json.loads((workspace / "scenario.json").read_text(encoding="utf-8"))

    assert len(scenario["emit_paths"]) == 3
    for relative in scenario["emit_paths"]:
        assert prompt.count(f'path "{relative}"') == 4
    assert "Round 4" in prompt


def test_mcp_read_trace_requires_every_path_in_round_order() -> None:
    scenario = {
        "emit_paths": ["context/AGENTS.md", "context/config.toml"],
    }
    events = "\n".join(
        json.dumps(
            {
                "type": "item.completed",
                "item": {
                    "type": "mcp_tool_call",
                    "tool": "htsave_read",
                    "arguments": {"path": path},
                },
            }
        )
        for _ in range(4)
        for path in scenario["emit_paths"]
    )
    runner_module._validate_mcp_read_trace(events, scenario)

    with pytest.raises(RuntimeError, match="prescribed read order"):
        runner_module._validate_mcp_read_trace(events.splitlines()[2], scenario)


def test_mcp_manifest_declares_the_htsave_server_and_shell_manifest_does_not() -> None:
    mcp = build_release_manifest(
        codex_executable="fake-codex", payload_lines=6, host="codex", path="mcp"
    )
    shell = build_release_manifest(
        codex_executable="fake-codex", payload_lines=6, host="codex", path="shell"
    )

    assert f"mcp_servers.htsave.command={json.dumps(sys.executable)}" in mcp.argv
    assert 'mcp_servers.htsave.args=["-m", "htsave.mcp_server"]' in mcp.argv
    assert not any(item.startswith("mcp_servers.") for item in shell.argv)
    assert mcp.fixture_digests != shell.fixture_digests


def test_manifest_round_trip_rejects_a_path_switched_manifest(tmp_path: Path) -> None:
    output = tmp_path / "round-trip"
    run_release_benchmark(output, payload_lines=3, host="codex", path="mcp")
    manifest_path = output / "manifest.json"

    assert load_release_manifest(manifest_path).path == "mcp"

    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["path"] = "shell"
    manifest_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="argv does not match"):
        load_release_manifest(manifest_path)


def test_mcp_attempts_get_an_isolated_hook_only_codex_home_that_is_not_retained(
    tmp_path: Path,
) -> None:
    observed: list[tuple[Path, dict[str, object]]] = []

    def inspect(request: ProcessRequest) -> ProcessOutput:
        assert request.isolated_home is not None
        assert request.env["CODEX_HOME"] == str(request.isolated_home)
        hooks = json.loads((request.isolated_home / "hooks.json").read_text(encoding="utf-8"))
        observed.append((request.isolated_home, hooks))
        # A real spawn adds credentials; a fake runner must never see them.
        assert not (request.isolated_home / "auth.json").exists()
        return ProcessOutput(1, "", "inspection only")

    output = tmp_path / "isolated-home"
    run_release_benchmark(
        output,
        confirm_paid_runs=True,
        process_runner=inspect,
        payload_lines=3,
        host="codex",
        path="mcp",
    )

    assert len(observed) == 80
    assert len({home for home, _ in observed}) == 80
    for home, hooks in observed:
        assert set(hooks["hooks"]) >= {"SessionStart", "PreToolUse", "PostToolUse", "Stop"}
        handler = hooks["hooks"]["PreToolUse"][0]["hooks"][0]
        assert handler["command"].endswith("-m htsave.codex_hooks")
        assert not home.exists()


def test_shell_attempts_do_not_provision_a_codex_home(tmp_path: Path) -> None:
    requests: list[ProcessRequest] = []

    def capture(request: ProcessRequest) -> ProcessOutput:
        requests.append(request)
        return ProcessOutput(1, "", "inspection only")

    run_release_benchmark(
        tmp_path / "shell-home",
        confirm_paid_runs=True,
        process_runner=capture,
        payload_lines=3,
        host="codex",
        path="shell",
    )

    assert len(requests) == 80
    assert all(request.isolated_home is None for request in requests)
    assert all("CODEX_HOME" not in request.env for request in requests)


def test_confirmation_gate_prevents_new_and_resumed_process_spawns(tmp_path: Path) -> None:
    calls = 0

    def forbidden(_: ProcessRequest) -> ProcessOutput:
        nonlocal calls
        calls += 1
        raise AssertionError("process runner must not be called")

    output = tmp_path / "dry-run"
    manifest = run_release_benchmark(
        output,
        process_runner=forbidden,
        payload_lines=3,
        host="codex",
    )
    resumed = resume_release_benchmark(
        output / "manifest.json",
        process_runner=forbidden,
    )

    assert calls == 0
    assert manifest.completed_count == resumed.completed_count == 0
    assert len(manifest.executions) == 80
    assert (output / "manifest.json").is_file()


def test_real_paid_shell_run_requires_transparent_posttool_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner_module,
        "probe_codex_compatibility",
        lambda **_: CodexCompatibility(
            "0.148.0", True, "supported", posttool_result_replacement=False
        ),
    )

    output = tmp_path / "blocked-paid-run"
    with pytest.raises(CompatibilityError, match="transparent PostToolUse"):
        run_release_benchmark(output, confirm_paid_runs=True, host="codex", path="shell")

    assert not output.exists()


def test_real_paid_mcp_run_requires_pretooluse_injection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner_module,
        "probe_codex_compatibility",
        lambda **_: CodexCompatibility("0.148.0", True, "supported", mcp_tool_injection=False),
    )

    output = tmp_path / "blocked-mcp-run"
    with pytest.raises(CompatibilityError, match="updatedInput"):
        run_release_benchmark(output, confirm_paid_runs=True, host="codex", path="mcp")

    assert not output.exists()


def test_mcp_paid_run_is_allowed_on_the_current_codex_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner_module,
        "probe_codex_compatibility",
        lambda **_: CodexCompatibility(
            "0.148.0",
            True,
            "supported",
            posttool_result_replacement=False,
            mcp_tool_injection=True,
        ),
    )
    spawned: list[ProcessRequest] = []

    def refuse_after_gate(request: ProcessRequest) -> ProcessOutput:
        spawned.append(request)
        return ProcessOutput(1, "", "stopped after the gate")

    runner_module._require_paid_contract("codex", "codex", "mcp")
    manifest = run_release_benchmark(
        tmp_path / "mcp-allowed",
        confirm_paid_runs=True,
        process_runner=refuse_after_gate,
        payload_lines=3,
        host="codex",
    )

    assert manifest.path == "mcp"
    assert len(spawned) == 80


def test_fake_codex_runs_all_80_slots_then_resumes_only_failed_attempt(
    tmp_path: Path,
) -> None:
    fake = FakeCodex(fail_once_execution="source_three_line_delta-p02-baseline")
    output = tmp_path / "release"

    first = run_release_benchmark(
        output,
        confirm_paid_runs=True,
        process_runner=fake,
        codex_executable="fake-codex",
        payload_lines=3,
        host="codex",
    )

    assert len(fake.requests) == 80
    assert first.completed_count == 79
    assert not first.release_report().passed
    assert len({request.cwd for request in fake.requests}) == 80
    assert all(
        any(
            value.startswith("mcp_servers.htsave.env.HTSAVE_STATE_DIR=")
            for value in request.argv
        )
        for request in fake.requests
    )
    assert all(
        any(
            value == f'mcp_servers.htsave.env.HTSAVE_BENCH_ARM="{request.arm}"'
            for value in request.argv
        )
        for request in fake.requests
    )
    assert len({_environment_without_arm(request) for request in fake.requests}) == 1
    assert {request.env["HTSAVE_BENCH_ARM"] for request in fake.requests} == {
        "baseline",
        "treatment",
    }
    assert all(Path(request.env["HTSAVE_STATE_DIR"]).is_absolute() for request in fake.requests)

    resumed = resume_release_benchmark(
        output / "manifest.json",
        confirm_paid_runs=True,
        process_runner=fake,
    )

    assert len(fake.requests) == 81
    assert fake.requests[-1].execution_id == "source_three_line_delta-p02-baseline"
    assert fake.requests[-1].attempt_number == 2
    assert resumed.completed_count == 80
    assert resumed.release_report().passed

    reloaded = load_release_manifest(output / "manifest.json")
    retried = next(
        execution
        for execution in reloaded.executions
        if execution.execution_id == "source_three_line_delta-p02-baseline"
    )
    assert [attempt.status for attempt in retried.attempts] == ["failed", "completed"]
    assert all(
        attempt.initial_tree_digest == reloaded.fixture_digests[execution.scenario_id]
        for execution in reloaded.executions
        for attempt in execution.attempts
    )
    assert not list((output / "runs").rglob("workspace-*"))
    assert (output / "runs" / retried.execution_id / "attempt-001" / "state").is_dir()
    assert (
        output / "runs" / retried.execution_id / "attempt-002" / "artifacts" / "events.jsonl"
    ).is_file()


class FakeCodex:
    def __init__(self, *, fail_once_execution: str) -> None:
        self.fail_once_execution = fail_once_execution
        self.failed = False
        self.requests: list[ProcessRequest] = []

    def __call__(self, request: ProcessRequest) -> ProcessOutput:
        self.requests.append(request)
        assert (request.cwd / ".git").is_dir()
        assert (request.cwd / "answer.schema.json").is_file()
        assert (request.cwd.parent / "state").is_dir()
        assert (request.cwd.parent / "artifacts").is_dir()
        status = subprocess.run(
            ("git", "status", "--porcelain"),
            cwd=request.cwd,
            text=True,
            capture_output=True,
            check=True,
        )
        assert status.stdout == ""

        if request.execution_id == self.fail_once_execution and not self.failed:
            self.failed = True
            return ProcessOutput(1, "", "simulated infrastructure failure")

        scenario = json.loads((request.cwd / "scenario.json").read_text(encoding="utf-8"))
        answer = {
            "scenario_id": request.scenario_id,
            "answers": scenario["expected_answers"],
            "assumptions": [],
        }
        (request.cwd.parent / "artifacts" / "answer.json").write_text(
            json.dumps(answer), encoding="utf-8", newline="\n"
        )
        input_tokens = 100 if request.arm == "baseline" else 70
        read_events = [
            json.dumps(
                {
                    "type": "item.completed",
                    "item": {
                        "type": "mcp_tool_call",
                        "tool": "htsave_read",
                        "arguments": {"path": path},
                    },
                }
            )
            for _ in range(4)
            for path in scenario["emit_paths"]
        ]
        events = "\n".join(
            [
                json.dumps(
                    {
                        "type": "thread.started",
                        "thread_id": f"thread-{request.execution_id}-{request.attempt_number}",
                    }
                ),
                json.dumps({"type": "turn.started"}),
                *read_events,
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": input_tokens,
                            "cached_input_tokens": input_tokens // 2,
                            "output_tokens": 5,
                            "reasoning_output_tokens": 1,
                        },
                    }
                ),
            ]
        )
        return ProcessOutput(0, events + "\n", "")


def _environment_without_arm(request: ProcessRequest) -> tuple[tuple[str, str], ...]:
    variable = {
        "HTSAVE_BENCH_ARM",
        "CODEX_HOME",
        "HTSAVE_STATE_DIR",
        "HTSAVE_BENCH_ARTIFACTS",
        "HTSAVE_BENCH_WORKSPACE",
    }
    return tuple(sorted((key, value) for key, value in request.env.items() if key not in variable))


def _driver(workspace: Path, operation: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        (sys.executable, "benchmark_driver.py", operation),
        cwd=workspace,
        capture_output=True,
        check=True,
    )
