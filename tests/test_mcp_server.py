from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from htsave.capabilities import issue_session_capability
from htsave.errors import CorruptObjectError, SecurityBoundaryError
from htsave.mcp_server import (
    HOOK_HYDRATE_TOOL,
    HOOK_READ_TOOL,
    _call_tool,
    hydrate_session_text,
    read_workspace_text,
    tool_definitions,
)
from htsave.models import DeliveryMode
from htsave.paths import build_state_paths
from htsave.registry import Registry
from htsave.transport import parse_transport


def _issue(
    *,
    state_root: Path,
    session_id: str,
    tool_name: str,
    tool_use_id: str,
    arguments: dict[str, object],
    cwd: Path,
) -> dict[str, str]:
    paths = build_state_paths(session_id, state_root)
    with Registry(paths.database, paths.session_key) as registry:
        if registry.active_generation() is None:
            registry.begin_generation("startup")
        token = issue_session_capability(
            registry,
            turn_id=f"turn-{tool_use_id}",
            tool_use_id=tool_use_id,
            tool_name=tool_name,
            arguments=arguments,
            model="gpt-5",
            cwd=str(cwd),
        )
    return {"token": token}


def _large_text() -> str:
    return "".join(
        f"line {index}: deterministic MCP context value {index}\n" for index in range(1200)
    )


def test_tool_schema_explicitly_allows_injected_context() -> None:
    definitions = {tool.name: tool for tool in tool_definitions()}

    assert "_htsave_context" in definitions["htsave_read"].inputSchema["properties"]
    assert "_htsave_context" in definitions["htsave_hydrate"].inputSchema["required"]
    assert definitions["htsave_read"].annotations.readOnlyHint is True


