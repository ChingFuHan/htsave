# htsave handoff

> 這份文件是交接記錄，不是完成宣告。接手的 agent 必須以目前工作樹、live manifest 與重新執行的命令輸出為準；只有在 `plan.md` 的所有交付物完成、再執行 `verify.md` 的驗證命令並通過後，才能將原始 goal 標記為 complete。

## 1. 交接摘要

### 原始目標

原始 goal 是：

> 執行 repo root 的 `plan.md`；僅在其完成後執行 `verify.md`，且兩者都成功才算任務完成。

`plan.md` 要求真實呼叫 agent CLI，覆蓋四個 deterministic scenario、每個 scenario 10 個 baseline/treatment pairs，每個可用 host 80 個 execution，產出中位數節省報告，更新雙語 README、release gates 與 `verify.md`，最後完成本地測試與打包驗收。

### 目前結論

- Codex MCP：80/80 final completed，report **passed**。
- Claude Code shell：80/80 final completed，report **passed**；這是修正 `modelUsage` parser 後的 v6 manifest，不能再使用舊 v5 數據。
- agy MCP：80/80 final completed，report **failed savings gate**；這是有效的 red empirical result，不是 execution failure。
- 本地 `pytest`、Ruff、`uv build` 已在文件更新前跑過，但文件同步與 `verify.md` 的完整驗證流程仍未完成。
- `README.md`、`README.zh-TW.md`、`docs/release-gates.md`、`verify.md` 目前仍含有舊 Claude-v5 數據與「agy benchmark 不可用」敘述，必須更新。
- 不要因 live manifests 與本地 tests 已完成，就直接宣告 goal complete。

### Repo / runtime snapshot

- Workspace: `/home/homelab2/work/htsave`
- Branch: `main`
- HEAD observed at handoff: `d7f301f`
- Snapshot time: `2026-08-21 21:10 CST`
- CLI versions observed:
  - `agy 1.1.17`
  - `codex-cli 0.148.0`
  - `Claude Code 2.1.238`
- 所有 shell command 遵守 repo instruction：前綴使用 `rtk`。
- 檔案修改使用 `apply_patch`；不要用 `cat >`、shell redirect 或 Python 寫檔。
- worktree 是使用者既有工作，不要 reset、checkout、clean 或覆蓋不相關變更。

## 2. 目前工作樹與已完成程式工作

### Worktree 狀態

接手時預期會看到以下既有變更；它們屬於本任務的一部分：

```text
 M README.md
 M README.zh-TW.md
 M benchmark/templates/prompt.txt
 M benchmark/templates/prompt_mcp.txt
 M docs/release-gates.md
 M src/htsave/benchmark.py
 M src/htsave/benchmark_runner.py
 M src/htsave/claude_install.py
 M src/htsave/cli.py
 M tests/test_benchmark.py
 M tests/test_benchmark_claude.py
 M tests/test_benchmark_runner.py
 M tests/test_claude_install.py
 M tests/test_cli.py
?? plan.md
?? plugin/htsave/skills/agy/
?? plugin/htsave/skills/claude/
?? src/htsave/agy_hooks.py
?? src/htsave/agy_install.py
?? tests/test_agy_hooks.py
?? tests/test_agy_install.py
?? tests/test_benchmark_agy.py
?? verify.md
?? handoff.md
```

先用下面命令確認實際狀態，不要假設檔案清單沒有變化：

```bash
rtk git status --short
rtk git diff --check
rtk git diff --stat
```

### 實作內容概覽

以下程式修正已完成，接手 agent 應保留並以測試驗證，不要為了整理 handoff 而重寫：

1. `src/htsave/benchmark.py`
   - `REQUIRED_PAIRS` 為 10，四個 scenario 共 80 execution slots/host。
   - host 現在包含 `agy`、`claude`、`codex`；agy 固定使用 explicit MCP path。
   - Claude parser 優先使用完整 session aggregate 的 `result.modelUsage.*`：
     `inputTokens` 作為 release input metric，`cacheReadInputTokens` 分開記錄，避免只讀最後一個 iteration。
   - 新增 agy `--output-format stream-json` result parser，讀取 `conversation_id`、terminal status 及 host-reported usage。
   - report 使用 10 個 pairwise reduction 的 median，不使用 baseline/treatment totals 的簡單比例替代。

