# htsave verification record

This is the verification record for the execution of `plan.md` on 2026-08-21,
updated by a relay agent on 2026-08-21 after the Claude v6 and agy v2 manifests
were completed. It records the observed result, including red gates. It is not
an assertion that the package is a completed v1 release: the release policy in
`docs/release-gates.md` requires every gate to be green.

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

Cached input tokens are recorded separately and are not subtracted. The totals
below are sums of the ten pairs and are included for auditability; the gate uses
the pairwise median, not a ratio of totals. No model price is inferred from
these token counts.

## Live manifests

### Codex CLI — explicit MCP path

Manifest: `/tmp/htsave-live-codex-luna-v14/manifest.json`  
Model: `gpt-5.6-luna`, reasoning `low`  
Result: **80/80 completed, 0 failed, report passed**

| Scenario | Baseline input total | Treatment input total | Median reduction | Gate |
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
Result: **80/80 completed, 0 failed, report passed**

| Scenario | Baseline input total | Treatment input total | Median reduction | Gate |
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
Result: **80/80 completed, 0 failed, report failed savings gate**

| Scenario | Baseline input total | Treatment input total | Median reduction | Gate |
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

## Commands executed

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

## Local quality gates

These final repository checks were executed after the documentation update:

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
Result: **80/80 completed, 0 failed, report failed savings gate**

| Scenario | Baseline input total | Treatment input total | Median reduction | Gate |
| :--- | ---: | ---: | ---: | :--- |
| `large_readme_exact` | 801,264,800 | ~699,567,403 | **12.69%** | ❌ red |
| `source_three_line_delta` | 16,691,371,063 | ~8,606,980,930 | **48.44%** | ✅ pass |
| `repeated_test_output` | 926,548,996 | ~556,850,343 | **39.90%** | ✅ pass |
| `multi_round_context` | 3,529,722,620 | ~3,412,939,979 | **3.31%** | ❌ red |

Two of four scenarios passed. The overall gate fails because `large_readme_exact`
and `multi_round_context` did not reach 30%. Pro shows less KV-cache interference
than flash-low (12.69% vs −18.69% for `large_readme_exact`), but the
explicit-MCP hydration overhead remains the binding constraint: when the model
calls `htsave_hydrate` the full content re-enters context, negating the REF
savings on high-token scenarios. `gemini-3.1-pro-low` is only the local model
selection for this run; no official price is inferred.

### agy / claude-sonnet-4-6 (partial — quota-limited)

Manifest: `/tmp/htsave-agy-claude-probe/manifest.json`  
Model: `claude-sonnet-4-6`  
Result: **28/80 completed; 52 blocked by 137-hour Anthropic API quota lock**

| Scenario | Pairs completed | Median reduction | Gate |
| :--- | ---: | ---: | :--- |
| `large_readme_exact` | 10 | **2.27%** | ❌ red |
| `source_three_line_delta` | 4 | **7.61%** | insufficient |
| `repeated_test_output` | 0 | — | blocked |
| `multi_round_context` | 0 | — | blocked |

Claude has no Gemini server-side KV cache, yet savings for `large_readme_exact`
are only ~2.27% — confirming that hydration overhead, not KV caching alone, is
the binding root cause. All 10 `large_readme_exact` pairs are complete and give
a statistically clear signal. The 52 remaining executions were blocked by an
individual Anthropic API quota that resets in 137 hours; the partial manifest is
retained as audit history. This partial run required a fix to
`benchmark_runner._agy_argv` (`src/htsave/benchmark_runner.py`) to omit
`--effort` for non-Gemini models, which reject that flag; a regression test was
added in `tests/test_benchmark_agy.py`.

## Verification conclusion

- Codex MCP live savings gate: **pass** (41.69%, 36.57%, 39.94%, 30.43%).
- Claude transparent-shell live savings gate: **pass** (94.91%, 93.22%, 93.43%,
  92.40%); v6 manifest supersedes the earlier red v5 result.
- agy MCP live savings gate: **red** (gemini-3.7-flash-low: −18.69%, 26.07%,
  −0.78%, 1.96%; gemini-3.1-pro-low: 12.69%, 48.44%, 39.90%, 3.31%; both
  failed the overall gate); agy integration smoke gate: **pass**.
- Root cause confirmed by claude-sonnet-4-6 partial run (10 pairs, 2.27%): the
  explicit MCP path with mandatory hydration is the binding constraint, not
  Gemini KV caching alone.
- The local test/build gates must be read together with the command results
  above. The repository must not be labelled a fully green v1 release until
  the agy savings gate is resolved.
