from __future__ import annotations

from dataclasses import dataclass

from htsave.compat import probe_codex_compatibility


@dataclass
class Result:
    returncode: int
    stdout: str | None


def test_parseable_codex_versions_are_capability_compatible() -> None:
    for version in ("0.148.0", "0.150.1", "0.151.0-alpha.1+build.2"):
        compatibility = probe_codex_compatibility(
            runner=lambda _, version=version: Result(0, f"codex-cli {version}\n")
        )

        assert compatibility.supported
        assert compatibility.detected_version == version
        assert compatibility.reason == "capability-compatible"
        assert not compatibility.posttool_result_replacement
        assert compatibility.mcp_tool_injection


def test_unparseable_or_failed_version_probe_fails_open() -> None:
    for result in (
        Result(0, "codex-cli not-a-semver\n"),
        Result(0, "unexpected\n"),
        Result(1, "codex-cli 0.148.0\n"),
    ):
        compatibility = probe_codex_compatibility(runner=lambda _, result=result: result)
        assert not compatibility.supported
        assert not compatibility.mcp_tool_injection
        assert not compatibility.posttool_result_replacement