2. `src/htsave/benchmark_runner.py`
   - agy 使用 `--prompt` 傳 prompt；不可依賴 plain stdin。
   - agy 使用隔離 HOME、trust workspace、MCP config、skill 與 hooks；每個 execution 都是新的 workspace/home/state。
   - agy `structured_output` 優先於 `result.response`。
   - agy trace 只計 `step_update.state == "DONE"` 的 htsave reads，避免 ACTIVE/DONE 重複。
   - Claude/Codex/agy 的 terminal usage、answer oracle、MCP read trace 都在 runner 層驗證。
   - retry/resume 保留同一 execution id 的歷史 attempts；不可用另一個 model 覆蓋失敗資料。

3. `src/htsave/agy_hooks.py` / `src/htsave/agy_install.py`
   - agy lifecycle hook 在 `call_mcp_tool` 的 nested `Arguments` 注入一次性 `_htsave_context` capability。
   - `HTSAVE_BENCH_WORKSPACE`、隔離 state 與 workspace containment 修正了 benchmark 中 capability 綁錯 workspace 的問題。
   - install/status/uninstall 只管理有 htsave ownership tag 的 skill/MCP/hooks。

4. 測試與模板
   - 新增 agy parser、hook capability、install lifecycle、isolated benchmark fake-host tests。
   - prompt/schema/template 強化 deterministic answer oracle、read order 與四輪 context fixture。

### 不可破壞的不變式

- 節省公式：

  ```text
  reduction = (baseline input_tokens - treatment input_tokens)
              / baseline input_tokens
  scenario gate = median(10 pairwise reductions) >= 30%
  ```

- `cached_input_tokens`/cache reads 分開記錄，不能為了讓節省比例變好而扣除或混入 `input_tokens`。
- 每個 execution 必須使用新 workspace、隔離 host home、隔離 state、初始 tree digest、prompt、schema 與 answer oracle。
- 所有 final completed attempts 都必須有 terminal usage、thread/conversation id、answer oracle 通過；MCP host 還要有指定 read trace。
- REF/DELTA 是精確可逆 transport，不是摘要；不得用語意相似或模型回答代替 oracle。
- capability、workspace containment、session isolation、hash 與 security boundary 失敗時 fail closed；一般 host contract/unknown event 仍依既有設計 fail open。
- 不宣稱任何模型的官方價格、折扣或帳戶實際費用；`gpt-5.6-luna` 與 `gemini-3.7-flash-low` 只是本次本地 model selection。
- red gate 必須如實記錄。不要刪除 failed attempts、改 threshold、換模型、換 manifest 或把 red 改成 passed。

## 3. Live benchmark 證據

以下路徑是 measurement host 上的 authoritative manifests。先重新跑 `benchmark report`，再把輸出抄入文件；不要使用舊文件中的數據覆蓋新 manifest。

### 3.1 Codex CLI / explicit MCP

- Manifest: `/tmp/htsave-live-codex-luna-v14/manifest.json`
- Host/model/path: `codex` / `gpt-5.6-luna` / `mcp`
- Reasoning: `low`
- Final status: 80/80 completed, 0 final failed
- Report: **passed**
- Historical retry distribution: 71 executions one attempt；6 two attempts；1 three；1 four；1 five。所有 final attempts completed。

| Scenario | Baseline input total | Treatment input total | Median reduction | Gate |
| :--- | ---: | ---: | ---: | :--- |
| `large_readme_exact` | 2,508,942 | 1,463,038 | **41.69%** | pass |
| `source_three_line_delta` | 2,801,927 | 1,777,972 | **36.57%** | pass |
| `repeated_test_output` | 2,194,297 | 1,372,861 | **39.94%** | pass |
| `multi_round_context` | 3,281,050 | 2,631,954 | **30.43%** | pass |

Re-run:

```bash
rtk uv run htsave benchmark report /tmp/htsave-live-codex-luna-v14/manifest.json
```

