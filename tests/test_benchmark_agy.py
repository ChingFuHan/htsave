"""Antigravity CLI paired-benchmark protocol and runner tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import htsave.benchmark_runner as runner_module
from htsave.benchmark import CodexExecProtocolError, Usage, parse_agy_exec_json
from htsave.benchmark_runner import (
    ProcessOutput,
    ProcessRequest,
    build_release_manifest,
    run_release_benchmark,
)
from htsave.errors import CompatibilityError


def _agy_result(
    *,
    answer: str,
    conversation_id: str = "conversation-1",
    input_tokens: int = 120,
    cache_read_tokens: int = 30,
    output_tokens: int = 9,
    thinking_tokens: int = 4,
    status: str = "SUCCESS",
    structured_output: dict[str, object] | None = None,
) -> str:
    result: dict[str, object] = {
        "conversation_id": conversation_id,
        "status": status,
        "usage": {
            "input_tokens": input_tokens,
            "cache_read_tokens": cache_read_tokens,
            "output_tokens": output_tokens,
            "thinking_tokens": thinking_tokens,
        },
        "response": answer,
    }
    if structured_output is not None:
        result["structured_output"] = structured_output
    return json.dumps(
        {
            "event": "result",
            "result": result,
        }
    )


def test_agy_stream_parser_reads_official_result_usage() -> None:
    payload = "\n".join(
        [
            json.dumps({"event": "step_update", "step_update": {"step_type": "thinking"}}),
            _agy_result(answer="{}"),
        ]
    )

    protocol = parse_agy_exec_json(payload)

    assert protocol.succeeded
    assert protocol.thread_id == "conversation-1"
    assert protocol.usage == Usage(120, 30, 9, 4)
    assert protocol.event_counts == {"result": 1, "step_update": 1}


def test_agy_stream_parser_rejects_missing_usage_and_reports_failed_result() -> None:
    failed = parse_agy_exec_json(
        _agy_result(answer="", status="ERROR")
    )
    assert failed.terminal == "failed"
    assert failed.usage is None
    assert failed.errors == ("agy terminal state: ERROR",)

    missing_usage = json.dumps(
        {
            "event": "result",
            "result": {
                "conversation_id": "conversation-2",
                "status": "SUCCESS",
            },
        }
    )
    with pytest.raises(CodexExecProtocolError, match="missing usage"):
        parse_agy_exec_json(missing_usage)


def test_agy_manifest_uses_stream_json_mcp_path() -> None:
    manifest = build_release_manifest(
        codex_executable="fake-agy",
        model="gemini-3.7-flash-low",
        host="agy",
    )

    assert manifest.host == "agy"
    assert manifest.path == "mcp"
    assert manifest.argv[:4] == (
        "fake-agy",
        "--output-format",
        "stream-json",
        "--model",
    )
    assert "--json-schema" in manifest.argv
    assert "--dangerously-skip-permissions" in manifest.argv
    assert "--add-dir" in manifest.argv

    with pytest.raises(ValueError, match="explicit MCP path"):
        build_release_manifest(host="agy", path="shell")


class FakeAgy:
    def __init__(self) -> None:
        self.requests: list[ProcessRequest] = []
        self.configs: list[dict[str, object]] = []

    def __call__(self, request: ProcessRequest) -> ProcessOutput:
        self.requests.append(request)
        assert request.isolated_home is not None
        assert request.env["HOME"] == str(request.isolated_home)
        assert "CODEX_HOME" not in request.env
        token = request.isolated_home / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
        assert not token.exists()

        config_path = request.isolated_home / ".gemini" / "config" / "mcp_config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        self.configs.append(config)
        server = config["mcpServers"]["htsave"]
        assert server["env"]["HTSAVE_BENCH_ARM"] == request.arm
        assert Path(server["env"]["HTSAVE_STATE_DIR"]).is_absolute()
        settings_path = (
            request.isolated_home / ".gemini" / "antigravity-cli" / "settings.json"
        )
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        assert settings["trustedWorkspaces"] == [str(request.cwd.resolve())]
        assert settings["permissions"]["allow"] == ["mcp(htsave/*)", "command(*)"]
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
                    "event": "step_update",
                    "step_update": {
                        "state": "DONE",
                        "step_type": "tool",
                        "tool_info": {
                            "parameters": {
                                "ServerName": "htsave",
                                "ToolName": "htsave_read",
                                "Arguments": {"path": path},
                            }
                        },
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
                    _agy_result(
                        answer=answer,
                        conversation_id=f"conversation-{request.execution_id}",
                        input_tokens=input_tokens,
                        structured_output=json.loads(answer),
                    ),
                ]
            )
            + "\n",
            "",
        )


def test_fake_agy_runs_all_slots_with_isolated_mcp_config(tmp_path: Path) -> None:
    fake = FakeAgy()
    output = tmp_path / "agy-release"

    manifest = run_release_benchmark(
        output,
        confirm_paid_runs=True,
        process_runner=fake,
        codex_executable="fake-agy",
        model="gemini-3.7-flash-low",
        payload_lines=3,
        host="agy",
    )

    assert len(fake.requests) == 80
    assert manifest.completed_count == 80
    assert manifest.release_report().passed
    assert len({request.argv for request in fake.requests}) == 4
    assert len({request.isolated_home for request in fake.requests}) == 80
    assert len(fake.configs) == 80
    assert not list((output / "runs").rglob("agy-home"))


def test_agy_paid_contract_rejects_missing_executable() -> None:
    with pytest.raises(CompatibilityError, match="agy executable was not found"):
        runner_module._require_paid_contract("agy", "definitely-not-an-agy", "mcp")