def test_read_without_metadata_is_byte_exact_full_only(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = "one\r\n雪\nlast"
    (workspace / "README.md").write_bytes(content.encode())

    result = read_workspace_text(path="README.md", fallback_workspace=workspace)

    assert result.encode() == content.encode()


def test_benchmark_baseline_arm_is_raw_even_with_hook_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = _large_text()
    (workspace / "README.md").write_text(content)
    context = _issue(
        state_root=state_root,
        session_id="session-one",
        tool_name=HOOK_READ_TOOL,
        tool_use_id="baseline-read",
        arguments={"path": "README.md"},
        cwd=workspace,
    )
    monkeypatch.setenv("HTSAVE_BENCH_ARM", "baseline")

    result = read_workspace_text(
        path="README.md",
        _htsave_context=context,
        state_root=state_root,
        fallback_workspace=workspace,
    )

    assert result == content
    paths = build_state_paths("session-one", state_root)
    with Registry(paths.database, paths.session_key) as registry:
        assert registry.stats().events == 0


def test_explicit_read_full_ref_and_hydrate_round_trip(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = _large_text()
    (workspace / "README.md").write_text(content)
    arguments = {"path": "README.md"}

    first_context = _issue(
        state_root=state_root,
        session_id="session-one",
        tool_name=HOOK_READ_TOOL,
        tool_use_id="read-1",
        arguments=arguments,
        cwd=workspace,
    )
    first = read_workspace_text(
        path="README.md",
        _htsave_context=first_context,
        state_root=state_root,
        fallback_workspace=workspace,
    )
    assert first == content

    paths = build_state_paths("session-one", state_root)
    with Registry(paths.database, paths.session_key) as registry:
        assert registry.stats().full == 1
        registry.confirm_pending()

    second_context = _issue(
        state_root=state_root,
        session_id="session-one",
        tool_name=HOOK_READ_TOOL,
        tool_use_id="read-2",
        arguments=arguments,
        cwd=workspace,
    )
    second = read_workspace_text(
        path="README.md",
        _htsave_context=second_context,
        state_root=state_root,
        fallback_workspace=workspace,
    )
    frame = parse_transport(second)
    assert frame.mode is DeliveryMode.REF

    hydrate_context = _issue(
        state_root=state_root,
        session_id="session-one",
        tool_name=HOOK_HYDRATE_TOOL,
        tool_use_id="hydrate-1",
        arguments={"ref": frame.target_hash},
        cwd=workspace,
    )
    hydrated = hydrate_session_text(
        ref=frame.target_hash,
        _htsave_context=hydrate_context,
        state_root=state_root,
    )
    assert hydrated.encode() == content.encode()


def test_invalid_read_capability_falls_back_to_full_not_cache(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = _large_text()
    (workspace / "README.md").write_text(content)

    result = read_workspace_text(
        path="README.md",
        _htsave_context={"token": "not-a-capability"},
        state_root=tmp_path / "state",
        fallback_workspace=workspace,
    )

    assert result == content


def test_valid_context_never_falls_back_across_workspace_boundary(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secret.txt"
    outside.write_text("secret")
    context = _issue(
        state_root=state_root,
        session_id="session-one",
        tool_name=HOOK_READ_TOOL,
        tool_use_id="read-1",
        arguments={"path": "../secret.txt"},
        cwd=workspace,
    )

    with pytest.raises(SecurityBoundaryError, match="escapes"):
        read_workspace_text(
            path="../secret.txt",
            _htsave_context=context,
            state_root=state_root,
            fallback_workspace=tmp_path,
        )


def test_hydrate_rejects_missing_or_cross_session_reference(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with pytest.raises(SecurityBoundaryError):
        hydrate_session_text(
            ref="sha256:" + "0" * 64,
            _htsave_context=None,
            state_root=state_root,
        )

    context = _issue(
        state_root=state_root,
        session_id="other-session",
        tool_name=HOOK_HYDRATE_TOOL,
        tool_use_id="hydrate-1",
        arguments={"ref": "sha256:" + "0" * 64},
        cwd=workspace,
    )
    with pytest.raises(CorruptObjectError, match="not registered"):
        hydrate_session_text(
            ref="sha256:" + "0" * 64,
            _htsave_context=context,
            state_root=state_root,
        )


def test_mcp_handler_marks_security_errors_as_tool_errors() -> None:
    result = asyncio.run(
        _call_tool(
            "htsave_hydrate",
            {"ref": "sha256:" + "0" * 64, "_htsave_context": None},
        )
    )

    assert result.isError is True
    assert result.content[0].type == "text"


def test_mcp_stdio_protocol_lists_and_calls_tools(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    content = "stdio exact\r\n雪\n"
    (workspace / "README.md").write_bytes(content.encode())
    environment = dict(os.environ)
    environment["HTSAVE_STATE_DIR"] = str(tmp_path / "state")
    process = subprocess.Popen(
        [sys.executable, "-m", "htsave.mcp_server"],
        cwd=workspace,
        env=environment,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    def request(request_id: int, method: str, params: dict[str, object]) -> dict[str, object]:
        assert process.stdin is not None
        assert process.stdout is not None
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
            + "\n"
        )
        process.stdin.flush()
        for line in process.stdout:
            response = json.loads(line)
            if response.get("id") == request_id:
                return response
        raise AssertionError("MCP server closed stdout before replying")

    try:
        initialized = request(
            1,
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "htsave-test", "version": "1"},
            },
        )
        assert initialized["result"]["serverInfo"]["name"] == "htsave"  # type: ignore[index]
        assert process.stdin is not None
        process.stdin.write(
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}})
            + "\n"
        )
        process.stdin.flush()
        listed = request(2, "tools/list", {})
        tool_names = {item["name"] for item in listed["result"]["tools"]}  # type: ignore[index]
        assert tool_names == {"htsave_read", "htsave_hydrate"}
        called = request(
            3, "tools/call", {"name": "htsave_read", "arguments": {"path": "README.md"}}
        )
        assert called["result"]["content"][0]["text"].encode() == content.encode()  # type: ignore[index]
    finally:
        if process.stdin is not None:
            process.stdin.close()
        process.wait(timeout=5)
        stderr = process.stderr.read() if process.stderr is not None else ""
        if process.stdout is not None:
            process.stdout.close()
        if process.stderr is not None:
            process.stderr.close()
        if process.returncode != 0:
            raise AssertionError(f"MCP server failed: {stderr}")
