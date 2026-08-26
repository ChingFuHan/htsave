"""Lifecycle hook adapter for Antigravity CLI (agy).

agy hook contract (from ~/.gemini/config/hooks.json):
- PreToolUse:     receives toolCall, can return {decision, overwrite}
- PostToolUse:    receives stepIdx + error, must return {}
- PreInvocation:  receives invocationNum, can return {injectSteps}
- Stop:           receives terminationReason, can return {decision: "continue"}

Key differences from Claude Code / Codex:
- No SessionStart -> lazy init on first PreToolUse
- No PreCompact -> generation stays open (no compact recovery)
- No SubagentStart/Stop -> no subagent bypass mode
- PostToolUse -> observer only, no output replacement

Token saving: MCP-only path (htsave_read / htsave_hydrate).
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .capabilities import issue_session_capability
from .paths import build_state_paths
from .registry import Registry

_HTSAVE_READ = "mcp__htsave__htsave_read"
_HTSAVE_HYDRATE = "mcp__htsave__htsave_hydrate"
PRE_INVOCATION_REMINDER = (
    "htsave: an HTSAVE/1 REF frame certifies byte-exact identity with content "
    "already delivered in this conversation. Never call htsave_hydrate to "
    "re-view a target whose FULL text was already delivered; hydrate only "
    "when a required fact was never delivered."
)


def _read_stdin() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def _write_stdout(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.flush()


def _detect_event(payload: dict[str, Any]) -> str:
    """Infer which agy lifecycle event triggered this hook."""
    if "invocationNum" in payload or "initialNumSteps" in payload:
        return "PreInvocation"
    if "toolCall" in payload or "tool_call" in payload:
        if any(key in payload for key in ("toolResult", "tool_result", "result", "error")):
            return "PostToolUse"
        return "PreToolUse"
    if "stepIdx" in payload and "toolCall" not in payload:
        return "PostToolUse"
    if "terminationReason" in payload:
        return "Stop"
    return "unknown"


def _handle_pre_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    """Allow tools and inject one-use context for htsave MCP calls."""

    tool_call = payload.get("toolCall") or payload.get("tool_call")
    if not isinstance(tool_call, Mapping):
        return {"decision": "allow"}
    call_name = tool_call.get("name")
    raw_args = tool_call.get("args") or tool_call.get("arguments")
    if not isinstance(raw_args, Mapping):
        return {"decision": "allow"}

    nested_key: str | None = None
    if call_name in {"call_mcp_tool", "mcp_tool"}:
        server = raw_args.get("ServerName", raw_args.get("serverName"))
        tool_name = raw_args.get("ToolName", raw_args.get("toolName"))
        if server != "htsave":
            return {"decision": "allow"}
        nested_key = "Arguments" if "Arguments" in raw_args else "arguments"
        public_arguments = raw_args.get(nested_key)
        capability_tool = {
            "htsave_read": _HTSAVE_READ,
            "htsave_hydrate": _HTSAVE_HYDRATE,
        }.get(tool_name)
    elif call_name in {_HTSAVE_READ, _HTSAVE_HYDRATE}:
        public_arguments = raw_args
        capability_tool = call_name
    else:
        return {"decision": "allow"}

    if capability_tool is None or not isinstance(public_arguments, Mapping):
        return {"decision": "allow"}
    session_id = _first_string(payload, "conversationId", "conversation_id", "sessionId")
    if session_id is None:
        return {"decision": "allow"}
    step = payload.get("stepIdx", payload.get("step_index", "unknown"))
    turn_id = _first_string(payload, "turnId", "turn_id") or f"agy-step-{step}"
    tool_use_id = _first_string(tool_call, "id", "toolCallId", "tool_use_id")
    if tool_use_id is None:
        tool_use_id = f"agy-tool-{step}"
    model = _first_string(payload, "modelName", "model") or ""
    cwd = _workspace_path(payload) or os.getcwd()
    state_root_value = os.environ.get("HTSAVE_STATE_DIR")
    state_root = Path(state_root_value) if state_root_value else None
    try:
        paths = build_state_paths(session_id, state_root)
        with Registry(paths.database, paths.session_key) as registry:
            registry.confirm_pending()
            token = issue_session_capability(
                registry,
                turn_id=turn_id,
                tool_use_id=tool_use_id,
                tool_name=capability_tool,
                arguments=dict(public_arguments),
                model=model,
                cwd=cwd,
            )
    except Exception:
        return {"decision": "allow"}

    updated_arguments = dict(public_arguments)
    updated_arguments["_htsave_context"] = {"token": token}
    if nested_key is None:
        return {"decision": "allow", "overwrite": updated_arguments}
    return {"decision": "allow", "overwrite": {nested_key: updated_arguments}}


def _first_string(value: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        candidate = value.get(name)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _workspace_path(payload: Mapping[str, Any]) -> str | None:
    paths = payload.get("workspacePaths") or payload.get("workspace_paths")
    if isinstance(paths, list):
        for path in paths:
            if isinstance(path, str) and path:
                return path
    return (
        _first_string(payload, "cwd", "workspace")
        or os.environ.get("HTSAVE_BENCH_WORKSPACE")
        or os.getcwd()
    )


def _handle_post_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    """Handle PostToolUse: observer mode, ingest text for CAS."""
    # agy PostToolUse cannot replace output, so always return {}.
    return {}


def _handle_pre_invocation(payload: dict[str, Any]) -> dict[str, Any]:
    """Inject one ephemeral anti-rehydration reminder before the model runs.

    REF frames already certify byte-exact identity; hydrating a previously
    delivered FULL target re-enters its whole content into model context for
    no informational gain.  Both benchmark arms receive identical hooks, so
    this reminder is symmetric and fair.
    """

    return {"injectSteps": [{"ephemeralMessage": PRE_INVOCATION_REMINDER}]}


def _handle_stop(payload: dict[str, Any]) -> dict[str, Any]:
    """Confirm pending receipts at session end; the response stays empty."""

    session_id = _first_string(payload, "conversationId", "conversation_id", "sessionId")
    if session_id is None:
        return {}
    state_root_value = os.environ.get("HTSAVE_STATE_DIR")
    state_root = Path(state_root_value) if state_root_value else None
    try:
        paths = build_state_paths(session_id, state_root)
        with Registry(paths.database, paths.session_key) as registry:
            registry.confirm_pending()
    except Exception:
        # Fail open: an unconfirmable registry must never block agy shutdown.
        return {}
    return {}


def main() -> None:
    try:
        payload = _read_stdin()
        event = _detect_event(payload)

        if event == "PreToolUse":
            result = _handle_pre_tool_use(payload)
        elif event == "PostToolUse":
            result = _handle_post_tool_use(payload)
        elif event == "PreInvocation":
            result = _handle_pre_invocation(payload)
        elif event == "Stop":
            result = _handle_stop(payload)
        else:
            result = {}

        _write_stdout(result)
    except Exception:
        # Fail open: return empty object so agy proceeds normally.
        _write_stdout({})


if __name__ == "__main__":
    main()
