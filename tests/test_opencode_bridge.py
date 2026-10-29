"""Execute the generated JavaScript and real Python adapter, without an LLM.

The driver follows OpenCode v1.18.23 hook input/output shapes. No test confirms
registry receipts; all acknowledgements must travel through the plugin.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from htsave.opencode_install import install
from htsave.paths import build_state_paths
from htsave.registry import Registry

DRIVER = r'''
import { pathToFileURL } from "node:url";
import { execFileSync } from "node:child_process";
const factory = (await import(pathToFileURL(process.argv[1]))).default;
let hooks = await factory({directory: process.cwd()});
const config = {};
await hooks.config(config);
const messages = new Map();
const original = Array.from({length: 240},
  (_, i) => `line ${i}: exact UTF-8 雪 value ${i}\n`).join("");
const changed = original.replace("line 18:", "line 18 edited:")
  .replace("line 79:", "line 79 edited:").replace("line 160:", "line 160 edited:");
async function model(id = "session-a", alter) {
  const output = {messages: [{info: {sessionID: id, role: "assistant"},
    parts: messages.get(id) ?? []}]};
  if (alter) alter(output);
  await hooks["experimental.chat.messages.transform"]({}, output);
  const system = {system: []};
  await hooks["experimental.chat.system.transform"]({sessionID: id}, system);
  await hooks["chat.params"]({sessionID: id}, {});
  return system.system;
}
async function call(callID, text = original, id = "session-a", extra = {}) {
  const input = {sessionID: id, callID, tool: "bash", args: {command: "cat fixture.txt"}};
  await hooks["tool.execute.before"](input, {args: input.args});
  const output = {output: text, title: "title", metadata: {exit: 0}, ...extra};
  await hooks["tool.execute.after"](input, output);
  const parts = messages.get(id) ?? [];
  parts.push({type: "tool", callID, state: {status: "completed", output: output.output,
    metadata: output.metadata, attachments: output.attachments, time: {}}});
  messages.set(id, parts);
  return output;
}
async function hydrate(callID, ref, id = "session-a") {
  const args = {ref};
  const input = {sessionID: id, callID, tool: "htsave_htsave_hydrate", args};
  await hooks["tool.execute.before"](input, {args});
  const text = execFileSync(process.env.HTSAVE_TEST_PYTHON, ["-c",
    "import json,sys; from htsave.mcp_server import hydrate_session_text; " +
    "print(json.dumps(hydrate_session_text(**json.load(sys.stdin))))"],
    {input: JSON.stringify(args), encoding: "utf8"});
  const result = {content: [{type: "text", text: JSON.parse(text)}]};
  await hooks["tool.execute.after"](input, result);
  return result.content[0].text;
}
'''


def bridge(tmp_path: Path, program: str) -> dict:
    node = shutil.which("node")
    assert node, "BLOCKED: Node is required for the generated OpenCode bridge acceptance gate"
    install(project_dir=tmp_path)
    environment = {
        **os.environ,
        "HTSAVE_STATE_DIR": str(tmp_path / "state"),
        "HTSAVE_TEST_PYTHON": sys.executable,
    }
    environment.pop("HTSAVE_BENCH_ARM", None)
    result = subprocess.run(
        [node, "--input-type=module", "-e", DRIVER + program,
         str(tmp_path / ".opencode/plugins/htsave.js")],
        cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=40,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_full_ref_delta_and_exact_hydration(tmp_path: Path) -> None:
    result = bridge(tmp_path, r'''
await model();
const first = await call("one");
await model();
const second = await call("two");
await model();
const third = await call("three", changed);
await model();
const ref = third.output.match(/target=(sha256:[a-f0-9]+)/)[1];
const hydrated = await hydrate("four", ref);
console.log(JSON.stringify({original, changed, first, second, third, hydrated}));
''')
    assert result["first"]["output"] == result["original"]
    assert "HTSAVE/1 REF" in result["second"]["output"]
    assert "HTSAVE/1 DELTA" in result["third"]["output"]
    assert result["hydrated"] == result["changed"]
    assert result["second"]["metadata"] == {"exit": 0}
    assert result["second"]["title"] == "title"


def test_parallel_batch_and_replayed_call_do_not_confirm_new_receipts(tmp_path: Path) -> None:
    result = bridge(tmp_path, r'''
await model();
const first = await call("one");
const parallel = await call("two");
const replay = await call("one");
await model();
const next = await call("three");
await hooks.dispose();
console.log(JSON.stringify({original, first, parallel, replay, next}));
''')
    assert result["first"]["output"] == result["parallel"]["output"] == result["original"]
    assert result["replay"]["output"] == result["original"]
    assert "HTSAVE/1 REF" in result["next"]["output"]
    paths = build_state_paths("session-a", tmp_path / "state")
    with Registry(paths.database, paths.session_key) as registry:
        assert len(registry.pending_receipts()) == 1
        assert registry.stats().events == 3


@pytest.mark.parametrize("change", ["prune", "compact", "restart", "alter"])
def test_context_loss_invalidates_possession(tmp_path: Path, change: str) -> None:
    actions = {
        "prune": 'messages.get("session-a")[0].state.time.compacted = Date.now();',
        "alter": 'messages.get("session-a")[0].state.output = "truncated";',
        "compact": '''const context = {context: []};
await hooks["experimental.session.compacting"]({sessionID: "session-a"}, context);
if (!context.context.some(x => x.includes(original))) throw Error("missing compact recovery");''',
        "restart": 'hooks = await factory({directory: process.cwd()}); await hooks.config({});',
    }
    result = bridge(tmp_path, r'''
await model(); await call("one"); await model(); await call("two");
''' + actions[change] + r'''
const system = await model();
const after = await call("three");
console.log(JSON.stringify({original, after, system}));
''')
    assert result["after"]["output"] == result["original"]
    assert any(result["original"] in entry for entry in result["system"])


def test_multiple_sessions_and_unseen_truncated_outputs(tmp_path: Path) -> None:
    result = bridge(tmp_path, r'''
await model(); await call("one");
messages.get("session-a")[0].state.output = "host truncated";
await model(); const unseen = await call("two");
await model("session-b"); const isolated = await call("one", original, "session-b");
console.log(JSON.stringify({original, unseen, isolated}));
''')
    assert result["unseen"]["output"] == result["original"]
    assert result["isolated"]["output"] == result["original"]


def test_ambiguous_results_remain_untouched(tmp_path: Path) -> None:
    result = bridge(tmp_path, r'''
await model(); await call("one"); await model();
const image = await call("two", original, "session-a", {attachments: [{mime: "image/png"}]});
const error = await call("three", original, "session-a", {isError: true});
const truncated = await call("four", original, "session-a", {metadata: {truncated: true}});
const failed = await call("five", original, "session-a", {metadata: {exit: 1}});
console.log(JSON.stringify({original, image, error, truncated, failed}));
''')
    assert all(
        result[k]["output"] == result["original"] for k in ("image", "error", "truncated", "failed")
    )


def test_foreign_mcp_is_preserved_and_disables_bridge(tmp_path: Path) -> None:
    result = bridge(tmp_path, r'''
hooks = await factory({directory: process.cwd()});
const foreign = {mcp: {htsave: {type: "remote", url: "https://example.invalid"}}};
await hooks.config(foreign); await model(); await call("one"); await model();
const after = await call("two");
const foreignArgs = {_htsave_context: {token: "foreign authority"}};
await hooks["tool.execute.before"](
  {tool: "htsave_htsave_read", sessionID: "session-a", callID: "foreign"}, {args: foreignArgs});
console.log(JSON.stringify({foreign, original, after, foreignArgs}));
''')
    assert result["foreign"]["mcp"]["htsave"]["url"] == "https://example.invalid"
    assert result["after"]["output"] == result["original"]
    assert result["foreignArgs"] == {"_htsave_context": {"token": "foreign authority"}}


@pytest.mark.parametrize("failure", ["invalid-json", "invalid-shape", "timeout"])
def test_subprocess_failure_disables_transforms_without_confirming(tmp_path: Path, failure: str):
    failing_call = {
        "invalid-json": '() => "not-json"',
        "invalid-shape": '() => JSON.stringify({generation: 1, context: {}})',
        "timeout": '(file, args, options) => originalExec(file, '
        '["-c", "import time; time.sleep(6)"], options)',
    }[failure]
    result = bridge(tmp_path, r'''
await model(); await call("one"); await model();
const cp = (await import("node:child_process")).default;
const {syncBuiltinESMExports} = await import("node:module");
const originalExec = cp.execFileSync;
''' + f"cp.execFileSync = {failing_call};\n" + r'''
syncBuiltinESMExports();
const failed = await call("two");
cp.execFileSync = originalExec;
syncBuiltinESMExports();
await model();
const later = await call("three");
console.log(JSON.stringify({original, failed, later}));
''')
    assert result["failed"]["output"] == result["later"]["output"] == result["original"]
    paths = build_state_paths("session-a", tmp_path / "state")
    with Registry(paths.database, paths.session_key) as registry:
        assert len(registry.pending_receipts()) == 1
        assert registry.stats().events == 1


def test_later_plugin_context_edit_is_observed_before_confirmation(tmp_path: Path):
    result = bridge(tmp_path, r'''
await model(); await call("one");
const output = {messages: [{info: {sessionID: "session-a"}, parts: messages.get("session-a")}]};
await hooks["experimental.chat.messages.transform"]({}, output);
// Another plugin changes the same host-owned array after our hook returns.
output.messages[0].parts[0].state.output = "other plugin removed full text";
await hooks["experimental.chat.system.transform"]({sessionID: "session-a"}, {system: []});
await hooks["chat.params"]({sessionID: "session-a"}, {});
const next = await call("two");
console.log(JSON.stringify({original, next}));
''')
    assert result["next"]["output"] == result["original"]


def test_missing_call_id_cannot_transform_or_inject_authority(tmp_path: Path):
    result = bridge(tmp_path, r'''
await model(); await call("one"); await model();
const next = await call(undefined);
console.log(JSON.stringify({original, next}));
''')
    assert result["next"]["output"] == result["original"]


def test_overlapping_plugin_instances_reject_stale_generation(tmp_path: Path):
    result = bridge(tmp_path, r'''
await model(); await call("one"); await model();
const previous = hooks;
hooks = await factory({directory: process.cwd()}); await hooks.config({});
await model(); await call("two");
hooks = previous;
const stale = await call("three");
console.log(JSON.stringify({original, stale}));
''')
    assert result["stale"]["output"] == result["original"]
