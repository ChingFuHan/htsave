"""Explicit, workspace-contained htsave MCP tools for Codex CLI."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anyio
from mcp import types
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server

from . import __version__
from .capabilities import consume_session_capability
from .errors import HtsaveError, SecurityBoundaryError
from .hashing import sha256_id
from .models import DeliveryMode
from .paths import default_state_root
from .workspace import WorkspaceReader

READ_TOOL = "htsave_read"
HYDRATE_TOOL = "htsave_hydrate"
HOOK_READ_TOOL = "mcp__htsave__htsave_read"
HOOK_HYDRATE_TOOL = "mcp__htsave__htsave_hydrate"
DELIVERY_PAD_ENV = "HTSAVE_DELIVERY_PAD_BYTES"


def _delivery_pad(payload: str) -> str:
    """Append a deterministic carrier for hosts that spill large tool results.

    agy truncates tool results beyond a small internal threshold into a local
    brain file and shows the model only a bounded preview. Empirically a tiny
    inline REF or DELTA frame prevents the provider's implicit prefix cache
    from engaging on later turns, which costs far more uncached input than the
    compressed frame saves; a multi-line carrier pushes the delivery over the
    threshold so repeated deliveries present the same stable, repeating
    preview shape as any other large tool result. The carrier is written to
    the local brain file and never reaches the API context in full; stored
    objects and hydration are unaffected because only the delivered payload
    gains the trailing lines. Disabled unless the environment sets a positive
    byte target.
    """

    raw = os.environ.get(DELIVERY_PAD_ENV)
    if not raw:
        return payload
    try:
        target = int(raw)
    except ValueError:
        return payload
    if target <= 0:
        return payload
    used = len(payload.encode("utf-8"))
    if used >= target:
        return payload
    lines: list[str] = []
    index = 0
    while used < target:
        line = f"PADLINE {index:04d} yyyyyyyyyyyyyyyyyyyyyyyyyyyyyy"
        lines.append(line)
        used += len(line) + 1
        index += 1
    return payload + "\n" + "\n".join(lines) + "\n"

_CONTEXT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "token": {
            "type": "string",
            "pattern": r"^v1\.[0-9a-f]{64}\.[A-Za-z0-9_-]{43}$",
        }
    },
    "required": ["token"],
    "additionalProperties": False,
}


def _public_read_arguments(
    path: str, start_line: int | None, end_line: int | None
) -> dict[str, Any]:
    arguments: dict[str, Any] = {"path": path}
    if start_line is not None:
        arguments["start_line"] = start_line
    if end_line is not None:
        arguments["end_line"] = end_line
    return arguments


def read_workspace_text(
    *,
    path: str,
    start_line: int | None = None,
    end_line: int | None = None,
    _htsave_context: object = None,
    state_root: Path | None = None,
    fallback_workspace: Path | None = None,
) -> str:
    """Read text; invalid/missing session metadata safely degrades to raw FULL."""

    public_arguments = _public_read_arguments(path, start_line, end_line)
    root = state_root or default_state_root()
    workspace = fallback_workspace or Path.cwd()
    # The benchmark's baseline arm is deliberately a raw passthrough. Keep
    # the legacy ``passthrough`` spelling as an explicit operator override.
    if _htsave_context is None or os.environ.get("HTSAVE_BENCH_ARM") in {
        "baseline",
        "passthrough",
    }:
        return WorkspaceReader(workspace).read(path, start_line=start_line, end_line=end_line).text

    trusted_context = False
    try:
        context_manager = consume_session_capability(
            _htsave_context,
            tool_name=HOOK_READ_TOOL,
            arguments=public_arguments,
            state_root=root,
        )
        with context_manager as session:
            trusted_context = True
            result = WorkspaceReader(Path(session.record.cwd)).read(
                path, start_line=start_line, end_line=end_line
            )
            source_fingerprint = sha256_id(f"{READ_TOOL}\0{session.record.arguments_hash}".encode())
            try:
                decision = session.engine.decide(
                    text=result.text,
                    source_fingerprint=source_fingerprint,
                    safe_label=result.safe_label,
                    tool_use_id=session.record.tool_use_id,
                )
            except (HtsaveError, OSError, sqlite3.Error):
                return result.text
            if decision.mode is DeliveryMode.REF or decision.mode is DeliveryMode.DELTA:
                return _delivery_pad(decision.payload)
            return decision.payload
    except (HtsaveError, OSError, sqlite3.Error):
        if trusted_context:
            raise
        # Read remains useful if hook metadata is missing, expired, or corrupt,
        # but the fallback workspace boundary is still enforced below.
        return WorkspaceReader(workspace).read(path, start_line=start_line, end_line=end_line).text


def hydrate_session_text(
    *,
    ref: str,
    _htsave_context: object,
    state_root: Path | None = None,
) -> str:
    """Return a registered target in full; session/security failures fail closed."""

    public_arguments = {"ref": ref}
    with consume_session_capability(
        _htsave_context,
        tool_name=HOOK_HYDRATE_TOOL,
        arguments=public_arguments,
        state_root=state_root or default_state_root(),
    ) as session:
        text = session.engine.hydrate_text(ref)
        decision = session.engine.decide(
            text=text,
            source_fingerprint=sha256_id(f"{HYDRATE_TOOL}\0{ref}".encode()),
            safe_label=ref,
            tool_use_id=session.record.tool_use_id,
            force_full=True,
        )
        if decision.mode not in {DeliveryMode.FULL, DeliveryMode.BYPASS}:
            raise SecurityBoundaryError("hydrate did not produce a full target")
        return decision.payload


def tool_definitions() -> list[types.Tool]:
    annotations = types.ToolAnnotations(
        readOnlyHint=True,
        destructiveHint=False,
        idempotentHint=True,
        openWorldHint=False,
    )
    return [
        types.Tool(
            name=READ_TOOL,
            title="Read workspace text with lossless context reuse",
            description=(
                "Read a UTF-8 regular file inside the active workspace. htsave may "
                "return FULL, REF, or verified DELTA; use htsave_hydrate when exact "
                "bytes are needed from a reference. Session metadata is injected by Codex."
            ),
            annotations=annotations,
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "minLength": 1},
                    "start_line": {"type": "integer", "minimum": 1},
                    "end_line": {"type": "integer", "minimum": 1},
                    "_htsave_context": _CONTEXT_SCHEMA,
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        ),
        types.Tool(
            name=HYDRATE_TOOL,
            title="Hydrate an htsave reference",
            description=(
                "Return the byte-exact UTF-8 target for an htsave SHA-256 reference "
                "owned by the active Codex session."
            ),
            annotations=annotations,
            inputSchema={
                "type": "object",
                "properties": {
                    "ref": {
                        "type": "string",
                        "pattern": r"^sha256:[0-9a-f]{64}$",
                    },
                    "_htsave_context": _CONTEXT_SCHEMA,
                },
                "required": ["ref", "_htsave_context"],
                "additionalProperties": False,
            },
        ),
    ]


server = Server("htsave")


@server.list_tools()
async def _list_tools() -> list[types.Tool]:
    return tool_definitions()


@server.call_tool()
async def _call_tool(name: str, arguments: Mapping[str, Any]) -> types.CallToolResult:
    try:
        if name == READ_TOOL:
            text = read_workspace_text(
                path=arguments["path"],
                start_line=arguments.get("start_line"),
                end_line=arguments.get("end_line"),
                _htsave_context=arguments.get("_htsave_context"),
            )
        elif name == HYDRATE_TOOL:
            text = hydrate_session_text(
                ref=arguments["ref"],
                _htsave_context=arguments.get("_htsave_context"),
            )
        else:
            raise SecurityBoundaryError("unknown htsave MCP tool")
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)])
    except (HtsaveError, OSError, KeyError, TypeError, ValueError) as exc:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"htsave: {exc}")],
            isError=True,
        )


async def _run() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="htsave",
                server_version=__version__,
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def main() -> None:
    anyio.run(_run)


if __name__ == "__main__":  # pragma: no cover - stdio entry point
    main()
