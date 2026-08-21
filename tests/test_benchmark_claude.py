"""Claude Code paired-benchmark tests.

The fake host emits the same result envelope as a real
``claude -p --output-format json`` run, including the per-turn
``usage.iterations`` breakdown that the parser has to sum.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import htsave.benchmark_runner as runner_module
from htsave.benchmark import CodexExecProtocolError, Usage, parse_claude_exec_json
from htsave.benchmark_runner import (
    ProcessOutput,
    ProcessRequest,
    build_release_manifest,
    load_release_manifest,
    run_release_benchmark,
)
from htsave.compat import ClaudeCompatibility
from htsave.errors import CompatibilityError

SUPPORTED = ClaudeCompatibility("2.1.237", True, "supported", posttool_result_replacement=True)


def _result(
    *,
    iterations: list[dict[str, int]],
    answer: str,
    session_id: str = "sess-1",
    completed: bool = True,
    model_usage: dict[str, dict[str, int]] | None = None,
) -> str:
    payload: dict[str, object] = {
            "type": "result",
            "session_id": session_id,
            "is_error": not completed,
            "terminal_reason": "completed" if completed else "api_error",
            "subtype": "success",
            "result": answer,
            "usage": {
                "input_tokens": iterations[-1]["input_tokens"],
                "cache_read_input_tokens": iterations[-1].get("cache_read_input_tokens", 0),
                "cache_creation_input_tokens": iterations[-1].get("cache_creation_input_tokens", 0),
                "output_tokens": iterations[-1].get("output_tokens", 0),
                "iterations": iterations,
            },
        }
    if model_usage is not None:
        payload["modelUsage"] = model_usage
    return json.dumps(payload)


def test_usage_is_summed_across_turns_not_taken_from_the_last_one() -> None:
    payload = _result(
        iterations=[
            {"input_tokens": 100, "cache_creation_input_tokens": 20, "output_tokens": 5},
            {"input_tokens": 40, "cache_read_input_tokens": 90, "output_tokens": 3},
        ],
        answer="{}",
    )

    protocol = parse_claude_exec_json(payload)

    assert protocol.terminal == "completed"
    # 100 + 20 + 0, then 40 + 0 + 90.
    assert protocol.usage.input_tokens == 250
    assert protocol.usage.cached_input_tokens == 90
    assert protocol.usage.output_tokens == 8


def test_model_usage_aggregates_the_full_claude_session() -> None:
    payload = _result(
        iterations=[{"input_tokens": 8, "cache_read_input_tokens": 900}],
        model_usage={
            "claude-haiku-4-5-20251001": {
                "inputTokens": 120,
                "cacheReadInputTokens": 350,
                "outputTokens": 17,
            }
        },
        answer="{}",
    )

    protocol = parse_claude_exec_json(payload)

    assert protocol.usage == Usage(120, 350, 17, 0)
    assert protocol.event_counts["modelUsage"] == 1


def test_single_turn_results_without_a_breakdown_still_parse() -> None:
    payload = json.dumps(
        {
            "session_id": "sess-2",
            "is_error": False,
            "terminal_reason": "completed",
            "result": "{}",
            "usage": {"input_tokens": 7, "cache_creation_input_tokens": 1, "output_tokens": 2},
        }
    )

    assert parse_claude_exec_json(payload).usage.input_tokens == 8


def test_failed_and_malformed_results_are_distinguished() -> None:
    failed = parse_claude_exec_json(
        _result(iterations=[{"input_tokens": 1}], answer="", completed=False)
    )
    assert failed.terminal == "failed"
    assert failed.usage is None

    with pytest.raises(CodexExecProtocolError, match="malformed"):
        parse_claude_exec_json("{not json")
    with pytest.raises(CodexExecProtocolError, match="session_id"):
        parse_claude_exec_json("{}")


def test_claude_is_the_default_host_and_measures_the_transparent_path() -> None:
    manifest = build_release_manifest(codex_executable="fake-claude", payload_lines=6)

    assert manifest.host == "claude"
    assert manifest.path == "shell"
    assert manifest.argv[:2] == ("fake-claude", "-p")
    assert "--strict-mcp-config" in manifest.argv
    assert manifest.argv[manifest.argv.index("--output-format") + 1] == "json"
    # The arm lives in the config directory, never in argv.
    assert not any("settings" in item for item in manifest.argv)


def test_claude_refuses_the_redundant_mcp_path() -> None:
    with pytest.raises(ValueError, match="transparent shell path"):
        build_release_manifest(payload_lines=6, host="claude", path="mcp")


def test_paid_claude_run_requires_transparent_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner_module,
        "probe_claude_compatibility",
        lambda **_: ClaudeCompatibility(
            "2.1.237", True, "supported", posttool_result_replacement=False
        ),
    )

    output = tmp_path / "blocked"
    with pytest.raises(CompatibilityError, match="transparent PostToolUse"):
        run_release_benchmark(output, confirm_paid_runs=True, host="claude")

    assert not output.exists()


class FakeClaude:
    """Emit a plausible result, and record how each attempt was provisioned."""

    def __init__(self) -> None:
        self.requests: list[ProcessRequest] = []
        self.settings: list[dict[str, object]] = []

    def __call__(self, request: ProcessRequest) -> ProcessOutput:
        self.requests.append(request)
        assert request.isolated_home is not None
        assert request.env["CLAUDE_CONFIG_DIR"] == str(request.isolated_home)
        assert "CODEX_HOME" not in request.env
        # A real spawn adds credentials; a fake runner must never see them.
        assert not (request.isolated_home / ".credentials.json").exists()
        settings = json.loads((request.isolated_home / "settings.json").read_text(encoding="utf-8"))
        self.settings.append(settings)

        scenario = json.loads((request.cwd / "scenario.json").read_text(encoding="utf-8"))
        answer = json.dumps(
            {
                "scenario_id": request.scenario_id,
                "answers": scenario["expected_answers"],
                "assumptions": [],
            }
        )
        per_turn = 100 if request.arm == "baseline" else 60
        return ProcessOutput(
            0,
            _result(
                iterations=[{"input_tokens": per_turn, "output_tokens": 2} for _ in range(4)],
                answer=answer,
                session_id=f"sess-{request.execution_id}-{request.attempt_number}",
            ),
            "",
        )


def test_fake_claude_runs_all_80_slots_with_arm_specific_isolated_configs(
    tmp_path: Path,
) -> None:
    fake = FakeClaude()
    output = tmp_path / "claude-release"

    manifest = run_release_benchmark(
        output,
        confirm_paid_runs=True,
        process_runner=fake,
        codex_executable="fake-claude",
        payload_lines=3,
        host="claude",
    )

    assert len(fake.requests) == 80
    assert manifest.completed_count == 80
    assert manifest.release_report().passed
    assert len({request.argv for request in fake.requests}) == 1
    assert len({request.isolated_home for request in fake.requests}) == 80

    # The treatment arm is exactly "htsave's hooks are installed".
    treatment = [
        settings
        for settings, request in zip(fake.settings, fake.requests, strict=True)
        if request.arm == "treatment"
    ]
    baseline = [
        settings
        for settings, request in zip(fake.settings, fake.requests, strict=True)
        if request.arm == "baseline"
    ]
    assert len(treatment) == len(baseline) == 40
    assert all(settings == {} for settings in baseline)
    for settings in treatment:
        commands = [
            handler["command"]
            for group in settings["hooks"]["PostToolUse"]
            for handler in group["hooks"]
        ]
        assert any(command.endswith("-m htsave.claude_hooks") for command in commands)

    # Nothing is retained that could hold credentials.
    assert not list((output / "runs").rglob("claude-config"))

    reloaded = load_release_manifest(output / "manifest.json")
    assert reloaded.host == "claude"
    assert reloaded.path == "shell"


def test_manifest_rejects_a_host_switched_manifest(tmp_path: Path) -> None:
    output = tmp_path / "host-switch"
    run_release_benchmark(output, payload_lines=3, host="claude")
    manifest_path = output / "manifest.json"

    value = json.loads(manifest_path.read_text(encoding="utf-8"))
    value["host"] = "codex"
    manifest_path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ValueError, match="argv does not match"):
        load_release_manifest(manifest_path)


def test_a_bounded_first_run_stops_early_and_resume_finishes_the_rest(
    tmp_path: Path,
) -> None:
    from htsave.benchmark_runner import resume_release_benchmark

    fake = FakeClaude()
    output = tmp_path / "bounded"

    smoke = run_release_benchmark(
        output,
        confirm_paid_runs=True,
        process_runner=fake,
        codex_executable="fake-claude",
        payload_lines=3,
        host="claude",
        max_executions=2,
    )

    assert len(fake.requests) == 2, "a bounded run must not spend the other 38 slots"
    assert smoke.completed_count == 2
    # One full pair, so the first scenario is already comparable.
    assert {request.arm for request in fake.requests} == {"baseline", "treatment"}
    assert not smoke.release_report().passed

    finished = resume_release_benchmark(
        output / "manifest.json",
        confirm_paid_runs=True,
        process_runner=fake,
    )

    assert len(fake.requests) == 80
    assert finished.completed_count == 80
    assert finished.release_report().passed


@pytest.mark.parametrize(
    "message",
    [
        '{"scenario_id": "x", "answers": {"a": 1}, "assumptions": []}',
        'Done.\n\n```json\n{"scenario_id": "x", "answers": {"a": 1}, "assumptions": []}\n```\n',
        "Here is the result:\n"
        '{"scenario_id": "x", "answers": {"a": 1}, "assumptions": []}\n'
        "Hope that helps.",
    ],
)
def test_a_final_message_wrapped_in_prose_or_fences_still_yields_the_answer(
    message: str,
) -> None:
    from htsave.benchmark_runner import _extract_json_object

    assert json.loads(_extract_json_object(message)) == {
        "scenario_id": "x",
        "answers": {"a": 1},
        "assumptions": [],
    }


def test_a_final_message_without_any_json_object_is_an_error() -> None:
    from htsave.benchmark_runner import _extract_json_object

    with pytest.raises(RuntimeError, match="did not contain a JSON object"):
        _extract_json_object("I could not complete the task.")
