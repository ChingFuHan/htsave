# htsave v1 architecture

`htsave` is a local repeated-context transport for agent CLIs. Its correctness
boundary is exact UTF-8 bytes, not semantic similarity. The domain package does
not know about hook JSON or MCP; per-host adapters translate supported events
into domain operations and fail open at that boundary.

Three hosts are supported, and they differ in one decisive way on the
transparent path. Claude Code honors `hookSpecificOutput.updatedToolOutput`,
so repeated tool results are replaced transparently. Codex CLI has no
equivalent successful-result response, so there its `PostToolUse` adapter can
only observe. Antigravity agy uses explicit MCP tools and fail-open lifecycle
hooks; like Codex, it has no transparent PostToolUse replacement contract.

## Non-negotiable invariants

1. **Raw-byte identity.** The object id is `sha256:<hex>` over the original
   UTF-8 bytes. Newlines, Unicode normalization, trailing whitespace, and a
   missing final newline are never rewritten.
2. **Session isolation.** A Codex `session_id` is SHA-256-derived into a local
   directory name. Every session owns a separate immutable CAS and SQLite
   registry. There is no global object lookup and no cross-session reuse.
3. **Storage is not possession.** A CAS hit alone can never authorize REF or
   DELTA. The target or base needs a confirmed receipt in the active context
   generation.
4. **Generation invalidation.** `startup`, `resume`, `clear`, and `compact`
   create a new generation. Old receipts remain auditable but cannot authorize
   a transformed response.
5. **Durable-before-delivery.** The full target is fsynced to CAS and registered
   before a FULL, REF, or DELTA is returned. A crash can leave an unreferenced
   CAS object, never a receipt whose target was intentionally omitted from CAS.
6. **Verified deltas.** `unified-diff-v1` creation immediately reapplies the
   patch, checks the base hash, and verifies the reconstructed target hash.
   Hydration repeats those checks and compares against the full target in CAS.
7. **Bounded delta chains.** A DELTA is eligible only when its complete
   model-facing envelope saves at least 20% and 128 estimated tokens. A would-be
   eighth consecutive delta, or cumulative delta cost above 60% of the current
   full content, becomes a FULL checkpoint.
8. **Delivery acknowledgement.** A receipt begins pending. Only a later,
   contract-valid Codex lifecycle signal may confirm it. Concurrent or ambiguous
   consumers remain FULL/BYPASS; a pending transformed target cannot become a
   base for another transformed delivery.
9. **Fail open at compatibility boundaries.** Unknown Codex versions, unknown
   response shapes, unsupported tool content, subagent ambiguity, registry/CAS
   failure, and hook failure preserve the original tool result. Security checks
   (workspace containment, symlink escape, session capability, and hashes) fail
   closed instead of leaking data.
10. **Estimates are labelled estimates.** Policy uses `tiktoken`; local counters
    are never presented as billed usage. Benchmarks use only the public
    `codex exec --json` terminal usage event.

## Layer ownership

```text
Codex lifecycle hooks        htsave MCP tools          htsave CLI
          |                         |                      |
          +----------- Codex adapters / trust boundary --+
                                    |
                      ContextEngine + transport codec
                                    |
                 immutable CAS + SQLite WAL registry
```

- `hashing`, `delta`, and `transport` own byte identity and reversible codecs.
- `engine` owns FULL/REF/DELTA/BYPASS policy, receipts, checkpoints, hydration,
  and estimated token accounting.
- `cas`, `registry`, and `paths` own local durability, migrations, concurrency,
  permissions, and session isolation.
- `codex_hooks`, `claude_hooks`, and `agy_hooks` own per-host event parsing,
  generation transitions, subagent ambiguity, and one-use MCP session metadata.
  Only `claude_hooks` emits a replacement result; `agy_hooks` injects a
  one-time `_htsave_context` capability into `call_mcp_tool` nested Arguments.
- `compat` owns host detection and what each host's contract permits.
- `mcp_server` owns workspace path authorization and explicit read/hydrate
  results.
