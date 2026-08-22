"""Paid-call-gated harness for deterministic paired Codex benchmarks.

The runner owns fixture generation, process isolation, manifest persistence, and
resume behavior.  It never launches Codex unless the caller explicitly passes
``confirm_paid_runs=True``.  Token parsing and release arithmetic remain in
``htsave.benchmark``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from htsave.agy_install import install as install_agy_integration
from htsave.benchmark import (
    BENCHMARK_HOSTS,
    BENCHMARK_PATHS,
    DEFAULT_BENCHMARK_PATH,
    DEFAULT_HOST,
    DEFAULT_PATH_FOR_HOST,
    REQUIRED_PAIRS,
    REQUIRED_SCENARIOS,
    BenchmarkPath,
    BenchmarkReport,
    Host,
    PairwiseResult,
    Usage,
    evaluate_benchmark,
    evaluate_scenario,
    parse_agy_exec_json,
    parse_claude_exec_json,
    parse_codex_exec_jsonl,
)
from htsave.claude_install import install as install_claude_integration
from htsave.compat import probe_claude_compatibility, probe_codex_compatibility
from htsave.errors import CompatibilityError
from htsave.plugin import MCP_MODULE, PLUGIN_NAME, load_rendered_hooks

MANIFEST_SCHEMA_VERSION = 3
PAIR_COUNT = REQUIRED_PAIRS
EXECUTION_COUNT = len(REQUIRED_SCENARIOS) * PAIR_COUNT * 2
DEFAULT_PAYLOAD_LINES = 1024
DEFAULT_MODEL_FOR_HOST: dict[str, str] = {
    "agy": "gemini-3.7-flash-low",
    "claude": "claude-opus-5",
    "codex": "gpt-5.6-sol",
}
_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
_PACKAGED_TEMPLATE_ROOT = Path(__file__).resolve().parent / "_benchmark_templates"
_SOURCE_TEMPLATE_ROOT = _REPOSITORY_ROOT / "benchmark" / "templates"


def _template_root() -> Path:
    """Resolve templates from an installed wheel, then from a source checkout."""

    if _PACKAGED_TEMPLATE_ROOT.is_dir():
        return _PACKAGED_TEMPLATE_ROOT
    if _SOURCE_TEMPLATE_ROOT.is_dir():
        return _SOURCE_TEMPLATE_ROOT
    raise RuntimeError("benchmark templates are not packaged")


Arm = Literal["baseline", "treatment"]
AttemptStatus = Literal["completed", "failed"]


@dataclass(frozen=True, slots=True)
class ProcessRequest:
    execution_id: str
    scenario_id: str
    pair_index: int
    arm: Arm
    attempt_number: int
    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    stdin: str
    timeout_seconds: int
    # The throwaway host home this attempt runs against: a CODEX_HOME for the
    # Codex MCP path, a CLAUDE_CONFIG_DIR for Claude Code.  A real spawn adds
    # credentials to it; fake runners never do.
    isolated_home: Path | None = None


@dataclass(frozen=True, slots=True)
class ProcessOutput:
    returncode: int
    stdout: str
    stderr: str


ProcessRunner = Callable[[ProcessRequest], ProcessOutput]


@dataclass(frozen=True, slots=True)
class AttemptRecord:
    attempt_number: int
    status: AttemptStatus
    initial_tree_digest: str | None
    returncode: int | None
    thread_id: str | None = None
    usage: Usage | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if (
            isinstance(self.attempt_number, bool)
            or not isinstance(self.attempt_number, int)
            or self.attempt_number < 1
        ):
            raise ValueError("attempt_number must be positive")
        if self.status == "completed":
            if self.usage is None or self.returncode != 0:
                raise ValueError("completed attempt must have usage and a zero return code")
        elif self.status == "failed":
            if self.usage is not None:
                raise ValueError("failed attempt must not have usage")
        else:  # pragma: no cover - Literal guard for untrusted manifest input
            raise ValueError(f"invalid attempt status: {self.status!r}")

    def to_dict(self) -> dict[str, object]:
        usage = None
        if self.usage is not None:
            usage = {
                "input_tokens": self.usage.input_tokens,
                "cached_input_tokens": self.usage.cached_input_tokens,
                "output_tokens": self.usage.output_tokens,
                "reasoning_output_tokens": self.usage.reasoning_output_tokens,
            }
        return {
            "attempt_number": self.attempt_number,
            "status": self.status,
            "initial_tree_digest": self.initial_tree_digest,
            "returncode": self.returncode,
            "thread_id": self.thread_id,
            "usage": usage,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> AttemptRecord:
        usage_value = value.get("usage")
        usage = Usage(**usage_value) if isinstance(usage_value, dict) else None
        status = value.get("status")
        if status not in {"completed", "failed"}:
            raise ValueError(f"invalid attempt status: {status!r}")
        return cls(
            attempt_number=_required_int(value, "attempt_number"),
            status=status,
            initial_tree_digest=_optional_str(value.get("initial_tree_digest")),
            returncode=_optional_int(value.get("returncode")),
            thread_id=_optional_str(value.get("thread_id")),
            usage=usage,
            error=_optional_str(value.get("error")),
        )


@dataclass(slots=True)
class ExecutionSpec:
    execution_id: str
    scenario_id: str
    pair_index: int
    arm: Arm
    order_index: int
    attempts: list[AttemptRecord] = field(default_factory=list)

    @property
    def completed_attempt(self) -> AttemptRecord | None:
        return next(
            (attempt for attempt in reversed(self.attempts) if attempt.status == "completed"),
            None,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_id": self.execution_id,
            "scenario_id": self.scenario_id,
            "pair_index": self.pair_index,
            "arm": self.arm,
            "order_index": self.order_index,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ExecutionSpec:
        scenario_id = _required_str(value, "scenario_id")
        pair_index = _required_int(value, "pair_index")
        arm = value.get("arm")
        if arm not in {"baseline", "treatment"}:
            raise ValueError(f"invalid benchmark arm: {arm!r}")
        expected_id = _execution_id(scenario_id, pair_index, arm)
        execution_id = _required_str(value, "execution_id")
        if execution_id != expected_id:
            raise ValueError(f"unexpected execution id: {execution_id!r}")
        attempts_value = value.get("attempts", [])
        if not isinstance(attempts_value, list):
            raise ValueError("attempts must be a list")
        attempts = [
            AttemptRecord.from_dict(item) for item in attempts_value if isinstance(item, dict)
        ]
        if len(attempts) != len(attempts_value):
            raise ValueError("attempts must contain objects")
        return cls(
            execution_id=execution_id,
            scenario_id=scenario_id,
            pair_index=pair_index,
            arm=arm,
            order_index=_required_int(value, "order_index"),
            attempts=attempts,
        )


@dataclass(slots=True)
class ReleaseManifest:
    codex_executable: str
    model: str
    reasoning_effort: str
    payload_lines: int
    timeout_seconds: int
    argv: tuple[str, ...]
    fixture_digests: dict[str, str]
    executions: list[ExecutionSpec]
    host: Host = DEFAULT_HOST
    path: BenchmarkPath = DEFAULT_BENCHMARK_PATH
    python_executable: str = ""
    schema_version: int = MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MANIFEST_SCHEMA_VERSION:
            raise ValueError(f"unsupported manifest schema version: {self.schema_version}")
        if self.host not in BENCHMARK_HOSTS:
            raise ValueError(f"unknown benchmark host: {self.host!r}")
        if self.path not in BENCHMARK_PATHS:
            raise ValueError(f"unknown benchmark path: {self.path!r}")
        if not self.python_executable:
            raise ValueError("python_executable must be recorded in the manifest")
        if self.payload_lines <= 0:
            raise ValueError("payload_lines must be positive")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if len(self.executions) != EXECUTION_COUNT:
            raise ValueError(f"release manifest must contain {EXECUTION_COUNT} executions")
        ids = [execution.execution_id for execution in self.executions]
        orders = [execution.order_index for execution in self.executions]
        if len(set(ids)) != len(ids):
            raise ValueError("execution ids must be unique")
        if sorted(orders) != list(range(EXECUTION_COUNT)):
            raise ValueError("execution order must be contiguous")
        if any(execution.order_index != index for index, execution in enumerate(self.executions)):
            raise ValueError("execution list order must match order_index")
        if set(self.fixture_digests) != set(REQUIRED_SCENARIOS):
            raise ValueError("fixture digests must cover all required scenarios")

    @property
    def completed_count(self) -> int:
        return sum(execution.completed_attempt is not None for execution in self.executions)

    def release_report(self) -> BenchmarkReport:
        by_key = {
            (execution.scenario_id, execution.pair_index, execution.arm): execution
            for execution in self.executions
        }
        scenarios = []
        for scenario_id in REQUIRED_SCENARIOS:
            pairs: list[PairwiseResult] = []
            for pair_index in range(PAIR_COUNT):
                baseline = by_key[(scenario_id, pair_index, "baseline")].completed_attempt
                treatment = by_key[(scenario_id, pair_index, "treatment")].completed_attempt
                if (
                    baseline is not None
                    and baseline.usage is not None
                    and treatment is not None
                    and treatment.usage is not None
                ):
                    pairs.append(PairwiseResult(pair_index, baseline.usage, treatment.usage))
            scenarios.append(evaluate_scenario(scenario_id, pairs))
        return evaluate_benchmark(scenarios)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "host": self.host,
            "path": self.path,
            "codex_executable": self.codex_executable,
            "python_executable": self.python_executable,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "payload_lines": self.payload_lines,
            "timeout_seconds": self.timeout_seconds,
            "argv": list(self.argv),
            "fixture_digests": dict(sorted(self.fixture_digests.items())),
            "executions": [execution.to_dict() for execution in self.executions],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> ReleaseManifest:
        argv_value = value.get("argv")
        digests_value = value.get("fixture_digests")
        executions_value = value.get("executions")
        if not isinstance(argv_value, list) or not all(
            isinstance(item, str) for item in argv_value
        ):
            raise ValueError("argv must be a list of strings")
        if not isinstance(digests_value, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in digests_value.items()
        ):
            raise ValueError("fixture_digests must be a string map")
        if not isinstance(executions_value, list):
            raise ValueError("executions must be a list")
        executions = [
            ExecutionSpec.from_dict(item) for item in executions_value if isinstance(item, dict)
        ]
        if len(executions) != len(executions_value):
            raise ValueError("executions must contain objects")
        codex_executable = _required_str(value, "codex_executable")
        python_executable = _required_str(value, "python_executable")
        model = _required_str(value, "model")
        reasoning_effort = _required_str(value, "reasoning_effort")
        path = value.get("path")
        if path not in BENCHMARK_PATHS:
            raise ValueError(f"unknown benchmark path: {path!r}")
        host = value.get("host")
        if host not in BENCHMARK_HOSTS:
            raise ValueError(f"unknown benchmark host: {host!r}")
        expected_argv = _host_argv(
            host, codex_executable, model, reasoning_effort, path, python_executable
        )
        if tuple(argv_value) != expected_argv:
            raise ValueError("manifest argv does not match its Codex execution settings")
        return cls(
            schema_version=_required_int(value, "schema_version"),
            host=host,
            path=path,
            codex_executable=codex_executable,
            python_executable=python_executable,
            model=model,
            reasoning_effort=reasoning_effort,
            payload_lines=_required_int(value, "payload_lines"),
            timeout_seconds=_required_int(value, "timeout_seconds"),
            argv=tuple(argv_value),
            fixture_digests=dict(digests_value),
            executions=executions,
        )


def build_release_manifest(
    *,
    codex_executable: str = "codex",
    model: str = "gpt-5.6-sol",
    reasoning_effort: str = "low",
    payload_lines: int = DEFAULT_PAYLOAD_LINES,
    timeout_seconds: int = 900,
    host: Host = DEFAULT_HOST,
    path: BenchmarkPath | None = None,
    python_executable: str | None = None,
) -> ReleaseManifest:
    """Create the immutable 4 x 5 x 2 release schedule without launching a host."""

    if payload_lines <= 0:
        raise ValueError("payload_lines must be positive")
    if host not in BENCHMARK_HOSTS:
        raise ValueError(f"unknown benchmark host: {host!r}")
    path = path if path is not None else DEFAULT_PATH_FOR_HOST[host]
    if path not in BENCHMARK_PATHS:
        raise ValueError(f"unknown benchmark path: {path!r}")
    interpreter = python_executable or sys.executable
    fixture_digests = {
        scenario_id: _generated_fixture_digest(scenario_id, payload_lines, path)
        for scenario_id in REQUIRED_SCENARIOS
    }
    executions: list[ExecutionSpec] = []
    order_index = 0
    for scenario_index, scenario_id in enumerate(REQUIRED_SCENARIOS):
        for pair_index in range(PAIR_COUNT):
            arms: tuple[Arm, Arm]
            if (scenario_index + pair_index) % 2 == 0:
                arms = ("baseline", "treatment")
            else:
                arms = ("treatment", "baseline")
            for arm in arms:
                executions.append(
                    ExecutionSpec(
                        execution_id=_execution_id(scenario_id, pair_index, arm),
                        scenario_id=scenario_id,
                        pair_index=pair_index,
                        arm=arm,
                        order_index=order_index,
                    )
                )
                order_index += 1
    argv = _host_argv(host, codex_executable, model, reasoning_effort, path, interpreter)
    return ReleaseManifest(
        host=host,
        path=path,
        codex_executable=codex_executable,
        python_executable=interpreter,
        model=model,
        reasoning_effort=reasoning_effort,
        payload_lines=payload_lines,
        timeout_seconds=timeout_seconds,
        argv=argv,
        fixture_digests=fixture_digests,
        executions=executions,
    )


def run_release_benchmark(
    output_dir: Path,
    *,
    confirm_paid_runs: bool = False,
    process_runner: ProcessRunner | None = None,
    codex_executable: str = "codex",
    model: str = "gpt-5.6-sol",
    reasoning_effort: str = "low",
    payload_lines: int = DEFAULT_PAYLOAD_LINES,
    timeout_seconds: int = 900,
    host: Host = DEFAULT_HOST,
    path: BenchmarkPath | None = None,
    python_executable: str | None = None,
    max_executions: int | None = None,
) -> ReleaseManifest:
    """Create a manifest and optionally execute its 80 paid-call slots."""

    output_dir = Path(output_dir).resolve()
    manifest_path = output_dir / "manifest.json"
    if manifest_path.exists():
        raise FileExistsError(f"manifest already exists; resume it: {manifest_path}")
    resolved_path = path if path is not None else DEFAULT_PATH_FOR_HOST[host]
    if process_runner is None and confirm_paid_runs:
        _require_paid_contract(host, codex_executable, resolved_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = build_release_manifest(
        codex_executable=codex_executable,
        model=model,
        reasoning_effort=reasoning_effort,
        payload_lines=payload_lines,
        timeout_seconds=timeout_seconds,
        host=host,
        path=resolved_path,
        python_executable=python_executable,
    )
    _write_manifest(manifest_path, manifest)
    if not confirm_paid_runs:
        return manifest
    _run_pending(manifest_path, manifest, process_runner or _spawn_codex, max_executions)
    return manifest


def resume_release_benchmark(
    manifest_path: Path,
    *,
    confirm_paid_runs: bool = False,
    process_runner: ProcessRunner | None = None,
    max_executions: int | None = None,
) -> ReleaseManifest:
    """Retry each incomplete execution once, preserving every prior attempt."""

    manifest_path = Path(manifest_path).resolve()
    manifest = load_release_manifest(manifest_path)
    if not confirm_paid_runs:
        return manifest
    if process_runner is None:
        _require_paid_contract(manifest.host, manifest.codex_executable, manifest.path)
    _run_pending(manifest_path, manifest, process_runner or _spawn_codex, max_executions)
    return manifest


def _require_paid_contract(host: Host, executable: str, path: BenchmarkPath) -> None:
    """Refuse paid runs whose measured delivery path the host cannot serve."""

    if host == "agy":
        if path != "mcp":
            raise CompatibilityError("agy paid benchmark requires the explicit MCP path")
        if shutil.which(executable) is None and not Path(executable).is_file():
            raise CompatibilityError(f"agy executable was not found: {executable}")
        return
    if host == "claude":
        claude = probe_claude_compatibility(executable=executable)
        if not claude.supported:
            raise CompatibilityError(
                f"paid benchmark requires a supported Claude Code build: {claude.reason}"
            )
        if not claude.posttool_result_replacement:
            raise CompatibilityError(
                "paid benchmark requires transparent PostToolUse result replacement; "
                "this Claude Code build does not provide it"
            )
        return
    compatibility = probe_codex_compatibility(executable=executable)
    if not compatibility.supported:
        raise CompatibilityError(
            f"paid benchmark requires a supported Codex hook contract: {compatibility.reason}"
        )
    if path == "mcp":
        if not compatibility.mcp_tool_injection:
            raise CompatibilityError(
                "paid benchmark requires PreToolUse updatedInput injection for the "
                "htsave MCP tools; this Codex version does not provide it"
            )
        return
    if not compatibility.posttool_result_replacement:
        raise CompatibilityError(
            "paid benchmark requires transparent PostToolUse result replacement; "
            f"Codex {compatibility.detected_version or 'unknown'} is observer-only"
        )


def load_release_manifest(path: Path) -> ReleaseManifest:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("manifest must be a JSON object")
    return ReleaseManifest.from_dict(value)


def generate_scenario_fixture(
    destination: Path,
    scenario_id: str,
    *,
    payload_lines: int = DEFAULT_PAYLOAD_LINES,
    path: BenchmarkPath = DEFAULT_BENCHMARK_PATH,
) -> None:
    """Generate one deterministic fixture tree in an existing empty directory."""

    if scenario_id not in REQUIRED_SCENARIOS:
        raise ValueError(f"unknown benchmark scenario: {scenario_id}")
    if path not in BENCHMARK_PATHS:
        raise ValueError(f"unknown benchmark path: {path!r}")
    if payload_lines <= 0:
        raise ValueError("payload_lines must be positive")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("fixture destination must be empty")
    template_root = _template_root()
    shutil.copy2(template_root / "benchmark_driver.py", destination / "benchmark_driver.py")

    if scenario_id == "large_readme_exact":
        scenario, facts = _generate_large_readme(destination, payload_lines)
    elif scenario_id == "source_three_line_delta":
        scenario, facts = _generate_source_delta(destination, payload_lines)
    elif scenario_id == "repeated_test_output":
        scenario, facts = _generate_test_output(destination, payload_lines)
    else:
        scenario, facts = _generate_multi_context(destination, payload_lines)
    steps = _scenario_steps(scenario_id, scenario, path)
    expected_reads = sum("htsave_read" in step for step in steps)
    if path == "shell":
        expected_reads = sum("benchmark_driver.py emit" in step for step in steps)

    scenario = {**scenario, "expected_answers": facts}
    _write_json(destination / "scenario.json", scenario)
    answer_properties = {
        key: _schema_for_value(value) for key, value in facts.items()
    }
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": {
            "scenario_id": {"type": "string", "const": scenario_id},
            "answers": {
                "type": "object",
                "properties": answer_properties,
                "required": sorted(answer_properties),
                "additionalProperties": False,
            },
            "assumptions": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 0,
            },
        },
        "required": ["scenario_id", "answers", "assumptions"],
        "additionalProperties": False,
    }
    _write_json(destination / "answer.schema.json", schema)
    prompt_template = "prompt.txt" if path == "shell" else "prompt_mcp.txt"
    prompt = (
        (template_root / prompt_template)
        .read_text(encoding="utf-8")
        .format(
            scenario_id=scenario_id,
            steps="\n".join(f"{index}. {command}" for index, command in enumerate(steps, 1)),
            expected_reads=expected_reads,
            fact_keys=json.dumps(sorted(facts), sort_keys=True),
        )
    )
    (destination / "prompt.txt").write_text(prompt, encoding="utf-8", newline="\n")


_MUTATE_STEP = "Run this shell command: python benchmark_driver.py mutate"
_EMIT_STEP = "python benchmark_driver.py emit"
_ROUNDS = 4


def _scenario_steps(
    scenario_id: str, scenario: Mapping[str, object], path: BenchmarkPath
) -> list[str]:
    """Render the ordered, path-specific instruction list for one scenario."""

    mutating = scenario_id == "source_three_line_delta"
    if path == "shell":
        round_steps = [_EMIT_STEP]
        mutate_step = "python benchmark_driver.py mutate"
    else:
        emit_paths = scenario["emit_paths"]
        if not isinstance(emit_paths, list) or not all(
            isinstance(item, str) for item in emit_paths
        ):
            raise ValueError("scenario emit_paths must be a list of strings")
        round_steps = [f'Call the htsave_read tool with path "{item}".' for item in emit_paths]
        mutate_step = _MUTATE_STEP
    steps = [f"Round 1: {step}" for step in round_steps]
    if mutating:
        steps.append(f"Between round 1 and round 2: {mutate_step}")
    for round_index in range(1, _ROUNDS):
        steps.extend(f"Round {round_index + 1}: {step}" for step in round_steps)
    return steps


def tree_digest(root: Path) -> str:
    """Hash relative paths and exact file bytes, excluding Git internals."""

    root = Path(root)
    digest = hashlib.sha256()
    paths = sorted(
        path
        for path in root.rglob("*")
        if path.is_file() and ".git" not in path.relative_to(root).parts
    )
    for path in paths:
        relative = path.relative_to(root).as_posix().encode()
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()


def _generate_large_readme(root: Path, lines: int) -> tuple[dict[str, object], dict[str, object]]:
    facts = {
        "codename": "saffron-otter-731",
        "retention_days": 37,
        "failover_region": "ap-southeast-2",
    }
    content = [
        "# Deterministic Operations Manual\n",
        f"Codename: {facts['codename']}\n",
        f"Retention days: {facts['retention_days']}\n",
        f"Failover region: {facts['failover_region']}\n",
        *_filler("readme", lines),
        *_filler("readme-tail", lines),
    ]
    (root / "README.md").write_text("".join(content), encoding="utf-8", newline="\n")
    return {"emit_paths": ["README.md"], "mutations": []}, facts


def _generate_source_delta(root: Path, lines: int) -> tuple[dict[str, object], dict[str, object]]:
    source_dir = root / "src"
    source_dir.mkdir()
    before_after = (
        ("RATE_LIMIT = 320\n", "RATE_LIMIT = 640\n"),
        ('REGION = "tpe-1"\n', 'REGION = "tpe-2"\n'),
        ("FEATURE_EPOCH = 1024\n", "FEATURE_EPOCH = 2048\n"),
    )
    content = [before for before, _ in before_after]
    content.append("SHARD_COUNT = 23\n")
    content.extend(f"# {line}" for line in _filler("source", lines))
    (source_dir / "large_module.py").write_text("".join(content), encoding="utf-8", newline="\n")
    mutations = [
        {"path": "src/large_module.py", "before": before, "after": after}
        for before, after in before_after
    ]
    facts = {
        "rate_limit": 640,
        "region": "tpe-2",
        "feature_epoch": 2048,
        "shard_count": 23,
    }
    return {"emit_paths": ["src/large_module.py"], "mutations": mutations}, facts


def _generate_test_output(root: Path, lines: int) -> tuple[dict[str, object], dict[str, object]]:
    output = [
        "FAILED test_atomicity - HTS-E410\n",
        "FAILED test_restore - HTS-E410\n",
        "FAILED test_generation - HTS-E410\n",
        f"{lines + 3} tests collected, 3 failed\n",
        *(
            f"PASSED tests/test_unit_{index:05d}.py::test_{_row_hash('tests', index)[:12]}\n"
            for index in range(lines)
        ),
    ]
    (root / "test-output.txt").write_text("".join(output), encoding="utf-8", newline="\n")
    facts = {
        "failed": 3,
        "error_code": "HTS-E410",
        "failed_tests": ["test_atomicity", "test_restore", "test_generation"],
    }
    return {"emit_paths": ["test-output.txt"], "mutations": []}, facts


def _generate_multi_context(root: Path, lines: int) -> tuple[dict[str, object], dict[str, object]]:
    context = root / "context"
    source = context / "src"
    source.mkdir(parents=True)
    # Four rounds of the three-file fixture must stay below the host's context
    # boundary in the raw baseline arm. The benchmark counts only paired
    # measurements whose two arms saw the same conversation; a compacted raw
    # arm would no longer be comparable to treatment.
    context_lines = max(3, lines // 6)
    per_file = max(1, context_lines // 3)
    (context / "AGENTS.md").write_text(
        "Release command: python -m acme.release --locked\n" + "".join(_filler("agents", per_file)),
        encoding="utf-8",
        newline="\n",
    )
    (context / "config.toml").write_text(
        "queue_limit = 73\n" + "".join(_filler("config", per_file)),
        encoding="utf-8",
        newline="\n",
    )
    (source / "app.py").write_text(
        'FALLBACK_STATE = "circuit-open"\n'
        + "".join(f"# {line}" for line in _filler("app", context_lines - 2 * per_file)),
        encoding="utf-8",
        newline="\n",
    )
    paths = ["context/AGENTS.md", "context/config.toml", "context/src/app.py"]
    facts = {
        "release_command": "python -m acme.release --locked",
        "queue_limit": 73,
        "fallback_state": "circuit-open",
    }
    return {"emit_paths": paths, "include_headers": True, "mutations": []}, facts


def _filler(seed: str, lines: int) -> list[str]:
    return [
        f"record {index:05d} seed={seed} digest={_row_hash(seed, index)} invariant=stable\n"
        for index in range(lines)
    ]


def _row_hash(seed: str, index: int) -> str:
    return hashlib.sha256(f"{seed}:{index}".encode()).hexdigest()


def _generated_fixture_digest(scenario_id: str, payload_lines: int, path: BenchmarkPath) -> str:
    with tempfile.TemporaryDirectory(prefix="htsave-benchmark-preflight-") as temporary:
        root = Path(temporary) / "workspace"
        generate_scenario_fixture(root, scenario_id, payload_lines=payload_lines, path=path)
        return tree_digest(root)


def _host_argv(
    host: Host,
    executable: str,
    model: str,
    reasoning_effort: str,
    path: BenchmarkPath,
    python_executable: str,
) -> tuple[str, ...]:
    if host not in BENCHMARK_HOSTS:
        raise ValueError(f"unknown benchmark host: {host!r}")
    if host == "agy":
        return _agy_argv(executable, model, reasoning_effort, path)
    if host == "claude":
        return _claude_argv(executable, model, path)
    return _codex_argv(executable, model, reasoning_effort, path, python_executable)


def _agy_argv(
    executable: str, model: str, reasoning_effort: str, path: BenchmarkPath
) -> tuple[str, ...]:
    if path != "mcp":
        raise ValueError("the agy benchmark measures its explicit MCP path")
    # --effort is a Gemini-specific flag; Claude and other vendors reject it.
    effort_args: tuple[str, ...] = (
        ("--effort", reasoning_effort) if model.startswith("gemini") else ()
    )
    return (
        executable,
        "--output-format",
        "stream-json",
        "--model",
        model,
        *effort_args,
        "--json-schema",
        "answer.schema.json",
        "--dangerously-skip-permissions",
        "--add-dir",
        "../state",
        "--add-dir",
        "../artifacts",
    )


def _claude_argv(executable: str, model: str, path: BenchmarkPath) -> tuple[str, ...]:
    """Build one constant argv; the arm lives in the isolated config directory.

    ``--strict-mcp-config`` keeps the operator's MCP servers out, and the
    isolated ``CLAUDE_CONFIG_DIR`` keeps their hooks and memory files out, so
    the only difference between arms is whether htsave's hooks are installed.
    """

    if path != "shell":
        raise ValueError(
            "the Claude Code benchmark measures the transparent shell path; "
            "its explicit MCP path is redundant there"
        )
    return (
        executable,
        "-p",
        "--output-format",
        "json",
        "--model",
        model,
        "--allowedTools",
        "Bash",
        "--disallowedTools",
        "Read",
        "--permission-mode",
        "bypassPermissions",
        "--strict-mcp-config",
    )


def _codex_argv(
    executable: str,
    model: str,
    reasoning_effort: str,
    path: BenchmarkPath,
    python_executable: str,
) -> tuple[str, ...]:
    if path not in BENCHMARK_PATHS:
        raise ValueError(f"unknown benchmark path: {path!r}")
    mcp_config: tuple[str, ...] = ()
    if path == "mcp":
        # ``--ignore-user-config`` drops config.toml, so the htsave MCP server is
        # declared inline.  The matching PreToolUse hook comes from the isolated
        # CODEX_HOME provisioned per attempt.
        mcp_config = (
            "--config",
            f"mcp_servers.htsave.command={json.dumps(python_executable)}",
            "--config",
            f'mcp_servers.htsave.args=["-m", "{MCP_MODULE}"]',
            "--config",
            "mcp_servers.htsave.startup_timeout_sec=10",
            "--config",
            "mcp_servers.htsave.tool_timeout_sec=60",
        )
    return (
        executable,
        "exec",
        "--json",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--dangerously-bypass-hook-trust",
        "--strict-config",
        "--color",
        "never",
        "--cd",
        ".",
        "--add-dir",
        "../state",
        "--add-dir",
        "../artifacts",
        "--model",
        model,
        "--approve-for-me",
        "never",
        "--config",
        f'model_reasoning_effort="{reasoning_effort}"',
        "--config",
        "sandbox_workspace_write.network_access=false",
        "--config",
        "analytics.enabled=false",
        "--config",
        "features.apps=false",
        "--config",
        "features.multi_agent=false",
        "--config",
        'web_search="disabled"',
        *mcp_config,
        "--output-schema",
        "answer.schema.json",
        "--output-last-message",
        "../artifacts/answer.json",
    )


def _run_pending(
    manifest_path: Path,
    manifest: ReleaseManifest,
    process_runner: ProcessRunner,
    max_executions: int | None = None,
) -> None:
    """Execute incomplete slots, optionally stopping after ``max_executions``.

    A bounded first run makes the wiring provable for the price of one pair
    instead of forty; ``--resume`` then finishes the rest with no new concept.
    """

    if max_executions is not None and max_executions <= 0:
        raise ValueError("max_executions must be positive")
    output_dir = manifest_path.parent
    executed = 0
    for execution in manifest.executions:
        if execution.completed_attempt is not None:
            continue
        if max_executions is not None and executed >= max_executions:
            return
        attempt = _run_attempt(output_dir, manifest, execution, process_runner)
        execution.attempts.append(attempt)
        executed += 1
        _write_manifest(manifest_path, manifest)


def _run_attempt(
    output_dir: Path,
    manifest: ReleaseManifest,
    execution: ExecutionSpec,
    process_runner: ProcessRunner,
) -> AttemptRecord:
    attempt_number = _next_attempt_number(output_dir, execution)
    attempt_root = output_dir / "runs" / execution.execution_id / f"attempt-{attempt_number:03d}"
    attempt_root.mkdir(parents=True, exist_ok=False)
    artifacts = attempt_root / "artifacts"
    state = attempt_root / "state"
    artifacts.mkdir()
    state.mkdir()
    workspace = Path(tempfile.mkdtemp(prefix="workspace-", dir=attempt_root))
    isolated_home: Path | None = None
    initial_digest: str | None = None
    returncode: int | None = None
    try:
        generate_scenario_fixture(
            workspace,
            execution.scenario_id,
            payload_lines=manifest.payload_lines,
            path=manifest.path,
        )
        _initialize_git_repository(workspace)
        initial_digest = tree_digest(workspace)
        expected = manifest.fixture_digests[execution.scenario_id]
        if initial_digest != expected:
            raise RuntimeError(
                f"fixture digest mismatch: expected {expected}, got {initial_digest}"
            )
        shutil.copy2(workspace / "prompt.txt", artifacts / "prompt.txt")
        shutil.copy2(workspace / "answer.schema.json", artifacts / "answer.schema.json")
        shutil.copy2(workspace / "scenario.json", artifacts / "scenario.json")
        prompt = (workspace / "prompt.txt").read_text(encoding="utf-8")

        env = dict(os.environ)
        env.update(
            {
                "HTSAVE_STATE_DIR": str(state.resolve()),
                "HTSAVE_BENCH_ARTIFACTS": str(artifacts.resolve()),
                "HTSAVE_BENCH_ARM": execution.arm,
                "HTSAVE_BENCH_WORKSPACE": str(workspace.resolve()),
            }
        )
        env.pop("CODEX_HOME", None)
        env.pop("CLAUDE_CONFIG_DIR", None)
        if manifest.host == "claude":
            isolated_home = _provision_claude_config(
                attempt_root,
                arm=execution.arm,
                python_executable=manifest.python_executable,
            )
            env["CLAUDE_CONFIG_DIR"] = str(isolated_home)
        elif manifest.host == "agy":
            isolated_home = _provision_agy_home(
                attempt_root,
                arm=execution.arm,
                python_executable=manifest.python_executable,
                state=state,
                workspace=workspace,
            )
            env["HOME"] = str(isolated_home)
        elif manifest.path == "mcp":
            isolated_home = _provision_codex_home(
                attempt_root, python_executable=manifest.python_executable
            )
            env["CODEX_HOME"] = str(isolated_home)
        request = ProcessRequest(
            execution_id=execution.execution_id,
            scenario_id=execution.scenario_id,
            pair_index=execution.pair_index,
            arm=execution.arm,
            attempt_number=attempt_number,
            argv=_attempt_argv(manifest, state=state, arm=execution.arm, prompt=prompt),
            cwd=workspace,
            env=env,
            stdin="" if manifest.host == "agy" else prompt,
            timeout_seconds=manifest.timeout_seconds,
            isolated_home=isolated_home,
        )
        output = process_runner(request)
        returncode = output.returncode
        (artifacts / "events.jsonl").write_text(output.stdout, encoding="utf-8", newline="\n")
        (artifacts / "stderr.log").write_text(output.stderr, encoding="utf-8", newline="\n")
        if output.returncode != 0:
            raise RuntimeError(f"Codex exited with status {output.returncode}")
        if manifest.host == "claude":
            protocol = parse_claude_exec_json(output.stdout)
            _write_claude_answer(output.stdout, artifacts / "answer.json")
        elif manifest.host == "agy":
            protocol = parse_agy_exec_json(output.stdout)
            _write_agy_answer(output.stdout, artifacts / "answer.json")
        else:
            protocol = parse_codex_exec_jsonl(output.stdout)
        if not protocol.succeeded or protocol.usage is None:
            detail = "; ".join(protocol.errors) or protocol.terminal
            raise RuntimeError(f"{manifest.host} did not complete successfully: {detail}")
        scenario = json.loads((workspace / "scenario.json").read_text(encoding="utf-8"))
        expected_answers = scenario.get("expected_answers") if isinstance(scenario, dict) else None
        if not isinstance(expected_answers, dict):
            raise RuntimeError("fixture is missing its deterministic answer oracle")
        if manifest.host == "codex" and manifest.path == "mcp":
            _validate_mcp_read_trace(output.stdout, scenario)
        elif manifest.host == "agy" and manifest.path == "mcp":
            _validate_agy_read_trace(output.stdout, scenario)
        _validate_answer(artifacts / "answer.json", execution.scenario_id, expected_answers)
        return AttemptRecord(
            attempt_number=attempt_number,
            status="completed",
            initial_tree_digest=initial_digest,
            returncode=returncode,
            thread_id=protocol.thread_id,
            usage=protocol.usage,
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        (artifacts / "runner-error.txt").write_text(error + "\n", encoding="utf-8", newline="\n")
        return AttemptRecord(
            attempt_number=attempt_number,
            status="failed",
            initial_tree_digest=initial_digest,
            returncode=returncode,
            error=error,
        )
    finally:
        shutil.rmtree(workspace)
        if isolated_home is not None:
            # The isolated home holds a copy of the operator's Codex credentials.
            # Retained artifacts must never include it.
            shutil.rmtree(isolated_home, ignore_errors=True)


def _attempt_argv(
    manifest: ReleaseManifest, *, state: Path, arm: Arm, prompt: str
) -> tuple[str, ...]:
    """Add per-attempt host settings that cannot live in the manifest argv."""

    if manifest.host == "agy":
        # agy does not consume a plain-text stdin prompt in print mode; its
        # public CLI requires the prompt as the --prompt option.
        return (*manifest.argv, "--prompt", prompt)

    if manifest.host != "codex" or manifest.path != "mcp":
        return manifest.argv
    insertion = manifest.argv.index("--output-schema")
    server_env = (
        "--config",
        f"mcp_servers.htsave.env.HTSAVE_STATE_DIR={json.dumps(str(state.resolve()))}",
        "--config",
        f"mcp_servers.htsave.env.HTSAVE_BENCH_ARM={json.dumps(arm)}",
    )
    return manifest.argv[:insertion] + server_env + manifest.argv[insertion:]


def _extract_json_object(text: str) -> str:
    """Recover one JSON object from a final message that may carry prose.

    Codex can force the shape with ``--output-schema``; Claude Code has no
    equivalent flag, so the model may wrap the answer in explanation or a code
    fence.  Extraction is deliberately permissive about the wrapper and changes
    nothing about correctness: the recovered object is still compared exactly
    against the fixture's oracle.
    """

    stripped = text.strip()
    candidates = [stripped]
    fence = re.findall(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    candidates.extend(reversed(fence))
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        candidates.append(stripped[start : end + 1])
    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return json.dumps(parsed, sort_keys=True)
    raise RuntimeError("the final message did not contain a JSON object")


def _write_claude_answer(stdout: str, destination: Path) -> None:
    """Claude Code has no --output-last-message; its final text is ``result``."""

    payload = json.loads(stdout)
    answer = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(answer, str):
        raise RuntimeError("claude result did not contain a final message")
    destination.write_text(_extract_json_object(answer), encoding="utf-8", newline="\n")


def _write_agy_answer(stdout: str, destination: Path) -> None:
    """Extract agy's final response from its stream-json result event."""

    result: Mapping[str, object] | None = None
    for line in stdout.splitlines():
        if not line.strip():
            continue
        event = json.loads(line)
        if isinstance(event, dict) and event.get("event") == "result":
            value = event.get("result")
            if isinstance(value, dict):
                result = value
    if result is None:
        raise RuntimeError("agy stream did not contain a result event")
    structured_output = result.get("structured_output")
    if isinstance(structured_output, dict):
        destination.write_text(
            json.dumps(structured_output, sort_keys=True), encoding="utf-8", newline="\n"
        )
        return
    answer = result.get("response")
    if not isinstance(answer, str):
        raise RuntimeError("agy result did not contain a final response")
    destination.write_text(_extract_json_object(answer), encoding="utf-8", newline="\n")


