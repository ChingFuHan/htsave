"""Tests for the agy hook-to-MCP capability boundary."""

from __future__ import annotations

import io
import json
from pathlib import Path

import htsave.agy_hooks as agy_hooks
from htsave.hashing import sha256_id
from htsave.mcp_server import read_workspace_text
from htsave.models import ContentObject, DeliveryMode
from htsave.paths import build_state_paths
from htsave.registry import Registry


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


def test_pre_invocation_is_detected_and_injects_one_ephemeral_reminder() -> None:
    payload = {"invocationNum": 3, "initialNumSteps": 10}
    assert agy_hooks._detect_event(payload) == "PreInvocation"
    response = agy_hooks._handle_pre_invocation(payload)
    steps = response["injectSteps"]
    assert len(steps) == 1
    message = steps[0]["ephemeralMessage"]
    assert "REF" in message
    assert "htsave_hydrate" in message


def test_stop_confirms_pending_receipts(tmp_path: Path, monkeypatch) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("HTSAVE_STATE_DIR", str(state))
    session_id = "conversation-stop"
    paths = build_state_paths(session_id, state)
    content = b"payload\n"
    object_hash = sha256_id(content)
    with Registry(paths.database, paths.session_key) as registry:
        generation = registry.begin_generation("startup")
        registry.record_object(
            ContentObject(
                object_hash=object_hash,
                codec="identity-utf8",
                byte_size=len(content),
                estimated_tokens=3,
            )
        )
        registry.record_delivery(
            generation=generation,
            tool_use_id="tool-1",
            source_fingerprint="source-1",
            safe_label="safe",
            target_hash=object_hash,
            base_hash=None,
            mode=DeliveryMode.REF,
            original_tokens=10,
            emitted_tokens=5,
            latency_ms=1.0,
            delta_depth=0,
            cumulative_delta_tokens=0,
        )
        assert len(registry.pending_receipts()) == 1

    assert agy_hooks._handle_stop({"conversationId": session_id}) == {}

    with Registry(paths.database, paths.session_key) as registry:
        assert registry.pending_receipts() == ()


def test_stop_without_session_identity_is_a_noop() -> None:
    assert agy_hooks._handle_stop({}) == {}


def test_session_start_begins_and_rotates_generations(
    tmp_path: Path, monkeypatch
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("HTSAVE_STATE_DIR", str(state))
    session_id = "conversation-ss"
    paths = build_state_paths(session_id, state)

    assert agy_hooks._handle_session_start({"conversationId": session_id}) == {}
    with Registry(paths.database, paths.session_key) as registry:
        first = registry.active_generation()
        assert first is not None

    assert agy_hooks._handle_session_start({"conversationId": session_id}) == {}
    with Registry(paths.database, paths.session_key) as registry:
        second = registry.active_generation()
        assert second is not None and second > first


def test_session_start_without_identity_or_with_bad_state_fails_open(
    tmp_path: Path, monkeypatch
) -> None:
    assert agy_hooks._handle_session_start({}) == {}
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("HTSAVE_STATE_DIR", str(blocker))
    assert agy_hooks._handle_session_start({"conversationId": "conversation-y"}) == {}


def test_main_routes_explicit_session_start_argv(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    state = tmp_path / "state"
    monkeypatch.setenv("HTSAVE_STATE_DIR", str(state))
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"conversationId": "ss-1"})))

    agy_hooks.main([agy_hooks.SESSION_START_ARG])

    assert json.loads(capsys.readouterr().out) == {}
    paths = build_state_paths("ss-1", state)
    with Registry(paths.database, paths.session_key) as registry:
        assert registry.active_generation() is not None


def test_stop_fails_open_on_unusable_state_root(tmp_path: Path, monkeypatch) -> None:
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("HTSAVE_STATE_DIR", str(blocker))
    assert agy_hooks._handle_stop({"conversationId": "conversation-x"}) == {}


def test_workspace_path_prefers_payload_and_never_globs_state_siblings(
    tmp_path: Path, monkeypatch
) -> None:
    # The removed benchmark glob hack would have hijacked the workspace from a
    # sibling of HTSAVE_STATE_DIR; it must stay ignored.
    monkeypatch.setenv("HTSAVE_STATE_DIR", str(tmp_path / "state"))
    decoy = tmp_path / "workspace-0000"
    decoy.mkdir()
    real = tmp_path / "real-workspace"
    real.mkdir()
    assert agy_hooks._workspace_path({"workspacePaths": [str(real)]}) == str(real)
    monkeypatch.setenv("HTSAVE_BENCH_WORKSPACE", str(decoy))
    assert agy_hooks._workspace_path({}) == str(decoy)
