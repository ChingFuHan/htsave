# htsave v1 architecture

`htsave` is a local repeated-context transport for agent CLIs. Its correctness
boundary is exact UTF-8 bytes, not semantic similarity. The domain package does
not know about hook JSON or MCP; per-host adapters translate supported events
into domain operations and fail open at that boundary.

Four host adapters exist. Claude Code and Codex CLI have release-gated paths,
and Antigravity agy has an installed but red explicit-MCP path. Claude Code
honors `hookSpecificOutput.updatedToolOutput`, so repeated tool results are
replaced transparently. OpenCode CLI exposes `tool.execute.after` mutation through
its plugin API, but its htsave adapter remains experimental and has no live
benchmark evidence. Codex CLI has no equivalent successful-result response, so
there its `PostToolUse` adapter can only observe. Antigravity agy uses explicit
MCP tools and fail-open lifecycle hooks; like Codex, it has no transparent
PostToolUse replacement contract.

## Non-negotiable invariants

1. **Raw-byte identity.** The object id is `sha256:<hex>` over the original
   UTF-8 bytes. Newlines, Unicode normalization, trailing whitespace, and a
   missing final newline are never rewritten.
2. **Session isolation.** A host `session_id` is SHA-256-derived into a local
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
   contract-valid host lifecycle signal may confirm it. Concurrent or ambiguous
   consumers remain FULL/BYPASS; a pending transformed target cannot become a
   base for another transformed delivery.
9. **Fail open at compatibility boundaries.** Unparseable Codex versions, invalid
   event schemas, unsupported tool content, subagent ambiguity, registry/CAS
   failure, and hook failure preserve the original tool result. Security checks
   (workspace containment, symlink escape, session capability, and hashes) fail
   closed instead of leaking data, subject to the explicit raw-read fallback
   described under MCP read boundary. A parseable Codex version enables a
   contract attempt; event validation is separate and accepts additive fields.
10. **Estimates are labelled estimates.** Policy uses `tiktoken`; local counters
    are never presented as billed usage. Benchmarks use host-specific parsers:
    Codex `input_tokens` includes cached input; Claude prefers full-session
    `modelUsage.*.inputTokens` with cache reads separate, while its fallback sums
    iteration input, cache creation, and cache reads on a distinct accounting
    basis; agy reports terminal `input_tokens` and cache reads separately.
    These metrics do not establish cross-host costs or official prices.

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
- `codex_hooks`, `claude_hooks`, `agy_hooks`, and the experimental
  `opencode_hooks` own
  per-host event parsing, generation transitions, subagent ambiguity, and
  one-use MCP session metadata. `claude_hooks` and `opencode_hooks` emit
  replacement results; `agy_hooks` injects a one-time `_htsave_context`
  capability into `call_mcp_tool` nested Arguments.
- `compat` owns host detection and what each host's contract permits.
- `mcp_server` owns workspace path authorization and explicit read/hydrate
  results.
- `plugin` (Codex marketplace), `claude_install` (the Claude Code plugin in
  `plugin/claude-code`, installed through `claude plugin`), `agy_install`
  (Antigravity `~/.gemini/config/`), and `opencode_install` (OpenCode
  `.opencode/plugins/`) own installation, and `cli` owns operator actions. They
  never delete session data implicitly, and each install module only ever adds
  or removes entries it has tagged as its own. Claude Code can drop that tag
  from hook handlers in `settings.json`, so legacy cleanup also recognizes a
  handler by its `-m htsave.claude_hooks` command.

## Lifecycle state

```text
Claude/Codex SessionStart(startup|resume|clear) -> new generation
Claude/Codex SessionStart(compact)              -> new generation + queued FULL recovery
agy SessionStart/Stop                           -> generation and receipt lifecycle
OpenCode model input + later tool call           -> scoped receipt confirmation

tool result -> CAS -> event + pending receipt
later valid host acknowledgement -> confirmed receipt
confirmed receipt + exact hash -> REF
confirmed same-source head      -> verified DELTA or FULL checkpoint

SubagentStart -> parent session FULL-only
last SubagentStop -> new root generation
PreCompact -> freeze generation + queue pending transformed target
```

Lifecycle coverage is host-specific. Claude Code and Codex expose compact and
subagent events; agy exposes its explicit MCP lifecycle hooks but no transparent
result replacement. OpenCode uses tool hooks, structured model-input hooks and
compaction; child sessions remain isolated by host session ID. Its disposal hook
never confirms receipts. Context loss rotates the generation and preserves
exact recovery from CAS.