def _provision_claude_config(attempt_root: Path, *, arm: Arm, python_executable: str) -> Path:
    """Create the isolated CLAUDE_CONFIG_DIR that carries this attempt's arm.

    The arm is the whole experiment: the treatment config has htsave's hooks
    installed and the baseline config has none.  Isolating the directory also
    keeps the operator's own hooks, MCP servers, and memory files out of both
    arms, so the measured difference is htsave alone.
    """

    home = attempt_root / "claude-config"
    home.mkdir()
    if os.name != "nt":
        home.chmod(0o700)
    settings = home / "settings.json"
    if arm == "treatment":
        install_claude_integration(
            settings_path=settings,
            python_executable=Path(python_executable),
        )
    else:
        _write_json(settings, {})
    return home


def _provision_codex_home(attempt_root: Path, *, python_executable: str) -> Path:
    """Create a throwaway CODEX_HOME carrying only the htsave lifecycle hooks.

    ``--ignore-user-config`` drops config.toml, but hooks and auth still resolve
    through CODEX_HOME.  An isolated home is therefore the only way to give both
    arms one identical hook set that excludes whatever the operator installed.
    Credentials are added later, by the real spawn alone.
    """

    home = attempt_root / "codex-home"
    home.mkdir()
    if os.name != "nt":
        home.chmod(0o700)
    _write_json(home / "hooks.json", load_rendered_hooks(Path(python_executable)))
    return home


