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

import { STRATEGIC_OPS_REGISTRY } from '../handlers/strategic-ops-registry.js';
import { normalizeProviderTokenUsage, type ProviderTokenUsage } from './usage.js';

export type ResidentEventKind = 'text' | 'tool' | 'tool_result' | 'result' | 'error' | 'system' | 'other';

/**
 * The `kind` of a LIVE, EPHEMERAL activity one-liner surfaced to the operator's
 * Workday activity strip. Mirrors the backend `strategic_session` activity kinds
 * (strategic_events.py). `command` = a Bash command, `file` = an edit/write,
 * `read` = a file read, `subagent` = a spawned Task/Agent, `tool_result` = a
 * tool's outcome, `tool` = any other tool. These NEVER persist — only the
 * native Workday `reply_to_operator` action reaches the durable transcript.
 */
export type ResidentActivityKind = 'command' | 'file' | 'read' | 'subagent' | 'tool_result' | 'tool';

/** Max chars for an activity one-liner (the backend caps + sanitizes again). */
const ACTIVITY_TEXT_MAX = 160;

function _firstLine(s: string): string {
  const nl = s.indexOf('\n');
  return nl >= 0 ? s.slice(0, nl) : s;
}

function _cap(s: string): string {
  return s.length > ACTIVITY_TEXT_MAX ? `${s.slice(0, ACTIVITY_TEXT_MAX - 1)}…` : s;
}

/** Trim a path to its last two segments (…/dir/file) so the strip stays compact. */
function _shortPath(p: string): string {
  const parts = p.split(/[\\/]/).filter(Boolean);
  return parts.length <= 2 ? p : `…/${parts.slice(-2).join('/')}`;
}

function _str(v: unknown): string {
  return typeof v === 'string' ? v : '';
}

/**
 * Compact one-liner describing a top-level `tool_use` for the live activity strip
 * — pure so it is unit-tested in isolation. Maps the common Claude Code tools to
 * a `{ kind, text }` the operator reads at a glance:
 *   Bash → `$ <cmd>` (command)   Edit/Write/MultiEdit/NotebookEdit → `edited <file>` (file)
 *   Read → `read <file>` (read)  Task/Agent → `spawned <type>: <desc>` (subagent)
 *   anything else → the bare tool name (tool). Returns undefined when there is
 *   nothing worth surfacing (e.g. an empty Bash command).
 */
export function describeToolUse(
  toolName: string | undefined,
  input: unknown,
): { kind: ResidentActivityKind; text: string } | undefined {
  const name = toolName || '';
  const obj = input && typeof input === 'object' ? (input as Record<string, unknown>) : {};
  let out: { kind: ResidentActivityKind; text: string } | undefined;
  if (name === 'Bash') {
    const cmd = _str(obj.command).trim();
    out = cmd ? { kind: 'command', text: `$ ${_firstLine(cmd)}` } : undefined;
  } else if (name === 'Read') {
    const f = _str(obj.file_path).trim();
    out = { kind: 'read', text: f ? `read ${_shortPath(f)}` : 'read' };
  } else if (name === 'Edit' || name === 'Write' || name === 'MultiEdit' || name === 'NotebookEdit') {
    const f = _str(obj.file_path || obj.notebook_path).trim();
    const verb = name === 'Write' ? 'wrote' : 'edited';
    out = { kind: 'file', text: f ? `${verb} ${_shortPath(f)}` : verb };
  } else if (name === 'Task' || name === 'Agent') {
    const type = _str(obj.subagent_type).trim();
    const desc = _str(obj.description).trim();
    const label = [type, desc].filter(Boolean).join(': ') || 'sub-agent';
    out = { kind: 'subagent', text: `spawned ${label}` };
  } else if (name) {
    out = { kind: 'tool', text: name };
  }
  if (!out) return undefined;
  return { kind: out.kind, text: _cap(out.text) };
}

/**
 * Compact one-liner summarizing a `tool_result` for the live activity strip —
 * pure so it is unit-tested in isolation. A leading ✓ / ✗ marks success vs a
 * reported error, with a short first-line preview of the result content when one
 * is present (kept tight; the backend sanitizes + caps again).
 */
export function describeToolResult(
  block: Record<string, unknown>,
): { kind: 'tool_result'; text: string } {
  const isError = block.is_error === true;
  const content = block.content;
  let preview = '';
  if (typeof content === 'string') {
    preview = content;
  } else if (Array.isArray(content)) {
    for (const c of content) {
      if (c && typeof c === 'object' && (c as Record<string, unknown>).type === 'text') {
        preview = _str((c as Record<string, unknown>).text);
        if (preview) break;
      }
    }
  }
  preview = _firstLine(preview.trim()).slice(0, 100).trim();
  const mark = isError ? '✗' : '✓';
  const text = preview ? `${mark} ${preview}` : isError ? '✗ tool error' : '✓ done';
  return { kind: 'tool_result', text: _cap(text) };
}

