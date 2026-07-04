/**
 * Pure, dependency-free helpers for the resident strategic handler
 * (strategic-resident.ts). Kept free of any `../index.js` import so the loop
 * logic — config resolution, message-cursor advancement, operator-message
 * combination, and prompt assembly — is unit-testable in isolation
 * (strategic-resident-lib.test.ts) without mocking the agent's heavy index
 * surface.
 */

export interface ResidentInboundMessage {
  id?: string;
  created_at?: string;
  from_type?: string;
  status?: string;
  message_type?: string;
  content?: string;
  context?: Record<string, unknown>;
}

export interface MessagesPollResponse {
  messages?: ResidentInboundMessage[];
  server_time?: string;
}

export interface SessionHeartbeatResponse {
  authority_held?: boolean;
  leased_until?: string;
  stop_requested?: boolean;
  status?: string;
}

/**
 * Fold a heartbeat response into the loop's authority-lease state.
 *
 * The resident session holds the strategic-write authority lease only while the
 * backend confirms `authority_held:true`. When an unattended session exceeds the
 * backend's cap the lease lapses (`authority_held:false`, `stop_requested:false`,
 * `status:ACTIVE`): the session must NOT keep self-prompting/mutating (the cloud
 * strategist has resumed — two writers), but it also must NOT end (only a
 * `stop_requested`/401 ends it). A later operator message re-arms authority and a
 * subsequent heartbeat reports `authority_held:true` → autonomous work resumes.
 *
 * An ABSENT `authority_held` (older backend that never sends it) leaves the state
 * unchanged, so the default (assumed-held) preserves the pre-lease behavior.
 */
export function nextAuthorityHeld(
  current: boolean,
  hb: SessionHeartbeatResponse | null | undefined,
): boolean {
  if (hb && typeof hb.authority_held === 'boolean') return hb.authority_held;
  return current;
}

/**
 * Whether the resident loop may run the AUTONOMOUS self-prompt this tick. It
 * needs BOTH the mode to allow it (not a read-only monitor session) AND a live
 * authority lease. When false the loop goes passive: it still polls operator
 * messages and heartbeats, but never self-prompts or mutates strategic state.
 */
export function autonomousActive(autonomousEnabled: boolean, authorityHeld: boolean): boolean {
  return autonomousEnabled && authorityHeld;
}

export interface ResidentConfig {
  idleTickMs: number;
  /** Inbound operator-message poll cadence (ms). Kept SHORTER than idleTickMs so
   *  a turn starts promptly after the operator sends — the "faster pickup" knob —
   *  while heartbeats stay paced at idleTickMs so we don't hammer the backend. */
  messagePollMs: number;
  turnTimeoutMs: number;
  /** false disables the autonomous self-prompt path (e.g. a read-only monitor mode) */
  autonomousEnabled: boolean;
}

function num(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback;
}

function str(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}

/**
 * Resolve the resident loop's timing knobs from the strategic payload the
 * backend ships (runtime settings surfaced onto the payload). Defaults: 5s idle
 * (heartbeat) tick, 1.5s inbound-message poll (fast operator pickup), 120s
 * per-turn timeout.
 */
export function residentConfig(payload: Record<string, unknown>): ResidentConfig {
  const idleTickSec = num(payload.strategic_session_idle_tick_seconds, 5);
  const messagePollSec = num(payload.strategic_session_message_poll_seconds, 1.5);
  const turnTimeoutSec = num(payload.strategic_session_turn_timeout_seconds, 120);
  // interaction_mode === 'monitor' (or an explicit flag) makes the session
  // read-only: it still chats + heartbeats but does not autonomously advance.
  const mode = str(payload.interaction_mode).toLowerCase();
  const autonomousEnabled =
    payload.strategic_session_autonomous === false ? false : mode !== 'monitor';
  const idleTickMs = Math.max(1000, Math.round(idleTickSec * 1000));
  return {
    idleTickMs,
    // Poll operator messages faster than the heartbeat tick so a turn starts
    // soon after send, but never below a 750ms floor (don't hammer) and never
    // slower than the idle tick.
    messagePollMs: Math.min(idleTickMs, Math.max(750, Math.round(messagePollSec * 1000))),
    turnTimeoutMs: Math.max(15_000, Math.round(turnTimeoutSec * 1000)),
    autonomousEnabled,
  };
}

/**
 * Inbound-message poll page size and the bounded de-dup set capacity. The poll
 * uses `created_at >= since` semantics (so a message sharing the boundary
 * timestamp is never skipped), advances the cursor to the MAX returned
 * `created_at`, and de-dups returned ids against a bounded seen-set — so a
 * message re-returned at the `>=` boundary is dropped exactly once.
 */
