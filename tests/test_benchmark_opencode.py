"""OpenCode CLI paired-benchmark protocol and runner tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import htsave.benchmark_runner as runner_module
from htsave.benchmark import CodexExecProtocolError, Usage, parse_opencode_exec_json
from htsave.benchmark_runner import (
    ProcessOutput,
    ProcessRequest,
    build_release_manifest,
    load_release_manifest,
    run_release_benchmark,
)
from htsave.compat import OpenCodeCompatibility
from htsave.errors import CompatibilityError


def _opencode_result(
    *,
    answer: str,
    session_id: str = "session-1",
    input_tokens: int = 120,
    cached_input_tokens: int = 30,
    output_tokens: int = 9,
    reasoning_output_tokens: int = 4,
    status: str = "completed",
    structured_output: dict[str, object] | None = None,
) -> str:
    result: dict[str, object] = {
        "session_id": session_id,
        "status": status,
        "usage": {
            "input_tokens": input_tokens,
            "cached_input_tokens": cached_input_tokens,
            "output_tokens": output_tokens,
            "reasoning_output_tokens": reasoning_output_tokens,
        },
        "response": answer,
    }
    if structured_output is not None:
        result["structured_output"] = structured_output
    return json.dumps({"event": "result", "result": result})


def test_opencode_stream_parser_reads_result_usage() -> None:
    payload = "\n".join(
        [
            json.dumps({"event": "step", "step": {"type": "thinking"}}),
            _opencode_result(answer="{}"),
        ]
    )
    protocol = parse_opencode_exec_json(payload)

    assert protocol.succeeded
    assert protocol.thread_id == "session-1"
    assert protocol.usage == Usage(120, 30, 9, 4)
    assert protocol.event_counts == {"result": 1, "step": 1}


def test_opencode_stream_parser_rejects_missing_usage() -> None:
    failed = parse_opencode_exec_json(
        _opencode_result(answer="", status="failed")
    )
    assert failed.terminal == "failed"
    assert failed.usage is None
    assert failed.errors == ("opencode terminal state: failed",)

    missing_usage = json.dumps(
        {
            "event": "result",
            "result": {
                "session_id": "session-2",
                "status": "completed",
            },
        }
    )
    with pytest.raises(CodexExecProtocolError, match="missing usage"):
        parse_opencode_exec_json(missing_usage)


def test_opencode_manifest_uses_json_mcp_path() -> None:
    manifest = build_release_manifest(
        codex_executable="fake-opencode",
        model="opencode/big-pickle",
        host="opencode",
    )

    assert manifest.host == "opencode"
    assert manifest.path == "mcp"
    assert manifest.argv[:3] == ("fake-opencode", "run", "--format")
    assert "json" in manifest.argv
    assert "--auto" in manifest.argv
    assert "--model" in manifest.argv

    with pytest.raises(ValueError, match="explicit MCP path"):
        build_release_manifest(host="opencode", path="shell")


class FakeOpenCode:
    def __init__(self) -> None:
        self.requests: list[ProcessRequest] = []

    def __call__(self, request: ProcessRequest) -> ProcessOutput:
        self.requests.append(request)
        assert request.isolated_home is not None
        assert request.env["OPENCODE_HOME"] == str(request.isolated_home)
        assert "CODEX_HOME" not in request.env
        assert "CLAUDE_CONFIG_DIR" not in request.env

        if request.arm == "treatment":
            plugin = request.cwd / ".opencode" / "plugins" / "htsave.js"
            assert plugin.is_file()

        prompt_index = request.argv.index("--prompt")
        assert request.argv[prompt_index + 1] == (request.cwd / "prompt.txt").read_text(
            encoding="utf-8"
        )
        assert request.stdin == ""

        scenario = json.loads((request.cwd / "scenario.json").read_text(encoding="utf-8"))
        answer = json.dumps(
            {
                "scenario_id": request.scenario_id,
                "answers": scenario["expected_answers"],
                "assumptions": [],
            }
        )
        reads = [
            json.dumps(
                {
                    "event": "tool_call",
                    "tool": {
                        "name": "htsave_read",
                        "arguments": {"path": path},
                    },
                }
            )
            for _ in range(4)
            for path in scenario["emit_paths"]
        ]
        input_tokens = 100 if request.arm == "baseline" else 60
        return ProcessOutput(
            0,
            "\n".join(
                [
                    *reads,
                    _opencode_result(
                        answer=answer,
                        session_id=f"session-{request.execution_id}",
                        input_tokens=input_tokens,
                        structured_output=json.loads(answer),
                    ),
                ]
            )
            + "\n",
            "",
        )


def test_fake_opencode_runs_all_80_slots(tmp_path: Path) -> None:
    fake = FakeOpenCode()
    output = tmp_path / "opencode-release"

    manifest = run_release_benchmark(
        output,
        confirm_paid_runs=True,
        process_runner=fake,
        codex_executable="fake-opencode",
        model="opencode/big-pickle",
        payload_lines=3,
        host="opencode",
    )

    assert len(fake.requests) == 80
    assert manifest.completed_count == 80
    assert manifest.release_report().passed
    assert not list((output / "runs").rglob("opencode-home"))


def test_opencode_paid_contract_rejects_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner_module,
        "probe_opencode_compatibility",
        lambda **_: OpenCodeCompatibility(
            None, False, "opencode-version-probe-failed"
        ),
    )
    with pytest.raises(CompatibilityError, match="supported OpenCode build"):
        runner_module._require_paid_contract("opencode", "definitely-not-opencode", "mcp")


@pytest.mark.parametrize(
    "message",
    [
        '{"scenario_id": "x", "answers": {"a": 1}, "assumptions": []}',
        'Here:\n```json\n{"scenario_id": "x", "answers": {"a": 1}, "assumptions": []}\n```\n',
        "Result:\n"
        '{"scenario_id": "x", "answers": {"a": 1}, "assumptions": []}\n'
        "Done.",
    ],
)
def test_opencode_answer_extraction(message: str) -> None:
    from htsave.benchmark_runner import _extract_json_object

    assert json.loads(_extract_json_object(message)) == {
        "scenario_id": "x",
        "answers": {"a": 1},
        "assumptions": [],
    }


def test_opencode_answer_without_json_is_error() -> None:
    from htsave.benchmark_runner import _extract_json_object

    with pytest.raises(RuntimeError, match="JSON object"):
        _extract_json_object("no json here at all")


def test_manifest_rejects_host_switch(tmp_path: Path) -> None:
    output = tmp_path / "host-switch"
    run_release_benchmark(output, payload_lines=3, host="opencode")
    manifest_path = output / "manifest.json"

    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["host"] = "codex"
    manifest_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="argv does not match"):
        load_release_manifest(manifest_path)


def test_bounded_first_run_and_resume(tmp_path: Path) -> None:
    from htsave.benchmark_runner import resume_release_benchmark

    fake = FakeOpenCode()
    output = tmp_path / "bounded"

    smoke = run_release_benchmark(
        output,
        confirm_paid_runs=True,
        process_runner=fake,
        codex_executable="fake-opencode",
        model="opencode/big-pickle",
        payload_lines=3,
        host="opencode",
        max_executions=2,
    )

    assert len(fake.requests) == 2
    assert smoke.completed_count == 2
    assert not smoke.release_report().passed

    finished = resume_release_benchmark(
        output / "manifest.json",
        confirm_paid_runs=True,
        process_runner=fake,
    )

    assert len(fake.requests) == 80
    assert finished.completed_count == 80
    assert finished.release_report().passed
