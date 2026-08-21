# htsave v1 release gates

The package may be built and tested while a gate is red, but it must not be
labelled a completed v1 release until every gate below is green.

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
  unknown-version fail-open behavior, and original PostToolUse preservation.
- Workspace reads reject absolute/relative escapes, symlink escapes,
  directories, special files, invalid UTF-8, invalid ranges, and cross-session
  hydrate attempts.

htsave has two delivery paths and two hosts. The paths are gated separately,
because Codex CLI supports one of them today and not the other; Claude Code
supports both. `htsave doctor` reports every combination.

### Claude Code transparent path gate — contract met; live savings gate passed

- `probe_claude_compatibility` reports `posttool_result_replacement` for the
  installed build, and the adapter emits
  `hookSpecificOutput.updatedToolOutput` only when it does.
- A repeated result inside one live session is delivered as REF or verified
  DELTA in the original response shape; `htsave hydrate` returns the observed
  bytes exactly.
- `htsave claude install` is idempotent, tags every entry it owns, preserves
  every foreign hook handler in `settings.json`, and `uninstall` previews unless
  `--yes` and never deletes session data.
- Verified live against Claude Code 2.1.238 across all 4 scenarios (80/80 runs,
  10 pairs each). All attempts passed the deterministic oracle. Measured median
  input reductions were 94.91%, 93.22%, 93.43%, and 92.40%; all four met the
  30% savings gate. The implementation contract is met and this empirical release
  gate passed. (Manifest: `/tmp/htsave-live-claude-v6/manifest.json`)
- Full manifest paths, arithmetic, and the savings decision are recorded
  in [verify.md](../verify.md).

### Codex MCP path gate — met

The explicit `htsave_read` and `htsave_hydrate` tools, reached through
PreToolUse `updatedInput` capability injection.

- `probe_codex_compatibility` reports `mcp_tool_injection` for the installed
  version, and `htsave benchmark run --path mcp` validates the supported contract.
- Verified live across all 4 scenarios (80/80 runs, 10 pairs each): all 4
  scenarios passed with 30.43% ~ 41.69% median pairwise input reduction against
  `gpt-5.6-luna` (Low), satisfying the release gate. `luna` is the local model
  selection for this run; no official public pricing claim is made.
- The MCP fixtures instruct the model to read every file through `htsave_read`
  and to call `htsave_hydrate` for any returned reference.
- Every attempt runs against a throwaway `CODEX_HOME` holding only the rendered
  htsave hooks, so both arms see one identical hook set and no operator hooks.
  Credentials are copied in by the real spawn alone and removed with the
  attempt.

### Antigravity agy integration gate — installed; live savings gate red

- `htsave --json agy status` reports `state: installed`, with hooks, MCP, and
  skill registered and no drift.
- Verified live against agy 1.1.17 across all 4 scenarios (80/80 runs, 10 pairs
  each). All attempts passed the deterministic oracle. Measured median input
  reductions were −18.69%, 26.07%, −0.78%, and 1.96%; none met the 30% savings
  gate. This is a valid red empirical result, not an execution failure.
  (Manifest: `/tmp/htsave-live-agy-v2/manifest.json`)
- No official model price is inferred. `gemini-3.7-flash-low` is only the local
  model selection for this run.
- **Root cause (post-hoc analysis of events.jsonl):** htsave correctly
  delivered REF frames on rounds 2–4 of each execution; the agy model followed
  the skill instructions and called `htsave_hydrate` only once per session.
  The red result is attributable to Gemini's server-side KV cache competing
  with htsave deduplication on the `input_tokens` metric: baseline arms sending
  repetitive full file content receive more cache hits (higher
  `cached_input_tokens`, lower billed `input_tokens`), while treatment arms
  sending compact REF tokens break the repetitive prefix pattern and receive
  fewer cache hits. htsave correctly reduces bytes transmitted, but Gemini's
  own caching handles repetition more effectively for billing purposes. This
  is a limitation of measuring billed `input_tokens` on a host with
  aggressive server-side KV caching, not a defect in htsave's implementation.