def _provision_agy_home(
    attempt_root: Path,
    *,
    arm: Arm,
    python_executable: str,
    state: Path,
    workspace: Path,
) -> Path:
    """Create an isolated HOME carrying only the htsave agy integration."""

    home = attempt_root / "agy-home"
    home.mkdir()
    if os.name != "nt":
        home.chmod(0o700)
    install_agy_integration(home=home)
    settings_path = home / ".gemini" / "antigravity-cli" / "settings.json"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    _write_json(
        settings_path,
        {
            "trustedWorkspaces": [str(workspace.resolve())],
            "permissions": {"allow": ["mcp(htsave/*)", "command(*)"]},
        },
    )
    config_path = home / ".gemini" / "config" / "mcp_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    server = config["mcpServers"][PLUGIN_NAME]
    server["env"] = {
        "HTSAVE_STATE_DIR": str(state.resolve()),
        "HTSAVE_BENCH_ARM": arm,
    }
    _write_json(config_path, config)
    return home


_CREDENTIAL_FILES: dict[str, tuple[str, str]] = {
    # isolated-home marker -> (source directory env var / default, file name)
    "codex-home": ("CODEX_HOME", "auth.json"),
    "claude-config": ("CLAUDE_CONFIG_DIR", ".credentials.json"),
    "agy-home": ("HOME", ".gemini/antigravity-cli/antigravity-oauth-token"),
}


