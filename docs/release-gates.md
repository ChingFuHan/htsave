# htsave v1 release gates

The package may be built and tested while a gate is red, but it must not be
labelled a completed v1 release until every gate below is green.

This file separates release-gated evidence from local smoke checks. The
recorded live results and forensic explanations below are historical and have
not been independently reverified in this checkout. On 2026-09-14, only the
three paths enumerated in the [local artifact check](../verify.md#local-artifact-check-2026-09-14)
were checked and found absent. Other historical artifact paths were not checked.
Local tests do not recompute these percentages or establish live savings.

## Deterministic correctness

- Python 3.11, 3.12, 3.13, and 3.14 test matrix passes on Linux, macOS, and Windows.
- Installing on either host leaves the operator's existing configuration intact:
  no foreign hook handler, MCP server, or unrelated setting is added, reordered,
  or removed.
- Unit, property, integration, concurrency, crash-recovery, migration, and
  hook-contract suites pass.
- Every stored target restores byte-for-byte (`100%`, no normalization).
- Wrong base, corrupted target, missing CAS object, newer registry schema, and
  incompatible Codex contract all take their specified safe path.
- Installation, status, and removal are idempotent; uninstall never deletes
  session data, and destructive data commands require `--yes`/`--apply`.
- POSIX directories/files are `0700`/`0600`; Windows state is restricted to the
  current user.
- No telemetry, network storage, automatic GC, transcript access, or
  cross-session reuse occurs.

## Codex contract, common to both paths

- Plugin manifest, default `hooks/hooks.json`, skill, and direct-map MCP config
  validate against the installed Codex CLI contract.
- Hook fixtures prove startup/resume/clear/compact generations, later-event
  receipt confirmation, pending compact recovery, subagent fail-open behavior,
  unparseable-version fail-open behavior, and original PostToolUse preservation.
  A parseable version enables a contract attempt; event schema validation is
  separate, accepts additive fields, and rejects malformed required fields.
- Workspace reads accept relative or absolute paths only after canonical
  containment is proven; symlink escapes, directories, special files, invalid
  UTF-8, invalid ranges, and cross-session hydrate attempts are rejected.

htsave has two delivery paths and four host adapters. Release gates currently
cover Claude Code, Codex CLI, and agy; OpenCode remains experimental with local
contract checks only. The paths are gated separately, because Codex CLI
supports one of them today and not the other; Claude Code supports both.
`htsave doctor` reports every combination.

### Claude Code transparent path gate — historical pass; not reverified

- `probe_claude_compatibility` reports `posttool_result_replacement` for the
  installed build, and the adapter emits
  `hookSpecificOutput.updatedToolOutput` only when it does.
- A repeated result inside one live session is delivered as REF or verified
  DELTA in the original response shape; `htsave hydrate` returns the observed
  bytes exactly.
- `htsave claude install` installs the Claude Code plugin idempotently, removes
  only htsave's legacy `settings.json` entries while preserving every foreign
  hook handler, and `uninstall` previews unless `--yes` and never deletes
  session data.
- Historically verified live (not reverified here) against Claude Code 2.1.238 across all 4 scenarios (80/80 runs,
  10 pairs each). All attempts passed the deterministic oracle. Measured median
  input reductions were 94.91%, 93.22%, 93.43%, and 92.40%; all four met the
  30% savings gate. The implementation contract and empirical release gate
  historically passed; neither is reverified here. (Manifest: `/tmp/htsave-live-claude-v6/manifest.json`)
- Full manifest paths, arithmetic, and the savings decision are recorded
  in [verify.md](../verify.md).

### Codex MCP path gate — historical pass; not reverified

The explicit `htsave_read` and `htsave_hydrate` tools, reached through
PreToolUse `updatedInput` capability injection.

- `probe_codex_compatibility` reports `mcp_tool_injection` when the installed
  Codex version is parseable, and the event parser remains the final
  compatibility boundary. `htsave benchmark run --path mcp` validates the
  supported contract.
- Plugin `install`/`status` also runs the configured stdio interpreter through a
  read-only `import htsave.mcp_server` check, reporting runtime drift instead
  of claiming a healthy integration when MCP startup cannot import the server.
- Historically verified live (not reverified here) across all 4 scenarios (80/80 runs, 10 pairs each): all 4
  scenarios passed with 30.43% ~ 41.69% median pairwise input reduction against
  `gpt-5.6-luna` (Low), satisfying the release gate. `luna` is the local model
  selection for this run; no official public pricing claim is made.
- The MCP fixtures instruct the model to read every file through `htsave_read`
  and to call `htsave_hydrate` only when exact bytes from a returned reference
  are needed.
- Every attempt runs against a throwaway `CODEX_HOME` holding only the rendered
  htsave hooks, so both arms see one identical hook set and no operator hooks.
  Credentials are copied in by the real spawn alone and removed with the
  attempt.

### Antigravity agy integration gate — historically installed; savings gate red

- The historical `htsave --json agy status` check reported `state: installed`, with hooks, MCP, and
  skill registered and no drift.
- Historically verified live (not reverified here) against agy 1.1.17 across all 4 scenarios (80/80 runs, 10 pairs
  each). All attempts passed the deterministic oracle. Measured median input
  reductions were −18.69%, 26.07%, −0.78%, and 1.96%; none met the 30% savings
  gate. This is a valid red empirical result, not an execution failure.
  (Manifest: `/tmp/htsave-live-agy-v2/manifest.json`)
- No official model price is inferred. `gemini-3.7-flash-low` is only the local
  model selection for this run.
- **Historical early interpretation — superseded by the post-v2 forensics below;
  not a current causal finding:** htsave correctly
  delivered REF frames on rounds 2–4 of each execution; in this v2 run the agy
  model called `htsave_hydrate` once per session. When that call was made, the
  full content re-entered the model context and reduced the byte savings for
  that delivery. The red result is attributable to two compounding factors.
  First, this hydration behavior in the v2 treatment reduced the benefit of
  compact frames. Second, on Gemini models,
  server-side KV caching additionally favours baseline arms whose repetitive
  full-file content produces more cache hits (lower billed `input_tokens`),
  while treatment arms sending compact REF tokens break the repetitive prefix
  pattern and receive fewer cache hits. These two effects together explain the
  red result; neither alone fully accounts for it.
- **Follow-up experiments:**
  - `gemini-3.1-pro-low` (80/80, `/tmp/htsave-agy-pro-v1/manifest.json`):
    Median reductions were 12.69%, 48.44%, 39.90%, and 3.31%. Two of four
    scenarios passed (source\_three\_line\_delta and repeated\_test\_output).
    The overall gate failed because large\_readme\_exact (12.69%) and
    multi\_round\_context (3.31%) did not reach 30%. The earlier hydration
    attribution is withdrawn: the later historical forensic record says Pro
    never called `htsave_hydrate` in either run. Its cause is not independently
    reverified here.
  - `claude-sonnet-4-6` via agy (28/80, quota-limited;
    `/tmp/htsave-agy-claude-probe/manifest.json`): The 10 completed pairs of
    large\_readme\_exact show a median reduction of 2.27%. The early hydration
    explanation was a historical interpretation, superseded by the later
    forensics; this partial result alone does not establish causality.
    The remaining 52 executions were blocked by a 137-hour Anthropic API quota
    lock and could not be completed. The partial manifest is retained as-is.
  - `benchmark_runner._agy_argv` was patched to omit `--effort` for
    non-Gemini models, which reject that flag. Test coverage added in
    `tests/test_benchmark_agy.py:test_agy_manifest_omits_effort_for_non_gemini_model`.

### Antigravity agy post-v2 forensics and hardening

The complete reverse-engineered agy hook/tool contract, including the
undocumented `SessionStart` event, injected-step billing semantics, and the
proto-level proof that `PostToolHookResult` has no fields, is documented in
[agy-contract.md](agy-contract.md).

**Engineering hardening shipped after v2:**

- `agy_hooks._handle_stop` now confirms pending receipts at session end;
  previously it returned `{}` while its docstring claimed confirmation, so a
  session's last REF/DELTA receipt stayed pending forever.
- `SessionStart` support: the event fires on agy 1.1.20 even though it is
  undocumented, so htsave now begins a real generation at session start
  (lazy init remains the fallback for older builds) instead of on the first
  tool call.
- The benchmark-only `workspace-*` glob fallback was removed from
  `_workspace_path`. Workspace resolution is now payload-first
  (`workspacePaths`), then `HTSAVE_BENCH_WORKSPACE`, then `cwd`.
- Anti-rehydration discipline: the agy skill forbids hydrating a target whose
  FULL text was already delivered, and a `PreInvocation` hook injects one
  ephemeral reminder before every model call. Both benchmark arms install
  identical hooks, so the reminder stays symmetric and fair. Live effect:
  treatment sessions went from one `htsave_hydrate` call each (v2) to zero
  (v3).

**Post-hoc forensics of the red gate (per-turn `events.jsonl` decomposition of
historical manifests; historical conclusions, not independently reverified):**

- agy never re-bills full content in either arm. Large tool results spill to
  brain files and only short summaries enter model context, and Gemini
  server-side KV caching absorbs what repetition remains. Turn-level usage is
  nearly identical across arms except where cache hits differ.
- The gate metric (uncached `input_tokens`, cached reported separately and
  never subtracted) is therefore dominated by which arm draws KV-cache
  misses. Pair-level results swing far beyond any transport effect: flash-low
  `large_readme_exact` pairs range from −54.21% to +42.74%; pro-low pairs
  include an arm with 1,261,556 cached tokens against 126,221 on the other.
- Counterfactual removal of every hydrate call moves flash-low scenario
  medians by only +3 to +10 points (for example −18.69% → −15.81% on
  `large_readme_exact`) — material, but structurally unable to reach 30%.
- A historical illustrative weighted-token view (not official pricing) (`input + 0.25 × cached`) is also negative
  (median −32.76% on `large_readme_exact`), under that assumed weighting. The historical interpretation was architectural: agy's
  spill-plus-KV-cache design already deduplicates repeated content natively,
  leaving htsave's explicit MCP path nothing measurable to save under this
  gate.

**Historical v3 reruns on agy 1.1.20 (both 80/80 after quota-resumed
retries; not reverified):** flash-low used zero hydrate calls and its `large_readme_exact`
median improved by +14.92 points (−18.69% → −3.77%) while the other scenarios moved within
noise; all four flash medians stayed red. The pro-low rerun landed at
7.02%, 43.78% (scenario pass), 10.52%, and 11.29% — overall red with wider
pair-level swings. Pro reportedly never hydrated in either run; removing
hydration cannot explain its change. The KV-cache attribution remains a
historical forensic conclusion, not independently reverified here. Full tables, quota-interruption handling, and method are
in [verify.md](../verify.md). The agy savings gate remains red; the structural
explanation is historical. agy is treated as an experimental, no-claims
integration until agy exposes a transparent replacement contract.


### OpenCode experimental integration — no release savings gate

- The local integration targets **OpenCode 1.18.23**. The installer manages
  `htsave.js` and the native htsave skill, upgrades owned legacy `.mjs` files,
  detects same-version content/Python drift, and rolls back partial writes.
  Foreign files and same-name MCP entries are never overwritten.
- The previous implementation used an incompatible `Plugin.define` API and a
  `.mjs` file that the host did not auto-discover. Its missing tool-use IDs,
  ordinary-tool receipt-confirmation gap, and manually confirmed unit-test
  sequence were historical defects, not evidence of a working bridge.
- The replacement uses `tool.execute.before` / `tool.execute.after`, native
  `sessionID` / `callID`, and local-MCP configuration. It checks final structured
  model-input text before freezing a generation-scoped receipt snapshot.
  A later tool-before confirms only that snapshot; parallel results and Stop /
  dispose never confirm one another. Missing or stale authority fails open.
- `tests/test_opencode_bridge.py` executes the generated JavaScript and actual
  Python adapter. `tests/test_opencode_local_host.py` runs the real 1.18.23 CLI
  with an isolated home/workspace/state and a local fake model, without account
  credentials. Native and MCP paths both exercise FULL, REF, three-line DELTA,
  exact hydrate, and native skill discovery. No sequence test manually confirms
  registry receipts.
- Context pruning, compaction and restart invalidate prior possession. Exact
  transformed targets remain recoverable through session-owned CAS. Host output
  truncation is not proof of full delivery; truncated content cannot authorize
  references. Large hydrate responses remain subject to the host's output limits.
- The required local gate fails explicitly if Node or OpenCode 1.18.23 is absent.
  `status` establishes file/Python installation health only. Passing the local
  gate is not a real-model benchmark, a savings claim, or evidence for other
  OpenCode versions. OpenCode remains experimental and outside release savings
  gates. No paid run was performed for this correction.

Pinned contract source: upstream tag `v1.18.23`, commit
`ef2880f379129aa048be9e9353e30aa168d42c17`:

- [Plugin signatures](https://github.com/anomalyco/opencode/blob/ef2880f379129aa048be9e9353e30aa168d42c17/packages/plugin/src/index.ts)
- [Discovery: .ts and .js](https://github.com/anomalyco/opencode/blob/ef2880f379129aa048be9e9353e30aa168d42c17/packages/opencode/src/config/plugin.ts)
- [Native and MCP hook invocation](https://github.com/anomalyco/opencode/blob/ef2880f379129aa048be9e9353e30aa168d42c17/packages/opencode/src/session/tools.ts)
- [System and model-request ordering](https://github.com/anomalyco/opencode/blob/ef2880f379129aa048be9e9353e30aa168d42c17/packages/opencode/src/session/llm/request.ts)
- [Pruned tool-result conversion](https://github.com/anomalyco/opencode/blob/ef2880f379129aa048be9e9353e30aa168d42c17/packages/opencode/src/session/message-v2.ts)
- [Local MCP configuration](https://github.com/anomalyco/opencode/blob/ef2880f379129aa048be9e9353e30aa168d42c17/packages/core/src/v1/config/mcp.ts)

### Codex transparent PostToolUse path gate — externally blocked

Arbitrary Bash/local/MCP results replaced in place, with no change to how the
model is prompted.

- A supported successful PostToolUse result-replacement contract exists and its
  real local contract test proves transparent FULL/REF/DELTA round trips.

This condition remains **blocked by the current Codex hook contract**: it does
not provide a successful arbitrary-result replacement response. The observer
hook must not use `block`, `continue:false`, or unsupported
`updatedMCPToolOutput` as a substitute, and `htsave benchmark run --path shell`
refuses paid runs until the contract lands. The MCP path gate above is
capability-based and unaffected by this block, as is Claude Code, which already
provides the contract.

## Paired benchmark

The benchmark measures one host and one path at a time, selected with `--host`
and `--path`. All combinations share one schedule, one set of fixtures, and one
set of thresholds.

- `--host claude` (default) measures the transparent `shell` path. The arm is
  the isolated `CLAUDE_CONFIG_DIR`: the treatment config has htsave's hooks
  installed and the baseline config has none, so the operator's own hooks, MCP
  servers, and memory files are out of both arms and the measured difference is
  htsave alone. Its explicit MCP path is refused as redundant.
- `--host codex` measures `mcp` by default; its `shell` path stays defined so
  the transparent path can be measured unchanged the day Codex ships the
  contract.
- `--host agy` measures the explicit MCP path, using an isolated HOME, trust
  workspace, MCP config, skill, and hooks; each execution gets a fresh
  workspace, home, and state.
- `--host opencode` is available for local harness and contract checks. It has
  no recorded live manifest and does not contribute to a release gate.

Usage fields retain each host's native meaning. Codex reports cached input as a
component of `input_tokens`; Claude prefers complete-session
`modelUsage.*.inputTokens` and records cache reads separately, while its older
fallback sums iteration input, cache creation, and cache reads on a distinct
accounting basis; agy reports
`input_tokens` and cache reads separately. Reductions compare only the same
host, model, path, and parser contract, not cross-host costs. Claude Code
reports usage per assistant turn under `usage.iterations`, and its top-level
counters describe only the final turn, so `parse_claude_exec_json` sums every
iteration when the aggregate is unavailable. Claude Code has no `--output-schema`, so
the final message is parsed out of prose or a code fence before being compared
exactly against the fixture oracle. The canonical model used for the recorded
run was `claude-haiku-4-5-20251001`; the old `claude-3-5-haiku` alias was not
available in this account.

`--max-executions N` runs at most N slots and stops; `--resume` finishes the
rest. The full schedule is 40 pairs / 80 executions (4 scenarios × 10 pairs).
Spending all 80 executions to prove wiring that one pair can prove is
waste, and the first bounded run caught a real answer-extraction failure that
would otherwise have burned every slot.

For each of four scenarios, run ten adjacent baseline/treatment pairs
from identical clean fixtures and isolated sessions:

1. large README reread;
2. large source with exactly three changed lines;
3. repeated test output;
4. multi-round AGENTS/config/source context.

Within a host/path, both arms use the same host version, model, supported
reasoning effort, sandbox, approval
policy, plugin/tool descriptions, prompt, initial tree digest, and deterministic
quality oracle. Arm order alternates AB/BA. The only behavior switch is htsave
passthrough versus treatment (Claude uses isolated hook configurations as above):
on the MCP path both arms call `htsave_read`, and
the baseline arm returns the raw file text instead of a reference or delta. Host-native usage output and final answers are retained. For Codex this is
`codex exec --json` JSONL plus the separate schema-constrained last message;
Claude and agy use their own parsers described above. Transcript files are
never read.

Per scenario:

```text
reduction = (baseline input_tokens - treatment input_tokens)
            / baseline input_tokens
median(reduction across 10 pairs) >= 30%
```

All executions must also have valid terminal usage, identical audit behavior,
the expected htsave decision trace, and the same exact deterministic answers.
Cached input tokens are reported but never subtracted from `input_tokens`.
Infrastructure failures are retained and resumed under the same pair/arm id;
they are not silently discarded or replaced.

Paid benchmark runs require explicit `--confirm-paid-runs`. A dry preflight and
fake-host integration suite must pass first, and the contract preflight for the
selected path must pass before any host process is spawned.

## Historical live evidence (not reverified)

The historical records include completed and partial runs:

| Host | Manifest | Executions | Historical report (not reverified) | Median input reduction by scenario |
| :--- | :--- | ---: | :--- | :--- |
| Codex CLI / MCP | `/tmp/htsave-live-codex-luna-v14/manifest.json` | 80/80 | **historical pass** | 41.69%, 36.57%, 39.94%, 30.43% |
| Claude Code / shell | `/tmp/htsave-live-claude-v6/manifest.json` | 80/80 | **historical pass** | 94.91%, 93.22%, 93.43%, 92.40% |
| agy / MCP (`gemini-3.7-flash-low`) | `/tmp/htsave-live-agy-v2/manifest.json` | 80/80 | **failed savings gate** | −18.69%, 26.07%, −0.78%, 1.96% |
| agy / MCP (`gemini-3.1-pro-low`) | `/tmp/htsave-agy-pro-v1/manifest.json` | 80/80 | **failed savings gate** | 12.69%, 48.44%, 39.90%, 3.31% |
| agy / MCP (`claude-sonnet-4-6`) | `/tmp/htsave-agy-claude-probe/manifest.json` | 28/80 (quota-limited) | **partial — directional** | 2.27% (large\_readme\_exact only, 10 pairs) |
| agy / MCP v3 zero-hydrate (`gemini-3.7-flash-low`, 1.1.20) | `/tmp/htsave-agy-v3/manifest.json` | 80/80 | **failed savings gate** | −3.77%, 22.80%, −0.43%, −4.32% |
| agy / MCP v3 zero-hydrate (`gemini-3.1-pro-low`, 1.1.20) | `/tmp/htsave-agy-pro-v2/manifest.json` | 80/80 | **failed savings gate** | 7.02%, 43.78%, 10.52%, 11.29% |

The reports above were historically generated by `htsave benchmark report` from the manifests
above. The exact fractions and all pairwise values remain in the JSON manifests;
the table rounds only for readability. The agy red gate is a valid empirical
result and is not a basis for claiming agy savings. The structural attribution
from the v3 forensics has not been independently reverified. This evidence does
not support a completed v1 release while the agy savings gate remains red.
