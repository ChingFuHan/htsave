# Antigravity CLI (agy) hook and tool contract reference

Reverse-engineered from the installed `agy` 1.1.20 binary (embedded
documentation plus the `exa.hooks_pb` proto descriptors) and verified against
live sessions. This is the authoritative map behind htsave's agy adapter and
the red savings gate documented in `docs/release-gates.md`.

## Lifecycle events

| Event | Fires | Input payload keys | Output contract |
| :--- | :--- | :--- | :--- |
| `SessionStart` | **yes, undocumented** | common only (`conversationId`, `workspacePaths`, `transcriptPath`, `artifactDirectoryPath`, `modelName`) | `{injectSteps}` |
| `PreToolUse` | before each tool | common + `toolCall{name,args}`, `stepIdx` | `{decision, reason, overwrite, permissionOverrides}` plus undocumented `allowTool`, `denyReason`; `decision` ∈ allow/deny/ask/force\_ask; `overwrite` is a shallow top-level args merge |
| `PostToolUse` | after each tool step | common + `stepIdx`, `toolCall`, `error` (when failed) | **empty object only** — `PostToolHookResult` has zero fields in the proto |
| `PreInvocation` | before every model call | common + `invocationNum`, `initialNumSteps` | `{injectSteps}` |
| `PostInvocation` | after tool calls finish | common + `invocationNum`, `initialNumSteps`, `modelOutput`, `modelThinking` | `{injectSteps, terminationBehavior}` (`force_continue`/`terminate`) |
| `Stop` | execution loop end | common + `executionNum`, `terminationReason`, `fullyIdle`, `finalModelOutput` | `{decision: "continue", reason}` |

Common fields on every payload: `conversationId`, `workspacePaths`,
`transcriptPath`, `artifactDirectoryPath`, `modelName`. All JSON keys are
camelCase (protojson).

## Injected steps

`injectSteps` entries accept exactly one of:

- `{"toolCall": {"name": ..., "args": {...}}}`
- `{"userMessage": "..."}`
- `{"ephemeralMessage": "..."}` — transient system message

Live-measured billing semantics: an injected message bills once on the turn
where it appears and does **not** persist into later turns' contexts (an
~8.8 KB ephemeral raised the next turn's input by ~0.2k tokens instead of its
own ~1.3k). A `SessionStart` `userMessage` reaches the model and is answerable
(verified with a secret-fact probe).

## Why no transparent replacement exists

The `hooks_pb` descriptor list ends at `PostToolHookResult` with no fields;
there is no `updatedToolOutput`, result-override, or suppress-style key
anywhere in the response protos. `PreToolUse.overwrite` can rewrite tool
*arguments* (this is how htsave injects `_htsave_context`) but cannot change
which tool runs, so native reads cannot be redirected onto htsave's MCP
tools. Output replacement therefore remains impossible without an upstream
contract change, and htsave's agy token path is MCP-only.

## Tool output spill ("brain") files

Large tool results are intercepted by an internal output-truncation decorator
(`tooldecorators.newOutputTruncationDecorator`, `boundedResult`,
`writeFullOutput`): full bytes are written under
`.gemini/antigravity-cli/brain/<conversation>/.system_generated/steps/<n>/output.txt`
and the model context receives a short summary (for example `2053 lines,
238705 bytes`). The limit lives in internal protos
(`McpToolConfig.max_output_bytes`, global `ToolOutputConfig.max_output_bytes`)
with **no settings.json, flag, or environment surface**, so it cannot be
tuned from outside. Combined with server-side KV caching this is why neither
benchmark arm ever re-bills full content on agy; see
`docs/release-gates.md` for the resulting structural red gate.

## Other observations

- `PreToolUse` handlers also fire inside subagent flows; there is still no
  subagent identity signal, so htsave keeps FULL-only delivery on agy.
- The genai content layer defines `CACHE_CONTROL_TYPE_EPHEMERAL`, but no hook
  or user-facing setting exposes per-request cache control.
- Hooks run synchronously via `sh -c` with cwd = the directory containing
  `hooks.json`; only `type: "command"` exists (no HTTP/prompt hooks).