### 3.2 Claude Code / transparent shell

- Manifest: `/tmp/htsave-live-claude-v6/manifest.json`
- Host/model/path: `claude` / `claude-haiku-4-5-20251001` / `shell`
- Claude Code: `2.1.238`
- Final status: 80/80 completed, 0 final failed
- Report: **passed**
- Retry distribution: 31 executions one attempt；49 executions two attempts。
- The 49 first attempts failed because the account session limit returned HTTP 429. After the quota reset, the same manifest was resumed; all 49 final retries succeeded. Keep those failed attempts in the manifest as audit history.
- This v6 result supersedes `/tmp/htsave-live-claude-v5/manifest.json`; do not publish v5's old low-savings numbers.

| Scenario | Baseline input total | Treatment input total | Median reduction | Gate |
| :--- | ---: | ---: | ---: | :--- |
| `large_readme_exact` | 12,760 | 658 | **94.91%** | pass |
| `source_three_line_delta` | 13,194 | 930 | **93.22%** | pass |
| `repeated_test_output` | 13,076 | 890 | **93.43%** | pass |
| `multi_round_context` | 13,306 | 1,090 | **92.40%** | pass |

The exact fractions from the report are:

```text
large_readme_exact:     1211/1276
source_three_line_delta: 1224/1313
repeated_test_output:    782207/837210
multi_round_context:     1631692/1765841
```

Re-run:

```bash
rtk uv run htsave benchmark report /tmp/htsave-live-claude-v6/manifest.json
```

### 3.3 Antigravity agy / explicit MCP

- Manifest: `/tmp/htsave-live-agy-v2/manifest.json`
- Host/model/path: `agy` / `gemini-3.7-flash-low` / `mcp`
- Final status: 80/80 completed, 0 final failed
- Report: **failed savings gate**; this is a valid red result, not unverified and not an execution failure.
- Retry distribution: 74 executions one attempt；5 two attempts；1 three attempts。
- There were 7 historical failed attempts; all affected executions eventually completed on the same manifest.
- `agy status` was installed with no drift; the benchmark uses agy's host-reported stream usage and makes no price/billing claim.

| Scenario | Baseline input total | Treatment input total | Median reduction | Gate |
| :--- | ---: | ---: | ---: | :--- |
| `large_readme_exact` | 765,338 | 804,254 | **−18.69%** | red |
| `source_three_line_delta` | 896,518 | 698,395 | **26.07%** | red |
| `repeated_test_output` | 782,637 | 855,713 | **−0.78%** | red |
| `multi_round_context` | 1,045,462 | 1,062,161 | **1.96%** | red |

Exact report median fractions:

```text
large_readme_exact:     -39682319/212284044
source_three_line_delta: 310114245/1189763278
repeated_test_output:    -16724343/2151118970
multi_round_context:     2224262/113532213
```

Re-run:

```bash
rtk uv run htsave benchmark report /tmp/htsave-live-agy-v2/manifest.json
rtk uv run htsave --json agy status
```

### 3.4 Report interpretation

- A report exit code of 0 means all required scenario savings gates passed.
- A report exit code of 1 can be the truthful result of a completed red empirical gate; it is not automatically an infrastructure failure.
- Use the final attempt for each execution when calculating the report, but retain every prior attempt for auditability.
- Never merge totals across hosts or infer price from token counts.

## 4. Existing documentation debt

The following files were written before the v6/v2 evidence was available and need a final synchronized update:

1. `plan.md`
   - Execution record still points to Claude v5 and says agy has no benchmark host.
   - Matrix/Phase 2/acceptance text reflects the earlier agy-smoke-only assumption.
   - Update with an explicit observed-deviation note: the implementation now has an agy host and produced a real 80-run red report.
   - Do not silently rewrite the original acceptance threshold or call red agy evidence green.

