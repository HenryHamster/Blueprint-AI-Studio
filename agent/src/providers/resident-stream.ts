/**
 * Pure, dependency-free parsing for the Claude Code bidirectional
 * `stream-json` protocol as consumed by the RESIDENT strategic session
 * (see runners.ts `startResidentClaudeSession`). Kept free of any `../index.js`
 * import so it is trivially unit-testable in isolation (resident-stream.test.ts)
 * and so the session wrapper can share one classifier.
 *
 * The resident session runs `claude -p --output-format stream-json
 * --input-format stream-json --include-partial-messages --verbose`, which emits
 * one JSON object per stdout line. This module classifies those lines into a
 * small set of semantic events the session state machine acts on:
 *   - text          incremental delta (from --include-partial-messages) OR a
 *                   finalized assistant text block
 *   - tool          a top-level Bash/other tool_use (carries the command + id)
 *   - tool_result   a tool's execution outcome (carries the tool_use_id + is_error)
 *   - result        a TOP-LEVEL result event = the current turn is complete
 *   - error         a structured provider error event
 *   - system        init / keepalive / tool-result-less user (carries session_id on init)
 *   - other         non-JSON log line or unknown type
 *
 * Nested events (a sub-agent the model spawned via the Task/Agent tool, marked
 * by a non-empty `parent_tool_use_id`) are flagged `nested:true`; the session
 * treats a nested `result` as sub-progress, never as the turn's completion.
 */

export type ResidentEventKind = 'text' | 'tool' | 'tool_result' | 'result' | 'error' | 'system' | 'other';

/**
 * Per-turn token usage carried on a top-level `result` event. Mirrors the
 * Anthropic result-usage block the one-shot runner already records
 * (runners.ts): `inputTokens` is the INCLUSIVE sum of the three input classes so
 * downstream telemetry stays backward-compatible, with the two cache classes
 * broken out for true-rate pricing.
 */
export interface ResidentTokenUsage {
  inputTokens: number;
  outputTokens: number;
  cacheCreationTokens: number;
  cacheReadTokens: number;
}

export interface ResidentStreamEvent {
  kind: ResidentEventKind;
  /** delta / assistant text for 'text'; result text for 'result'; message for 'error' */
  text?: string;
  /** tool name for 'tool' */
  toolName?: string;
  /** bash command string for a Bash 'tool' event (used to detect applied strategic ops) */
  command?: string;
  /** the CLI-assigned tool_use id — set on a 'tool' event (block.id) and on the
   *  matching 'tool_result' event (block.tool_use_id) so the session can
   *  correlate a `./messages send` invocation with its execution outcome. */
  toolUseId?: string;
  /** for a 'tool_result' event: the tool reported an error (non-zero exit for Bash). */
  isError?: boolean;
  /** session id when the event carried one (system init, result) */
  sessionId?: string;
  /** true for a TOP-LEVEL result event — the completion signal for the active turn */
  isTurnResult?: boolean;
  /** true for a finalized top-level assistant text block (vs a streamed delta) */
  isAssistantMessage?: boolean;
  /** true when the event came from a nested sub-agent (parent_tool_use_id set) */
  nested?: boolean;
  /** token usage parsed from a top-level 'result' event (when the CLI reported it) */
  usage?: ResidentTokenUsage;
}

/**
 * Extract the per-turn token usage from a `result` object's `usage` block, or
 * undefined when absent/empty. `inputTokens` folds in the two cache classes so
 * total-input telemetry stays backward-compatible (see runners.ts).
 */
export function extractResultUsage(obj: Record<string, unknown>): ResidentTokenUsage | undefined {
  const usage = obj.usage;
  if (!usage || typeof usage !== 'object') return undefined;
  const u = usage as Record<string, unknown>;
  const cacheCreationTokens = Number(u.cache_creation_input_tokens ?? 0) || 0;
  const cacheReadTokens = Number(u.cache_read_input_tokens ?? 0) || 0;
  const inputTokens = (Number(u.input_tokens ?? 0) || 0) + cacheCreationTokens + cacheReadTokens;
  const outputTokens = Number(u.output_tokens ?? 0) || 0;
  if (inputTokens <= 0 && outputTokens <= 0) return undefined;
  return { inputTokens, outputTokens, cacheCreationTokens, cacheReadTokens };
}

