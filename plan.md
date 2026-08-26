# Real Token 實機測試與數據文檔化計畫 (等級 5：超大壓力樣本 80 執行規模)

## 目標

透過真實呼叫 AI Agent CLI（消耗真實 API Token），以 **等級 5：超大壓力樣本規格（4 大情境 $\times$ 10 Pairs / 每個可用平台 80 次執行 / 10-Pair 中位數統計）**，實機驗證在 **agy CLI**、**Codex CLI** 與 **Claude Code** 上的去重省 token 效果、無損性與 Skill / MCP 正確性。若平台沒有 benchmark host 或官方 usage stream，必須明確記錄為 unverified。

測試完成後，**將 4 大情境、80 次實測的完整 Token 節省數據、中位數統計表與公平性驗證報告寫入 Repo 的 `README.md`、`README.zh-TW.md`、`docs/release-gates.md` 與 `verify.md`**，以極大化樣本數與極高統計置信度展現 htsave 的技術價值。

**實作與執行順序：agy → codex → claude → 數據文檔化 (README / Docs / verify.md)**

---

## 測試規模與模型矩陣：等級 5（超大壓力樣本 80 Runs）

採用當前環境可用且具備完整工具調用能力的模型。模型價格不由本計畫推定；實際帳戶計費與本地 model metadata 必須分開記錄：

| 順位 | 平台 | 指定模型 | 備選模型 | 執行規模 | 預估費用 (80 Runs) | 特性與價值說明 |
|:---:|---|---|---|:---:|:---:|---|
| **1** | **agy CLI** | `gemini-3.7-flash-low` | — | **80 次執行**（實際完成；savings gate red）| 以實際帳戶計費為準 | MCP 路徑實機配對量測完成；四個 scenario 中位數均未達 30%，red 屬有效實證結果。 |
| **2** | **Codex CLI** | `gpt-5.6-luna` (Reasoning: `low`) | `gpt-5.6-sol` | **80 次執行** (4 情境 $\times$ 10 Pairs) | 以實際帳戶計費為準 | MCP 路徑的 `htsave_read` 實機配對量測；luna 僅為本地選擇，不宣稱官方定價。 |
| **3** | **Claude Code** | `claude-haiku-4-5-20251001` | 可用模型依帳戶而定 | **80 次執行** (4 情境 $\times$ 10 Pairs) | 以實際帳戶計費為準 | 透明 shell 路徑的實機配對量測；舊 `claude-3-5-haiku` alias 不可用。 |
| **實際合計** | **可用 benchmark hosts** | **Codex + Claude + agy** | — | **240 次實機執行** | 不彙總價格 | agy 完成 80 次實機執行，savings gate 為 red；結果與 gate 詳見 `verify.md`。 |

---

## 測試情境全覆蓋（4 大核心情境 $\times$ 10 Pairs 中位數）

1. **`large_readme_exact`（完全相同檔案重讀）**：驗證 `FULL` $\rightarrow$ `REF` 傳輸與極致去重（10 Pairs，門檻 $\ge 30\%$）。
2. **`source_three_line_delta`（原始碼微幅修改）**：驗證 `DELTA`（Unified Diff v1）差分補丁去重（10 Pairs，門檻 $\ge 30\%$）。
3. **`repeated_test_output`（重複執行指令/測試輸出）**：驗證終端命令重複輸出的透明攔截去重（10 Pairs，門檻 $\ge 30\%$）。
4. **`multi_round_context`（多輪交談上下文混合）**：驗證長對話歷程中的累計去重與快取穩定度（10 Pairs，門檻 $\ge 30\%$）。

---

## 執行步驟規劃 (依照 agy → codex → claude → 文檔化 順序)

### Phase 1: 憑證與環境 Pre-flight 預檢

在扣款前執行嚴格預檢，確保環境與憑證完整：

1. **CLI 執行檔檢查**：
   ```bash
   rtk which agy codex claude
   rtk agy --version
   rtk codex --version
   rtk claude --version
   ```