2. `README.md` and `README.zh-TW.md`
   - Live table still contains old Claude-v5 totals and low-savings medians.
   - Replace with Claude-v6 and agy-v2 rows/totals above.
   - Update source manifest paths and total host/run description.
   - Remove the statement that benchmark CLI supports only Claude/Codex; code now supports agy MCP.
   - Update CLI example host choices from `claude|codex` to include `agy` where appropriate.
   - Keep the no-official-price disclaimer and link to `verify.md`.

3. `docs/release-gates.md`
   - Claude gate currently describes the old 0.70%, −3.03%, 9.76%, 7.00% result; replace with v6 passed medians.
   - Codex section is already consistent with luna v14 but recheck it from report output.
   - Rename agy section from “token benchmark unavailable” to a live red empirical gate, while retaining the installed/no-drift integration result.
   - Update recorded live evidence table to list all three manifests and their report outcomes.

4. `verify.md`
   - Replace Claude-v5 section with `/tmp/htsave-live-claude-v6` and its exact table.
   - Replace agy unverified/no-manifest text with `/tmp/htsave-live-agy-v2` and its red table.
   - Update commands and recorded local gate output (`pytest` was observed as 241 passed in the latest full run).
   - Keep the global `doctor` failure as diagnostic context if it still reproduces; explain that isolated live runs are the authoritative host evidence.
   - Keep reproduction commands clearly labelled as optional paid re-runs; do not execute them merely to inspect existing manifests.

5. `docs/architecture.md`
   - It currently opens with “Two hosts are supported” and lists only Claude/Codex adapters.
   - Reconcile this with the new agy integration/benchmark if the final scope keeps agy as a supported host. Preserve the existing distinction: Claude replaces transparent output, Codex uses explicit MCP, agy uses explicit MCP and fail-open lifecycle hooks.

## 5. Exact next-step procedure

### Step A — Establish final evidence again

Run the three report commands and retain their raw JSON output in the terminal/log. Do not start a new paid benchmark directory and do not use Claude-v5:

```bash
rtk uv run htsave benchmark report /tmp/htsave-live-codex-luna-v14/manifest.json
rtk uv run htsave benchmark report /tmp/htsave-live-claude-v6/manifest.json
rtk uv run htsave benchmark report /tmp/htsave-live-agy-v2/manifest.json
rtk uv run htsave --json agy status
```

The expected truthful outcomes are: Codex exit 0/passed, Claude exit 0/passed, agy exit 1/red savings gate, agy status exit 0/installed/no drift.

### Step B — Update the plan deliverables

Use `apply_patch` to synchronize `plan.md`, both README files, `docs/release-gates.md`, `docs/architecture.md` where needed, and `verify.md`. Use the exact manifest paths and metrics in Section 3. Do not round in a way that changes gate decisions; display rounded percentages only alongside the retained exact manifest fractions.

The docs must say:

- Codex MCP: green/pass, 80/80.
- Claude shell v6: green/pass, 80/80, model `claude-haiku-4-5-20251001`.
- agy MCP v2: red savings gate, 80/80 correct executions, no price claim.
- A red empirical savings gate is reported honestly and is not converted into a fabricated success.
- The local doctor diagnostic is separate from isolated benchmark evidence.

### Step C — Run plan's local quality gates after documentation edits

```bash
rtk uv run pytest -q
rtk uv run ruff check src tests
rtk uv build
```

Observed baseline before the final documentation edits:

- `pytest -q`: `241 passed in 12.82s`
- `ruff check src tests`: `All checks passed!`
- `uv build`: built `dist/htsave-1.0.0.tar.gz` and `dist/htsave-1.0.0-py3-none-any.whl`

Rerun after edits and record the actual current output in `verify.md`; do not reuse the old 233-test line without rerunning.

### Step D — Only after Step A–C, execute `verify.md`

`verify.md` is a record, not an executable script. Execute its required non-paid verification commands in order, and record fresh results:

```bash
rtk uv run pytest -q tests/test_benchmark_runner.py tests/test_benchmark_mcp_path.py
rtk uv run htsave benchmark report /tmp/htsave-live-codex-luna-v14/manifest.json
rtk uv run htsave benchmark report /tmp/htsave-live-claude-v6/manifest.json
rtk uv run htsave benchmark report /tmp/htsave-live-agy-v2/manifest.json
rtk uv run htsave --json agy status
rtk uv run pytest -q
rtk uv run ruff check src tests
rtk uv build
rtk uv run htsave --json doctor
```