/**
 * Per-turn token usage carried on a top-level `result` event. Mirrors the
 * Anthropic result-usage block the one-shot runner already records
 * (runners.ts): `inputTokens` is the INCLUSIVE sum of the three input classes so
 * downstream telemetry stays backward-compatible, with the two cache classes
 * broken out for true-rate pricing.
 */
export type ResidentTokenUsage = ProviderTokenUsage;

export interface ResidentStreamEvent {
  kind: ResidentEventKind;
  /** delta / assistant text for 'text'; result text for 'result'; message for 'error' */
  text?: string;
  /** tool name for 'tool' */
  toolName?: string;
  /** raw tool arguments for a 'tool' event, retained for contract-bound tools */
  input?: unknown;
  /** bash command string for a Bash 'tool' event (used to detect applied strategic ops) */
  command?: string;
  /** the CLI-assigned tool_use id — set on a 'tool' event (block.id) and on the
   *  matching 'tool_result' event (block.tool_use_id) so the session can
   *  correlate a native Workday reply invocation with its execution outcome. */
  toolUseId?: string;
  /** for a 'tool_result' event: the tool reported an error (non-zero exit for Bash). */
  isError?: boolean;
  /** Bounded raw tool-result text retained only for exact acknowledgement parsing. */
  toolResultText?: string;
  /** session id when the event carried one (system init, result) */
  sessionId?: string;
  /** true for a TOP-LEVEL result event — the completion signal for the active turn */
  isTurnResult?: boolean;
  /** Structured terminal reason emitted by newer Claude Code result frames. */
  terminalReason?: string;
  /** HTTP status carried by a structured terminal API error result. */
  apiErrorStatus?: number;
  /** true for a finalized top-level assistant text block (vs a streamed delta) */
  isAssistantMessage?: boolean;
  /** true when the event came from a nested sub-agent (parent_tool_use_id set) */
  nested?: boolean;
  /** token usage parsed from a top-level 'result' event (when the CLI reported it) */
  usage?: ResidentTokenUsage;
  /** turn count parsed from a top-level 'result' event (when the CLI reported it) */
  numTurns?: number;
  /** LIVE activity-strip kind for a 'tool' / 'tool_result' event (ephemeral) */
  activityKind?: ResidentActivityKind;
  /** LIVE activity-strip one-liner text for a 'tool' / 'tool_result' event */
  activityText?: string;
}

/**
 * Extract the per-turn token usage from a `result` object's `usage` block, or
 * undefined when absent/empty. `inputTokens` folds in the two cache classes so
 * total-input telemetry stays backward-compatible (see runners.ts).
 */
export function extractResultUsage(obj: Record<string, unknown>): ResidentTokenUsage | undefined {
  const usage = obj.usage;
  if (!usage || typeof usage !== 'object') return undefined;
  return normalizeProviderTokenUsage(usage as Record<string, unknown>);
}

/**
 * Extract the per-turn model-turn count from a `result` object, or undefined
 * when absent/invalid.
 */
