"""Required OpenCode 1.18.23 acceptance gate with a loopback-only fake model.

No account credentials are inherited and no paid provider is configured. Missing
host binaries fail this gate explicitly rather than turning it into a green skip.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from htsave.opencode_install import install
from htsave.tokens import tiktoken_cache_dir


@pytest.mark.parametrize("delivery", ["native", "mcp"])
def test_real_host_full_ref_delta_hydrate_and_skill(tmp_path: Path, delivery: str) -> None:
    executable = shutil.which("opencode")
    assert executable, "BLOCKED: OpenCode 1.18.23 is required for local host acceptance"
    version = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, timeout=15, check=True,
    ).stdout.strip()
    assert version == "1.18.23", f"BLOCKED: expected OpenCode 1.18.23, found {version}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original = "".join(f"line {i}: exact UTF-8 雪 value {i}\n" for i in range(240))
    changed = original.replace("line 18:", "line 18 edited:").replace(
        "line 79:", "line 79 edited:",
    ).replace("line 160:", "line 160 edited:")
    fixture = workspace / "fixture.txt"
    fixture.write_text(original)
    install(project_dir=workspace)
    requests: list[dict] = []
    errors: list[str] = []
    tool_outputs: dict[int, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(body)
            try:
                tools = {t["function"]["name"] for t in body.get("tools", [])}
                history = [m for m in body["messages"] if m["role"] == "tool"]
                stage = len(history)
                call = None
                if tools:
                    assert {"htsave_htsave_read", "htsave_htsave_hydrate", "skill"} <= tools
                    if history:
                        tool_outputs[stage - 1] = history[-1]["content"]
                    if stage < 3:
                        if stage == 2:
                            fixture.write_text(changed)
                        name = "bash" if delivery == "native" else "htsave_htsave_read"
                        arguments = (
                            {"command": "cat fixture.txt", "description": "Read fixture"}
                            if delivery == "native" else {"path": "fixture.txt"}
                        )
                    elif stage == 3:
                        ref = re.search(r"target=(sha256:[a-f0-9]+)", history[-1]["content"])
                        assert ref, history[-1]["content"][:500]
                        name, arguments = "htsave_htsave_hydrate", {"ref": ref.group(1)}
                    elif stage == 4:
                        name, arguments = "skill", {"name": "htsave"}
                    if stage < 5:
                        call = {"id": f"local-call-{stage}", "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)}}
                content = "local fixture complete" if tools else "Local fixture"
                delta = {"role": "assistant", "content": None if call else content}
                if call:
                    delta["tool_calls"] = [{"index": 0, **call}]
                finish = "tool_calls" if call else "stop"
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                for part, reason in [(delta, None), ({}, finish)]:
                    chunk = {"id": "fixture-response", "object": "chat.completion.chunk",
                             "created": 1, "model": "fixture",
                             "choices": [{"index": 0, "delta": part, "finish_reason": reason}]}
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            except Exception as exc:
                errors.append(repr(exc))
                self.send_error(400, "local fixture failed")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    home = tmp_path / "isolated-home"
    home.mkdir()
    config = {
        "enabled_providers": ["local"],
        "model": "local/fixture", "small_model": "local/fixture",
        "provider": {"local": {
            "npm": "@ai-sdk/openai-compatible", "name": "Local fixture",
            "options": {"baseURL": f"http://127.0.0.1:{server.server_port}/v1", "apiKey": "dummy"},
            "models": {"fixture": {"name": "Fixture", "tool_call": True,
                                   "limit": {"context": 128000, "output": 2048}}},
        }},
        "permission": "allow", "share": "disabled",
        "compaction": {"auto": False, "prune": False},
    }
    environment = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home), "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_DATA_HOME": str(home / "data"), "XDG_CACHE_HOME": str(home / "cache"),
        "XDG_STATE_HOME": str(home / "state"),
        "HTSAVE_STATE_DIR": str(tmp_path / "htsave-state"),
        "TIKTOKEN_CACHE_DIR": tiktoken_cache_dir(),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "OPENCODE_CONFIG_CONTENT": json.dumps(config),
        "OPENCODE_DISABLE_DEFAULT_PLUGINS": "true",
        "OPENCODE_DISABLE_MODELS_FETCH": "true",
        "OPENCODE_DISABLE_AUTOUPDATE": "true",
        "OPENCODE_DISABLE_SHARE": "true",
    }
    try:
        result = subprocess.run(
            [executable, "run", "--format", "json", "--model", "local/fixture",
             "--title", "Local htsave acceptance", "Run the local fixture sequence."],
            cwd=workspace, env=environment, capture_output=True, text=True, timeout=90,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    (tmp_path / "requests.json").write_text(json.dumps(requests, ensure_ascii=False, indent=2))
    (tmp_path / "host.stdout").write_text(result.stdout)
    (tmp_path / "host.stderr").write_text(result.stderr)
    assert result.returncode == 0 and not errors, (
        errors, result.stdout[-3000:], result.stderr[-3000:],
    )
    assert len(tool_outputs) == 5, (tool_outputs, result.stdout[-3000:], result.stderr[-3000:])
    assert tool_outputs[0] == original
    assert "HTSAVE/1 REF" in tool_outputs[1]
    assert "HTSAVE/1 DELTA" in tool_outputs[2]
    assert tool_outputs[3] == changed
    assert "Hydration and context changes" in tool_outputs[4]