### Codex transparent PostToolUse path gate — externally blocked

Arbitrary Bash/local/MCP results replaced in place, with no change to how the
model is prompted.

- A supported successful PostToolUse result-replacement contract exists and its
  real local contract test proves transparent FULL/REF/DELTA round trips.

This condition is **blocked by Codex CLI 0.148.0 and 0.149.0-alpha.4**: neither
provides a successful arbitrary-result replacement response. The observer hook
must not use `block`, `continue:false`, or unsupported `updatedMCPToolOutput` as
a substitute, and `htsave benchmark run --path shell` refuses paid runs until
the contract lands. The MCP path gate above is unaffected by this block, and
so is Claude Code, which already provides the contract.

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

Claude Code reports usage per assistant turn under `usage.iterations`, and its
top-level counters describe only the final turn, so `parse_claude_exec_json`
sums every iteration. Cache reads are counted as billed input and reported
separately; they are never subtracted. Claude Code has no `--output-schema`, so
the final message is parsed out of prose or a code fence before being compared
exactly against the fixture oracle. The canonical model used for the recorded
run was `claude-haiku-4-5-20251001`; the old `claude-3-5-haiku` alias was not
available in this account.

`--max-executions N` runs at most N slots and stops; `--resume` finishes the
rest. Spending the full 40 slots to prove wiring that one pair can prove is
waste, and the first bounded run caught a real answer-extraction failure that
would otherwise have burned every slot.

For each of four scenarios, run ten adjacent baseline/treatment pairs
from identical clean fixtures and isolated sessions:

1. large README reread;
2. large source with exactly three changed lines;
3. repeated test output;
4. multi-round AGENTS/config/source context.

Both arms use the same Codex version, model, reasoning effort, sandbox, approval
policy, plugin/tool descriptions, prompt, initial tree digest, and deterministic
quality oracle. Arm order alternates AB/BA. The only behavior switch is htsave
passthrough versus treatment: on the MCP path both arms call `htsave_read`, and
the baseline arm returns the raw file text instead of a reference or delta. Raw
`codex exec --json` JSONL and the separate schema-constrained last message are
retained; transcript files are never read.

Per scenario:

```text
reduction = (baseline input_tokens - treatment input_tokens)
            / baseline input_tokens
median(reduction across >=5 pairs) >= 30%
```

All executions must also have valid terminal usage, identical audit behavior,
the expected htsave decision trace, and the same exact deterministic answers.
Cached input tokens are reported but never subtracted from `input_tokens`.
Infrastructure failures are retained and resumed under the same pair/arm id;
they are not silently discarded or replaced.

Paid benchmark runs require explicit `--confirm-paid-runs`. A dry preflight and
fake-host integration suite must pass first, and the contract preflight for the
selected path must pass before any host process is spawned.

## Recorded live evidence

The completed manifests used for the release decision are:

| Host | Manifest | Executions | Report | Median input reduction by scenario |
| :--- | :--- | ---: | :--- | :--- |
| Codex CLI / MCP | `/tmp/htsave-live-codex-luna-v14/manifest.json` | 80/80 | **passed** | 41.69%, 36.57%, 39.94%, 30.43% |
| Claude Code / shell | `/tmp/htsave-live-claude-v6/manifest.json` | 80/80 | **passed** | 94.91%, 93.22%, 93.43%, 92.40% |
| agy / MCP | `/tmp/htsave-live-agy-v2/manifest.json` | 80/80 | **failed savings gate** | −18.69%, 26.07%, −0.78%, 1.96% |

The reports above are generated by `htsave benchmark report` from the manifests
above. The exact fractions and all pairwise values remain in the JSON manifests;
the table rounds only for readability. The agy red gate is a valid empirical
result and is not a basis for claiming agy savings. This evidence does not
support a completed v1 release while the agy savings gate remains red.
