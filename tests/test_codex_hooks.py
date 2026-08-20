from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from htsave.capabilities import canonical_arguments_hash
from htsave.cas import ContentAddressedStore
from htsave.codex_hooks import (
    SUPPORTED_CODEX_VERSION,
    PostToolUseEvent,
    decode_observed_text,
    dispatch_hook,
    main,
    parse_hook_event,
)
from htsave.compat import CodexCompatibility
from htsave.engine import ContextEngine
from htsave.errors import CompatibilityError
from htsave.models import DeliveryMode
from htsave.paths import build_state_paths
from htsave.registry import Registry
from htsave.tokens import TokenEstimator
from htsave.transport import parse_transport


def _common(
    tmp_path: Path,
    event_name: str,
    *,
    session_id: str = "session-one",
) -> dict[str, object]:
    return {
        "session_id": session_id,
        "transcript_path": str(tmp_path / "must-not-be-read.jsonl"),
        "cwd": str(tmp_path),
        "hook_event_name": event_name,
        "model": "gpt-5",
    }


def _session_start(
    tmp_path: Path,
    source: str,
    *,
    session_id: str = "session-one",
) -> dict[str, object]:
    return {
        **_common(tmp_path, "SessionStart", session_id=session_id),
        "permission_mode": "default",
        "source": source,
    }


def _pre_tool(
    tmp_path: Path,
    *,
    tool_name: str = "mcp__htsave__htsave_read",
    tool_input: object | None = None,
    tool_use_id: str = "pre-tool-1",
    session_id: str = "session-one",
) -> dict[str, object]:
    return {
        **_common(tmp_path, "PreToolUse", session_id=session_id),
        "permission_mode": "default",
        "turn_id": "turn-1",
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "tool_input": {"path": "README.md"} if tool_input is None else tool_input,
    }


def _post_tool(
    tmp_path: Path,
    *,
    tool_name: str = "Bash",
    tool_input: object | None = None,
    tool_response: object = "result",
    tool_use_id: str = "post-tool-1",
    session_id: str = "session-one",
) -> dict[str, object]:
    return {
        **_common(tmp_path, "PostToolUse", session_id=session_id),
        "permission_mode": "default",
        "turn_id": "turn-1",
        "tool_name": tool_name,
        "tool_use_id": tool_use_id,
        "tool_input": {"command": "printf result"} if tool_input is None else tool_input,
        "tool_response": tool_response,
    }


def _pre_compact(tmp_path: Path, *, session_id: str = "session-one") -> dict[str, object]:
    return {
        **_common(tmp_path, "PreCompact", session_id=session_id),
        "turn_id": "turn-compact",
        "trigger": "auto",
    }


def _subagent_start(
    tmp_path: Path,
    agent_id: str,
    *,
    session_id: str = "session-one",
) -> dict[str, object]:
    return {
        **_common(tmp_path, "SubagentStart", session_id=session_id),
        "permission_mode": "default",
        "turn_id": "turn-agent",
        "agent_id": agent_id,
        "agent_type": "worker",
    }


def _subagent_stop(
    tmp_path: Path,
    agent_id: str,
    *,
    session_id: str = "session-one",
) -> dict[str, object]:
    return {
        **_common(tmp_path, "SubagentStop", session_id=session_id),
        "permission_mode": "default",
        "turn_id": "turn-agent",
        "agent_id": agent_id,
        "agent_type": "worker",
        "agent_transcript_path": str(tmp_path / "also-must-not-be-read.jsonl"),
        "stop_hook_active": False,
        "last_assistant_message": None,
    }


def _stop(tmp_path: Path, *, session_id: str = "session-one") -> dict[str, object]:
    return {
        **_common(tmp_path, "Stop", session_id=session_id),
        "permission_mode": "default",
        "turn_id": "turn-stop",
        "stop_hook_active": False,
        "last_assistant_message": "done",
    }


def _large_text() -> str:
    return "".join(
        f"line {number:04d}: deterministic hook context value {number}\n" for number in range(1200)
    )


def _state_root(tmp_path: Path) -> Path:
    return tmp_path / "state"


def _paths(tmp_path: Path, session_id: str = "session-one"):
    return build_state_paths(session_id, _state_root(tmp_path))


