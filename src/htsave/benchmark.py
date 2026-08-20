"""Deterministic parsing and release gates for Codex benchmark results.

This module deliberately does not launch Codex.  It consumes the public JSONL
stream produced by ``codex exec --json`` and evaluates already-paired usage
measurements without reading persisted sessions or rollout transcripts.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from statistics import median
from types import MappingProxyType
from typing import Literal

REQUIRED_SCENARIOS = (
    "large_readme_exact",
    "source_three_line_delta",
    "repeated_test_output",
    "multi_round_context",
)

# Which htsave delivery path the treatment arm exercises.  ``mcp`` drives the
# explicit ``htsave_read``/``htsave_hydrate`` tools that Codex supports today;
# ``shell`` drives transparent PostToolUse replacement, for which Codex provides
# no successful-result contract yet.
BenchmarkPath = Literal["mcp", "shell"]
BENCHMARK_PATHS: tuple[BenchmarkPath, ...] = ("mcp", "shell")
DEFAULT_BENCHMARK_PATH: BenchmarkPath = "mcp"

# Which agent CLI is measured.  Claude Code is the default because it is the
# only host whose transparent path can run at all.
Host = Literal["claude", "codex"]
BENCHMARK_HOSTS: tuple[Host, ...] = ("claude", "codex")
DEFAULT_HOST: Host = "claude"
DEFAULT_PATH_FOR_HOST: dict[Host, BenchmarkPath] = {"claude": "shell", "codex": "mcp"}

_USAGE_FIELDS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
)
_KNOWN_EVENT_TYPES = {
    "thread.started",
    "turn.started",
    "turn.completed",
    "turn.failed",
    "error",
}


class CodexExecProtocolError(ValueError):
    """The ``codex exec --json`` stream violates the supported contract."""


@dataclass(frozen=True, slots=True)
class Usage:
    """Actual token usage reported by a completed Codex turn."""

    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int

    def __post_init__(self) -> None:
        for name in _USAGE_FIELDS:
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")
        if self.cached_input_tokens > self.input_tokens:
            raise ValueError("cached_input_tokens cannot exceed input_tokens")


@dataclass(frozen=True, slots=True)
class ProtocolResult:
    """Validated terminal state from one Codex exec JSONL stream."""

    thread_id: str
    terminal: Literal["completed", "failed"]
    usage: Usage | None
    event_counts: Mapping[str, int]
    unknown_event_types: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.thread_id:
            raise ValueError("thread_id must not be empty")
        if self.terminal not in {"completed", "failed"}:
            raise ValueError(f"unsupported terminal state: {self.terminal!r}")
        if self.terminal == "completed" and self.usage is None:
            raise ValueError("completed protocol result requires usage")
        if self.terminal == "failed" and self.usage is not None:
            raise ValueError("failed protocol result cannot contain usage")
        object.__setattr__(
            self,
            "event_counts",
            MappingProxyType(dict(self.event_counts)),
        )

    @property
    def succeeded(self) -> bool:
        """Whether Codex completed without an explicit error event."""

        return self.terminal == "completed" and not self.errors


def parse_codex_exec_jsonl(payload: str | Iterable[str]) -> ProtocolResult:
    """Parse one public ``codex exec --json`` event stream.

    Additive event fields and unknown event types are preserved as diagnostics.
    Structural ambiguity is rejected: exactly one thread and one terminal turn
    event must be present, and completed turns must contain the four documented
    usage counters.
    """

    lines = payload.splitlines() if isinstance(payload, str) else payload
    event_counts: Counter[str] = Counter()
    unknown_event_types: set[str] = set()
    errors: list[str] = []
    thread_id: str | None = None
    terminal: Literal["completed", "failed"] | None = None
    usage: Usage | None = None

    for line_number, line in enumerate(lines, start=1):
        if not isinstance(line, str):
            raise CodexExecProtocolError(f"line {line_number}: expected text JSONL input")
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise CodexExecProtocolError(f"line {line_number}: malformed JSON: {exc.msg}") from exc
        if not isinstance(event, dict):
            raise CodexExecProtocolError(f"line {line_number}: event must be a JSON object")

        event_type = event.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise CodexExecProtocolError(
                f"line {line_number}: event type must be a non-empty string"
            )
        event_counts[event_type] += 1
        if event_type not in _KNOWN_EVENT_TYPES and not event_type.startswith("item."):
            unknown_event_types.add(event_type)

        if event_type == "thread.started":
            if thread_id is not None:
                raise CodexExecProtocolError(f"line {line_number}: duplicate thread.started event")
            candidate = event.get("thread_id")
            if not isinstance(candidate, str) or not candidate:
                raise CodexExecProtocolError(
                    f"line {line_number}: thread_id must be a non-empty string"
                )
            thread_id = candidate
        elif event_type == "turn.completed":
            if terminal is not None:
                raise CodexExecProtocolError(f"line {line_number}: duplicate terminal turn event")
            terminal = "completed"
            usage = _parse_usage(event.get("usage"), line_number)
        elif event_type == "turn.failed":
            if terminal is not None:
                raise CodexExecProtocolError(f"line {line_number}: duplicate terminal turn event")
            terminal = "failed"
            errors.append(_event_error(event, "turn.failed"))
        elif event_type == "error":
            errors.append(_event_error(event, "error"))

    if thread_id is None:
        raise CodexExecProtocolError("missing thread.started event")
    if terminal is None:
        raise CodexExecProtocolError("missing terminal turn event")

    return ProtocolResult(
        thread_id=thread_id,
        terminal=terminal,
        usage=usage,
        event_counts=event_counts,
        unknown_event_types=tuple(sorted(unknown_event_types)),
        errors=tuple(errors),
    )


def _parse_usage(value: object, line_number: int) -> Usage:
    if not isinstance(value, dict):
        raise CodexExecProtocolError(f"line {line_number}: turn.completed usage must be an object")
    missing = [name for name in _USAGE_FIELDS if name not in value]
    if missing:
        raise CodexExecProtocolError(
            f"line {line_number}: usage missing required fields: {', '.join(missing)}"
        )
    try:
        return Usage(**{name: value[name] for name in _USAGE_FIELDS})
    except ValueError as exc:
        raise CodexExecProtocolError(f"line {line_number}: {exc}") from exc


def _event_error(event: Mapping[str, object], fallback: str) -> str:
    detail = event.get("error")
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    if isinstance(detail, dict):
        message = detail.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    message = event.get("message")
    if isinstance(message, str) and message.strip():
        return message.strip()
    return fallback


@dataclass(frozen=True, slots=True)
class PairwiseResult:
    """The token measurements for one adjacent baseline/treatment pair."""

    pair_index: int
    baseline: Usage
    treatment: Usage

    def __post_init__(self) -> None:
        if isinstance(self.pair_index, bool) or not isinstance(self.pair_index, int):
            raise ValueError("pair_index must be a non-negative integer")
        if self.pair_index < 0:
            raise ValueError("pair_index must be a non-negative integer")
        if self.baseline.input_tokens == 0:
            raise ValueError("baseline input_tokens must be greater than zero")

    @property
    def input_reduction(self) -> Fraction:
        """Exact paired reduction based only on total input tokens."""

        return Fraction(
            self.baseline.input_tokens - self.treatment.input_tokens,
            self.baseline.input_tokens,
        )


@dataclass(frozen=True, slots=True)
class ScenarioReport:
    """Release decision for a single repeated-context scenario."""

    scenario_id: str
    pairs: tuple[PairwiseResult, ...]
    required_pairs: int = 5
    threshold: Fraction = Fraction(3, 10)
    median_input_reduction: Fraction | None = field(init=False)
    passed: bool = field(init=False)

    def __post_init__(self) -> None:
        if not self.scenario_id:
            raise ValueError("scenario_id must not be empty")
        if isinstance(self.required_pairs, bool) or not isinstance(self.required_pairs, int):
            raise ValueError("required_pairs must be a positive integer")
        if self.required_pairs <= 0:
            raise ValueError("required_pairs must be a positive integer")
        if not isinstance(self.threshold, Fraction):
            raise TypeError("threshold must be a Fraction")

        pairs = tuple(sorted(self.pairs, key=lambda pair: pair.pair_index))
        indexes = [pair.pair_index for pair in pairs]
        if len(set(indexes)) != len(indexes):
            raise ValueError("pair indexes must be unique within a scenario")
        reductions = tuple(pair.input_reduction for pair in pairs)
        middle = Fraction(median(reductions)) if reductions else None

        object.__setattr__(self, "pairs", pairs)
        object.__setattr__(self, "median_input_reduction", middle)
        object.__setattr__(
            self,
            "passed",
            len(pairs) >= self.required_pairs and middle is not None and middle >= self.threshold,
        )

    @property
    def pairwise_input_reductions(self) -> tuple[Fraction, ...]:
        return tuple(pair.input_reduction for pair in self.pairs)


def evaluate_scenario(
    scenario_id: str,
    pairs: Iterable[PairwiseResult],
    *,
    required_pairs: int = 5,
    threshold: Fraction = Fraction(3, 10),
) -> ScenarioReport:
    """Build the exact median release gate for one scenario."""

    return ScenarioReport(
        scenario_id=scenario_id,
        pairs=tuple(pairs),
        required_pairs=required_pairs,
        threshold=threshold,
    )


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    """Release decision requiring every named scenario to pass independently."""

    scenarios: tuple[ScenarioReport, ...]
    required_scenarios: tuple[str, ...] = REQUIRED_SCENARIOS
    missing_scenarios: tuple[str, ...] = field(init=False)
    passed: bool = field(init=False)

    def __post_init__(self) -> None:
        scenarios = tuple(self.scenarios)
        required = tuple(self.required_scenarios)
        if len(set(required)) != len(required):
            raise ValueError("required_scenarios must not contain duplicates")
        by_id = {report.scenario_id: report for report in scenarios}
        if len(by_id) != len(scenarios):
            raise ValueError("scenario ids must be unique")
        missing = tuple(name for name in required if name not in by_id)
        passed = not missing and all(by_id[name].passed for name in required)

        object.__setattr__(self, "scenarios", scenarios)
        object.__setattr__(self, "required_scenarios", required)
        object.__setattr__(self, "missing_scenarios", missing)
        object.__setattr__(self, "passed", passed)


def evaluate_benchmark(
    scenarios: Iterable[ScenarioReport],
    *,
    required_scenarios: Iterable[str] = REQUIRED_SCENARIOS,
) -> BenchmarkReport:
    """Build a release report without pooling token savings across scenarios."""

    return BenchmarkReport(
        scenarios=tuple(scenarios),
        required_scenarios=tuple(required_scenarios),
    )


def parse_claude_exec_json(payload: str) -> ProtocolResult:
    """Parse one public ``claude -p --output-format json`` result object.

    Claude Code reports usage per assistant turn under ``usage.iterations``,
    and the top-level counters describe only the final turn.  A repeated-context
    benchmark must compare what the whole session was charged, so every
    iteration is summed.  Cache reads are counted as input (they are billed and
    are disjoint from ``input_tokens`` here) and additionally reported on their
    own, never subtracted.
    """

    try:
        result = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise CodexExecProtocolError(f"malformed claude JSON result: {exc.msg}") from exc
    if not isinstance(result, dict):
        raise CodexExecProtocolError("claude result must be a JSON object")

    session_id = result.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise CodexExecProtocolError("claude result is missing session_id")

    subtype = result.get("subtype")
    failed = bool(result.get("is_error")) or result.get("terminal_reason") != "completed"
    errors: list[str] = []
    if failed:
        detail = result.get("terminal_reason") or subtype or "unknown"
        errors.append(f"claude terminal state: {detail}")
        return ProtocolResult(
            thread_id=session_id,
            terminal="failed",
            usage=None,
            event_counts=MappingProxyType({}),
            errors=tuple(errors),
        )

    usage_value = result.get("usage")
    if not isinstance(usage_value, dict):
        raise CodexExecProtocolError("claude result is missing usage")
    iterations = usage_value.get("iterations")
    if not isinstance(iterations, list) or not iterations:
        # A single-turn session may omit the breakdown; the top-level counters
        # then already describe the whole session.
        iterations = [usage_value]

    totals = Counter()
    for index, iteration in enumerate(iterations, start=1):
        if not isinstance(iteration, dict):
            raise CodexExecProtocolError(f"usage iteration {index} must be an object")
        for field_name in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        ):
            value = iteration.get(field_name, 0)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise CodexExecProtocolError(
                    f"usage iteration {index} field {field_name} must be a non-negative integer"
                )
            totals[field_name] += value

    cached = totals["cache_read_input_tokens"]
    billed_input = totals["input_tokens"] + totals["cache_creation_input_tokens"] + cached
    usage = Usage(
        input_tokens=billed_input,
        cached_input_tokens=cached,
        output_tokens=totals["output_tokens"],
        reasoning_output_tokens=0,
    )
    return ProtocolResult(
        thread_id=session_id,
        terminal="completed",
        usage=usage,
        event_counts=MappingProxyType({"usage.iterations": len(iterations)}),
    )