The reproduction section's paid benchmark commands are for third-party reproduction. Do not launch them again when the completed manifests above are available unless the user explicitly requests new paid evidence. If a literal interpretation of “execute verify.md” requires every code block, document that the reproduction commands are intentionally not run because the file itself says they are not needed to inspect completed manifests.

### Step E — Final requirement audit

Before claiming completion, verify all of the following from current state:

```bash
rtk git status --short
rtk git diff --check
rtk jq '{final_completed: ([.executions[] | select(.attempts[-1].status == "completed")] | length), final_failed: ([.executions[] | select(.attempts[-1].status == "failed")] | length)}' /tmp/htsave-live-codex-luna-v14/manifest.json
rtk jq '{final_completed: ([.executions[] | select(.attempts[-1].status == "completed")] | length), final_failed: ([.executions[] | select(.attempts[-1].status == "failed")] | length)}' /tmp/htsave-live-claude-v6/manifest.json
rtk jq '{final_completed: ([.executions[] | select(.attempts[-1].status == "completed")] | length), final_failed: ([.executions[] | select(.attempts[-1].status == "failed")] | length)}' /tmp/htsave-live-agy-v2/manifest.json
```

Confirm:

- Every plan deliverable exists and contains current v6/v2 data.
- All three final manifests have 80 completed executions and zero final failures.
- Every final completed execution has usage and thread/conversation id.
- Answers passed schema/oracle; Codex and agy MCP traces were validated.
- The report outcomes in docs match fresh command outputs.
- `verify.md` was executed only after plan deliverables were synchronized.
- No stale Claude-v5 or agy-unavailable statement remains in the final evidence sections.

Only after these checks should the active goal be updated to `complete`. If a report is red, record it as red; “both plan and verify succeeded” means the plan/verification process and evidence documentation completed truthfully, not that every empirical savings host must magically pass.

## 6. Failure handling and do-not-do list

### Claude quota / 429

- Use the existing v6 manifest with `--resume` if any execution is incomplete.
- Never create a new model/manifest to hide failed attempts.
- Preserve 429 attempts in the manifest; final status is what drives the report, retry history is part of the audit.

### Red empirical report

- agy-v2 is expected to exit 1 because all four medians are below 30%.
- Do not lower the threshold, subtract cache reads, remove negative pairs, alter the oracle, or publish only successful pairs.

### Stale evidence

- `/tmp/htsave-live-claude-v5/manifest.json` is superseded by v6 for final docs.
- The old `verify.md`/README/release-gates agy-unavailable text is not current after agy-v2.
- Do not quote old 233-test output after the current suite has 241 tests.

### Safety / repository handling

- Do not run `git reset --hard`, `git checkout --`, `git clean`, recursive deletion, or broad cleanup.
- Do not commit, push, or send external messages unless explicitly requested.
- Keep paid commands explicit with `--confirm-paid-runs`; do not infer billing from local `stats` or tiktoken estimates.

## 7. Handoff checklist

The next agent should check boxes only after observing the evidence, not based on this document's assertions:

- [ ] Read `AGENTS.md`/RTK instructions and current `git status`.
- [ ] Re-run the three authoritative benchmark reports.
- [ ] Confirm Codex v14, Claude v6, and agy v2 final counts and report outcomes.
- [ ] Update `plan.md` execution record and observed agy deviation.
- [ ] Update English and Traditional Chinese README tables/claims.
- [ ] Update `docs/release-gates.md` for Claude-v6 pass and agy-v2 red.
- [ ] Reconcile the agy host in `docs/architecture.md` if kept in final scope.
- [ ] Replace stale sections in `verify.md` and record fresh command output.
- [ ] Run `pytest -q`, Ruff, and `uv build` after documentation edits.
- [ ] Execute the required `verify.md` commands only after the plan deliverables are complete.
- [ ] Perform the final manifest/document/invariant audit.
- [ ] Only then mark the original goal complete.