def _seed_pending(tmp_path: Path, *, tool_use_id: str = "domain-tool-1") -> tuple[int, str]:
    paths = _paths(tmp_path)
    with Registry(paths.database, paths.session_key) as registry:
        generation = registry.ensure_generation()
        engine = ContextEngine(
            ContentAddressedStore(paths.objects),
            registry,
            TokenEstimator("gpt-5"),
        )
        decision = engine.decide(
            text=_large_text(),
            source_fingerprint="seed-source",
            safe_label="seed",
            tool_use_id=tool_use_id,
        )
        return generation, decision.target_hash


def test_v0148_parser_requires_documented_fields_and_accepts_additive_unknowns(
    tmp_path: Path,
) -> None:
    payload = _post_tool(tmp_path)
    payload["future_top_level"] = {"nested": True}
    event = parse_hook_event(payload)

    assert isinstance(event, PostToolUseEvent)
    assert event.tool_response == "result"

    missing = dict(payload)
    del missing["tool_response"]
    with pytest.raises(CompatibilityError, match="tool_response"):
        parse_hook_event(missing)

    wrong_type = dict(payload)
    wrong_type["transcript_path"] = 42
    with pytest.raises(CompatibilityError, match="transcript_path"):
        parse_hook_event(wrong_type)

    with pytest.raises(CompatibilityError, match="contract version"):
        parse_hook_event(payload, contract_version="0.149.0")


def test_source_fingerprint_uses_tool_name_and_canonical_public_input(
    tmp_path: Path,
) -> None:
    first = parse_hook_event(
        _post_tool(
            tmp_path,
            tool_input={"path": "雪.md", "range": {"end": 3, "start": 1}},
        )
    )
    second = parse_hook_event(
        _post_tool(
            tmp_path,
            tool_input={
                "range": {"start": 1, "end": 3},
                "_htsave_context": {"token": "ignored"},
                "path": "雪.md",
            },
        )
    )
    assert isinstance(first, PostToolUseEvent)
    assert isinstance(second, PostToolUseEvent)

    first_text = decode_observed_text(first)
    second_text = decode_observed_text(second)
    assert first_text is not None
    assert second_text is not None
    assert first_text.source_fingerprint == second_text.source_fingerprint
    assert first_text.codec == "codex-bash-text-v1"


def test_observer_codecs_accept_only_unambiguous_bash_or_mcp_text(tmp_path: Path) -> None:
    mcp = parse_hook_event(
        _post_tool(
            tmp_path,
            tool_name="mcp__files__read",
            tool_input={"path": "README.md"},
            tool_response={
                "content": [
                    {
                        "type": "text",
                        "text": "byte-exact\r\n雪 ",
                        "future_block_field": True,
                    }
                ],
                "structuredContent": {},
                "future_result_field": "additive",
            },
        )
    )
    assert isinstance(mcp, PostToolUseEvent)
    decoded = decode_observed_text(mcp)
    assert decoded is not None
    assert decoded.text == "byte-exact\r\n雪 "
    assert decoded.codec == "mcp-call-result-single-text-v1"

    rejected = [
        _post_tool(tmp_path, tool_name="apply_patch", tool_response="patched"),
        _post_tool(tmp_path, tool_name="update_plan", tool_response="updated"),
        _post_tool(tmp_path, tool_name="Bash", tool_response={"output": "guess"}),
        _post_tool(
            tmp_path,
            tool_name="mcp__files__read",
            tool_response={"content": [{"type": "text", "text": "x"}], "isError": True},
        ),
        _post_tool(
            tmp_path,
            tool_name="mcp__files__read",
            tool_response={
                "content": [{"type": "text", "text": "x"}],
                "structuredContent": {"also": "visible"},
            },
        ),
        _post_tool(
            tmp_path,
            tool_name="mcp__files__read",
            tool_response={
                "content": [
                    {"type": "text", "text": "one"},
                    {"type": "text", "text": "two"},
                ]
            },
        ),
    ]
    for payload in rejected:
        event = parse_hook_event(payload)
        assert isinstance(event, PostToolUseEvent)
        assert decode_observed_text(event) is None