2. **各平台憑證存在性檢查**：
   - **Claude Code**：檢查 `~/.claude/.credentials.json` 或 `ANTHROPIC_API_KEY`。
   - **Codex CLI**：檢查 `~/.codex/auth.json` 或 `OPENAI_API_KEY`。
   - **agy CLI**：確認當前環境具備 Gemini 存取權限。
3. **htsave Doctor 診斷**：
   ```bash
   rtk uv run htsave doctor
   ```
   確認 SQLite WAL、CAS 目錄權限與契約探測均通過。

---

### Phase 2: agy CLI 整合與可用性檢查 (第一順位)

1. **安裝 agy 整合**：
   ```bash
   rtk uv run htsave agy install
   rtk uv run htsave agy status
   ```
2. 確認 `agy status` 的 hooks、MCP、skill 均已註冊且沒有 drift。當前
   benchmark CLI 沒有 `agy` host，因此不得把 `stats` 當成官方 token usage。
3. **讀取本地狀態統計（僅 smoke evidence）**：
   ```bash
   rtk uv run htsave stats --json
   ```
4. **記錄結果**：整合 smoke check 通過；agy token gate 為 unverified，直到
   CLI 提供可配對 benchmark host 與官方 usage stream。

---

### Phase 3: Codex CLI 實機 Token 測試 (第二順位，80 次執行)

執行 80 次超大樣本 Benchmark（4 大情境 $\times$ 10 Pairs，MCP 路徑）：
```bash
rtk mkdir -p /tmp/htsave-live-codex

rtk uv run htsave benchmark run \
  --output /tmp/htsave-live-codex \
  --host codex \
  --path mcp \
  --model gpt-5.6-luna \
  --reasoning-effort low \
  --confirm-paid-runs

rtk uv run htsave benchmark report /tmp/htsave-live-codex/manifest.json
```

**記錄指標**：
- 4 大情境各自 10 個 Pair 的 Baseline vs Treatment 官方計費 Input Tokens
- 各情境的 **10-Pair Median Input Reduction %**（中位數節省比例）
- 驗證所有 80 個 attempt 的 Answer 100% 通過 Oracle 驗證。

---

### Phase 4: Claude Code 實機 Token 測試 (第三順位，80 次執行)

執行 80 次超大樣本 Benchmark（4 大情境 $\times$ 10 Pairs，透明 shell 路徑）：
```bash
rtk mkdir -p /tmp/htsave-live-claude

rtk uv run htsave benchmark run \
  --output /tmp/htsave-live-claude \
  --host claude \
  --path shell \
  --model claude-haiku-4-5-20251001 \
  --payload-lines 2048 \
  --confirm-paid-runs

rtk uv run htsave benchmark report /tmp/htsave-live-claude/manifest.json
```

**備選機制**：
- canonical Haiku 順利完成全部 80 次執行後，記錄完整中位數數據；本次
  80/80 完成但四個 30% savings gate 均未通過。
- 若部分 Scenario 因結尾格式失敗，保留 failed attempt 並在同一 manifest
  上 resume；不可用另一個模型覆蓋原始結果。

---

### Phase 5: Verify Agent 獨立檢核與公平性報告生成 (`verify.md` & 文檔更新)

在各平台實測執行完畢後，由 **Verify Agent（獨立驗證稽核者）** 進行嚴格覆核，確保所有數據**公平、公正、公開、無造假**：

1. **Verify Agent 獨立稽核清單 (Audit Checklist)**：
   - 🔍 **原始日誌核對**：逐一檢驗 `/tmp/htsave-live-*` 目錄中的 `manifest.json`、`stdout.txt`、`stderr.txt` 與官方 JSONL Token Usage 記錄。
   - 🔍 **隔離性覆核**：確認 Baseline 與 Treatment 均在獨立拋棄式 Home 目錄下運行，無快取污染或自訂 hook 干擾。
   - 🔍 **真實計費驗證**：確認所有 Token 數據均來自官方 API 實際扣款計費欄位（`input_tokens`, `output_tokens`），杜絕任何模擬假數據。
   - 🔍 **100% 無損驗證**：確認已執行的 160 次 Codex/Claude attempt 均通過
     `answer.schema.json` 與 Oracle 比對；agy 沒有 token manifest，另記為 unverified。

