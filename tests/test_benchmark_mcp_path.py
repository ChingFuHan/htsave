"""Free, local proof that the MCP benchmark path can actually reduce tokens.

The paid benchmark measures Codex-reported ``input_tokens``.  This module drives
the same fixtures and the same ordered read sequence through ``htsave_read``
directly, so the mechanism the paid run depends on is proven before any Codex
process is spawned.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from htsave.benchmark import REQUIRED_SCENARIOS
from htsave.benchmark_runner import generate_scenario_fixture
from htsave.capabilities import issue_session_capability
from htsave.mcp_server import HOOK_READ_TOOL, read_workspace_text
from htsave.paths import build_state_paths
from htsave.registry import Registry
from htsave.tokens import TokenEstimator

_ROUNDS = 4
_REQUIRED_REDUCTION = 0.30


def _read(
    *,
    state_root: Path,
    session_id: str,
    workspace: Path,
    relative: str,
    tool_use_id: str,
) -> str:
    """Reproduce one PreToolUse capability injection plus one htsave_read call."""

    arguments: dict[str, object] = {"path": relative}
    paths = build_state_paths(session_id, state_root)
    with Registry(paths.database, paths.session_key) as registry:
        if registry.active_generation() is None:
            registry.begin_generation("startup")
        # Codex confirms delivery of the previous result on the next tool call.
        registry.confirm_pending()
        token = issue_session_capability(
            registry,
            turn_id=f"turn-{tool_use_id}",
            tool_use_id=tool_use_id,
            tool_name=HOOK_READ_TOOL,
            arguments=arguments,
            model="gpt-5",
            cwd=str(workspace),
        )
    return read_workspace_text(
        path=relative,
        _htsave_context={"token": token},
        state_root=state_root,
        fallback_workspace=workspace,
    )


def _run_scenario(tmp_path: Path, scenario_id: str, *, treatment: bool) -> int:
    arm = "treatment" if treatment else "baseline"
    workspace = tmp_path / arm / "workspace"
    state_root = tmp_path / arm / "state"
    generate_scenario_fixture(workspace, scenario_id, payload_lines=512, path="mcp")
    scenario = json.loads((workspace / "scenario.json").read_text(encoding="utf-8"))
    estimator = TokenEstimator("gpt-5")

    delivered = 0
    call = 0
    for round_index in range(_ROUNDS):
        if round_index == 1 and scenario["mutations"]:
            subprocess.run(
                (sys.executable, "benchmark_driver.py", "mutate"),
                cwd=workspace,
                capture_output=True,
                check=True,
            )
        for relative in scenario["emit_paths"]:
            call += 1
            if treatment:
                text = _read(
                    state_root=state_root,
                    session_id=f"{scenario_id}-{arm}",
                    workspace=workspace,
                    relative=relative,
                    tool_use_id=f"read-{call:03d}",
                )
            else:
                # The baseline arm is the same call with raw passthrough.
                text = (workspace / relative).read_text(encoding="utf-8")
            delivered += estimator.estimate(text).count
    return delivered


@pytest.mark.parametrize("scenario_id", REQUIRED_SCENARIOS)
def test_mcp_path_delivers_at_least_30_percent_fewer_tokens(
    tmp_path: Path, scenario_id: str
) -> None:
    baseline = _run_scenario(tmp_path, scenario_id, treatment=False)
    treatment = _run_scenario(tmp_path, scenario_id, treatment=True)

    reduction = (baseline - treatment) / baseline

    assert baseline > 0
    assert reduction >= _REQUIRED_REDUCTION, (
        f"{scenario_id}: baseline={baseline} treatment={treatment} reduction={reduction:.3f}"
    )
