from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from htsave.benchmark import (
    REQUIRED_SCENARIOS,
    CodexExecProtocolError,
    PairwiseResult,
    Usage,
    evaluate_benchmark,
    evaluate_scenario,
    parse_codex_exec_jsonl,
)

FIXTURES = Path(__file__).parent / "fixtures" / "codex_exec"


def _usage(input_tokens: int, cached_input_tokens: int = 0) -> Usage:
    return Usage(
        input_tokens=input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=5,
        reasoning_output_tokens=1,
    )


def _pairs(
    baseline_input: int,
    treatment_input: int,
    *,
    baseline_cached: int = 0,
    treatment_cached: int = 0,
) -> tuple[PairwiseResult, ...]:
    return tuple(
        PairwiseResult(
            pair_index=index,
            baseline=_usage(baseline_input, baseline_cached),
            treatment=_usage(treatment_input, treatment_cached),
        )
        for index in range(5)
    )


def test_parse_official_codex_exec_jsonl_fixture() -> None:
    result = parse_codex_exec_jsonl((FIXTURES / "success.jsonl").read_text(encoding="utf-8"))

    assert result.thread_id == "0199a213-81c0-7800-8aa1-bbab2a035a53"
    assert result.terminal == "completed"
    assert result.succeeded
    assert result.usage == Usage(24763, 24448, 122, 0)
    assert result.event_counts["item.completed"] == 1
    assert result.unknown_event_types == ()


def test_parser_accepts_additive_fields_and_unknown_event_types() -> None:
    result = parse_codex_exec_jsonl((FIXTURES / "additive.jsonl").read_text(encoding="utf-8"))

    assert result.succeeded
    assert result.usage == Usage(100, 80, 10, 2)
    assert result.unknown_event_types == ("future.snapshot",)
    assert result.event_counts["future.snapshot"] == 1


def test_parser_rejects_malformed_json_with_line_number() -> None:
    payload = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-1"}',
            "not-json",
            '{"type":"turn.completed","usage":{}}',
        ]
    )

    with pytest.raises(CodexExecProtocolError, match=r"line 2: malformed JSON"):
        parse_codex_exec_jsonl(payload)


@pytest.mark.parametrize(
    "payload, message",
    [
        (
            "\n".join(
                [
                    '{"type":"thread.started","thread_id":"thread-1"}',
                    '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}',
                    '{"type":"turn.failed","error":{"message":"late failure"}}',
                ]
            ),
            "duplicate terminal turn event",
        ),
        (
            '{"type":"thread.started","thread_id":"thread-1"}',
            "missing terminal turn event",
        ),
        (
            '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}',
            "missing thread.started event",
        ),
        (
            "\n".join(
                [
                    '{"type":"thread.started","thread_id":"thread-1"}',
                    '{"type":"thread.started","thread_id":"thread-1"}',
                    '{"type":"turn.failed"}',
                ]
            ),
            "duplicate thread.started event",
        ),
    ],
)
def test_parser_rejects_duplicate_or_missing_lifecycle_events(payload: str, message: str) -> None:
    with pytest.raises(CodexExecProtocolError, match=message):
        parse_codex_exec_jsonl(payload)


@pytest.mark.parametrize(
    "usage, message",
    [
        (
            '{"input_tokens":10,"cached_input_tokens":0,"output_tokens":1}',
            "usage missing required fields: reasoning_output_tokens",
        ),
        (
            '{"input_tokens":true,"cached_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}',
            "input_tokens must be a non-negative integer",
        ),
        (
            '{"input_tokens":10,"cached_input_tokens":11,"output_tokens":1,"reasoning_output_tokens":0}',
            "cached_input_tokens cannot exceed input_tokens",
        ),
    ],
)
def test_parser_rejects_invalid_usage(usage: str, message: str) -> None:
    payload = "\n".join(
        [
            '{"type":"thread.started","thread_id":"thread-1"}',
            f'{{"type":"turn.completed","usage":{usage}}}',
        ]
    )

    with pytest.raises(CodexExecProtocolError, match=message):
        parse_codex_exec_jsonl(payload)


def test_failed_turn_is_valid_protocol_but_not_successful() -> None:
    result = parse_codex_exec_jsonl(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-1"}',
                '{"type":"turn.failed","error":{"message":"model unavailable"}}',
            ]
        )
    )

    assert result.terminal == "failed"
    assert result.usage is None
    assert result.errors == ("model unavailable",)
    assert not result.succeeded


def test_explicit_error_event_marks_completed_protocol_unsuccessful() -> None:
    result = parse_codex_exec_jsonl(
        "\n".join(
            [
                '{"type":"thread.started","thread_id":"thread-1"}',
                '{"type":"error","message":"transient stream error"}',
                '{"type":"turn.completed","usage":{"input_tokens":10,"cached_input_tokens":0,"output_tokens":1,"reasoning_output_tokens":0}}',
            ]
        )
    )

    assert result.terminal == "completed"
    assert result.errors == ("transient stream error",)
    assert not result.succeeded


def test_exact_thirty_percent_median_reduction_passes() -> None:
    report = evaluate_scenario("scenario", _pairs(100, 70))

    assert report.median_input_reduction == Fraction(3, 10)
    assert report.passed


def test_twenty_nine_percent_median_reduction_fails() -> None:
    report = evaluate_scenario("scenario", _pairs(100, 71))

    assert report.median_input_reduction == Fraction(29, 100)
    assert not report.passed


def test_cached_input_tokens_do_not_affect_release_reduction() -> None:
    uncached = evaluate_scenario("uncached", _pairs(100, 70))
    mostly_cached = evaluate_scenario(
        "cached",
        _pairs(100, 70, baseline_cached=99, treatment_cached=70),
    )

    assert uncached.median_input_reduction == mostly_cached.median_input_reduction
    assert mostly_cached.median_input_reduction == Fraction(3, 10)


def test_benchmark_requires_each_of_four_scenarios_to_pass() -> None:
    reports = [evaluate_scenario(name, _pairs(100, 0)) for name in REQUIRED_SCENARIOS[:-1]]
    reports.append(evaluate_scenario(REQUIRED_SCENARIOS[-1], _pairs(100, 71)))

    benchmark = evaluate_benchmark(reports)

    assert not benchmark.passed
    assert benchmark.missing_scenarios == ()
    assert sum(report.median_input_reduction for report in reports) / 4 > Fraction(3, 10)


def test_benchmark_reports_missing_required_scenario() -> None:
    reports = [evaluate_scenario(name, _pairs(100, 70)) for name in REQUIRED_SCENARIOS[:-1]]

    benchmark = evaluate_benchmark(reports)

    assert not benchmark.passed
    assert benchmark.missing_scenarios == (REQUIRED_SCENARIOS[-1],)
