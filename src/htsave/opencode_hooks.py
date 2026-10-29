"""OpenCode 1.18.23 adapter for the generated project plugin.

The bridge passes host session/call identities and an active generation fence.
Only a snapshot observed in model input and followed by a tool invocation can
confirm delivery. Tool completion and plugin disposal never confirm possession.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .capabilities import canonical_arguments_hash, issue_session_capability
from .cas import ContentAddressedStore
from .engine import ContextEngine
from .hashing import sha256_id
from .models import DeliveryMode
from .paths import build_state_paths
from .registry import Registry
from .tokens import TokenEstimator
from .transport import parse_transport, render_full_frame

_HTSAVE_READ = "mcp__htsave__htsave_read"
_HTSAVE_HYDRATE = "mcp__htsave__htsave_hydrate"
_HTSAVE_MCP_TOOLS = {
    "htsave_htsave_read": _HTSAVE_READ,
    "htsave_htsave_hydrate": _HTSAVE_HYDRATE,
}
SESSION_START_ARG = "session-start"


def _read_stdin() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    return json.loads(raw)


def _write_stdout(payload: dict[str, Any]) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.flush()


def _first_string(value: Mapping[str, Any], *names: str) -> str | None:
    for name in names:
        candidate = value.get(name)
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _workspace_path(payload: Mapping[str, Any]) -> str | None:
    return (
        _first_string(payload, "cwd", "workspace")
        or os.environ.get("HTSAVE_BENCH_WORKSPACE")
        or os.getcwd()
    )


def _paths(payload: Mapping[str, Any]):
    session_id = _first_string(payload, "sessionId")
    if session_id is None:
        raise ValueError("missing session identity")
    state = os.environ.get("HTSAVE_STATE_DIR")
    return build_state_paths(session_id, Path(state) if state else None)


def _generation_matches(registry: Registry, payload: Mapping[str, Any]) -> bool:
    generation = payload.get("generation")
    return (
        isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation == registry.active_generation()
        and not registry.active_is_frozen()
    )


def _engine(paths, registry: Registry) -> ContextEngine:
    return ContextEngine(
        ContentAddressedStore(paths.objects),
        registry,
        TokenEstimator(registry.active_model() or ""),
    )


def _handle_pre_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    tool_name = payload.get("tool")
    raw_input = payload.get("input")
    tool_use_id = _first_string(payload, "toolUseId")
    if not isinstance(tool_name, str) or not isinstance(raw_input, Mapping) or not tool_use_id:
        return {}
    try:
        paths = _paths(payload)
        with Registry(paths.database, paths.session_key) as registry:
            if not _generation_matches(registry, payload):
                return {}
            snapshot = payload.get("receipts", [])
            if not isinstance(snapshot, list):
                return {}
            registry.confirm_receipts(payload["generation"], tuple(snapshot))
            response: dict[str, Any] = {"generation": payload["generation"]}
            if tool_name not in _HTSAVE_MCP_TOOLS:
                return response
            arguments = dict(raw_input)
            # A model-supplied capability is never reused by the bridge.
            arguments.pop("_htsave_context", None)
            token = issue_session_capability(
                registry,
                turn_id=f"opencode-{tool_use_id}",
                tool_use_id=tool_use_id,
                tool_name=_HTSAVE_MCP_TOOLS[tool_name],
                arguments=arguments,
                model=_first_string(payload, "model") or "",
                cwd=_workspace_path(payload) or os.getcwd(),
            )
            if not _generation_matches(registry, payload):
                return {}
            response["input"] = {**arguments, "_htsave_context": {"token": token}}
            return response
    except Exception:
        return {}


def _decode_mcp_result_text(
    result: Mapping[str, Any],
) -> tuple[str, str] | None:
    """Extract unambiguous text from an MCP result and return (text, codec)."""

    if result.get("attachments") or result.get("structuredContent") is not None:
        return None
    metadata = result.get("metadata")
    if isinstance(metadata, Mapping) and (
        metadata.get("truncated")
        or metadata.get("error")
        or ("exit" in metadata and metadata["exit"] != 0)
    ):
        return None
    is_error = result.get("isError")
    if is_error is not None and is_error is not False:
        return None
    content = result.get("content")
    if isinstance(content, list) and len(content) == 1:
        block = content[0]
        if isinstance(block, Mapping) and block.get("type") == "text":
            text = block.get("text")
            if isinstance(text, str):
                return text, "mcp-call-result-single-text-v1"
    if "content" in result:
        return None
    text = result.get("output")
    if isinstance(text, str):
        return text, "opencode-tool-output-v1"
    return None


def _rebuild_mcp_result(result: Mapping[str, Any], replacement: str) -> dict[str, Any]:
    """Rebuild the result object with replacement text, preserving shape."""

    content = result.get("content")
    if isinstance(content, list) and len(content) == 1:
        block = content[0]
        if isinstance(block, Mapping) and block.get("type") == "text":
            return {**result, "content": [{**block, "text": replacement}]}
    if "output" in result:
        return {**result, "output": replacement}
    return dict(result)


def _handle_post_tool_use(payload: dict[str, Any]) -> dict[str, Any]:
    tool = payload.get("tool")
    tool_use_id = _first_string(payload, "toolUseId")
    result = payload.get("result")
    arguments = payload.get("input")
    if (
        payload.get("status") != "completed"
        or not isinstance(tool, str)
        or not tool_use_id
        or not isinstance(result, Mapping)
        or not isinstance(arguments, Mapping)
    ):
        return {}
    decoded = _decode_mcp_result_text(result)
    if decoded is None:
        return {}
    text, _ = decoded
    try:
        paths = _paths(payload)
        with Registry(paths.database, paths.session_key) as registry:
            if not _generation_matches(registry, payload):
                return {}
            response: dict[str, Any] = {"generation": payload["generation"]}
            if tool not in _HTSAVE_MCP_TOOLS:
                fingerprint = sha256_id(
                    f"opencode-observer-source-v1\0{tool}\0{canonical_arguments_hash(arguments)}"
                    .encode()
                )
                decision = _engine(paths, registry).decide(
                    text=text, source_fingerprint=fingerprint,
                    safe_label=f"{tool} text result", tool_use_id=tool_use_id,
                )
                if decision.payload != text:
                    response["result"] = _rebuild_mcp_result(result, decision.payload)
            else:
                event = registry.delivery_event(payload["generation"], tool_use_id)
                if event is None:
                    return response
                if event.mode in {DeliveryMode.FULL, DeliveryMode.BYPASS}:
                    if sha256_id(text.encode("utf-8")) != event.target_hash:
                        return {}
                else:
                    frame = parse_transport(text)
                    if (
                        frame.mode != event.mode
                        or frame.target_hash != event.target_hash
                        or frame.base_hash != event.base_hash
                    ):
                        return {}
                    _engine(paths, registry).hydrate_transport(text)
            if not _generation_matches(registry, payload):
                return {}
            for receipt in registry.pending_receipts():
                if receipt.tool_use_id == tool_use_id:
                    response["receipt"] = receipt.receipt_id
                    break
            return response
    except Exception:
        return {}


def _handle_session_start(payload: dict[str, Any]) -> dict[str, Any]:
    """Rotate on bridge start or loss of context; retain exact pending recovery."""
    try:
        paths = _paths(payload)
        with Registry(paths.database, paths.session_key) as registry:
            engine = _engine(paths, registry)
            recovery = list(engine.pending_recovery_context())
            generation = registry.active_generation()
            for call_id in payload.get("recover", []):
                if not isinstance(call_id, str) or generation is None:
                    continue
                event = registry.delivery_event(generation, call_id)
                if event and event.mode.value in {"ref", "delta"}:
                    recovery.append(render_full_frame(engine.hydrate_bytes(event.target_hash)))
            generation = registry.begin_generation("opencode-context-reset")
            return {"generation": generation, "context": list(dict.fromkeys(recovery))}
    except Exception:
        return {}


def _handle_compact(payload: dict[str, Any]) -> dict[str, Any]:
    # The pending recovery is read before rotation, never treated as possession.
    return _handle_session_start(payload)


def _handle_stop(payload: dict[str, Any]) -> dict[str, Any]:
    """Disposal is not proof of model consumption."""
    return {}


def main(argv: list[str] | None = None) -> None:
    try:
        arguments = list(sys.argv[1:] if argv is None else argv)
        payload = _read_stdin()
        event = arguments[0] if arguments else "unknown"

        if event == SESSION_START_ARG:
            result = _handle_session_start(payload)
        elif event == "pre-tool-use":
            result = _handle_pre_tool_use(payload)
        elif event == "post-tool-use":
            result = _handle_post_tool_use(payload)
        elif event == "compact":
            result = _handle_compact(payload)
        elif event == "stop":
            result = _handle_stop(payload)
        else:
            result = {}

        _write_stdout(result)
    except Exception:
        _write_stdout({})


if __name__ == "__main__":
    main()