/**
 * Split a raw stdout chunk into complete NDJSON lines, carrying any trailing
 * partial line forward. Returns the complete non-empty lines plus the new carry.
 */
export function splitStreamChunk(carry: string, chunk: string): { lines: string[]; carry: string } {
  const combined = carry + chunk;
  const lastNewline = combined.lastIndexOf('\n');
  if (lastNewline < 0) return { lines: [], carry: combined };
  const complete = combined.slice(0, lastNewline);
  const rest = combined.slice(lastNewline + 1);
  return { lines: complete.split('\n').filter(l => l.length > 0), carry: rest };
}

function readSessionId(obj: Record<string, unknown>): string | undefined {
  const sid = obj.session_id;
  return typeof sid === 'string' && sid ? sid : undefined;
}

/**
 * Extract incremental text from an `--include-partial-messages` stream_event.
 * Only `content_block_delta` → `text_delta` produces operator-visible chat text;
 * thinking_delta and other SSE deltas are ignored (liveness only).
 */
export function extractDeltaText(obj: Record<string, unknown>): string | undefined {
  const ev = obj.event;
  if (!ev || typeof ev !== 'object') return undefined;
  const e = ev as Record<string, unknown>;
  if (e.type !== 'content_block_delta') return undefined;
  const delta = e.delta;
  if (!delta || typeof delta !== 'object') return undefined;
  const d = delta as Record<string, unknown>;
  if (d.type === 'text_delta' && typeof d.text === 'string') return d.text;
  return undefined;
}

const STRATEGIC_MUTATING_VERBS = new Set([
  'set-problem-model', 'set-running-update', 'reflect', 'pending-confirm', 'set-vision',
  'create-direction', 'update-direction', 'retire-direction', 'stage-direction',
  'approve-direction', 'reject-direction', 'create-message', 'resolve-message',
  'spawn-task', 'spawn-fix', 'set-retirement-critiques', 'set-evidence-progress',
  'set-acceptance-criteria', 'cite-memos', 'set-strategic-challenge',
]);

/**
 * True when a Bash command invokes a MUTATING `./strategic <verb>` op. Reads
 * (`./fetch`, `./design-doc get`) do NOT count — only an applied mutation is a
 * lease-renewing "progress" event. Used by the session to set
 * `appliedStrategicOp` so the handler renews the authority lease only on real
 * progress (the dead-man's-switch contract), never bare liveness.
 */