SQLite runs in WAL mode with foreign keys, `BEGIN IMMEDIATE` writer
transactions, `synchronous=FULL`, an ordered migration history, and a stored
session-key binding. A newer schema is rejected. Migration retries are
idempotent, and transaction failures roll back without partial logical rows.

## Claude Code contract boundary

Claude Code exposes `hookSpecificOutput.updatedToolOutput`, documented as
"Replaces the tool output before it is sent to the model", for every tool. The
narrower `updatedMCPToolOutput` also exists and is marked legacy in favor of it.
`PreToolUse` additionally supports `updatedInput`, which carries the one-use
htsave capability into the MCP tools. Delivered as a plugin, those tools are
named `mcp__plugin_htsave_htsave__*`; the hook binds their capabilities to the
canonical `mcp__htsave__*` names the MCP server checks. So on this host the
transparent path runs:
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

## Codex hook contract boundary

The documented Codex hook contract exposes successful input replacement on
`PreToolUse`, but no successful arbitrary output replacement on `PostToolUse`.
The adapter follows Caveman's compatibility shape: a parseable CLI version
only opts into trying the adapter; each event still validates its required
fields, and a changed or unknown event shape fails open. Additive fields are
ignored. Therefore:

- the explicit `htsave_read` / `htsave_hydrate` MCP path can return FULL, REF,
  and DELTA;
- `PostToolUse` can safely observe and ingest supported text while returning
  `{}` so the original result is unchanged;
- `PreToolUse.updatedInput` remains the transformed-delivery capability boundary.
  If a host does not pass it, `htsave_read` performs a raw workspace-contained
  read without consulting session cache; `htsave_hydrate` rejects missing
  capability metadata.
- transparent token-saving replacement is release-gated off until Codex ships
  an official successful-result replacement contract.

The last item is a product release blocker, not an implementation shortcut.
Using a blocking hook response would violate the required transparent and
fail-open semantics.

## MCP read boundary

`htsave_read` accepts relative or absolute paths only after canonical resolution
proves that the result stays inside the active workspace. It rejects symlink
escapes, directories, special files, invalid UTF-8, and invalid line ranges.
Missing, malformed, or expired read capability metadata takes a raw read inside
`fallback_workspace` or the process working directory without session cache
reuse. An accepted capability binds the session workspace; read failures raise
and never switch to fallback workspace. After a successful read,
the MCP reader catches `HtsaveError`, `OSError`, and `sqlite3.Error` from
`ContextEngine.decide` and returns the exact raw text. Other failures after capability acceptance propagate.
`htsave_hydrate` always requires a valid capability and a reference
owned by the same session. FULL results may remain plain text; `HTSAVE/1` is
used for transformed deliveries and recovery.

## OpenCode experimental boundary

The installer writes `.opencode/plugins/htsave.js` and a native project skill.
Its generated plugin follows the OpenCode 1.18.23 hook map, mutating the original
argument/result objects rather than replacing hook-local object references.
Host `sessionID` identifies state; `callID` identifies a delivery.

The bridge records the exact emitted-text hash and receipt ID. It retains the
structured model-input object until all message-transform hooks finish, then
matches completed, unpruned text parts against those hashes. A subsequent
`chat.params` freezes the candidate receipt IDs; only a later tool-before event
confirms those IDs in the same generation. New outputs in a parallel batch stay
pending. MCP post-hooks validate the delivered bytes/frame against the recorded
delivery before remembering the receipt; they do not re-ingest it.

Missing identities, stale generations, malformed hook replies, subprocess
failure and ambiguous results preserve original output. Restart, compaction or
loss/change of previously confirmed text invalidates the generation; pending
transformed deliveries and known transformed targets are recovered as exact FULL
context from CAS. Recovery does not automatically establish possession. Hydration
requires a current capability and session-owned, hash-verified content.

Local fake-model tests exercise both native and MCP paths through the actual
1.18.23 host. Other versions and real-model savings remain unverified. See
[OpenCode evidence and limits](release-gates.md#opencode-experimental-integration--no-release-savings-gate)
for pinned upstream sources and acceptance commands.

Official contracts:

- <https://developers.openai.com/codex/hooks>
- <https://developers.openai.com/codex/plugins>
- <https://developers.openai.com/codex/noninteractive>

## Explicit non-goals

No semantic deduplication, embeddings, RAG, LLM summaries, distributed cache,
telemetry, automatic GC, Web UI, transcript parsing, hosted-tool interception,
or app-server/wire proxy.