def _install_host_credentials(home: Path) -> None:
    """Copy the operator's credentials into an isolated home for one real run."""

    entry = _CREDENTIAL_FILES.get(home.name)
    if entry is None:
        raise RuntimeError(f"unknown isolated home layout: {home.name}")
    variable, file_name = entry
    if variable == "CODEX_HOME":
        default = Path.home() / ".codex"
    elif variable == "CLAUDE_CONFIG_DIR":
        default = Path.home() / ".claude"
    else:
        default = Path.home()
    source = Path(os.environ.get(variable) or default)
    credentials = source / file_name
    if not credentials.is_file():
        raise RuntimeError(f"credentials were not found at {credentials}")
    target = home / file_name
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(credentials, target)
    if os.name != "nt":
        target.chmod(0o600)


def _spawn_codex(request: ProcessRequest) -> ProcessOutput:
    if request.isolated_home is not None:
        _install_host_credentials(request.isolated_home)
    completed = subprocess.run(
        request.argv,
        cwd=request.cwd,
        env=dict(request.env),
        input=request.stdin,
        text=True,
        capture_output=True,
        timeout=request.timeout_seconds,
        check=False,
    )
    return ProcessOutput(completed.returncode, completed.stdout, completed.stderr)


def _initialize_git_repository(workspace: Path) -> None:
    if shutil.which("git") is None:
        raise RuntimeError("git is required for benchmark workspace isolation")
    _run_git(workspace, "init", "-q")
    _run_git(workspace, "add", "--all")
    commit_env = dict(os.environ)
    commit_env.update(
        {
            "GIT_AUTHOR_DATE": "2000-01-01T00:00:00+00:00",
            "GIT_COMMITTER_DATE": "2000-01-01T00:00:00+00:00",
        }
    )
    _run_git(
        workspace,
        "-c",
        "user.name=htsave benchmark",
        "-c",
        "user.email=benchmark@invalid.example",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "-q",
        "-m",
        "deterministic benchmark fixture",
        env=commit_env,
    )
    status = _run_git(workspace, "status", "--porcelain")
    if status.stdout:
        raise RuntimeError("benchmark workspace is dirty before execution")


