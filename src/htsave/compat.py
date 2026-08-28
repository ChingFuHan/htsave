"""Host contract detection for fail-open adapter gates.

Each supported host gets its own probe.  Codex CLI is treated like Caveman's
native adapter: a parseable version enables the documented event adapter, and
the event parser remains the final compatibility boundary.  Claude Code is
detected from the running binary's reported version and, unlike Codex, does
provide a successful PostToolUse result-replacement response.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

# Claude Code has carried ``updatedToolOutput`` on PostToolUse since 2.x; the
# floor is a minimum, not a pin, because the field is documented and stable.
MINIMUM_CLAUDE_VERSION = (2, 1, 0)
_CODEX_SEMVER = re.compile(
    r"^(?P<version>[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?)$"
)
_CODEX_VERSION_OUTPUT = re.compile(r"^codex-cli (?P<version>.+)$")
_CLAUDE_VERSION = re.compile(r"^([0-9]+)\.([0-9]+)\.([0-9]+)\b")


class VersionCommandResult(Protocol):
    returncode: int
    stdout: str | None


VersionRunner = Callable[[Sequence[str]], VersionCommandResult]


def _parse_codex_version(value: str) -> str | None:
    match = _CODEX_SEMVER.fullmatch(value.strip())
    return match.group("version") if match is not None else None


@dataclass(frozen=True, slots=True)
class CodexCompatibility:
    detected_version: str | None
    supported: bool
    reason: str
    # Codex's supported hook contract is observer-only.  Keep this separate
    # from ``supported`` so callers cannot mistake a safe fail-open adapter
    # version for a transparent benchmark-capable version.
    posttool_result_replacement: bool = False
    # PreToolUse ``updatedInput`` injection plus MCP tool dispatch is the
    # supported path on every version whose event contract can be attempted.
    mcp_tool_injection: bool = False


def codex_compatibility_for_version(version: str) -> CodexCompatibility:
    """Build the version-independent Codex capability result.

    A valid version only opts into trying the documented adapter.  Individual
    hook events still validate their required fields and fail open when Codex
    changes the event contract.
    """

    parsed = _parse_codex_version(version)
    if parsed is None:
        return CodexCompatibility(None, False, "unknown-codex-version-output")
    return CodexCompatibility(
        parsed,
        True,
        "capability-compatible",
        posttool_result_replacement=False,
        mcp_tool_injection=True,
    )


def _run_version(argv: Sequence[str]) -> VersionCommandResult:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=3)


def probe_codex_compatibility(
    *, executable: str = "codex", runner: VersionRunner | None = None
) -> CodexCompatibility:
    try:
        result = (runner or _run_version)((executable, "--version"))
    except (OSError, subprocess.SubprocessError):
        return CodexCompatibility(None, False, "codex-version-probe-failed")
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return CodexCompatibility(None, False, "codex-version-probe-failed")
    match = _CODEX_VERSION_OUTPUT.fullmatch(result.stdout.strip())
    if match is None:
        return CodexCompatibility(None, False, "unknown-codex-version-output")
    return codex_compatibility_for_version(match.group("version"))


@dataclass(frozen=True, slots=True)
class ClaudeCompatibility:
    """What the installed Claude Code build lets the htsave adapter do."""

    detected_version: str | None
    supported: bool
    reason: str
    # Claude Code honors ``hookSpecificOutput.updatedToolOutput`` for every tool,
    # which is exactly the transparent replacement Codex does not provide.
    posttool_result_replacement: bool = False
    # ``PreToolUse`` may rewrite tool arguments, which carries the htsave
    # capability token into the MCP tools.
    pretooluse_updated_input: bool = False


def _parse_claude_version(text: str) -> tuple[int, int, int] | None:
    match = _CLAUDE_VERSION.match(text.strip())
    if match is None:
        return None
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)))


def probe_claude_compatibility(
    *, executable: str = "claude", runner: VersionRunner | None = None
) -> ClaudeCompatibility:
    """Detect Claude Code and whether it can transparently replace tool output."""

    try:
        result = (runner or _run_version)((executable, "--version"))
    except (OSError, subprocess.SubprocessError):
        return ClaudeCompatibility(None, False, "claude-version-probe-failed")
    if result.returncode != 0 or not isinstance(result.stdout, str):
        return ClaudeCompatibility(None, False, "claude-version-probe-failed")
    version = _parse_claude_version(result.stdout)
    if version is None:
        return ClaudeCompatibility(None, False, "unknown-claude-version-output")
    rendered = ".".join(str(part) for part in version)
    if version < MINIMUM_CLAUDE_VERSION:
        return ClaudeCompatibility(rendered, False, "claude-version-below-minimum")
    return ClaudeCompatibility(
        rendered,
        True,
        "supported",
        posttool_result_replacement=True,
        pretooluse_updated_input=True,
    )
