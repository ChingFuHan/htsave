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

### Claude Code transparent path gate — met

- `probe_claude_compatibility` reports `posttool_result_replacement` for the
  installed build, and the adapter emits
  `hookSpecificOutput.updatedToolOutput` only when it does.
- A repeated result inside one live session is delivered as REF or verified
  DELTA in the original response shape; `htsave hydrate` returns the observed
  bytes exactly.
- `htsave claude install` is idempotent, tags every entry it owns, preserves
  every foreign hook handler in `settings.json`, and `uninstall` previews unless
  `--yes` and never deletes session data.
- Verified live against Claude Code 2.1.237: three identical `cat` calls
  produced 28290 bytes, then two 131-byte REF frames; 3 events, 1 FULL, 2 REF,
  13468 of 20397 estimated tokens saved.
- One real paired benchmark pair (`large_readme_exact`, 256 payload lines)
  measured 95057 baseline against 45848 treatment billed input tokens, a 51.8%
  reduction against the 30% threshold, at a cost of $1.23.

The remaining 38 slots are unrun by operator choice, so the median-across-five
-pairs gate is **not** yet satisfied for any scenario. Finish it with:

```bash
htsave benchmark run --output DIR --resume --confirm-paid-runs
```

At the observed rate a full 40-slot run costs roughly $25.

### Codex MCP path gate — acceptable now

The explicit `htsave_read` and `htsave_hydrate` tools, reached through
PreToolUse `updatedInput` capability injection.

- `probe_codex_compatibility` reports `mcp_tool_injection` for the installed
  version, and `htsave benchmark run --path mcp` refuses a paid run when it does
  not.
- The MCP fixtures instruct the model to read every file through `htsave_read`
  and to call `htsave_hydrate` for any returned reference.
- Every attempt runs against a throwaway `CODEX_HOME` holding only the rendered
  htsave hooks, so both arms see one identical hook set and no operator hooks.
  Credentials are copied in by the real spawn alone and removed with the
  attempt.

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

Claude Code reports usage per assistant turn under `usage.iterations`, and its
top-level counters describe only the final turn, so `parse_claude_exec_json`
sums every iteration. Cache reads are counted as billed input and reported
separately; they are never subtracted. Claude Code has no `--output-schema`, so
the final message is parsed out of prose or a code fence before being compared
exactly against the fixture oracle.

`--max-executions N` runs at most N slots and stops; `--resume` finishes the
rest. Spending the full 40 slots to prove wiring that one pair can prove is
waste, and the first bounded run caught a real answer-extraction failure that
would otherwise have burned every slot.

For each of four scenarios, run at least five adjacent baseline/treatment pairs
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
fake-Codex integration suite must pass first, and the contract preflight for the
selected path must pass before any Codex process is spawned.
