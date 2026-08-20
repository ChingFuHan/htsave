from __future__ import annotations

from dataclasses import dataclass

from htsave.compat import SUPPORTED_CODEX_VERSION, probe_codex_compatibility


@dataclass
class Result:
    returncode: int
    stdout: str | None


def test_exact_pinned_codex_version_is_supported() -> None:
    compatibility = probe_codex_compatibility(
        runner=lambda _: Result(0, f"codex-cli {SUPPORTED_CODEX_VERSION}\n")
    )

    assert compatibility.supported
    assert compatibility.detected_version == SUPPORTED_CODEX_VERSION
    assert not compatibility.posttool_result_replacement
    assert compatibility.mcp_tool_injection


def test_unknown_or_different_versions_fail_open() -> None:
    for result in (
        Result(0, "codex-cli 0.149.0\n"),
        Result(0, "unexpected\n"),
        Result(1, "codex-cli 0.148.0\n"),
    ):
        compatibility = probe_codex_compatibility(runner=lambda _, result=result: result)
        assert not compatibility.supported
        assert not compatibility.mcp_tool_injection
        assert not compatibility.posttool_result_replacement