export function isStrategicMutation(command: string | undefined): boolean {
  if (!command) return false;
  const m = command.match(/(?:^|[\s;&|(])(?:\.\/)?strategic\s+([a-z][a-z-]*)/);
  if (!m) return false;
  return STRATEGIC_MUTATING_VERBS.has(m[1]);
}

/**
 * True when a Bash command invokes `./messages send …` — the agent posting a
 * durable operator-facing reply (ProjectMessage, `from_type=strategic`). The
 * resident loop tracks this per turn so it only fires its own persistence
 * backstop when the agent did NOT already send the reply itself.
 */
export function isMessageSend(command: string | undefined): boolean {
  if (!command) return false;
  return /(?:^|[\s;&|(])(?:\.\/)?messages\s+send\b/.test(command);
}

/**
 * Classify a single stdout line into zero or more semantic events. A non-JSON
 * or unparseable line becomes a single `other` event carrying the raw text so
 * the caller may surface it as a plain log line.
 */
export function parseResidentLine(line: string): ResidentStreamEvent[] {
  if (!line.startsWith('{')) {
    return line.trim() ? [{ kind: 'other', text: line }] : [];
  }
  let obj: Record<string, unknown>;
  try {
    obj = JSON.parse(line) as Record<string, unknown>;
  } catch {
    return [{ kind: 'other', text: line }];
  }
  const type = typeof obj.type === 'string' ? obj.type : '';
  const nested = obj.parent_tool_use_id != null && obj.parent_tool_use_id !== '';
  const sessionId = readSessionId(obj);
  const events: ResidentStreamEvent[] = [];

  if (type === 'stream_event') {
    const dt = extractDeltaText(obj);
    if (dt) events.push({ kind: 'text', text: dt, nested, sessionId });
    else if (sessionId) events.push({ kind: 'system', nested, sessionId });
    return events;
  }

  if (type === 'assistant') {
    const message = obj.message as Record<string, unknown> | undefined;
    const content = message?.content;
    if (Array.isArray(content)) {
      for (const rawBlock of content) {
        if (!rawBlock || typeof rawBlock !== 'object') continue;
        const block = rawBlock as Record<string, unknown>;
        if (block.type === 'text' && typeof block.text === 'string') {
          events.push({ kind: 'text', text: block.text, isAssistantMessage: true, nested, sessionId });
        } else if (block.type === 'tool_use') {
          const toolName = typeof block.name === 'string' ? block.name : '';
          const toolUseId = typeof block.id === 'string' && block.id ? block.id : undefined;
          let command: string | undefined;
          const input = block.input;
          if (toolName === 'Bash' && input && typeof input === 'object') {
            const c = (input as Record<string, unknown>).command;
            if (typeof c === 'string') command = c;
          }
          events.push({ kind: 'tool', toolName, command, toolUseId, nested, sessionId });
        }
      }
    }
    if (events.length === 0) events.push({ kind: 'system', nested, sessionId });
    return events;
  }

  if (type === 'result') {
    const result = typeof obj.result === 'string' ? obj.result : '';
    events.push({
      kind: 'result', text: result, isTurnResult: !nested, nested, sessionId,
      usage: extractResultUsage(obj),
    });
    return events;
  }

  if (type === 'error') {
    const err = obj.error as Record<string, unknown> | undefined;
    const message = err && typeof err.message === 'string' ? err.message : 'unknown provider error';
    events.push({ kind: 'error', text: message, nested, sessionId });
    return events;
  }

  if (type === 'user') {
    // A `user` line carries the tool_result blocks for the tools the model just
    // ran. Surface each tool_result so the session can correlate a `./messages
    // send` invocation with its execution outcome (is_error ⇒ the curl failed);
    // a `user` line with no tool_result is bare protocol noise (→ system).
    const message = obj.message as Record<string, unknown> | undefined;
    const content = message?.content;
    if (Array.isArray(content)) {
      for (const rawBlock of content) {
        if (!rawBlock || typeof rawBlock !== 'object') continue;
        const block = rawBlock as Record<string, unknown>;
        if (block.type === 'tool_result') {
          const toolUseId = typeof block.tool_use_id === 'string' && block.tool_use_id
            ? block.tool_use_id : undefined;
          events.push({ kind: 'tool_result', toolUseId, isError: block.is_error === true, nested, sessionId });
        }
      }
    }
    if (events.length === 0) events.push({ kind: 'system', nested, sessionId });
    return events;
  }

  if (type === 'system') {
    events.push({ kind: 'system', nested, sessionId });
    return events;
  }

  return [{ kind: 'other', nested, sessionId }];
}

/**
 * Build one NDJSON user-turn line for the resident session's stdin.
 * Shape mirrors the Claude Code stream-json input protocol exactly.
 */
export function encodeUserTurn(text: string): string {
  return JSON.stringify({
    type: 'user',
    message: { role: 'user', content: [{ type: 'text', text }] },
  }) + '\n';
}

/**
 * Build one NDJSON control_request line requesting a cooperative interrupt of
 * the in-flight turn. A fresh request_id is included so the CLI can correlate.
 */
export function encodeInterrupt(requestId: string): string {
  return JSON.stringify({
    type: 'control_request',
    request_id: requestId,
    request: { subtype: 'interrupt' },
  }) + '\n';
}