@pytest.mark.parametrize("source", ["startup", "resume", "clear", "compact"])
def test_every_session_start_source_creates_a_new_generation_even_if_incompatible(
    tmp_path: Path,
    source: str,
) -> None:
    paths = _paths(tmp_path)
    before = 0
    if paths.database.exists():
        with Registry(paths.database, paths.session_key) as registry:
            before = int(
                registry.connection.execute("SELECT COUNT(*) FROM generations").fetchone()[0]
            )

    response = dispatch_hook(
        _session_start(tmp_path, source),
        state_root=_state_root(tmp_path),
        codex_version="99.0.0",
    )

    assert response == {}
    with Registry(paths.database, paths.session_key) as registry:
        assert (
            int(registry.connection.execute("SELECT COUNT(*) FROM generations").fetchone()[0])
            == before + 1
        )
        row = registry.connection.execute(
            "SELECT source, active FROM generations ORDER BY generation_id DESC LIMIT 1"
        ).fetchone()
        assert (str(row["source"]), int(row["active"])) == (source, 1)


def test_pretool_confirms_prior_pending_and_injects_full_one_use_input(
    tmp_path: Path,
) -> None:
    dispatch_hook(
        _session_start(tmp_path, "startup"),
        state_root=_state_root(tmp_path),
        codex_version=SUPPORTED_CODEX_VERSION,
    )
    generation, target_hash = _seed_pending(tmp_path)
    original_input = {
        "future_argument": {"preserve": True},
        "path": "README.md",
        "_htsave_context": {"token": "untrusted"},
    }

    response = dispatch_hook(
        _pre_tool(tmp_path, tool_input=original_input),
        state_root=_state_root(tmp_path),
        codex_version=SUPPORTED_CODEX_VERSION,
    )

    specific = response["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["hookEventName"] == "PreToolUse"
    assert specific["permissionDecision"] == "allow"
    updated = specific["updatedInput"]
    assert isinstance(updated, dict)
    assert updated["path"] == "README.md"
    assert updated["future_argument"] == {"preserve": True}
    context = updated["_htsave_context"]
    assert isinstance(context, dict)
    assert isinstance(context["token"], str)
    assert context["token"].startswith(f"v1.{_paths(tmp_path).session_key}.")
    assert canonical_arguments_hash(updated) == canonical_arguments_hash(original_input)

    with Registry(_paths(tmp_path).database, _paths(tmp_path).session_key) as registry:
        assert registry.pending_receipts(generation) == ()
        assert registry.confirmed_object_exists(generation, target_hash)
        capability = registry.connection.execute(
            "SELECT tool_name, arguments_hash, model, cwd, consumed_at FROM capabilities"
        ).fetchone()
        assert capability is not None
        assert str(capability["tool_name"]) == "mcp__htsave__htsave_read"
        assert str(capability["arguments_hash"]) == canonical_arguments_hash(original_input)
        assert str(capability["model"]) == "gpt-5"
        assert str(capability["cwd"]) == str(tmp_path)
        assert capability["consumed_at"] is None


def test_pretool_only_rewrites_the_two_exact_htsave_mcp_names(tmp_path: Path) -> None:
    dispatch_hook(
        _session_start(tmp_path, "startup"),
        state_root=_state_root(tmp_path),
        codex_version=SUPPORTED_CODEX_VERSION,
    )
    for index, (tool_name, tool_input) in enumerate(
        [
            ("mcp__htsave__htsave_read", {"path": "README.md"}),
            ("mcp__htsave__htsave_hydrate", {"ref": "sha256:" + "0" * 64}),
        ]
    ):
        response = dispatch_hook(
            _pre_tool(
                tmp_path,
                tool_name=tool_name,
                tool_input=tool_input,
                tool_use_id=f"allowed-{index}",
            ),
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        assert response["hookSpecificOutput"]["updatedInput"]["_htsave_context"]

    response = dispatch_hook(
        _pre_tool(
            tmp_path,
            tool_name="mcp__other__htsave_read",
            tool_use_id="not-allowed",
        ),
        state_root=_state_root(tmp_path),
        codex_version=SUPPORTED_CODEX_VERSION,
    )
    assert response == {}
    with Registry(_paths(tmp_path).database, _paths(tmp_path).session_key) as registry:
        assert (
            int(registry.connection.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0]) == 2
        )


def test_unknown_codex_version_disables_contract_actions_but_not_session_generation(
    tmp_path: Path,
) -> None:
    assert (
        dispatch_hook(
            _session_start(tmp_path, "startup"),
            state_root=_state_root(tmp_path),
            codex_version="0.149.0",
        )
        == {}
    )
    generation, _ = _seed_pending(tmp_path)

    assert (
        dispatch_hook(
            _pre_tool(tmp_path),
            state_root=_state_root(tmp_path),
            codex_version="0.149.0",
        )
        == {}
    )
    assert (
        dispatch_hook(
            _post_tool(tmp_path, tool_use_id="unknown-version-post"),
            state_root=_state_root(tmp_path),
            codex_version="0.149.0",
        )
        == {}
    )
    assert (
        dispatch_hook(
            _subagent_start(tmp_path, "agent-unknown"),
            state_root=_state_root(tmp_path),
            codex_version="0.149.0",
        )
        == {}
    )

    with Registry(_paths(tmp_path).database, _paths(tmp_path).session_key) as registry:
        assert len(registry.pending_receipts(generation)) == 1
        assert (
            int(registry.connection.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0]) == 0
        )
        assert (
            int(registry.connection.execute("SELECT COUNT(*) FROM active_agents").fetchone()[0])
            == 0
        )
        assert registry.stats().events == 1


