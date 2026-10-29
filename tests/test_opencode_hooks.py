"""Trust-boundary tests; complete delivery sequences live in test_opencode_bridge."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

import htsave.opencode_hooks as hooks
from htsave.cas import ContentAddressedStore
from htsave.errors import CorruptObjectError
from htsave.mcp_server import hydrate_session_text, read_workspace_text
from htsave.paths import build_state_paths
from htsave.registry import Registry


def payload(tmp_path: Path, monkeypatch) -> dict:
    monkeypatch.setenv("HTSAVE_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.delenv("HTSAVE_BENCH_ARM", raising=False)
    value = {"sessionId": "session-one", "toolUseId": "one", "cwd": str(tmp_path),
             "tool": "htsave_htsave_read", "input": {"path": "file.txt"}}
    value.update(hooks._handle_session_start(value))
    return value


def test_real_opencode_tool_name_injects_valid_session_capability(tmp_path, monkeypatch):
    value = payload(tmp_path, monkeypatch)
    (tmp_path / "file.txt").write_bytes(b"exact text\r\n")
    response = hooks._handle_pre_tool_use(value)
    assert read_workspace_text(
        **response["input"], state_root=tmp_path / "state", fallback_workspace=tmp_path
    ) == "exact text\r\n"


@pytest.mark.parametrize("field", ["toolUseId", "sessionId", "generation"])
def test_missing_identity_never_issues_capability(tmp_path, monkeypatch, field):
    value = payload(tmp_path, monkeypatch)
    del value[field]
    assert hooks._handle_pre_tool_use(value) == {}


def test_stale_generation_and_invalid_receipts_fail_open(tmp_path, monkeypatch):
    value = payload(tmp_path, monkeypatch)
    value["receipts"] = [True]
    assert hooks._handle_pre_tool_use(value) == {}
    value["receipts"] = []
    hooks._handle_session_start(value)
    assert hooks._handle_pre_tool_use(value) == {}


def test_model_supplied_capability_is_replaced(tmp_path, monkeypatch):
    value = payload(tmp_path, monkeypatch)
    value["input"]["_htsave_context"] = {"token": "untrusted"}
    response = hooks._handle_pre_tool_use(value)
    assert response["input"]["_htsave_context"]["token"].startswith("v1.")


@pytest.mark.parametrize("result", [
    {"content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]},
    {"content": [{"type": "image"}], "output": "fallback must not win"},
    {"output": "text", "attachments": [{"mime": "image/png"}]},
    {"output": "text", "isError": True},
    {"output": "text", "metadata": {"truncated": True}},
    {"output": "text", "metadata": {"exit": 1}},
    {"output": "text", "metadata": {"exit": None}},
    {"output": "text", "structuredContent": {}},
])
def test_ambiguous_results_are_not_ingested(tmp_path, monkeypatch, result):
    value = payload(tmp_path, monkeypatch)
    value.update(tool="bash", status="completed", result=result)
    assert hooks._handle_post_tool_use(value) == {}
    paths = build_state_paths(value["sessionId"], tmp_path / "state")
    with Registry(paths.database, paths.session_key) as registry:
        assert registry.pending_receipts() == ()


def test_stop_does_not_confirm_and_selective_confirmation_is_generation_bound(
    tmp_path, monkeypatch,
):
    value = payload(tmp_path, monkeypatch)
    value.update(tool="bash", status="completed", result={"output": "text\n" * 300})
    first = hooks._handle_post_tool_use(value)
    value["toolUseId"] = "two"
    second = hooks._handle_post_tool_use(value)
    assert hooks._handle_stop(value) == {}
    paths = build_state_paths(value["sessionId"], tmp_path / "state")
    with Registry(paths.database, paths.session_key) as registry:
        assert len(registry.pending_receipts()) == 2
        assert registry.confirm_receipts(value["generation"], (first["receipt"],)) == 1
        assert [r.receipt_id for r in registry.pending_receipts()] == [second["receipt"]]
        registry.begin_generation("reset")
        assert registry.confirm_receipts(value["generation"], (second["receipt"],)) == 0
        with pytest.raises(ValueError):
            registry.confirm_receipts(True, ())


def test_hydration_keeps_session_ownership(tmp_path, monkeypatch):
    value = payload(tmp_path, monkeypatch)
    value.update(tool="bash", status="completed", result={"output": "text\n" * 300})
    hooks._handle_post_tool_use(value)
    paths = build_state_paths(value["sessionId"], tmp_path / "state")
    with Registry(paths.database, paths.session_key) as registry:
        ref = registry.pending_receipts()[0].object_hash
    value.update(sessionId="session-two", tool="htsave_htsave_hydrate", input={"ref": ref})
    value.update(hooks._handle_session_start(value))
    response = hooks._handle_pre_tool_use(value)
    with pytest.raises(Exception, match=r"registered|session|target|object"):
        hydrate_session_text(**response["input"], state_root=tmp_path / "state")


def test_main_rejects_invalid_json_and_state_errors(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("bad json"))
    hooks.main(["pre-tool-use"])
    assert json.loads(capsys.readouterr().out) == {}
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory")
    monkeypatch.setenv("HTSAVE_STATE_DIR", str(blocker))
    assert hooks._handle_session_start({"sessionId": "session"}) == {}


def test_corrupt_cas_preserves_native_result_and_rejects_hydrate(tmp_path, monkeypatch):
    value = payload(tmp_path, monkeypatch)
    value.update(tool="bash", status="completed", result={"output": "text\n" * 300})
    hooks._handle_post_tool_use(value)
    paths = build_state_paths(value["sessionId"], tmp_path / "state")
    with Registry(paths.database, paths.session_key) as registry:
        ref = registry.pending_receipts()[0].object_hash
    ContentAddressedStore(paths.objects).path_for(ref).write_bytes(b"corrupt")
    value["toolUseId"] = "two"
    assert hooks._handle_post_tool_use(value) == {}
    value.update(tool="htsave_htsave_hydrate", input={"ref": ref})
    response = hooks._handle_pre_tool_use(value)
    with pytest.raises(CorruptObjectError):
        hydrate_session_text(**response["input"], state_root=tmp_path / "state")
