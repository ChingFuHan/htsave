"""Fail-open Claude Code lifecycle adapter.

Claude Code provides what Codex CLI does not: ``hookSpecificOutput.updatedToolOutput``
replaces a tool result before it reaches the model, for every tool rather than for
MCP tools alone.  This module is therefore the first adapter able to run htsave's
transparent path, where a repeated tool result is delivered as a reference or a
verified delta with no change to how the operator works.

Like ``htsave.codex_hooks`` this is only a compatibility boundary: the domain
engine knows nothing about hook JSON, and every uncertainty degrades to the empty
response so the original tool output survives untouched.

The payload shape is narrower than Codex's, and the differences are load-bearing:

* there is no ``turn_id``; ``prompt_id`` is the analogue and is itself optional,
  so the tool-use id is the last-resort turn identity;
* ``model`` is frequently absent (it is missing from every ``claude -p`` event),
  so it is recorded on the generation when seen and the token estimator falls
  back to its default encoding when it is not;
* ``permission_mode`` is absent on ``SessionStart``.
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
from .compat import ClaudeCompatibility, probe_claude_compatibility
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
# Tools whose result htsave must never rewrite: its own MCP tools already carry
# frames, and edit-shaped results are not repeated context.
_NEVER_OBSERVED: Final = frozenset({"Edit", "Write", "NotebookEdit", "TodoWrite"})
_MISSING_TURN_ID: Final = "no-prompt-id"


@dataclass(frozen=True, slots=True)
class CommonHookEvent:
    session_id: str
    transcript_path: str | None
    cwd: str
    hook_event_name: str
    model: str | None


@dataclass(frozen=True, slots=True)
class SessionStartEvent(CommonHookEvent):
    source: str


@dataclass(frozen=True, slots=True)
class PreToolUseEvent(CommonHookEvent):
    turn_id: str
    tool_name: str
    tool_use_id: str
    tool_input: object
    agent_id: str | None
    agent_type: str | None


@dataclass(frozen=True, slots=True)
class PostToolUseEvent(CommonHookEvent):
    turn_id: str
    tool_name: str
    tool_use_id: str
    tool_input: object
    tool_response: object
    agent_id: str | None
    agent_type: str | None


@dataclass(frozen=True, slots=True)
class CompactEvent(CommonHookEvent):
    trigger: str


@dataclass(frozen=True, slots=True)
class SubagentStartEvent(CommonHookEvent):
    turn_id: str
    agent_id: str
    agent_type: str


@dataclass(frozen=True, slots=True)
class SubagentStopEvent(CommonHookEvent):
    turn_id: str
    agent_id: str
    agent_type: str


@dataclass(frozen=True, slots=True)
class StopEvent(CommonHookEvent):
    turn_id: str


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
class ObservedResponse:
    """Model-facing text from one tool result, plus how to put it back."""

    text: str
    source_fingerprint: str
    safe_label: str
    codec: str
    rebuild: Callable[[str], object]


def _require_mapping(payload: object, label: str) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or any(not isinstance(key, str) for key in payload):
        raise CompatibilityError(f"{label} must be a string-keyed object")
    return payload


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


def _optional_str(
    value: Mapping[str, Any],
    field: str,
    *,
    allowed: frozenset[str] | None = None,
) -> str | None:
    """Accept an absent or null field, but never a wrong-typed or unknown one."""

    item = value.get(field)
    if item is None:
        return None
    if not isinstance(item, str) or not item:
        raise CompatibilityError(f"{field} must be a non-empty string when present")
    if allowed is not None and item not in allowed:
        raise CompatibilityError(f"unsupported {field}")
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
    return {
        "session_id": _require_str(value, "session_id"),
        "transcript_path": _optional_str(value, "transcript_path"),
        "cwd": _require_str(value, "cwd"),
        "hook_event_name": event_name,
        "model": _optional_str(value, "model"),
    }


def _turn_id(value: Mapping[str, Any]) -> str:
    """Claude Code has no ``turn_id``; ``prompt_id`` is the closest identity."""

    prompt_id = _optional_str(value, "prompt_id")
    if prompt_id is not None:
        return prompt_id
    tool_use_id = _optional_str(value, "tool_use_id")
    return tool_use_id if tool_use_id is not None else _MISSING_TURN_ID


def parse_hook_event(payload: object) -> HookEvent:
    """Parse one Claude Code hook event, accepting only additive unknown fields."""

    value = _require_mapping(payload, "hook input")
    event_name = _require_str(value, "hook_event_name")
    common = _common(value, event_name)
    # Validated for shape even where htsave does not branch on it, so an
    # unrecognized permission surface fails open instead of being ignored.
    _optional_str(value, "permission_mode", allowed=_PERMISSION_MODES)

    if event_name == "SessionStart":
        return SessionStartEvent(
            **common,
            source=_require_str(value, "source", allowed=_SESSION_SOURCES),
        )
    if event_name == "PreToolUse":
        agent_id, agent_type = _optional_agent(value)
        return PreToolUseEvent(
            **common,
            turn_id=_turn_id(value),
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
            turn_id=_turn_id(value),
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
            trigger=_require_str(value, "trigger", allowed=_COMPACT_TRIGGERS),
        )
    if event_name == "SubagentStart":
        return SubagentStartEvent(
            **common,
            turn_id=_turn_id(value),
            agent_id=_require_str(value, "agent_id"),
            agent_type=_require_str(value, "agent_type"),
        )
    if event_name == "SubagentStop":
        return SubagentStopEvent(
            **common,
            turn_id=_turn_id(value),
            agent_id=_require_str(value, "agent_id"),
            agent_type=_require_str(value, "agent_type"),
        )
    if event_name == "Stop":
        return StopEvent(**common, turn_id=_turn_id(value))
    raise CompatibilityError("unsupported Claude Code hook event")


def _bash_response(response: Mapping[str, Any]) -> tuple[str, Callable[[str], object]] | None:
    """Bash delivers ``stdout``; anything that muddies the bytes is left alone."""

    stdout = response.get("stdout")
    stderr = response.get("stderr")
    if not isinstance(stdout, str) or not isinstance(stderr, str):
        return None
    # A non-empty stderr, an interrupt, or an image means the model-facing bytes
    # are not exactly ``stdout``.
    if stderr or response.get("interrupted") is not False or response.get("isImage") is not False:
        return None

    def rebuild(text: str) -> object:
        return {**response, "stdout": text}

    return stdout, rebuild


def _read_response(response: Mapping[str, Any]) -> tuple[str, Callable[[str], object]] | None:
    """Read delivers ``file.content``; its line counts move with the content."""

    if response.get("type") != "text":
        return None
    file = response.get("file")
    if not isinstance(file, Mapping) or any(not isinstance(key, str) for key in file):
        return None
    content = file.get("content")
    if not isinstance(content, str):
        return None

    def rebuild(text: str) -> object:
        return {
            **response,
            "file": {**file, "content": text, "numLines": text.count("\n") + 1},
        }

    return content, rebuild


def _mcp_response(response: Mapping[str, Any]) -> tuple[str, Callable[[str], object]] | None:
    """Only a single, non-error, unstructured text block is unambiguous."""

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
    if not isinstance(text, str):
        return None

    def rebuild(replacement: str) -> object:
        return {**response, "content": [{**block, "text": replacement}]}

    return text, rebuild


def decode_observed_response(event: PostToolUseEvent) -> ObservedResponse | None:
    """Return only response text whose model-facing bytes are unambiguous."""

    if event.tool_name in _NEVER_OBSERVED or event.tool_name in _HTSAVE_MCP_TOOLS:
        return None
    if not isinstance(event.tool_response, Mapping) or any(
        not isinstance(key, str) for key in event.tool_response
    ):
        return None

    if event.tool_name == "Bash":
        decoded = _bash_response(event.tool_response)
        codec = "claude-bash-stdout-v1"
    elif event.tool_name == "Read":
        decoded = _read_response(event.tool_response)
        codec = "claude-read-file-content-v1"
    elif event.tool_name.startswith("mcp__"):
        decoded = _mcp_response(event.tool_response)
        codec = "mcp-call-result-single-text-v1"
    else:
        return None
    if decoded is None:
        return None
    text, rebuild = decoded

    if not isinstance(event.tool_input, Mapping) or any(
        not isinstance(key, str) for key in event.tool_input
    ):
        return None
    arguments_hash = canonical_arguments_hash(event.tool_input)
    fingerprint = sha256_id(
        (f"claude-observer-source-v1\0{event.tool_name}\0{arguments_hash}").encode()
    )
    return ObservedResponse(
        text=text,
        source_fingerprint=fingerprint,
        safe_label=f"{event.tool_name} text result",
        codec=codec,
        rebuild=rebuild,
    )


def _registry(event: CommonHookEvent, state_root: Path | None) -> Registry:
    paths = build_state_paths(event.session_id, state_root)
    return Registry(paths.database, paths.session_key)


def _engine(
    registry: Registry,
    session_id: str,
    model: str | None,
    state_root: Path | None,
) -> ContextEngine:
    paths = build_state_paths(session_id, state_root)
    return ContextEngine(
        ContentAddressedStore(paths.objects),
        registry,
        TokenEstimator(model),
    )


def _session_model(event: CommonHookEvent, registry: Registry) -> str | None:
    """Prefer the event's model, then the one recorded when the generation began."""

    return event.model if event.model is not None else registry.active_model()