def test_injected_compatibility_probe_is_fail_open(tmp_path: Path) -> None:
    calls = 0

    def incompatible() -> CodexCompatibility:
        nonlocal calls
        calls += 1
        return CodexCompatibility("0.149.0", False, "incompatible-codex-version")

    assert (
        dispatch_hook(
            _pre_tool(tmp_path),
            state_root=_state_root(tmp_path),
            compatibility_probe=incompatible,
        )
        == {}
    )
    assert calls == 1
    assert not _paths(tmp_path).database.exists()


def test_posttool_bash_is_observer_only_and_preserves_exact_bytes(tmp_path: Path) -> None:
    text = "first\r\n雪 with trailing spaces  \nno-final-newline"
    response = dispatch_hook(
        _post_tool(tmp_path, tool_response=text),
        state_root=_state_root(tmp_path),
        codex_version=SUPPORTED_CODEX_VERSION,
    )

    assert response == {}
    with Registry(_paths(tmp_path).database, _paths(tmp_path).session_key) as registry:
        stats = registry.stats()
        assert (stats.events, stats.bypassed, stats.full, stats.refs, stats.deltas) == (
            1,
            1,
            0,
            0,
            0,
        )
        assert registry.pending_receipts() == ()
        event = registry.connection.execute(
            "SELECT target_hash, bypass_reason FROM events"
        ).fetchone()
        assert str(event["bypass_reason"]) == "posttool-observer-only"
        stored = ContentAddressedStore(_paths(tmp_path).objects).get(str(event["target_hash"]))
        assert stored == text.encode("utf-8")