export function extractResultTurns(obj: Record<string, unknown>): number | undefined {
  const numTurns = Number(obj.num_turns ?? 0);
  if (!Number.isFinite(numTurns) || numTurns <= 0) return undefined;
  return Math.floor(numTurns);
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

/** ./strategic aliases that post operator messages — NOT lease-renewing strategic
 *  progress events. The resident uses `./messages send` for operator replies;
 *  if it falls back to `./strategic create-message`, the reply is posted but
 *  must not renew the authority/freshness lease as if strategic state changed. */
const MESSAGE_OP_ALIASES = new Set(['create-message', 'resolve-message']);

const STRATEGIC_MUTATING_VERBS = new Set(
  STRATEGIC_OPS_REGISTRY
    .filter(e => e.cli && e.alias !== null && !MESSAGE_OP_ALIASES.has(e.alias))
    .map(e => e.alias as string),
);

/**
 * True when a Bash command invokes a MUTATING `./strategic <verb>` op. Reads
 * (`./fetch`, `./design-doc get`) do NOT count — only an applied mutation is a
 * lease-renewing "progress" event. Used by the session to set
 * `appliedStrategicOp` so the handler renews the authority lease only on real
 * progress (the dead-man's-switch contract), never bare liveness.
 *
 * Message-posting ops (`create-message`, `resolve-message`) are intentionally
 * excluded: posting a reply is operator communication, not a strategic-state
 * mutation, and must not renew the lease.
 */
export function isStrategicMutation(command: string | undefined): boolean {
  if (!command) return false;
  const m = command.match(/(?:^|[\s;&|(])(?:[^\s;&|()]*\/)?strategic\s+([a-z][a-z-]*)/);
  if (!m) return false;
  return STRATEGIC_MUTATING_VERBS.has(m[1]);
}

/**
 * True when a Bash command posts a durable operator-facing reply (ProjectMessage,
 * `from_type=strategic`). The resident loop tracks this per turn so it only fires
 * its own `persistReply` backstop when the agent did NOT already post the reply.
 *
 * Covers both the primary path (`./messages send …`) and the fallback
 * (`./strategic create-message …`) so a resident that replies via the strategic
 * script is not double-posted by the backstop.
 */
export function isMessageSend(command: string | undefined): boolean {
  if (!command) return false;
  if (/(?:^|[\s;&|(])(?:[^\s;&|()]*\/)?messages\s+send\b/.test(command)) return true;
  return /(?:^|[\s;&|(])(?:[^\s;&|()]*\/)?strategic\s+create-message\b/.test(command);
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
          const input = block.input;
          let command: string | undefined;
          if (toolName === 'Bash' && input && typeof input === 'object') {
            const c = (input as Record<string, unknown>).command;
            if (typeof c === 'string') command = c;
          }
          const activity = describeToolUse(toolName, input);
          events.push({
            kind: 'tool', toolName, input, command, toolUseId, nested, sessionId,
            activityKind: activity?.kind, activityText: activity?.text,
          });
        }
      }
    }
    if (events.length === 0) events.push({ kind: 'system', nested, sessionId });
    return events;
  }

  if (type === 'result') {
    const result = typeof obj.result === 'string' ? obj.result : '';
    const terminalReason = typeof obj.terminal_reason === 'string'
      ? obj.terminal_reason
      : undefined;
    const rawApiErrorStatus = obj.api_error_status;
    const parsedApiErrorStatus = typeof rawApiErrorStatus === 'number'
      ? rawApiErrorStatus
      : (typeof rawApiErrorStatus === 'string' && rawApiErrorStatus.trim()
          ? Number(rawApiErrorStatus)
          : Number.NaN);
    const apiErrorStatus = Number.isFinite(parsedApiErrorStatus)
      ? Math.trunc(parsedApiErrorStatus)
      : undefined;
    // Claude Code can emit a terminal provider rejection with subtype=success
    // and no is_error bit.  terminal_reason/api_error_status are the canonical
    // failure signal in that shape; treating it as ordinary success makes the
    // Resident keep waiting/retrying the same dead account until hard timeout.
    const structuredApiError = terminalReason === 'api_error'
      || terminalReason === 'aborted_streaming'
      || (apiErrorStatus !== undefined && apiErrorStatus >= 400);
    events.push({
      kind: 'result', text: result, isError: obj.is_error === true || structuredApiError,
      isTurnResult: !nested, nested, sessionId, terminalReason, apiErrorStatus,
      usage: extractResultUsage(obj),
      numTurns: extractResultTurns(obj),
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
    // ran. Surface each tool_result so the session can correlate a native
    // Workday reply invocation with its execution outcome (is_error ⇒ the tool
    // rejected it); a `user` line with no tool_result is bare protocol noise.
    const message = obj.message as Record<string, unknown> | undefined;
    const content = message?.content;
    if (Array.isArray(content)) {
      for (const rawBlock of content) {
        if (!rawBlock || typeof rawBlock !== 'object') continue;
        const block = rawBlock as Record<string, unknown>;
        if (block.type === 'tool_result') {
          const toolUseId = typeof block.tool_use_id === 'string' && block.tool_use_id
            ? block.tool_use_id : undefined;
          const activity = describeToolResult(block);
          const rawContent = block.content;
          const toolResultText = typeof rawContent === 'string'
            ? rawContent.slice(0, 16_384)
            : Array.isArray(rawContent)
              ? rawContent.flatMap((item) => (
                  item && typeof item === 'object'
                    && (item as Record<string, unknown>).type === 'text'
                    && typeof (item as Record<string, unknown>).text === 'string'
                    ? [(item as Record<string, unknown>).text as string]
                    : []
                )).join('\n').slice(0, 16_384)
              : undefined;
          events.push({
            kind: 'tool_result', toolUseId, isError: block.is_error === true, nested, sessionId,
            activityKind: activity.kind, activityText: activity.text, toolResultText,
          });
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
 * Build one NDJSON user-turn line for the resident session's stdin with plain text.
 * Shape mirrors the Claude Code stream-json input protocol exactly.
 */
export function encodeUserTurn(text: string): string {
  return JSON.stringify({
    type: 'user',
    message: { role: 'user', content: [{ type: 'text', text }] },
  }) + '\n';
}

/**
 * Build one NDJSON user-turn line with multiple content blocks (text + images).
 * Supports the Messages API content block format for multimodal input so
 * operator-attached images reach the LLM as image blocks rather than being
 * silently dropped.
 */
export function encodeUserTurnBlocks(blocks: Array<Record<string, unknown>>): string {
  return JSON.stringify({
    type: 'user',
    message: { role: 'user', content: blocks },
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