2. **[NEW] 生成 Repo 根目錄的公平性驗證報告 `verify.md`**：
   - **公平性保證協定 (Fairness & Audit Protocol)**
   - **可用平台 $\times$ 4 大情境完整 10-Pair 實測數據矩陣**（Baseline
     Tokens、Treatment Tokens、節省比例 %）；不虛構 agy 或價格數據。
   - **第三方獨立重現指南 (Reproduction Guide)**：任何人皆可按指令自行驗證。

3. **更新 `README.md` & `README.zh-TW.md`**：
   - 在 `## Runtime paths` / `## 執行路徑` 之後新增實測成效表並連結 `[verify.md](verify.md)`。
4. **更新 `docs/release-gates.md`**：
   - 記錄 80 次 Codex 與 80 次 Claude 實測 usage 證據；Codex MCP gate
     綠燈，Claude savings gate 綠燈（v6），agy savings gate 紅燈（80/80 有效實證結果）。

---

### Phase 6: 全套迴歸測試與打包驗收

```bash
# 全套本地單元/整合測試
rtk uv run pytest -q

# 代碼風格檢查
rtk uv run ruff check src tests

# 套件打包驗證
rtk uv build
```

---

## 產出與驗收標準

1. **可用平台超大壓力樣本規模（80 Runs / Codex、80 Runs / Claude，共 160 實機執行）**：完整覆蓋 4 大情境、10 輪重複抽樣，產出中位數報告；agy 僅有整合 smoke check。
2. **Verify Agent 檢核**：完成兩份 live manifest、原始 usage、隔離性與無損 oracle 稽核，並如實保留 red/unverified gate。
3. **生成 `verify.md`**：於 repo 根目錄生成詳盡、客觀的第三方公平性驗證與重現指南。
4. **README 價值呈現**：雙語 README 具備清晰的 4 大情境實測數據對照表與 `verify.md` 索引，讓使用者一目了然看見真實商業價值。

## Execution record

- Codex MCP: `/tmp/htsave-live-codex-luna-v14/manifest.json`, 80/80，四個
  scenario 中位數 30.43%–41.69%，report passed。
- Claude shell: `/tmp/htsave-live-claude-v6/manifest.json`, 80/80，四個
  scenario 中位數 92.40%–94.91%，report passed。（v5 manifest 已由 v6 取代；
  v5 的 0.70%、-3.03%、9.76%、7.00% 紅燈數據不可作為當前結果發布。v6 修正了
  `modelUsage` parser 僅讀最後一個 iteration 的問題。）
- agy MCP: `/tmp/htsave-live-agy-v2/manifest.json`, 80/80，四個 scenario
  中位數 -18.69%–26.07%，report failed savings gate；所有答案仍通過 oracle。
  此為有效的 red 實證結果，非執行失敗。（偏差：原計畫假設 agy 僅做 smoke
  check；實際已實作 agy benchmark host 並完成 80 次實機執行。）
- agy MCP v3（零 hydrate 重跑）: `/tmp/htsave-agy-v3/manifest.json`
  80/80 完成，`large_readme_exact` 中位數 −18.69%→−3.77%（+14.92 點），
  四個 gate 仍紅；`/tmp/htsave-agy-pro-v2/manifest.json` 於 agy 1.1.20
  中途撞個人 quota、以間隔 resume 補跑。逐回合鑑識、工程修復與最終表格
  詳見 [verify.md](verify.md) 的「agy red-gate forensics」。
- 文件與驗證命令結果集中於 [verify.md](verify.md)。
