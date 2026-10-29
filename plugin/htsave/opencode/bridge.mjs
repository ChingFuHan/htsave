// OpenCode v1.18.23: packages/plugin/src/index.ts and session/tools.ts.
// Rendered with an ownership header and PYTHON constant by opencode_install.
import { execFileSync } from "node:child_process";
import { createHash } from "node:crypto";

const digest = (text) => createHash("sha256").update(text, "utf8").digest("hex");
const object = (value) => value !== null && typeof value === "object" && !Array.isArray(value);
const identity = (value) => typeof value === "string" && value.length > 0;
const ownTools = new Set(["htsave_htsave_read", "htsave_htsave_hydrate"]);

function callHook(event, payload, cwd) {
  try {
    const result = execFileSync(PYTHON, ["-m", "htsave.opencode_hooks", event], {
      input: JSON.stringify(payload), encoding: "utf8", cwd,
      timeout: 5000, maxBuffer: 16 * 1024 * 1024, stdio: ["pipe", "pipe", "pipe"],
    });
    const parsed = JSON.parse(result);
    if (!object(parsed) || !Number.isSafeInteger(parsed.generation) || parsed.generation < 1) return {};
    if ("context" in parsed && (!Array.isArray(parsed.context) ||
        parsed.context.some((text) => typeof text !== "string"))) return {};
    if ("receipt" in parsed &&
        (!Number.isSafeInteger(parsed.receipt) || parsed.receipt < 1)) return {};
    if ("input" in parsed && !object(parsed.input)) return {};
    if ("result" in parsed && textResult(parsed.result) === undefined) return {};
    return parsed;
  } catch {
    return {};
  }
}

function textResult(result) {
  if (!object(result) || result.isError || result.attachments?.length ||
      result.structuredContent != null || result.metadata?.truncated || result.metadata?.error) return;
  if (object(result.metadata) && "exit" in result.metadata && result.metadata.exit !== 0) return;
  if ("content" in result) {
    if (Array.isArray(result.content) && result.content.length === 1 &&
        result.content[0]?.type === "text" && typeof result.content[0].text === "string") {
      return result.content[0].text;
    }
    return;
  }
  if (typeof result.output === "string") return result.output;
}

