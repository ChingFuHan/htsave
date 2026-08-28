"""Fail-open, version-independent Codex lifecycle adapter.

The domain engine deliberately has no knowledge of Codex hook JSON.  This
module is the compatibility boundary: it validates the documented event
shape, performs only lifecycle operations that the installed Codex contract
supports, and returns an empty object whenever the boundary is uncertain.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, TextIO, TypeAlias

from .capabilities import canonical_arguments_hash, issue_session_capability
from .cas import ContentAddressedStore
from .compat import CodexCompatibility, codex_compatibility_for_version, probe_codex_compatibility
from .engine import ContextEngine
from .errors import CompatibilityError
from .hashing import sha256_id
from .paths import build_state_paths
from .registry import RecoveryTarget, Registry
from .tokens import TokenEstimator
from .transport import render_full_frame

_HTSAVE_MCP_TOOLS: Final = frozenset(
    {
        "mcp__htsave__htsave_read",
        "mcp__htsave__htsave_hydrate",
    }
)
_PERMISSION_MODES: Final = frozenset(
    {"default", "acceptEdits", "plan", "dontAsk", "bypassPermissions"}
)
_SESSION_SOURCES: Final = frozenset({"startup", "resume", "clear", "compact"})
_COMPACT_TRIGGERS: Final = frozenset({"manual", "auto"})
_VERSION_UNSET = object()


@dataclass(frozen=True, slots=True)
class CommonHookEvent:
    session_id: str
    transcript_path: str | None
    cwd: str
    hook_event_name: str
    model: str


@dataclass(frozen=True, slots=True)
class SessionStartEvent(CommonHookEvent):
    permission_mode: str
    source: str


@dataclass(frozen=True, slots=True)
class PreToolUseEvent(CommonHookEvent):
    permission_mode: str
    turn_id: str
    tool_name: str
    tool_use_id: str
    tool_input: object
    agent_id: str | None
    agent_type: str | None


@dataclass(frozen=True, slots=True)
class PostToolUseEvent(CommonHookEvent):
    permission_mode: str
    turn_id: str
    tool_name: str
    tool_use_id: str
    tool_input: object
    tool_response: object
    agent_id: str | None
    agent_type: str | None


@dataclass(frozen=True, slots=True)
class CompactEvent(CommonHookEvent):
    turn_id: str
    trigger: str


@dataclass(frozen=True, slots=True)
class SubagentStartEvent(CommonHookEvent):
    permission_mode: str
    turn_id: str
    agent_id: str
    agent_type: str


@dataclass(frozen=True, slots=True)
class SubagentStopEvent(CommonHookEvent):
    permission_mode: str
    turn_id: str
    agent_id: str
    agent_type: str
    agent_transcript_path: str | None
    stop_hook_active: bool
    last_assistant_message: str | None


@dataclass(frozen=True, slots=True)
class StopEvent(CommonHookEvent):
    permission_mode: str
    turn_id: str
    stop_hook_active: bool
    last_assistant_message: str | None


HookEvent: TypeAlias = (
    SessionStartEvent
    | PreToolUseEvent
    | PostToolUseEvent
    | CompactEvent
    | SubagentStartEvent
    | SubagentStopEvent
    | StopEvent
)


@dataclass(frozen=True, slots=True)
class ObservedText:
    text: str
    source_fingerprint: str
    safe_label: str
    codec: str


def _require_mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise CompatibilityError(f"{field} must be a JSON object")
    return value


def _require_str(
    value: Mapping[str, Any],
    field: str,
    *,
    allowed: frozenset[str] | None = None,
) -> str:
    item = value.get(field)
    if not isinstance(item, str) or not item:
        raise CompatibilityError(f"{field} must be a non-empty string")
    if allowed is not None and item not in allowed:
        raise CompatibilityError(f"unsupported {field}")
    return item


def _require_optional_str(value: Mapping[str, Any], field: str) -> str | None:
    if field not in value:
        raise CompatibilityError(f"missing required field: {field}")
    item = value[field]
    if item is not None and not isinstance(item, str):
        raise CompatibilityError(f"{field} must be a string or null")
    return item


def _require_bool(value: Mapping[str, Any], field: str) -> bool:
    item = value.get(field)
    if type(item) is not bool:
        raise CompatibilityError(f"{field} must be a boolean")
    return item


def _require_field(value: Mapping[str, Any], field: str) -> object:
    if field not in value:
        raise CompatibilityError(f"missing required field: {field}")
    return value[field]


def _optional_agent(value: Mapping[str, Any]) -> tuple[str | None, str | None]:
    agent_id = value.get("agent_id")
    agent_type = value.get("agent_type")
    if agent_id is None and agent_type is None:
        return None, None
    if (
        not isinstance(agent_id, str)
        or not agent_id
        or not isinstance(agent_type, str)
        or not agent_type
    ):
        raise CompatibilityError("optional subagent identity is incomplete")
    return agent_id, agent_type


def _common(value: Mapping[str, Any], event_name: str) -> dict[str, object]:
    reported_event = _require_str(value, "hook_event_name")
    if reported_event != event_name:
        raise CompatibilityError("hook event name changed while parsing")
    return {
        "session_id": _require_str(value, "session_id"),
        "transcript_path": _require_optional_str(value, "transcript_path"),
        "cwd": _require_str(value, "cwd"),
        "hook_event_name": reported_event,
        "model": _require_str(value, "model"),
    }


def _permission_mode(value: Mapping[str, Any]) -> str:
    return _require_str(value, "permission_mode", allowed=_PERMISSION_MODES)


def _parse_codex_hook(payload: object) -> HookEvent:
    value = _require_mapping(payload, "hook input")
    event_name = _require_str(value, "hook_event_name")
    common = _common(value, event_name)

    if event_name == "SessionStart":
        return SessionStartEvent(
            **common,
            permission_mode=_permission_mode(value),
            source=_require_str(value, "source", allowed=_SESSION_SOURCES),
        )
    if event_name == "PreToolUse":
        agent_id, agent_type = _optional_agent(value)
        return PreToolUseEvent(
            **common,
            permission_mode=_permission_mode(value),
            turn_id=_require_str(value, "turn_id"),
            tool_name=_require_str(value, "tool_name"),
            tool_use_id=_require_str(value, "tool_use_id"),
            tool_input=_require_field(value, "tool_input"),
            agent_id=agent_id,
            agent_type=agent_type,
        )
    if event_name == "PostToolUse":
        agent_id, agent_type = _optional_agent(value)
        return PostToolUseEvent(
            **common,
            permission_mode=_permission_mode(value),
            turn_id=_require_str(value, "turn_id"),
            tool_name=_require_str(value, "tool_name"),
            tool_use_id=_require_str(value, "tool_use_id"),
            tool_input=_require_field(value, "tool_input"),
            tool_response=_require_field(value, "tool_response"),
            agent_id=agent_id,
            agent_type=agent_type,
        )
    if event_name in {"PreCompact", "PostCompact"}:
        return CompactEvent(
            **common,
            turn_id=_require_str(value, "turn_id"),
            trigger=_require_str(value, "trigger", allowed=_COMPACT_TRIGGERS),
        )
    if event_name == "SubagentStart":
        return SubagentStartEvent(
            **common,
            permission_mode=_permission_mode(value),
            turn_id=_require_str(value, "turn_id"),
            agent_id=_require_str(value, "agent_id"),
            agent_type=_require_str(value, "agent_type"),
        )
    if event_name == "SubagentStop":
        return SubagentStopEvent(
            **common,
            permission_mode=_permission_mode(value),
            turn_id=_require_str(value, "turn_id"),
            agent_id=_require_str(value, "agent_id"),
            agent_type=_require_str(value, "agent_type"),
            agent_transcript_path=_require_optional_str(value, "agent_transcript_path"),
            stop_hook_active=_require_bool(value, "stop_hook_active"),
            last_assistant_message=_require_optional_str(value, "last_assistant_message"),
        )
    if event_name == "Stop":
        return StopEvent(
            **common,
            permission_mode=_permission_mode(value),
            turn_id=_require_str(value, "turn_id"),
            stop_hook_active=_require_bool(value, "stop_hook_active"),
            last_assistant_message=_require_optional_str(value, "last_assistant_message"),
        )
    raise CompatibilityError("unsupported Codex hook event")


def parse_hook_event(
    payload: object, *, contract_version: str | None = None
) -> HookEvent:
    """Parse one Codex hook event, accepting only additive unknown fields.

    ``contract_version`` remains accepted for callers that recorded it, but
    the parser is intentionally version-independent.  Required fields are the
    capability boundary; a renamed or removed field still fails open in the
    dispatch layer.
    """

    del contract_version
    return _parse_codex_hook(payload)


def _mcp_text(response: object) -> str | None:
    if not isinstance(response, Mapping):
        return None
    is_error = response.get("isError")
    if is_error is not None and is_error is not False:
        return None
    structured = response.get("structuredContent")
    if structured is not None and not (isinstance(structured, Mapping) and len(structured) == 0):
        return None
    content = response.get("content")
    if not isinstance(content, list) or len(content) != 1:
        return None
    block = content[0]
    if not isinstance(block, Mapping) or block.get("type") != "text":
        return None
    text = block.get("text")
    return text if isinstance(text, str) else None


def decode_observed_text(event: PostToolUseEvent) -> ObservedText | None:
    """Return only response text whose model-facing bytes are unambiguous."""

    if event.tool_name == "apply_patch" or event.tool_name in _HTSAVE_MCP_TOOLS:
        return None
    if event.tool_name == "Bash":
        if not isinstance(event.tool_response, str):
            return None
        text = event.tool_response
        codec = "codex-bash-text-v1"
    elif event.tool_name.startswith("mcp__"):
        text = _mcp_text(event.tool_response)
        if text is None:
            return None
        codec = "mcp-call-result-single-text-v1"
    else:
        return None

    if not isinstance(event.tool_input, Mapping) or any(
        not isinstance(key, str) for key in event.tool_input
    ):
        return None
    arguments_hash = canonical_arguments_hash(event.tool_input)
    fingerprint = sha256_id(
        (f"codex-observer-source-v1\0{event.tool_name}\0{arguments_hash}").encode()
    )
    return ObservedText(
        text=text,
        source_fingerprint=fingerprint,
        safe_label=f"{event.tool_name} text result",
        codec=codec,
    )


def _engine(
    registry: Registry,
    session_id: str,
    model: str,
    state_root: Path | None,
) -> ContextEngine:
    paths = build_state_paths(session_id, state_root)
    return ContextEngine(
        ContentAddressedStore(paths.objects),
        registry,
        TokenEstimator(model),
    )


def _registry(event: CommonHookEvent, state_root: Path | None) -> Registry:
    paths = build_state_paths(event.session_id, state_root)
    return Registry(paths.database, paths.session_key)


def _session_start(
    event: SessionStartEvent,
    *,
    state_root: Path | None,
    compatible: bool,
) -> dict[str, object]:
    with _registry(event, state_root) as registry:
        source_generation = registry.active_generation()
        if event.source != "compact" or source_generation is None or not compatible:
            registry.begin_generation(event.source)
            return {}

        engine = _engine(registry, event.session_id, event.model, state_root)
        targets = registry.recovery_targets(source_generation)
        if not targets:
            targets = registry.prepare_compaction()
        verified: list[tuple[RecoveryTarget, bytes, str]] = []
        for target in targets:
            try:
                content = engine.hydrate_bytes(target.object_hash)
                verified.append((target, content, render_full_frame(content)))
            except Exception:
                # Corrupt recovery state must not prevent the Codex generation
                # transition or guess at content.
                continue
        generation = registry.begin_generation(event.source)
        frames = tuple(frame for _, _, frame in verified)
        if not frames:
            return {}
        for target, content, _ in verified:
            try:
                text = content.decode("utf-8")
                engine.decide(
                    text=text,
                    source_fingerprint=sha256_id(
                        f"compact-recovery\0{target.source_fingerprint}".encode()
                    ),
                    safe_label="compact recovery",
                    tool_use_id=f"compact-recovery-{target.recovery_id}-{generation}",
                    force_full=True,
                )
                registry.mark_recovery_consumed(target.recovery_id, generation)
            except Exception:
                # The verified FULL frame remains safe to inject. Without a
                # durable receipt, later reads simply stay FULL.
                continue
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n\n".join(frames),
            }
        }


def _pre_tool_use(
    event: PreToolUseEvent,
    *,
    state_root: Path | None,
    mcp_tool_injection: bool,
) -> dict[str, object]:
    if event.tool_name in _HTSAVE_MCP_TOOLS and not mcp_tool_injection:
        return {}
    with _registry(event, state_root) as registry:
        if event.agent_id is not None:
            registry.subagent_started(event.agent_id, event.agent_type or "unknown")
            return {}
        registry.confirm_pending()
        if event.tool_name not in _HTSAVE_MCP_TOOLS:
            return {}
        if not isinstance(event.tool_input, Mapping) or any(
            not isinstance(key, str) for key in event.tool_input
        ):
            return {}
        token = issue_session_capability(
            registry,
            turn_id=event.turn_id,
            tool_use_id=event.tool_use_id,
            tool_name=event.tool_name,
            arguments=event.tool_input,
            model=event.model,
            cwd=event.cwd,
        )
        updated_input = dict(event.tool_input)
        updated_input["_htsave_context"] = {"token": token}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
                "updatedInput": updated_input,
            }
        }


def _post_tool_use(event: PostToolUseEvent, *, state_root: Path | None) -> None:
    observed = decode_observed_text(event)
    if observed is None and event.agent_id is None:
        return
    with _registry(event, state_root) as registry:
        if event.agent_id is not None:
            registry.subagent_started(event.agent_id, event.agent_type or "unknown")
        if observed is None:
            return
        engine = _engine(registry, event.session_id, event.model, state_root)
        engine.decide(
            text=observed.text,
            source_fingerprint=observed.source_fingerprint,
            safe_label=observed.safe_label,
            tool_use_id=event.tool_use_id,
            allow_transform=False,
            bypass_reason="posttool-observer-only",
        )


def _dispatch_compatible(
    event: HookEvent,
    *,
    state_root: Path | None,
    mcp_tool_injection: bool,
) -> dict[str, object]:
    if isinstance(event, PreToolUseEvent):
        return _pre_tool_use(
            event,
            state_root=state_root,
            mcp_tool_injection=mcp_tool_injection,
        )
    if isinstance(event, PostToolUseEvent):
        _post_tool_use(event, state_root=state_root)
        return {}
    if isinstance(event, CompactEvent):
        if event.hook_event_name == "PreCompact":
            with _registry(event, state_root) as registry:
                registry.prepare_compaction()
        return {}
    if isinstance(event, SubagentStartEvent):
        with _registry(event, state_root) as registry:
            registry.subagent_started(event.agent_id, event.agent_type)
        return {}
    if isinstance(event, SubagentStopEvent):
        with _registry(event, state_root) as registry:
            removed, remaining = registry.finish_subagent(event.agent_id)
            if removed and remaining == 0:
                registry.begin_generation("subagent-stop", preserve_agents=True)
        return {}
    if isinstance(event, StopEvent):
        with _registry(event, state_root) as registry:
            registry.confirm_pending()
        return {}
    raise CompatibilityError("unsupported compatible hook event")


def _resolve_compatibility(
    codex_version: str | object | None,
    compatibility_probe: Callable[[], CodexCompatibility],
) -> CodexCompatibility:
    if codex_version is _VERSION_UNSET:
        return compatibility_probe()
    if not isinstance(codex_version, str):
        return CodexCompatibility(None, False, "unknown-codex-version-output")
    return codex_compatibility_for_version(codex_version)


def dispatch_hook(
    payload: object,
    *,
    state_root: Path | None = None,
    codex_version: str | object | None = _VERSION_UNSET,
    compatibility_probe: Callable[[], CodexCompatibility] = probe_codex_compatibility,
) -> dict[str, object]:
    """Process one event.  Every adapter error degrades to the empty response."""

    try:
        event = parse_hook_event(payload)
        compatibility = _resolve_compatibility(codex_version, compatibility_probe)
        if isinstance(event, SessionStartEvent):
            compatible = event.source == "compact" and compatibility.supported
            return _session_start(
                event,
                state_root=state_root,
                compatible=compatible,
            )

        if not compatibility.supported:
            return {}
        return _dispatch_compatible(
            event,
            state_root=state_root,
            mcp_tool_injection=compatibility.mcp_tool_injection,
        )
    except Exception:
        _mark_ambiguous_on_parse_failure(payload, state_root)
        return {}


def _mark_ambiguous_on_parse_failure(payload: object, state_root: Path | None) -> None:
    """Best-effort FULL-only state when Codex signals an unidentifiable subagent."""

    try:
        if not isinstance(payload, Mapping):
            return
        event_name = payload.get("hook_event_name")
        has_agent_signal = event_name == "SubagentStart" or (
            event_name in {"PreToolUse", "PostToolUse"}
            and ("agent_id" in payload or "agent_type" in payload)
        )
        if not has_agent_signal:
            return
        session_id = payload.get("session_id")
        turn_id = payload.get("turn_id")
        if not isinstance(session_id, str) or not session_id:
            return
        identity_seed = turn_id if isinstance(turn_id, str) and turn_id else "missing-turn-id"
        identity_material = f"{event_name}\0{identity_seed}".encode()
        sentinel = f"unknown-{sha256_id(identity_material)[7:]}"
        paths = build_state_paths(session_id, state_root)
        with Registry(paths.database, paths.session_key) as registry:
            registry.subagent_started(sentinel, "unknown")
    except Exception:
        return


def _reject_json_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number is not supported: {value}")


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    dispatcher: Callable[[object], Mapping[str, object]] = dispatch_hook,
) -> int:
    """Read one JSON value, write one JSON object, and always exit successfully."""

    del argv  # The hook protocol has no command-line arguments.
    input_stream = sys.stdin if stdin is None else stdin
    output_stream = sys.stdout if stdout is None else stdout
    response: Mapping[str, object] = {}
    try:
        payload = json.loads(
            input_stream.read(),
            parse_constant=_reject_json_constant,
        )
        candidate = dispatcher(payload)
        if isinstance(candidate, Mapping):
            response = candidate
    except Exception:
        response = {}
    try:
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except Exception:
        encoded = "{}"
    try:
        output_stream.write(encoded + "\n")
        output_stream.flush()
    except Exception:
        # A closed stdout cannot affect Codex's fail-open exit status.
        pass
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
