"""Claude Code adapter tests.

Every payload here is shaped from real events captured out of Claude Code
2.1.237 via a temporary dump hook, so the fixtures match the live contract
rather than a guess at it.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from htsave.claude_hooks import (
    decode_observed_response,
    dispatch_hook,
    main,
    parse_hook_event,
)
from htsave.compat import ClaudeCompatibility
from htsave.paths import build_state_paths
from htsave.registry import Registry
from htsave.transport import parse_transport

SUPPORTED = ClaudeCompatibility(
    "2.1.237",
    True,
    "supported",
    posttool_result_replacement=True,
    pretooluse_updated_input=True,
)
UNSUPPORTED = ClaudeCompatibility(None, False, "claude-version-probe-failed")

SESSION = "a3e3e4d1-094d-4f3e-9137-6b28fb1215c6"


def _large_text() -> str:
    return "".join(f"line {index}: deterministic claude context {index}\n" for index in range(900))


def _session_start(source: str = "startup", **extra: object) -> dict[str, object]:
    return {
        "session_id": SESSION,
        "transcript_path": "/home/user/.claude/projects/x/session.jsonl",
        "cwd": "/workspace",
        "hook_event_name": "SessionStart",
        "source": source,
        **extra,
    }


def _read_response(content: str, path: str = "/workspace/sample.txt") -> dict[str, object]:
    return {
        "type": "text",
        "file": {
            "filePath": path,
            "content": content,
            "numLines": content.count("\n") + 1,
            "startLine": 1,
            "totalLines": content.count("\n") + 1,
        },
    }


def _post_read(content: str, tool_use_id: str, **extra: object) -> dict[str, object]:
    return {
        "session_id": SESSION,
        "transcript_path": "/home/user/.claude/projects/x/session.jsonl",
        "cwd": "/workspace",
        "prompt_id": "92e5327b-0982-4192-a37f-1acaebe0aa6c",
        "permission_mode": "bypassPermissions",
        "effort": {"level": "high"},
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_use_id": tool_use_id,
        "tool_input": {"file_path": "/workspace/sample.txt"},
        "tool_response": _read_response(content),
        "duration_ms": 12,
        **extra,
    }


def _post_bash(stdout: str, tool_use_id: str, **response: object) -> dict[str, object]:
    return {
        "session_id": SESSION,
        "transcript_path": None,
        "cwd": "/workspace",
        "prompt_id": "92e5327b-0982-4192-a37f-1acaebe0aa6c",
        "permission_mode": "default",
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_use_id": tool_use_id,
        "tool_input": {"command": "cat sample.txt"},
        "tool_response": {
            "stdout": stdout,
            "stderr": "",
            "interrupted": False,
            "isImage": False,
            "noOutputExpected": False,
            **response,
        },
        "duration_ms": 7,
    }


def _stop() -> dict[str, object]:
    return {
        "session_id": SESSION,
        "transcript_path": None,
        "cwd": "/workspace",
        "prompt_id": "92e5327b-0982-4192-a37f-1acaebe0aa6c",
        "permission_mode": "default",
        "hook_event_name": "Stop",
    }


def test_payload_without_model_turn_id_or_permission_mode_still_parses() -> None:
    event = parse_hook_event(_session_start())

    assert event.model is None
    assert event.session_id == SESSION

    tool_event = parse_hook_event(_post_bash("out", "toolu_1"))

    # prompt_id stands in for Codex's turn_id.
    assert tool_event.turn_id == "92e5327b-0982-4192-a37f-1acaebe0aa6c"


def test_turn_identity_falls_back_to_tool_use_id_then_a_sentinel() -> None:
    payload = _post_bash("out", "toolu_9")
    del payload["prompt_id"]

    assert parse_hook_event(payload).turn_id == "toolu_9"

    stop = _stop()
    del stop["prompt_id"]

    assert parse_hook_event(stop).turn_id == "no-prompt-id"


def test_unknown_permission_mode_or_source_fails_open(tmp_path: Path) -> None:
    bad_mode = _post_bash("out", "toolu_2")
    bad_mode["permission_mode"] = "brandNewMode"
    bad_source = _session_start("teleport")

    for payload in (bad_mode, bad_source):
        assert dispatch_hook(payload, state_root=tmp_path / "state", compatibility=SUPPORTED) == {}


def test_repeated_read_is_replaced_in_place_and_keeps_the_response_shape(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    content = _large_text()

    first = dispatch_hook(
        _post_read(content, "toolu_read_1"), state_root=state_root, compatibility=SUPPORTED
    )
    # Delivery of the first result is confirmed by the next lifecycle event.
    dispatch_hook(_stop(), state_root=state_root, compatibility=SUPPORTED)
    second = dispatch_hook(
        _post_read(content, "toolu_read_2"), state_root=state_root, compatibility=SUPPORTED
    )

    assert first == {}, "the first delivery is already the full bytes"

    output = second["hookSpecificOutput"]
    assert output["hookEventName"] == "PostToolUse"
    replaced = output["updatedToolOutput"]

    # Same shape as the original Read response, only the content differs.
    assert set(replaced) == {"type", "file"}
    assert replaced["type"] == "text"
    assert set(replaced["file"]) == {
        "filePath",
        "content",
        "numLines",
        "startLine",
        "totalLines",
    }
    assert replaced["file"]["filePath"] == "/workspace/sample.txt"
    assert replaced["file"]["content"] != content

    frame = parse_transport(replaced["file"]["content"])
    assert frame.mode.value in {"ref", "delta"}
    assert replaced["file"]["numLines"] == replaced["file"]["content"].count("\n") + 1


def test_repeated_bash_stdout_is_replaced_and_hydrates_byte_exact(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    stdout = _large_text()

    dispatch_hook(_post_bash(stdout, "toolu_b1"), state_root=state_root, compatibility=SUPPORTED)
    dispatch_hook(_stop(), state_root=state_root, compatibility=SUPPORTED)
    second = dispatch_hook(
        _post_bash(stdout, "toolu_b2"), state_root=state_root, compatibility=SUPPORTED
    )

    replaced = second["hookSpecificOutput"]["updatedToolOutput"]
    assert set(replaced) == {
        "stdout",
        "stderr",
        "interrupted",
        "isImage",
        "noOutputExpected",
    }
    frame = parse_transport(replaced["stdout"])

    paths = build_state_paths(SESSION, state_root)
    with Registry(paths.database, paths.session_key) as registry:
        stats = registry.stats()
    assert stats.refs + stats.deltas >= 1

    from htsave.cas import ContentAddressedStore

    stored = ContentAddressedStore(paths.objects).get(frame.target_hash)
    assert stored.decode() == stdout


def test_unconfirmed_delivery_is_never_replaced(tmp_path: Path) -> None:
    """Cache presence is not proof the model still holds the content."""

    state_root = tmp_path / "state"
    content = _large_text()

    dispatch_hook(_post_read(content, "toolu_x1"), state_root=state_root, compatibility=SUPPORTED)
    # No Stop/PreToolUse in between, so the first delivery stays pending.
    second = dispatch_hook(
        _post_read(content, "toolu_x2"), state_root=state_root, compatibility=SUPPORTED
    )

    assert second == {}


def test_ambiguous_or_unsupported_results_pass_through_untouched(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    content = _large_text()

    noisy = _post_bash(content, "toolu_n1", stderr="warning: partial")
    interrupted = _post_bash(content, "toolu_n2", interrupted=True)
    image = _post_bash(content, "toolu_n3", isImage=True)
    edit = _post_read(content, "toolu_n4")
    edit["tool_name"] = "Edit"

    for payload in (noisy, interrupted, image, edit):
        dispatch_hook(payload, state_root=state_root, compatibility=SUPPORTED)
        dispatch_hook(_stop(), state_root=state_root, compatibility=SUPPORTED)
        assert dispatch_hook(payload, state_root=state_root, compatibility=SUPPORTED) == {}, (
            payload["tool_name"]
        )


def test_htsave_own_mcp_results_are_never_re_ingested(tmp_path: Path) -> None:
    payload = _post_read(_large_text(), "toolu_self")
    payload["tool_name"] = "mcp__htsave__htsave_read"

    assert decode_observed_response(parse_hook_event(payload)) is None


def test_incompatible_claude_never_transforms_but_still_tracks_generations(
    tmp_path: Path,
) -> None:
    state_root = tmp_path / "state"
    content = _large_text()

    assert dispatch_hook(_session_start(), state_root=state_root, compatibility=UNSUPPORTED) == {}
    dispatch_hook(_post_read(content, "toolu_i1"), state_root=state_root, compatibility=UNSUPPORTED)
    dispatch_hook(_stop(), state_root=state_root, compatibility=UNSUPPORTED)
    second = dispatch_hook(
        _post_read(content, "toolu_i2"), state_root=state_root, compatibility=UNSUPPORTED
    )

    assert second == {}
    paths = build_state_paths(SESSION, state_root)
    with Registry(paths.database, paths.session_key) as registry:
        assert registry.active_generation() is not None


def test_session_start_begins_a_generation_and_records_the_model(tmp_path: Path) -> None:
    state_root = tmp_path / "state"

    dispatch_hook(
        _session_start(model="claude-opus-5"), state_root=state_root, compatibility=SUPPORTED
    )

    paths = build_state_paths(SESSION, state_root)
    with Registry(paths.database, paths.session_key) as registry:
        assert registry.active_model() == "claude-opus-5"


def test_new_generation_stops_replacement_from_the_previous_one(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    content = _large_text()

    dispatch_hook(_post_read(content, "toolu_g1"), state_root=state_root, compatibility=SUPPORTED)
    dispatch_hook(_stop(), state_root=state_root, compatibility=SUPPORTED)
    dispatch_hook(_session_start("clear"), state_root=state_root, compatibility=SUPPORTED)
    after_clear = dispatch_hook(
        _post_read(content, "toolu_g2"), state_root=state_root, compatibility=SUPPORTED
    )

    assert after_clear == {}


def test_corrupt_state_degrades_to_the_original_output(tmp_path: Path) -> None:
    state_root = tmp_path / "state"
    content = _large_text()

    dispatch_hook(_post_read(content, "toolu_c1"), state_root=state_root, compatibility=SUPPORTED)
    dispatch_hook(_stop(), state_root=state_root, compatibility=SUPPORTED)

    paths = build_state_paths(SESSION, state_root)
    with closing(sqlite3.connect(paths.database)) as connection, connection:
        connection.execute("DROP TABLE receipts")

    assert (
        dispatch_hook(
            _post_read(content, "toolu_c2"), state_root=state_root, compatibility=SUPPORTED
        )
        == {}
    )


@pytest.mark.parametrize("payload", ["not json", "[1,2,3]", '{"hook_event_name": "Nope"}'])
def test_main_always_emits_one_json_object(payload: str, tmp_path: Path, capsys) -> None:
    import io

    stdout = io.StringIO()
    code = main(stdin=io.StringIO(payload), stdout=stdout)

    assert code == 0
    assert json.loads(stdout.getvalue()) == {}
