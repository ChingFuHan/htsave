# htsave verification record

This is the verification record for the 2026-08-21 release benchmark plan
(the plan file itself is retained only in git history),
updated by a relay agent on 2026-08-21 after the Claude v6 and agy v2 manifests
were completed. It records the observed result, including red gates. It is not
an assertion that the package is a completed v1 release: the release policy in
`docs/release-gates.md` requires every gate to be green.

This record keeps historical live measurements separate from the current
checkout. All live results and forensic conclusions below are historical and
have not been independently reverified during this documentation correction.
OpenCode has local integration tests but no live manifest or savings result.

## OpenCode runtime correction (2026-09-14)

This later runtime change supersedes the OpenCode limitations and source-state
digest recorded by the earlier documentation-only audit below. It does not
replace or reverify any historical live-model measurement.

The installed host was OpenCode **1.18.23**. Its pinned upstream contract uses
an async plugin function, `tool.execute.before` / `tool.execute.after`,
`sessionID` / `callID`, and local MCP configuration. Its plugin discovery scans
`.js` and `.ts`; the old generated `.mjs` and `Plugin.define` bridge were not a
working integration. The corrected installer generates `htsave.js`, installs a
native skill, migrates only owned legacy files, preserves foreign files/config,
and detects content and Python-runtime drift. Partial writes are rolled back.
The exact upstream commit and source links are in the
[OpenCode release evidence](docs/release-gates.md#opencode-experimental-integration--no-release-savings-gate).

The bridge observes completed, unpruned model-input text, freezes matching
receipt IDs before the model request, and acknowledges only those IDs at a
later tool invocation in the same generation. Same-batch parallel results and
disposal cannot acknowledge delivery. Context loss rotates the generation and
preserves exact recovery from CAS. MCP tool names are mapped to existing
capability names; the public MCP API and SQLite schema are unchanged.

Fresh local checks executed for this change:

```bash
rtk proxy .venv/bin/python -m pytest -q
# 320 passed in 155.04s

rtk proxy .venv/bin/python -m pytest -q tests/test_opencode_hooks.py tests/test_opencode_install.py
# 35 passed in 21.56s

rtk proxy .venv/bin/python -m pytest -q tests/test_opencode_bridge.py tests/test_opencode_local_host.py
# 17 passed in 71.97s

rtk proxy .venv/bin/ruff check src tests
# All checks passed!

rtk proxy uv build
# Built dist/htsave-1.0.0.tar.gz and dist/htsave-1.0.0-py3-none-any.whl

rtk git diff --check
# clean
```

The full-suite run preceded the final malformed-envelope shape validation and
its additional test. The final 17-test run rechecked the entire affected
JavaScript bridge and both real-host paths after that hardening. The focused
35-test run checked the final Python hook/installer behavior. These overlapping
counts are not additive and are not a claim that a final 321-test full-suite
run occurred.

The actual OpenCode CLI ran with isolated HOME/XDG directories, workspace and
htsave state, no inherited account credentials, and a loopback fake model. Both
native Bash and explicit MCP sequences delivered exact FULL, REF, a verified
three-line DELTA, exact hydrated target bytes, and the native htsave skill to
the fake model. Requests and host stdout/stderr are written into pytest's
temporary test directories by the reproducible acceptance test. This is local
host-contract evidence, not real-model usage, savings, or pricing evidence.

The generated-JavaScript tests also cover same-batch calls, replay, missing call
IDs, separate sessions, overlapping bridge instances, restart, compact/prune,
context edits by a later plugin, ambiguous/error results, subprocess timeout,
invalid JSON, malformed reply fields and foreign MCP configuration. Python
tests cover selective generation-bound confirmation, corrupted CAS, cross-session
hydrate rejection, ownership, legacy migration, Python drift and write rollback.
Sequence tests never manually confirm registry receipts.

Final SHA-256 over sorted `src/**/*.py`, `tests/**/*.py`, and all files under
`plugin/` (each relative path concatenated with its file bytes):
`d796e5fcb5a79aa37b9cf6195abf67c01092550fc7c1d4e587d70c86bf95ce22`.
The final wheel's runtime modules, bridge and skill matched the checkout bytes.
Relative document links and heading anchors passed checks; existing historical
numeric table rows were preserved. Preexisting benchmark, CLI and compatibility
source/test changes were preserved byte-for-byte against the pre-edit snapshot.

No paid benchmark, release publication, version bump, global host installation,
or database migration was performed. OpenCode remains experimental; other host
versions and real-model savings are unverified. Host output limits still apply
to large hydrate responses. agy's savings gate remains red and Codex transparent
replacement remains externally blocked.

## Local artifact check (2026-09-14)

A local `Path.is_file()` check returned `False` for exactly these three paths:

- `/tmp/htsave-live-codex-luna-v14/manifest.json`
- `/tmp/htsave-live-claude-v6/manifest.json`
- `/tmp/htsave-live-agy-v2/manifest.json`

Other historical artifact paths were not checked. None of the live measurements
were recomputed, and no paid benchmark was run for this correction.

## Scope and invariants

The paired benchmark uses four deterministic scenarios and ten adjacent
baseline/treatment pairs per scenario. Each attempt gets a new workspace,
isolated host home, initial tree digest, prompt, schema, and answer oracle. The
runner records the host's terminal usage and validates the exact answer object.
For the Codex MCP path it also requires the ordered `htsave_read` trace. The
gate is:

```text
pairwise reduction = (baseline input_tokens - treatment input_tokens)
                     / baseline input_tokens
scenario gate = median(10 pairwise reductions) >= 30%
```

The totals below are sums of the ten pairs and are included for auditability;
the gate uses the pairwise median, not a ratio of totals. Usage semantics follow
each host parser: Codex includes cached input in `input_tokens`; Claude prefers
complete-session `modelUsage.*.inputTokens` and records cache reads separately,
with an older iteration fallback that sums input, cache creation, and cache
reads on a distinct accounting basis; agy reports `input_tokens` and cache reads separately. Cached input is
never subtracted from the gate metric. No model price is inferred from these
token counts, and reductions are not cross-host cost comparisons.

## Historical live manifests (not reverified)

### Codex CLI — explicit MCP path

Manifest: `/tmp/htsave-live-codex-luna-v14/manifest.json`  
Model: `gpt-5.6-luna`, reasoning `low`  
Historical result (not reverified): **80/80 completed, 0 failed, report passed**

| Scenario | Baseline input total | Treatment input total | Median reduction | Historical gate (not reverified) |
| :--- | ---: | ---: | ---: | :--- |
| `large_readme_exact` | 2,508,942 | 1,463,038 | **41.69%** | ✅ pass |
| `source_three_line_delta` | 2,801,927 | 1,777,972 | **36.57%** | ✅ pass |
| `repeated_test_output` | 2,194,297 | 1,372,861 | **39.94%** | ✅ pass |
| `multi_round_context` | 3,281,050 | 2,631,954 | **30.43%** | ✅ pass |

The exact fractions and all ten pairwise values are retained in the manifest.
`gpt-5.6-luna` is only the local model selection for this run; this report does
not claim an official public price or discount for it.

### Claude Code — transparent shell path

Manifest: `/tmp/htsave-live-claude-v6/manifest.json`  
Model: `claude-haiku-4-5-20251001`  
Claude Code: `2.1.238`  
Historical result (not reverified): **80/80 completed, 0 failed, report passed**

| Scenario | Baseline input total | Treatment input total | Median reduction | Historical gate (not reverified) |
| :--- | ---: | ---: | ---: | :--- |
| `large_readme_exact` | 12,760 | 658 | **94.91%** | ✅ pass |
| `source_three_line_delta` | 13,194 | 930 | **93.22%** | ✅ pass |
| `repeated_test_output` | 13,076 | 890 | **93.43%** | ✅ pass |
| `multi_round_context` | 13,306 | 1,090 | **92.40%** | ✅ pass |

The exact fractions from the report are:

```text
large_readme_exact:     1211/1276
source_three_line_delta: 1224/1313
repeated_test_output:    782207/837210
multi_round_context:     1631692/1765841
```

All Claude answers passed the deterministic oracle and all four medians exceeded
30%. This supersedes the earlier v5 manifest (`/tmp/htsave-live-claude-v5/`),
which had a failed savings gate and must not be used as the authoritative result.
The v6 result was produced after fixing the `modelUsage` parser to read the
complete session aggregate `result.modelUsage.*` instead of only the last turn.

Retry distribution: 31 executions one attempt; 49 executions two attempts.
The 49 first attempts failed with HTTP 429. After the quota reset, the same
manifest was resumed; all 49 final retries succeeded. Failed attempts are
retained in the manifest as audit history.

### Antigravity agy — explicit MCP path

Manifest: `/tmp/htsave-live-agy-v2/manifest.json`  
Model: `gemini-3.7-flash-low`  
agy: `1.1.17`  
Historical result (not reverified): **80/80 completed, 0 failed, report failed savings gate**

| Scenario | Baseline input total | Treatment input total | Median reduction | Historical gate (not reverified) |
| :--- | ---: | ---: | ---: | :--- |
| `large_readme_exact` | 765,338 | 804,254 | **−18.69%** | ❌ red |
| `source_three_line_delta` | 896,518 | 698,395 | **26.07%** | ❌ red |
| `repeated_test_output` | 782,637 | 855,713 | **−0.78%** | ❌ red |
| `multi_round_context` | 1,045,462 | 1,062,161 | **1.96%** | ❌ red |

The exact fractions from the report are:

```text
large_readme_exact:     -39682319/212284044
source_three_line_delta: 310114245/1189763278
repeated_test_output:    -16724343/2151118970
multi_round_context:     2224262/113532213
```

All agy answers passed the deterministic oracle, but none of the four medians
reached 30%. This is a valid red empirical result, not an execution failure
and not a basis for claiming agy savings. The red gate must not be altered by
lowering the threshold, subtracting cache reads, removing negative pairs, or
any other means.

`agy install` and `agy status` confirm the integration gate passes:

```json
{"drifts": [], "hooks_registered": true, "mcp_registered": true,
 "skill_installed": true, "state": "installed"}
```

Retry distribution: 74 executions one attempt; 5 two attempts; 1 three attempts.
There were 7 historical failed attempts; all affected executions eventually
completed on the same manifest.

## Historical commands executed

The focused benchmark checks completed before the live runs:

```bash
rtk uv run pytest -q tests/test_benchmark_runner.py tests/test_benchmark_mcp_path.py
# 26 passed
```

The authoritative reports were regenerated from the completed manifests:

```bash
rtk uv run htsave benchmark report /tmp/htsave-live-codex-luna-v14/manifest.json
# exit 0; report.passed: true; completed: 80

rtk uv run htsave benchmark report /tmp/htsave-live-claude-v6/manifest.json
# exit 0; report.passed: true; completed: 80

rtk uv run htsave benchmark report /tmp/htsave-live-agy-v2/manifest.json
# exit 1; report.passed: false; completed: 80

rtk uv run htsave --json agy status
# state: installed; hooks_registered/mcp_registered/skill_installed: true; drifts: []
```

The agy command's non-zero exit is the expected, truthful result of its red
savings gate. It must not be replaced with a fabricated success status.

## Historical local quality gates

These checks were executed during the historical measurement update:

```bash
rtk uv run pytest -q
# 241 passed in 12.82s
rtk uv run ruff check src tests
# All checks passed!
rtk uv build
# dist/htsave-1.0.0.tar.gz and dist/htsave-1.0.0-py3-none-any.whl built
```

The diagnostic command is also informative but is not a substitute for the
isolated benchmark:

```bash
rtk uv run htsave --json doctor
```

On the measurement host it returned `ok: false` because the operator's global
Claude configuration is drifted, the Codex plugin is not installed/trusted in
that global home, and Codex 0.148.0 has no transparent PostToolUse replacement
contract. The live benchmark deliberately used isolated homes and still passed
the Codex MCP contract; no global configuration was rewritten to hide this
diagnostic state.

## Earlier checkout checks (2026-09-14)

The documentation and status corrections in this checkout were checked without
running paid benchmarks:

```bash
rtk .venv/bin/python -m pytest -q
# 293 passed in 11.97s
rtk .venv/bin/ruff check src tests
# All checks passed!
rtk git diff --check
# clean
```

These local checks do not recreate the absent live manifests or establish a
new host savings gate.

## Documentation correction verification (2026-09-14)

The subsequent strict audit ran `rtk proxy .venv/bin/python -m pytest -q`
with **293 passed in 12.46s**, `rtk proxy .venv/bin/ruff check src tests`
with all checks passed, and `rtk git diff --check` with no errors. These are
reused audit results, distinct from the earlier 11.97s run above; neither suite
was rerun for this documentation-only correction.

Before editing, the combined SHA-256 matched the audited snapshot:
`73a5de3921c0cfe1092b74bd24c8e1960196eb1007f432ae0d0f1640b95eb3df`.
The digest concatenates each sorted path and its bytes for `src/**/*.py`,
`tests/**/*.py`, and the five documents changed here. A separate before/after
content comparison confirmed all source and test files remained unchanged.
Existing worktree modifications were preserved.

The five correction criteria were checked against the current source and
historical records:

- Both READMEs and architecture distinguish invalid-capability raw fallback,
  accepted-workspace read rejection, and raw-text fallback for the three caught
  exception classes from `ContextEngine.decide` in `mcp_server.py`.
- Pro hydration causality is withdrawn. Early agy interpretations are marked
  superseded; later spill/KV-cache explanations remain historical and unverified.
- The release document records all three OpenCode limitations: missing unique
  IDs, the ordinary-tool early return before receipt confirmation, and the unit
  test's manual IDs/confirmation. Both READMEs and architecture link to it.
- Live headings and tables identify historical evidence, and the local artifact
  check names exactly the three absent paths. No historical measurements were
  recomputed or promoted to current passes.
- Host-specific usage accounting, parseable-version contract attempts with
  separate event validation, and 40 pairs / 80 executions are documented. Each
  of the four scenarios still requires ten pairs and a median reduction of 30%.

Fresh checks used `rtk proxy python` to compare the saved pre-edit contents,
historical table numbers, percentages, artifact paths, and Mermaid blocks, and
to resolve relative Markdown file links and heading anchors. All passed. The
three explicit `Path.is_file()` checks returned `False`; other historical
artifact paths were not checked. `rtk git diff --check` passed.

Only `README.md`, `README.zh-TW.md`, `docs/architecture.md`,
`docs/release-gates.md`, and this record changed in this correction. The agy
savings gate remains red, Codex transparent replacement remains externally
blocked, and OpenCode runtime repair remains deferred. This verification does
not establish a completed v1 release.

## Reproduction

The following commands reproduce the same harness and require explicit paid-run
confirmation. They are not needed to inspect the already completed manifests.

```bash
rtk uv sync --extra dev

rtk uv run htsave benchmark run \
  --output /tmp/htsave-reproduce-codex \
  --host codex --path mcp \
  --model gpt-5.6-luna --reasoning-effort low \
  --confirm-paid-runs
rtk uv run htsave benchmark report /tmp/htsave-reproduce-codex/manifest.json

rtk uv run htsave benchmark run \
  --output /tmp/htsave-reproduce-claude \
  --host claude --path shell \
  --model claude-haiku-4-5-20251001 --payload-lines 2048 \
  --confirm-paid-runs
rtk uv run htsave benchmark report /tmp/htsave-reproduce-claude/manifest.json

rtk uv run htsave agy install
rtk uv run htsave --json agy status

rtk uv run htsave benchmark run \
  --output /tmp/htsave-reproduce-agy \
  --host agy --path mcp \
  --model gemini-3.7-flash-low \
  --confirm-paid-runs
rtk uv run htsave benchmark report /tmp/htsave-reproduce-agy/manifest.json
```

## Follow-up agy model experiments

Two additional agy benchmark runs were executed to investigate whether the red
result is Gemini-model-specific or a structural limitation of the explicit MCP
path.

### agy / gemini-3.1-pro-low

Manifest: `/tmp/htsave-agy-pro-v1/manifest.json`  
Model: `gemini-3.1-pro-low`  
Historical result (not reverified): **80/80 completed, 0 failed, report failed savings gate**

| Scenario | Baseline input total | Treatment input total | Median reduction | Historical gate (not reverified) |
| :--- | ---: | ---: | ---: | :--- |
| `large_readme_exact` | 801,264,800 | ~699,567,403 | **12.69%** | ❌ red |
| `source_three_line_delta` | 16,691,371,063 | ~8,606,980,930 | **48.44%** | ✅ pass |
| `repeated_test_output` | 926,548,996 | ~556,850,343 | **39.90%** | ✅ pass |
| `multi_round_context` | 3,529,722,620 | ~3,412,939,979 | **3.31%** | ❌ red |

Two of four scenarios passed. The overall gate fails because `large_readme_exact`
and `multi_round_context` did not reach 30%. The earlier claim that hydration
was the binding constraint is withdrawn: the later historical forensic record
says Pro never called `htsave_hydrate` in either run. The reported
`large_readme_exact` comparison remains 12.69% vs −18.69% for flash-low; it does
not independently establish a cause. `gemini-3.1-pro-low` is only the local
model selection for this run; no official price is inferred.

### agy / claude-sonnet-4-6 (partial — quota-limited)

Manifest: `/tmp/htsave-agy-claude-probe/manifest.json`  
Model: `claude-sonnet-4-6`  
Historical result (not reverified): **28/80 completed; 52 blocked by 137-hour Anthropic API quota lock**

| Scenario | Pairs completed | Median reduction | Historical gate (not reverified) |
| :--- | ---: | ---: | :--- |
| `large_readme_exact` | 10 | **2.27%** | ❌ red |
| `source_three_line_delta` | 4 | **7.61%** | insufficient |
| `repeated_test_output` | 0 | — | blocked |
| `multi_round_context` | 0 | — | blocked |

The historical sample reported ~2.27% on `large_readme_exact`, with all 10
pairs complete. Earlier hydration and structural-ceiling interpretations are
superseded by the historical forensics below; this partial sample does not
independently establish a model-general cause. The 52 remaining executions
were blocked by an individual Anthropic API quota that resets in 137 hours; the
partial manifest is retained as audit history. This partial run required a fix
to `benchmark_runner._agy_argv` (`src/htsave/benchmark_runner.py`) to omit
`--effort` for non-Gemini models, which reject that flag; a regression test was
added in `tests/test_benchmark_agy.py`.

## Historical agy red-gate forensics (2026-08-26; not reverified)

A read-only decomposition of the recorded manifests' per-turn `events.jsonl`
streams (`step_update.usage` per agent response) reattributed the red gate.
The following are preserved historical conclusions, not independently verified
findings of this checkout audit:

1. **Neither arm ever re-bills full content on agy.** Large tool results are
   spilled by agy to `.system_generated` brain files; the model context sees a
   short summary (for example `view_file` returning `2053 lines, 238705
   bytes`). Turn-level inputs stay at ~4–17k tokens in both arms even though
   the fixture is ~239 KB. htsave's REF frames therefore replace summaries,
   not billed content.
2. **The gate metric is a KV-cache lottery.** Summing per-turn uncached input
   reproduces the manifest totals exactly (example p00 treatment: 85,979 =
   15,825+16,255+4,339+16,889+17,139+5,782+9,750). Baseline arms draw large
   cached reads (up to 1,261,556 tokens on one pro-low arm) while treatment
   arms draw misses on the same pairs; flash-low `large_readme_exact`
   reductions range from −54.21% to +42.74% across pairs.
3. **Hydration is secondary.** Counterfactually deleting every hydrate call's
   tail turns moves flash-low medians only to −15.81%, +11.69%, +2.33%,
   26.07% (from −18.69%, +1.96%, −0.78%, 26.07%). A historical illustrative weighted-token view (not official pricing)
   (`input + 0.25 × cached`) is also negative (−32.76% median on
   `large_readme_exact`), under that assumed weighting; this is not a general cost claim.
4. **Engineering hardening verified live.** `agy_hooks._handle_stop` now
   confirms pending receipts (previously a no-op); the benchmark-only
   `workspace-*` glob was removed from `_workspace_path`; the agy skill and a
   new official-contract `PreInvocation` hook inject an anti-rehydration
   reminder symmetrically in both arms.

### v3 zero-hydrate rerun — gemini-3.7-flash-low, agy 1.1.20

Manifest: `/tmp/htsave-agy-v3/manifest.json`, **80/80 completed**, every
completed attempt passed the deterministic oracle, treatment sessions called
`htsave_hydrate` zero times (v2: once per session), and Stop confirmed every
receipt. Individual Gemini quota interruptions were retried under the same
pair/arm ids with spaced `--resume --max-executions 2` passes; all retry
attempts remain in the manifest audit history.

| Scenario | Median input reduction | Historical gate (not reverified) |
| :--- | ---: | :--- |
| `large_readme_exact` | **−3.77%** | ❌ red |
| `source_three_line_delta` | **22.80%** | ❌ red |
| `repeated_test_output` | **−0.43%** | ❌ red |
| `multi_round_context` | **−4.32%** | ❌ red |

Versus v2 medians: `large_readme_exact` improved by +14.92 points
(−18.69% → −3.77%). The historical interpretation attributed this to hydration
as the largest controllable loss; the other scenarios were described as noise
(+0.35, −3.27, −6.28 points). No scenario approached the 30% gate, which
was interpreted historically as structural KV-cache variance after removing
hydration. This causal interpretation has not been independently reverified.

### v3 pro rerun — gemini-3.1-pro-low, agy 1.1.20

Manifest: `/tmp/htsave-agy-pro-v2/manifest.json`, **80/80 completed** after
spaced `--resume` passes through individual Gemini quota windows; every failed
attempt is retained in the manifest audit history.

| Scenario | Median input reduction | versus v1 | Historical gate (not reverified) |
| :--- | ---: | ---: | :--- |
| `large_readme_exact` | **7.02%** | −5.67 pts | ❌ red |
| `source_three_line_delta` | **43.78%** | −4.66 pts | ✅ pass |
| `repeated_test_output` | **10.52%** | −29.38 pts | ❌ red |
| `multi_round_context` | **11.29%** | +7.98 pts | ❌ red |

The overall gate stays red, with recorded pair-level swings of
−101.7% to +84.9% within single scenarios. The later historical record says
Pro never called `htsave_hydrate` in either run, contradicting the earlier
hydration explanation. Attribution to KV-cache and model variance remains a
historical forensic conclusion, not independently reverified here.

## Historical verification conclusion (live evidence not reverified)

- Codex MCP live savings gate: **historical pass; not reverified** (41.69%, 36.57%, 39.94%, 30.43%).
- Claude transparent-shell live savings gate: **historical pass; not reverified** (94.91%, 93.22%, 93.43%,
  92.40%); v6 manifest supersedes the earlier red v5 result.
- agy MCP live savings gate: **red** (gemini-3.7-flash-low: −18.69%, 26.07%,
  −0.78%, 1.96%; gemini-3.1-pro-low: 12.69%, 48.44%, 39.90%, 3.31%; both
  failed the overall gate); agy integration smoke gate: **historical pass; not reverified**.
- Historical causal interpretation from the 2026-08-26 forensics, not
  independently reverified: agy's spill-plus-KV-cache
  architecture leaves nothing measurable for the explicit MCP path to save;
  flash hydration was a secondary factor and was absent in v3 (flash-low
  `large_readme_exact` −18.69% → −3.77%, all four medians still red; pro-low
  80/80 rerun also red with one scenario pass). The red gate is structural
  while agy exposes no transparent replacement contract.
- OpenCode integration: **experimental**; local install and hook tests exist,
  but no live benchmark evidence or release savings gate exists.
- The local test/build gates must be read together with the command results
  above. The repository must not be labelled a fully green v1 release until
  the agy savings gate is resolved or agy is reclassified as an unsupported
  host for this gate.