export const MESSAGE_POLL_LIMIT = 20;
export const SEEN_MESSAGE_CAP = 500;
/** Safety bound on re-polls within one poll cycle (drains a backlog, never loops forever). */
export const MAX_POLL_PAGES = 100;

/**
 * The high-water cursor for the inbound-message poll. Prefer the session's
 * persisted `message_cursor`; else start a short window in the past so a message
 * sent moments before the session started (often the one that triggered it) is
 * still picked up on the first poll.
 */
export function initialCursor(
  task: Record<string, unknown>,
  payload: Record<string, unknown>,
  now: number = Date.now(),
): string {
  const ctx = task.context && typeof task.context === 'object'
    ? task.context as Record<string, unknown>
    : {};
  const fromCtx = str(ctx.message_cursor);
  if (fromCtx) return fromCtx;
  const fromPayload = str(payload.message_cursor);
  if (fromPayload) return fromPayload;
  return new Date(now - 2 * 60_000).toISOString();
}

/**
 * Advance the cursor to the MAX `created_at` among the returned messages,
 * falling back to the prior cursor when none is parseable. This replaces the
 * old `server_time`-based advance, which truncated a full page: with `>= since`
 * query semantics on the backend, re-polling from the max returned `created_at`
 * re-fetches the boundary message (dropped by the seen-set) and picks up any
 * same-or-later message the first page could not fit. Compared numerically
 * (Date.parse) so mixed ISO precision cannot mis-order, but the ORIGINAL string
 * is returned as the cursor so the backend's `>=` match stays exact.
 */
export function advanceCursorFromMessages(
  msgs: ResidentInboundMessage[] | null | undefined,
  fallback: string,
): string {
  let maxStr = fallback;
  let maxMs = Date.parse(fallback);
  if (Number.isNaN(maxMs)) maxMs = -Infinity;
  for (const m of Array.isArray(msgs) ? msgs : []) {
    const c = typeof m.created_at === 'string' ? m.created_at : '';
    if (!c) continue;
    const ms = Date.parse(c);
    if (Number.isNaN(ms)) continue;
    if (ms > maxMs) { maxMs = ms; maxStr = c; }
  }
  return maxStr;
}

/**
 * A bounded, insertion-ordered set of already-seen message ids. Caps memory for
 * a multi-hour session by evicting the oldest ids once `cap` is exceeded — far
 * more ids than could ever share a single `>= since` boundary, so an id is only
 * ever evicted long after it could still be re-returned by a poll.
 */
export class BoundedSeenSet {
  private readonly set = new Set<string>();
  private readonly order: string[] = [];
  constructor(private readonly cap: number = SEEN_MESSAGE_CAP) {}

  has(id: string): boolean {
    return this.set.has(id);
  }

  add(id: string): void {
    if (!id || this.set.has(id)) return;
    this.set.add(id);
    this.order.push(id);
    while (this.order.length > this.cap) {
      const old = this.order.shift();
      if (old !== undefined) this.set.delete(old);
    }
  }

  get size(): number {
    return this.set.size;
  }
}

/**
 * Filter a poll page down to messages not already processed, recording each
 * kept id in the bounded seen-set. A message with no id cannot be de-duped, so
 * it is passed through (the backend always stamps an id; this is a safety net).
 */
export function dedupNewMessages(
  msgs: ResidentInboundMessage[] | null | undefined,
  seen: BoundedSeenSet,
): ResidentInboundMessage[] {
  const out: ResidentInboundMessage[] = [];
  for (const m of Array.isArray(msgs) ? msgs : []) {
    const id = typeof m.id === 'string' ? m.id : '';
    if (!id) { out.push(m); continue; }
    if (seen.has(id)) continue;
    seen.add(id);
    out.push(m);
  }
  return out;
}

/** Extract the human text of an inbound operator message. */
export function extractMessageContent(msg: ResidentInboundMessage): string {
  const content = str(msg.content).trim();
  return content;
}

/**
 * Combine one or more pending operator messages into a single turn payload. When
 * several arrive between polls they are concatenated in order with a short header
 * so the agent sees them as a batch rather than losing all but the last.
 */
export function combineOperatorMessages(msgs: ResidentInboundMessage[]): string {
  const texts = msgs.map(extractMessageContent).filter(Boolean);
  if (texts.length === 0) return '';
  if (texts.length === 1) return texts[0];
  return texts.map((t, i) => `[operator message ${i + 1}/${texts.length}]\n${t}`).join('\n\n');
}

export interface BootstrapContext {
  contextPrefix: string;
  projectName: string;
  problemModelJson: string;
  runningUpdate: string;
  directionsText: string;
  visionJson: string;
  openMessagesText: string;
  sessionId: string;
}

/**
 * Assemble the persistent role framing + grounding context delivered on the
 * FIRST turn of the session (prepended to that turn's text). The session retains
 * this in its conversation memory for the rest of its life, so subsequent turns
 * stay lean. Describes the resident role, the two operating speeds, and the
 * `./strategic` / `./fetch` / `./design-doc` / `./messages` tools.
 */