def _session_start(
    event: SessionStartEvent,
    *,
    state_root: Path | None,
    compatible: bool,
) -> dict[str, object]:
    with _registry(event, state_root) as registry:
        source_generation = registry.active_generation()
        if event.source != "compact" or source_generation is None or not compatible:
            registry.begin_generation(event.source, model=event.model)
            return {}

        engine = _engine(registry, event.session_id, _session_model(event, registry), state_root)
        targets = registry.recovery_targets(source_generation)
        if not targets:
            targets = registry.prepare_compaction()
        verified: list[tuple[RecoveryTarget, bytes, str]] = []
        for target in targets:
            try:
                content = engine.hydrate_bytes(target.object_hash)
                verified.append((target, content, render_full_frame(content)))
            except Exception:
                # Corrupt recovery state must not block the generation
                # transition or guess at content.
                continue
        generation = registry.begin_generation(event.source, model=event.model)
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
                # The verified FULL frame is still safe to inject. Without a
                # durable receipt, later reads simply stay FULL.
                continue
        return {
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n\n".join(frames),
            }
        }


def _pre_tool_use(event: PreToolUseEvent, *, state_root: Path | None) -> dict[str, object]:
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
            model=_session_model(event, registry) or "",
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


def _post_tool_use(
    event: PostToolUseEvent,
    *,
    state_root: Path | None,
    allow_transform: bool,
) -> dict[str, object]:
    """Ingest the result and, when Claude Code allows it, replace it in place."""

    observed = decode_observed_response(event)
    if observed is None and event.agent_id is None:
        return {}
    with _registry(event, state_root) as registry:
        if event.agent_id is not None:
            registry.subagent_started(event.agent_id, event.agent_type or "unknown")
        if observed is None:
            return {}
        engine = _engine(registry, event.session_id, _session_model(event, registry), state_root)
        decision = engine.decide(
            text=observed.text,
            source_fingerprint=observed.source_fingerprint,
            safe_label=observed.safe_label,
            tool_use_id=event.tool_use_id,
            allow_transform=allow_transform,
            bypass_reason=None if allow_transform else "posttool-observer-only",
        )
        if decision.payload == observed.text:
            # FULL and BYPASS already equal the original bytes; replacing them
            # would only add framing the model does not need.
            return {}
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "updatedToolOutput": observed.rebuild(decision.payload),
            }
        }