export default async function htsave({ directory }) {
  const sessions = new Map();
  let enabled = true;
  let modelContext = {messages: []};
  let contextEpoch = 0;
  const invoke = (event, payload) => callHook(event, { ...payload, cwd: directory }, directory);

  function reset(sessionID, previous, event = "session-start") {
    const result = invoke(event, {
      sessionId: sessionID,
      recover: previous ? [...previous.deliveries.keys()] : [],
    });
    const state = {
      generation: result.generation,
      disabled: !Number.isSafeInteger(result.generation),
      deliveries: new Map(), proposed: [], ready: [], observed: false, observedEpoch: -1,
      recovery: [...(previous?.recovery ?? []), ...(result.context ?? [])],
    };
    sessions.set(sessionID, state);
    return state;
  }

  function session(sessionID) {
    if (!enabled || !identity(sessionID)) return;
    return sessions.get(sessionID) ?? reset(sessionID);
  }

  function observe(id) {
    let state = session(id);
    if (!state || state.disabled || state.observedEpoch === contextEpoch) return state;
    const texts = new Map();
    for (const message of Array.isArray(modelContext.messages) ? modelContext.messages : []) {
      if (!object(message) || message.info?.sessionID !== id || !Array.isArray(message.parts)) continue;
      for (const part of message.parts) {
        if (!object(part) || part.type !== "tool" || part.state?.status !== "completed" ||
            part.state.time?.compacted || part.state.attachments?.length ||
            part.state.metadata?.truncated || typeof part.state.output !== "string") continue;
        texts.set(part.callID, digest(part.state.output));
      }
    }
    if ([...state.deliveries].some(([call, record]) =>
      record.confirmed && texts.get(call) !== record.hash)) state = reset(id, state);
    state.proposed = [...state.deliveries]
      .filter(([call, record]) => texts.get(call) === record.hash)
      .map(([, record]) => record.receipt);
    state.observed = true;
    state.observedEpoch = contextEpoch;
    return state;
  }

  return {
    async config(config) {
      const expected = { type: "local", command: [PYTHON, "-m", "htsave.mcp_server"], cwd: directory, enabled: true };
      config.mcp ??= {};
      if (config.mcp.htsave !== undefined) {
        // A config entry has no ownership marker: leave it alone, even if similar.
        enabled = false;
        return;
      }
      config.mcp.htsave = expected;
    },

    async "tool.execute.before"(input, output) {
      if (!enabled || !object(input) || !object(output?.args)) return;
      // Never forward model-supplied session authority, including invalid events.
      if (ownTools.has(input.tool)) delete output.args._htsave_context;
      if (!identity(input.callID)) return;
      const state = session(input.sessionID);
      if (!state || state.disabled) return;
      const response = invoke("pre-tool-use", {
        sessionId: input.sessionID, toolUseId: input.callID, tool: input.tool,
        input: output.args, generation: state.generation, receipts: state.ready,
      });
      if (response.generation !== state.generation) {
        state.disabled = true;
        return;
      }
      for (const record of state.deliveries.values()) {
        if (state.ready.includes(record.receipt)) record.confirmed = true;
      }
      if (state.observed) state.recovery = [];
      if (ownTools.has(input.tool) && object(response.input)) {
        // OpenCode continues using the original args object after the hook.
        Object.assign(output.args, response.input);
      }
    },

    async "tool.execute.after"(input, output) {
      if (!object(input) || !identity(input.callID) || !object(input.args)) return;
      const state = session(input.sessionID);
      if (!state || state.disabled || textResult(output) === undefined) return;
      const response = invoke("post-tool-use", {
        sessionId: input.sessionID, toolUseId: input.callID, tool: input.tool,
        input: input.args, result: output, status: "completed", generation: state.generation,
      });
      if (response.generation !== state.generation) {
        state.disabled = true;
        return;
      }
      if (object(response.result)) {
        if (textResult(response.result) === undefined) { state.disabled = true; return; }
        // Preserve host metadata, attachments and result object identity.
        if ("content" in output) output.content = response.result.content;
        else output.output = response.result.output;
      }
      const text = textResult(output);
      if (Number.isSafeInteger(response.receipt) && typeof text === "string") {
        const prior = state.deliveries.get(input.callID);
        state.deliveries.set(input.callID, {
          receipt: response.receipt, hash: digest(text), confirmed: prior?.confirmed ?? false,
        });
      }
    },

    async "experimental.chat.messages.transform"(_input, output) {
      // Keep the shared object until all message-transform hooks have finished.
      // Later plugins can prune or rewrite parts after this callback returns.
      modelContext = object(output) ? output : {messages: []};
      contextEpoch++;
    },

    async "chat.params"(input) {
      if (!object(input)) return;
      const state = observe(input.sessionID);
      if (!state || state.disabled) return;
      // Freeze before the model emits a batch of possibly parallel tool calls.
      state.ready = state.observed ? [...state.proposed] : [];
      state.proposed = [];
    },

    async "experimental.chat.system.transform"(input, output) {
      if (!object(input) || !object(output)) return;
      const state = observe(input.sessionID);
      if (!state || state.disabled || !Array.isArray(output.system)) return;
      output.system.push(HTSAVE_INSTRUCTIONS, ...state.recovery);
    },

    async "experimental.session.compacting"(input, output) {
      if (!object(input) || !object(output)) return;
      const previous = session(input.sessionID);
      if (!previous) return;
      const state = reset(input.sessionID, previous, "compact");
      if (Array.isArray(output.context)) output.context.push(...state.recovery);
    },

    async dispose() {
      // No acknowledgement: disposal can follow cancellation or a failed request.
      sessions.clear();
    },
  };
}