def test_posttool_single_text_mcp_is_observed_but_unknown_responses_are_bypassed(
    tmp_path: Path,
) -> None:
    assert (
        dispatch_hook(
            _post_tool(
                tmp_path,
                tool_name="mcp__files__read",
                tool_input={"path": "README.md"},
                tool_response={
                    "content": [{"type": "text", "text": "mcp exact text"}],
                    "isError": False,
                },
                tool_use_id="accepted-mcp",
            ),
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )

    rejected = [
        ("apply_patch", "patch output"),
        ("update_plan", "local output"),
        ("Bash", {"output": "not the documented text shape"}),
        (
            "mcp__files__read",
            {"content": [{"type": "text", "text": "error"}], "isError": True},
        ),
        (
            "mcp__files__read",
            {
                "content": [{"type": "text", "text": "text"}],
                "structuredContent": {"extra": "model-facing"},
            },
        ),
    ]
    for index, (tool_name, tool_response) in enumerate(rejected):
        assert (
            dispatch_hook(
                _post_tool(
                    tmp_path,
                    tool_name=tool_name,
                    tool_response=tool_response,
                    tool_use_id=f"rejected-{index}",
                ),
                state_root=_state_root(tmp_path),
                codex_version=SUPPORTED_CODEX_VERSION,
            )
            == {}
        )

    with Registry(_paths(tmp_path).database, _paths(tmp_path).session_key) as registry:
        assert registry.stats().events == 1


def test_precompact_queues_pending_transform_and_compact_start_returns_full_recovery(
    tmp_path: Path,
) -> None:
    dispatch_hook(
        _session_start(tmp_path, "startup"),
        state_root=_state_root(tmp_path),
        codex_version=SUPPORTED_CODEX_VERSION,
    )
    text = _large_text()
    paths = _paths(tmp_path)
    with Registry(paths.database, paths.session_key) as registry:
        engine = ContextEngine(
            ContentAddressedStore(paths.objects), registry, TokenEstimator("gpt-5")
        )
        first = engine.decide(
            text=text,
            source_fingerprint="compact-source",
            safe_label="compact",
            tool_use_id="compact-full",
        )
        assert first.mode is DeliveryMode.FULL
        registry.confirm_pending()
        transformed = engine.decide(
            text=text,
            source_fingerprint="compact-source",
            safe_label="compact",
            tool_use_id="compact-ref",
        )
        assert transformed.mode is DeliveryMode.REF
        source_generation = registry.active_generation()
        assert source_generation is not None

    assert (
        dispatch_hook(
            _pre_compact(tmp_path),
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )
    # A retry must not duplicate the queued recovery row.
    assert (
        dispatch_hook(
            _pre_compact(tmp_path),
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )
    with Registry(paths.database, paths.session_key) as registry:
        assert registry.active_is_frozen()
        assert len(registry.recovery_targets(source_generation)) == 1

    response = dispatch_hook(
        _session_start(tmp_path, "compact"),
        state_root=_state_root(tmp_path),
        codex_version=SUPPORTED_CODEX_VERSION,
    )

    specific = response["hookSpecificOutput"]
    assert isinstance(specific, dict)
    assert specific["hookEventName"] == "SessionStart"
    context = specific["additionalContext"]
    assert isinstance(context, str)
    frame = parse_transport(context)
    assert frame.mode is DeliveryMode.FULL
    assert frame.payload == text.encode("utf-8")
    with Registry(paths.database, paths.session_key) as registry:
        next_generation = registry.active_generation()
        assert next_generation is not None and next_generation != source_generation
        assert registry.recovery_targets(source_generation) == ()
        pending = registry.pending_receipts(next_generation)
        assert len(pending) == 1
        assert pending[0].mode is DeliveryMode.FULL
        assert pending[0].object_hash == transformed.target_hash
        consumed = registry.connection.execute(
            "SELECT consumed_generation FROM compact_recovery"
        ).fetchone()
        assert int(consumed["consumed_generation"]) == next_generation


def test_subagent_hooks_are_idempotent_and_only_last_real_stop_resets_generation(
    tmp_path: Path,
) -> None:
    dispatch_hook(
        _session_start(tmp_path, "startup"),
        state_root=_state_root(tmp_path),
        codex_version=SUPPORTED_CODEX_VERSION,
    )
    paths = _paths(tmp_path)
    with Registry(paths.database, paths.session_key) as registry:
        original_generation = registry.active_generation()

    for _ in range(2):
        assert (
            dispatch_hook(
                _subagent_start(tmp_path, "agent-1"),
                state_root=_state_root(tmp_path),
                codex_version=SUPPORTED_CODEX_VERSION,
            )
            == {}
        )
    with Registry(paths.database, paths.session_key) as registry:
        assert registry.active_generation() == original_generation
        assert registry.active_is_ambiguous()
        assert (
            int(registry.connection.execute("SELECT COUNT(*) FROM active_agents").fetchone()[0])
            == 1
        )

    assert (
        dispatch_hook(
            _subagent_stop(tmp_path, "unknown-agent"),
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )
    with Registry(paths.database, paths.session_key) as registry:
        assert registry.active_generation() == original_generation

    assert (
        dispatch_hook(
            _subagent_stop(tmp_path, "agent-1"),
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )
    with Registry(paths.database, paths.session_key) as registry:
        reset_generation = registry.active_generation()
        assert reset_generation is not None and reset_generation != original_generation
        assert not registry.active_is_ambiguous()

    assert (
        dispatch_hook(
            _subagent_stop(tmp_path, "agent-1"),
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )
    with Registry(paths.database, paths.session_key) as registry:
        assert registry.active_generation() == reset_generation


def test_optional_tool_agent_identity_never_confirms_or_issues_capability(
    tmp_path: Path,
) -> None:
    dispatch_hook(
        _session_start(tmp_path, "startup"),
        state_root=_state_root(tmp_path),
        codex_version=SUPPORTED_CODEX_VERSION,
    )
    generation, _ = _seed_pending(tmp_path)
    payload = _pre_tool(tmp_path)
    payload.update({"agent_id": "agent-inline", "agent_type": "worker"})

    assert (
        dispatch_hook(
            payload,
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )
    with Registry(_paths(tmp_path).database, _paths(tmp_path).session_key) as registry:
        assert len(registry.pending_receipts(generation)) == 1
        assert registry.active_is_ambiguous()
        assert registry.connection.execute("SELECT COUNT(*) FROM capabilities").fetchone()[0] == 0


def test_posttool_agent_identity_is_recorded_even_for_unknown_response_shape(
    tmp_path: Path,
) -> None:
    dispatch_hook(
        _session_start(tmp_path, "startup"),
        state_root=_state_root(tmp_path),
        codex_version=SUPPORTED_CODEX_VERSION,
    )
    payload = _post_tool(tmp_path, tool_response={"future": "shape"})
    payload.update({"agent_id": "agent-inline", "agent_type": "worker"})

    assert (
        dispatch_hook(
            payload,
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )
    with Registry(_paths(tmp_path).database, _paths(tmp_path).session_key) as registry:
        assert registry.active_is_ambiguous()
        assert registry.stats().events == 0


def test_unknown_subagent_identity_marks_session_full_only(tmp_path: Path) -> None:
    payload = _subagent_start(tmp_path, "agent-will-be-removed")
    del payload["agent_id"]

    assert (
        dispatch_hook(
            payload,
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )
    with Registry(_paths(tmp_path).database, _paths(tmp_path).session_key) as registry:
        assert registry.active_is_ambiguous()


def test_posttool_does_not_reingest_htsave_own_mcp_result(tmp_path: Path) -> None:
    result = {"content": [{"type": "text", "text": "HTSAVE/1 REF"}]}
    assert (
        dispatch_hook(
            _post_tool(
                tmp_path,
                tool_name="mcp__htsave__htsave_read",
                tool_response=result,
            ),
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )
    assert not _paths(tmp_path).database.exists()


def test_stop_confirms_pending_receipts(tmp_path: Path) -> None:
    dispatch_hook(
        _session_start(tmp_path, "startup"),
        state_root=_state_root(tmp_path),
        codex_version=SUPPORTED_CODEX_VERSION,
    )
    generation, target_hash = _seed_pending(tmp_path)

    assert (
        dispatch_hook(
            _stop(tmp_path),
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )
    with Registry(_paths(tmp_path).database, _paths(tmp_path).session_key) as registry:
        assert registry.pending_receipts(generation) == ()
        assert registry.confirmed_object_exists(generation, target_hash)


def test_unknown_malformed_and_internal_errors_always_fail_open(tmp_path: Path) -> None:
    unknown = {**_common(tmp_path, "FutureHook"), "future": True}
    assert (
        dispatch_hook(
            unknown,
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )
    assert not _paths(tmp_path).database.exists()

    malformed = _pre_tool(tmp_path)
    del malformed["session_id"]
    assert (
        dispatch_hook(
            malformed,
            state_root=_state_root(tmp_path),
            codex_version=SUPPORTED_CODEX_VERSION,
        )
        == {}
    )

    def explode() -> CodexCompatibility:
        raise RuntimeError("probe failed")

    assert (
        dispatch_hook(
            _post_tool(tmp_path),
            state_root=_state_root(tmp_path),
            compatibility_probe=explode,
        )
        == {}
    )


@pytest.mark.parametrize(
    "raw",
    ["", "not json", "[]", "{} {}", '{"number": NaN}'],
)
def test_main_reads_one_json_object_and_writes_empty_object_on_bad_input(raw: str) -> None:
    output = io.StringIO()
    assert main(stdin=io.StringIO(raw), stdout=output) == 0
    assert output.getvalue() == "{}\n"


def test_main_catches_dispatcher_errors_and_posttool_has_no_unsupported_output_fields(
    tmp_path: Path,
) -> None:
    def explode(_: object) -> dict[str, object]:
        raise RuntimeError("adapter failure")

    output = io.StringIO()
    assert main(stdin=io.StringIO("{}"), stdout=output, dispatcher=explode) == 0
    assert output.getvalue() == "{}\n"

    response = dispatch_hook(
        _post_tool(tmp_path),
        state_root=_state_root(tmp_path),
        codex_version=SUPPORTED_CODEX_VERSION,
    )
    encoded = json.dumps(response)
    assert response == {}
    assert "continue" not in encoded
    assert "updatedMCPToolOutput" not in encoded
    assert "decision" not in encoded