def _dispatch_compatible(
    event: HookEvent, *, state_root: Path | None, allow_transform: bool
) -> dict[str, object]:
    if isinstance(event, PreToolUseEvent):
        return _pre_tool_use(event, state_root=state_root)
    if isinstance(event, PostToolUseEvent):
        return _post_tool_use(
            event,
            state_root=state_root,
            allow_transform=allow_transform,
        )
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


def dispatch_hook(
    payload: object,
    *,
    state_root: Path | None = None,
    compatibility: ClaudeCompatibility | None = None,
    compatibility_probe: Callable[[], ClaudeCompatibility] = probe_claude_compatibility,
) -> dict[str, object]:
    """Process one event.  Every adapter error degrades to the empty response."""

    try:
        detected = compatibility if compatibility is not None else compatibility_probe()
        event = parse_hook_event(payload)
        if isinstance(event, SessionStartEvent):
            return _session_start(
                event,
                state_root=state_root,
                compatible=detected.supported,
            )
        if not detected.supported:
            return {}
        return _dispatch_compatible(
            event,
            state_root=state_root,
            allow_transform=detected.posttool_result_replacement,
        )
    except Exception:
        _mark_ambiguous_on_parse_failure(payload, state_root)
        return {}


def _mark_ambiguous_on_parse_failure(payload: object, state_root: Path | None) -> None:
    """Best-effort FULL-only state when an unidentifiable subagent is signalled."""

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
        prompt_id = payload.get("prompt_id")
        if not isinstance(session_id, str) or not session_id:
            return
        seed = prompt_id if isinstance(prompt_id, str) and prompt_id else _MISSING_TURN_ID
        identity_material = f"{event_name}\0{seed}".encode()
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
        payload = json.loads(input_stream.read(), parse_constant=_reject_json_constant)
        candidate = dispatcher(payload)
        if isinstance(candidate, Mapping):
            response = candidate
    except Exception:
        response = {}
    try:
        encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
    except Exception:
        encoded = "{}"
    output_stream.write(encoded)
    output_stream.flush()
    return 0


if __name__ == "__main__":  # pragma: no cover - module entry point
    raise SystemExit(main())