export function buildBootstrapPrompt(ctx: BootstrapContext): string {
  const sections: string[] = [];
  if (ctx.contextPrefix.trim()) sections.push(ctx.contextPrefix.trim());

  sections.push(`# You are the resident strategist for ${ctx.projectName || 'this project'}

You are a STANDING, long-lived strategic agent — not a one-shot cycle. This
process stays warm across many turns and holds the project's context and your
conversation with the operator in memory. You operate at two speeds:

- **Interactive (operator chat):** when the operator sends a message, answer it
  directly and conversationally, take a quick action if they ask, and keep it
  tight. First priority is always a live operator message. You MUST post your
  conversational answer to the operator with \`./messages send '<answer>'\` — the
  live stream is a transcript, but the durable chat reply the operator reads is
  the message you send. Every reply to an operator message REQUIRES a
  \`./messages send\`; do not answer only in the stream.
- **Autonomous (between messages):** when no operator input is pending you are
  handed a short "advance the project" turn. Take ONE bounded step — refine the
  problem model, open/refine/retire a direction, spawn needed work, or refine the
  running update — then stop so the loop can check for operator input again. Your
  autonomous turn can be interrupted the moment the operator speaks.

Never wait on the operator to proceed. Never claim the session is "done" — it
ends only when the operator stops it.

## Your tools (bash helpers already provisioned in this workspace)
- \`./strategic <op> <json|file>\` — apply strategic operations (set-problem-model,
  set-running-update, create-direction, update-direction, retire-direction,
  stage-direction, approve-direction, reject-direction, spawn-task, reflect,
  set-evidence-progress, set-retirement-critiques, pending-confirm, …). These are
  your MUTATING actions on the cloud source of truth.
- \`./fetch <kind> <id>\` — pull full detail (problem-model, direction, gap,
  section, playbook, message, version-phase) when context was trimmed.
- \`./design-doc <get|list|set|delete> <key>\` — read/update design-doc sections.
- \`./messages send '<text>'\` / \`./messages resolve <ref> '<how>'\` — talk to the
  operator and close addressed messages.

All strategic state you write describes the MANAGED project (see CLAUDE.md) —
never this execution environment, this prompt, or the helper commands.`);

  if (ctx.problemModelJson.trim()) {
    sections.push(`## Current problem model\n\`\`\`json\n${ctx.problemModelJson.trim()}\n\`\`\``);
  } else {
    sections.push('## Current problem model\n_(empty — bootstrap it from the directions, vision, and open operator messages below plus the codebase.)_');
  }
  if (ctx.runningUpdate.trim()) {
    sections.push(`## Current running update (operator-facing snapshot)\n${ctx.runningUpdate.trim()}`);
  }
  if (ctx.directionsText.trim()) {
    sections.push(`## Active directions\n${ctx.directionsText.trim()}`);
  }
  if (ctx.visionJson.trim()) {
    sections.push(`## Vision\n\`\`\`json\n${ctx.visionJson.trim()}\n\`\`\``);
  }
  if (ctx.openMessagesText.trim()) {
    sections.push(`## Open operator messages\n${ctx.openMessagesText.trim()}`);
  }
  return sections.join('\n\n');
}

/** Stop reasons that represent a clean, expected end of the session (operator
 *  stop / task-driven terminal). Anything else is an abnormal end. */
export const CLEAN_STOP_REASONS: ReadonlySet<string> = new Set(['stop_requested', 'task_terminal']);

export interface TerminalStatusDecision {
  status: 'COMPLETED' | 'FAILED';
  failureCategory?: string;
}

/**
 * Decide the resident session's terminal status from its ACTUAL stop reason and
 * the child's exit code. COMPLETED only on a clean operator/task-driven stop or
 * an orderly exit(0); a crash (`session_process_exited` non-zero/signal,
 * `loop_error:*`) or a wedge (`turn_hard_timeout`, `session_stalled`) is FAILED
 * with a failure category — so a limped/dead session never reports success.
 */
export function resolveTerminalStatus(stopReason: string, exitCode: number | null): TerminalStatusDecision {
  if (CLEAN_STOP_REASONS.has(stopReason)) return { status: 'COMPLETED' };
  if (stopReason === 'session_process_exited') {
    // Orderly exit(0) is clean; non-zero or signal-kill (null) is a crash.
    return exitCode === 0
      ? { status: 'COMPLETED' }
      : { status: 'FAILED', failureCategory: 'claude_code_failed' };
  }
  if (stopReason === 'turn_hard_timeout' || stopReason === 'session_stalled') {
    return { status: 'FAILED', failureCategory: 'timeout' };
  }
  // loop_error: … and any other unexpected reason.
  return { status: 'FAILED', failureCategory: 'claude_code_failed' };
}

