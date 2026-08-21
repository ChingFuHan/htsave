"""Tests for the agy hook-to-MCP capability boundary."""

from __future__ import annotations

from pathlib import Path

import htsave.agy_hooks as agy_hooks
from htsave.mcp_server import read_workspace_text


def test_pre_tool_use_injects_context_into_nested_mcp_arguments(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "state"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "README.md").write_text("codename: saffron\n", encoding="utf-8")
    monkeypatch.setenv("HTSAVE_STATE_DIR", str(state))
    monkeypatch.setenv("HTSAVE_BENCH_ARM", "treatment")

    response = agy_hooks._handle_pre_tool_use(
        {
            "conversationId": "conversation-1",
            "stepIdx": 3,
            "modelName": "gemini-3.7-flash-low",
            "workspacePaths": [str(workspace)],
            "toolCall": {
                "id": "tool-1",
                "name": "call_mcp_tool",
                "args": {
                    "ServerName": "htsave",
                    "ToolName": "htsave_read",
                    "Arguments": {"path": "README.md"},
                },
            },
        }
    )

    assert response["decision"] == "allow"
    overwrite = response["overwrite"]
    context = overwrite["Arguments"]["_htsave_context"]
    assert read_workspace_text(
        path="README.md",
        _htsave_context=context,
        state_root=state,
        fallback_workspace=workspace,
    ) == "codename: saffron\n"


def test_non_htsave_and_post_tool_calls_are_allowed_without_overwrite() -> None:
    assert agy_hooks._handle_pre_tool_use(
        {"toolCall": {"name": "run_command", "args": {"CommandLine": "true"}}}
    ) == {"decision": "allow"}
    assert agy_hooks._detect_event(
        {"stepIdx": 1, "toolCall": {}, "result": {"ok": True}}
    ) == "PostToolUse"