- `plugin` (Codex marketplace), `claude_install` (Claude Code `settings.json`),
  and `agy_install` (Antigravity `~/.gemini/config/`) own installation, and
  `cli` owns operator actions. They never delete session data implicitly, and
  each install module only ever adds or removes entries it has tagged as its own.

## Lifecycle state

```text
SessionStart(startup|resume|clear) -> new generation
SessionStart(compact)              -> new generation + queued FULL recovery

tool result -> CAS -> event + pending receipt
later valid PreToolUse/Stop -> confirmed receipt
confirmed receipt + exact hash -> REF
confirmed same-source head      -> verified DELTA or FULL checkpoint

SubagentStart -> parent session FULL-only
last SubagentStop -> new root generation
PreCompact -> freeze generation + queue pending transformed target
```

SQLite runs in WAL mode with foreign keys, `BEGIN IMMEDIATE` writer
transactions, `synchronous=FULL`, an ordered migration history, and a stored
session-key binding. A newer schema is rejected. Migration retries are
idempotent, and transaction failures roll back without partial logical rows.

## Claude Code contract boundary

Claude Code exposes `hookSpecificOutput.updatedToolOutput`, documented as
"Replaces the tool output before it is sent to the model", for every tool. The
narrower `updatedMCPToolOutput` also exists and is marked legacy in favor of it.
`PreToolUse` additionally supports `updatedInput`, which carries the one-use
htsave capability into the MCP tools. So on this host the transparent path runs:
a repeated result is delivered as REF or verified DELTA in the original response
shape, with the same receipt, generation, and checkpoint rules as every other
path.

Its payload is narrower than Codex's, and three differences are load-bearing:

- there is no `turn_id`; `prompt_id` is the analogue and is itself optional, so
  the tool-use id is the last-resort turn identity;
- `model` is absent from every `claude -p` event, so it is recorded on the
  generation when present and the token estimator falls back to its default
  encoding when it is not;
- `permission_mode` is absent on `SessionStart`.

Only results whose model-facing bytes are unambiguous are ingested: `Bash`
stdout when stderr is empty and the call was neither interrupted nor an image,
`Read` `file.content`, and single-text non-error MCP results. Note that the
bytes htsave preserves are the bytes the *tool delivered*: Claude Code strips
the trailing newline from Bash stdout before any hook observes it, so a
hydrated Bash result matches that stdout exactly rather than the file on disk.

Claude Code also de-duplicates an identical repeated `Read` of an unchanged file
on its own, so htsave adds nothing in that specific case. It adds savings where
the host does not de-duplicate: repeated command output, repeated MCP results,
and re-reads of a file that changed slightly, which become verified deltas.

## Codex 0.148.0 contract boundary

The official release contract exposes successful input replacement on
`PreToolUse`, but no successful arbitrary output replacement on `PostToolUse`.
`updatedMCPToolOutput` and `suppressOutput` are parsed and rejected;
`decision: "block"` rejects nested code-mode promises, and `continue: false`
changes result/control semantics. Therefore:

- the explicit `htsave_read` / `htsave_hydrate` MCP path can return FULL, REF,
  and DELTA;
- `PostToolUse` can safely observe and ingest supported text while returning
  `{}` so the original result is unchanged;
- transparent token-saving replacement is release-gated off until Codex ships
  an official successful-result replacement contract.

The last item is a product release blocker, not an implementation shortcut.
Using a blocking hook response would violate the required transparent and
fail-open semantics.

Official contracts:

- <https://developers.openai.com/codex/hooks>
- <https://developers.openai.com/codex/plugins>
- <https://developers.openai.com/codex/noninteractive>

## Explicit non-goals

No semantic deduplication, embeddings, RAG, LLM summaries, distributed cache,
telemetry, automatic GC, Web UI, transcript parsing, hosted-tool interception,
app-server/wire proxy, or adapters for non-Codex agents.