/** The `kind` vocabulary the backend `strategic_session` delta accepts
 *  (strategic_events.py `_STRATEGIC_SESSION_KINDS`). The five activity kinds
 *  (command / file / read / subagent / tool_result) drive the LIVE, ephemeral
 *  Workday activity strip; only `message` reaches the durable transcript, and
 *  only as a ProjectMessage — never via a stream frame. */
export type StreamFrameKind =
  | 'reasoning' | 'action' | 'tool' | 'status' | 'message'
  | 'command' | 'file' | 'read' | 'subagent' | 'tool_result';

/** Optional lifecycle status on a frame: `responding` opens an operator turn
 *  (drives the working indicator), `done` closes it (clears the activity strip). */
export type StreamFrameStatus = 'responding' | 'done';

interface CoalescedItem { kind: StreamFrameKind; text: string; open: boolean; status?: StreamFrameStatus; }

/**
 * Ordered, coalescing frame buffer shared by ALL live-stream frame kinds (text
 * deltas, tool calls, status lines) so a SINGLE rate budget governs the socket.
 *
 * Assistant text deltas coalesce into a trailing OPEN `message` item (many deltas
 * → one frame); a tool/status frame first SEALS that open text item — preserving
 * the text-before-tool ordering — then enqueues itself as its own sealed item.
 * The owner drains at most ONE item per flush tick, so a burst of tool_use blocks
 * arriving in a single assistant message (previously flushed+emitted immediately,
 * one WS frame each) can no longer exceed the backend's 20-frame/s close
 * threshold — it drains at the same ≤10/s budget as text.
 *
 * Pure and dependency-free: it owns queue POLICY (coalescing + ordering); the
 * caller owns TIMING (the flush-interval timer) and the actual WS emit.
 */
export class FrameCoalescer {
  private items: CoalescedItem[] = [];

  /** Enqueue an assistant text delta, merging into a trailing open text item. */
  pushText(text: string): void {
    if (!text) return;
    const tail = this.items.at(-1);
    if (tail && tail.kind === 'message' && tail.open) tail.text += text;
    else this.items.push({ kind: 'message', text, open: true });
  }

  /** Enqueue a discrete tool/status/activity frame, sealing any open text item
   *  first so it stays ordered BEFORE this frame (and later deltas start a fresh
   *  item). An optional lifecycle `status` (responding / done) rides the frame. */
  pushFrame(kind: StreamFrameKind, text: string, status?: StreamFrameStatus): void {
    const tail = this.items.at(-1);
    if (tail && tail.open) tail.open = false;
    this.items.push({ kind, text, open: false, status });
  }

  /** Seal the trailing open text item so a later delta starts a FRESH frame. A
   *  turn-boundary flush calls this so the ENDING turn's tail text can never
   *  merge into the NEXT turn's first delta once the residual drains lazily
   *  (the rate-limited drain no longer empties the queue synchronously at the
   *  boundary). No-op when the queue is empty or the tail is already sealed. */
  seal(): void {
    const tail = this.items.at(-1);
    if (tail && tail.open) tail.open = false;
  }

  get isEmpty(): boolean { return this.items.length === 0; }
  get length(): number { return this.items.length; }

  /** Remove and return the next item to emit (front of queue), or null if empty. */
  next(): { kind: StreamFrameKind; text: string; status?: StreamFrameStatus } | null {
    const item = this.items.shift();
    return item ? { kind: item.kind, text: item.text, status: item.status } : null;
  }

  /** Remove and return ALL queued items in order. NOTE: the boundary flush no
   *  longer uses this — it seals + hands off to the rate-limited one-at-a-time
   *  drain (see SessionStreamer.flush) so a large residual can never burst past
   *  the backend WS-close cap. Retained as a general drain-all utility. */
  drain(): Array<{ kind: StreamFrameKind; text: string }> {
    const out = this.items.map(i => ({ kind: i.kind, text: i.text }));
    this.items = [];
    return out;
  }
}

/**
 * The bounded autonomous self-prompt. Deliberately short so the turn returns
 * quickly and the loop can re-poll for operator input. One step per turn.
 */
export function buildAutonomousPrompt(): string {
  return `No operator message is pending right now. Take ONE small, bounded step to
advance the project, then STOP so I can check for operator input:

- If the problem model or running update is stale, refine it (one focused edit).
- If a direction is clearly dead, retire it; if there is an obvious gap, open a
  direction for it; if a direction needs decomposition or investigation, spawn a
  research/plan/design/web_research/compute_experiment task.
- If nothing genuinely needs doing this tick, say so in one line and take no
  action (do not invent busy-work).

Apply any decision through the ./strategic helper. Keep this turn short.`;
}