def _run_git(
    workspace: Path,
    *arguments: str,
    env: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    completed = subprocess.run(
        ("git", *arguments),
        cwd=workspace,
        env=dict(env) if env is not None else None,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(f"git {' '.join(arguments)} failed: {message}")
    return completed


def _validate_answer(path: Path, scenario_id: str, expected_answers: Mapping[str, object]) -> None:
    if not path.is_file():
        raise RuntimeError("the host did not produce a final answer artifact")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("answer must be a JSON object")
    if set(value) != {"scenario_id", "answers", "assumptions"}:
        raise RuntimeError("answer contains missing or additional fields")
    if value["scenario_id"] != scenario_id:
        raise RuntimeError("answer scenario_id does not match execution")
    if not isinstance(value["answers"], dict):
        raise RuntimeError("answer answers field must be an object")
    if value["answers"] != dict(expected_answers):
        raise RuntimeError("answer does not match the deterministic fixture oracle")
    if value["assumptions"] != []:
        raise RuntimeError("answer assumptions must be empty")


def _validate_mcp_read_trace(payload: str, scenario: Mapping[str, object]) -> None:
    """Require the MCP arm to execute every prescribed read in order.

    The prompt contains the answer oracle so a host can otherwise produce a
    valid answer while skipping reads. That would make baseline and treatment
    measure different conversations, so such an attempt is not a valid pair.
    """

    emit_paths = scenario.get("emit_paths")
    if not isinstance(emit_paths, list) or not all(
        isinstance(path, str) for path in emit_paths
    ):
        raise RuntimeError("fixture is missing valid MCP emit paths")
    expected = tuple(path for _ in range(_ROUNDS) for path in emit_paths)
    observed: list[str] = []
    for line in payload.splitlines():
        event = json.loads(line)
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict) or item.get("type") != "mcp_tool_call":
            continue
        if item.get("tool") != "htsave_read":
            continue
        arguments = item.get("arguments")
        if not isinstance(arguments, dict) or not isinstance(arguments.get("path"), str):
            raise RuntimeError("htsave_read trace contains an invalid path")
        observed.append(arguments["path"])
    if tuple(observed) != expected:
        raise RuntimeError(
            "htsave_read trace does not match the prescribed read order: "
            f"expected {list(expected)}, observed {observed}"
        )


def _validate_agy_read_trace(payload: str, scenario: Mapping[str, object]) -> None:
    """Require agy's stream-json MCP calls to match the prescribed read order."""

    emit_paths = scenario.get("emit_paths")
    if not isinstance(emit_paths, list) or not all(
        isinstance(path, str) for path in emit_paths
    ):
        raise RuntimeError("fixture is missing valid agy emit paths")
    expected = tuple(path for _ in range(_ROUNDS) for path in emit_paths)
    observed: list[str] = []
    for line in payload.splitlines():
        event = json.loads(line)
        if not isinstance(event, dict) or event.get("event") != "step_update":
            continue
        update = event.get("step_update")
        if not isinstance(update, dict) or update.get("step_type") != "tool":
            continue
        if update.get("state") != "DONE":
            continue
        info = update.get("tool_info")
        if not isinstance(info, dict):
            continue
        parameters = info.get("parameters")
        if not isinstance(parameters, dict):
            continue
        if parameters.get("ServerName") != "htsave" or parameters.get("ToolName") != "htsave_read":
            continue
        arguments = parameters.get("Arguments")
        if not isinstance(arguments, dict) or not isinstance(arguments.get("path"), str):
            raise RuntimeError("agy htsave_read trace contains an invalid path")
        observed.append(arguments["path"])
    if tuple(observed) != expected:
        raise RuntimeError(
            "agy htsave_read trace does not match the prescribed read order: "
            f"expected {list(expected)}, observed {observed}"
        )


def _next_attempt_number(output_dir: Path, execution: ExecutionSpec) -> int:
    execution_root = output_dir / "runs" / execution.execution_id
    existing = [
        int(path.name.removeprefix("attempt-"))
        for path in execution_root.glob("attempt-[0-9][0-9][0-9]")
        if path.is_dir()
    ]
    recorded = [attempt.attempt_number for attempt in execution.attempts]
    return max([0, *existing, *recorded]) + 1


def _execution_id(scenario_id: str, pair_index: int, arm: Arm) -> str:
    if scenario_id not in REQUIRED_SCENARIOS:
        raise ValueError(f"unknown scenario id: {scenario_id}")
    if pair_index not in range(PAIR_COUNT):
        raise ValueError(f"pair index out of range: {pair_index}")
    return f"{scenario_id}-p{pair_index:02d}-{arm}"


def _write_manifest(path: Path, manifest: ReleaseManifest) -> None:
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(manifest.to_dict(), indent=2, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    if os.name != "nt":
        descriptor = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _schema_for_value(value: object) -> dict[str, object]:
    """Render a strict response-schema fragment from deterministic oracle data."""

    if isinstance(value, bool):
        return {"type": "boolean"}
    if isinstance(value, int):
        return {"type": "integer"}
    if isinstance(value, float):
        return {"type": "number"}
    if isinstance(value, str):
        return {"type": "string"}
    if isinstance(value, list):
        items = _schema_for_value(value[0]) if value else {}
        return {"type": "array", "items": items}
    if isinstance(value, dict):
        properties = {key: _schema_for_value(item) for key, item in value.items()}
        return {
            "type": "object",
            "properties": properties,
            "required": sorted(properties),
            "additionalProperties": False,
        }
    raise TypeError(f"unsupported oracle value type: {type(value).__name__}")


def _required_str(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _required_int(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"{key} must be an integer")
    return item


def _optional_str(value: object) -> str | None:
    if value is None or isinstance(value, str):
        return value
    raise ValueError("expected a string or null")


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("expected an integer or null")
    return value
