import { spawn } from 'child_process';
import { registerChildPid, unregisterChildPid } from '../processTracking.js';
import {
  WORKSPACE, getAgentToken, log, recordLlmCallStart, recordLlmTokens, recordNumTurns, sanitize,
} from '../index.js';
import { writeAgentTokenFile } from '../token-file.js';
import { PROCESS_KILL_GRACE_MS } from '../constants.js';
import {
  parseResidentLine, splitStreamChunk, encodeUserTurn, encodeUserTurnBlocks, encodeInterrupt,
  isStrategicMutation, isMessageSend, type ResidentStreamEvent,
} from './resident-stream.js';
import { getProviderRunner } from './runners.js';

// ── Resident (persistent, streaming, interactive) Claude session ─────────────
//
// The one-shot `runProviderAgent` above spawns `claude -p ... --output-format
// stream-json`, feeds ONE prompt via a closing stdin fd, and terminates on the
// terminal `result` event. The RESIDENT strategic agent needs the opposite: a
// long-lived warm process that holds repo/context/conversation in memory and
// answers MANY turns (operator chat + autonomous self-prompts) over its life.
//
// This clones the anthropic branch of `buildRunCommand` into a resident variant
// that appends `--input-format stream-json` and keeps stdin OPEN, so each turn
// is one NDJSON `user` line written to the child's stdin and each reply is a
// stream of `assistant`/`stream_event` deltas terminated by a top-level
// `result`. The one-shot wall-clock kill + no-progress + result-grace watchdogs
// are DELIBERATELY NOT armed (they would SIGTERM a warm, idle session); only a
// per-active-turn stall watchdog and a large idle ceiling protect against a
// genuinely wedged / orphaned process. The session is transport-agnostic — it
// emits parsed events; the handler owns WebSocket streaming.

const DEFAULT_RESIDENT_TURN_TIMEOUT_MS = 120_000;   // per-active-turn stall budget
const DEFAULT_RESIDENT_IDLE_CEILING_MS = 30 * 60_000; // warm-but-orphaned backstop
const RESIDENT_INTERRUPT_SIGINT_GRACE_MS = 4_000;   // control_request → SIGINT fallback
const RESIDENT_TURN_FORCE_SETTLE_MS = 20_000;       // stall+interrupt → force-settle
// ABSOLUTE per-turn ceiling, measured from turn dispatch and NEVER reset by
// output. The stall watchdog above resets on every stdout chunk, so a wedged
// child that keeps emitting keepalive / partial-message noise but never returns
// a top-level `result` resets the stall clock forever and limps the turn (and
// the whole task) indefinitely. This hard deadline guarantees termination: on
// expiry it re-issues the cooperative interrupt and, if that + SIGINT still do
// not settle the turn, escalates to close() (SIGTERM→SIGKILL the process group)
// and settles the turn with `hardTimedOut` so the handler posts terminal FAILED
// instead of spinning silently. Default is a generous backstop — legitimate
// operator-reply and bounded autonomous turns finish well within it.
const DEFAULT_RESIDENT_TURN_HARD_TIMEOUT_MS = 15 * 60_000;

export interface ResidentTurnResult {
  /** authoritative final assistant text for this turn (from the `result` event) */
  resultText: string;
  /** all assistant text streamed before the terminal result; retained separately
   *  so marker consumers can recover a marker hidden by a closing tool call. */
  accumulatedTurnText: string;
  /** the turn was cut short by a cooperative interrupt (new operator input / stop) */
  interrupted: boolean;
  /** the turn's per-turn stall watchdog fired (no output within the turn budget) */
  timedOut: boolean;
  /** the ABSOLUTE per-turn hard timeout fired — the process was force-killed as wedged */
  hardTimedOut: boolean;
  /** a mutating `./strategic <verb>` op ran during the turn (a lease-renewing progress event) */
  appliedStrategicOp: boolean;
  /** any tool_use ran during the turn */
  sawToolUse: boolean;
  /** the agent posted a durable operator reply via `./messages send` during the
   *  turn AND that send was CONFIRMED to succeed (its tool_result carried no
   *  error). A send whose result never arrived, or came back is_error, leaves
   *  this false so the handler's persistReply backstop still runs. */
  sentOperatorMessage: boolean;
  /** the underlying process exited (or was closed) before/while this turn ran */
  processGone: boolean;
  /** wall-clock ms from sendTurn() call to the first streamed text token; null when
   *  no text arrived (tool-only or empty turn, or interrupted before first token). */
  ttftMs: number | null;
  /** total wall-clock ms from sendTurn() call to the turn resolving (settleTurn). */
  turnMs: number;
  /** number of tool_use calls that fired during this turn. */
  toolCallCount: number;
  /** cumulative ms spent inside tool calls (from tool_use event to tool_result
   *  event) during this turn. Zero when no tool calls completed. */
  totalToolCallDurationMs: number;
  /** mean ms between consecutive assistant text tokens (after the first) across
   *  all streaming deltas this turn. Null when ≤1 text token arrived. */
  meanInterTokenMs: number | null;
  /** inclusive input token count reported by this turn's top-level `result` event;
   *  zero when no usage block was reported (interrupted / pre-result child exit).
   *  The handler uses this to detect context-window pressure and trigger compaction
   *  before the model's context limit is reached. */
  inputTokens: number;
}

export interface ResidentSessionEvent {
  kind: 'text' | 'tool' | 'error' | 'status' | 'activity';
  text?: string;
  toolName?: string;
  /** compact one-liner for the LIVE, ephemeral Workday activity strip (a Bash
   *  command, file edit, read, spawned sub-agent, or tool result). Present on
   *  'tool' events (the tool call) and on 'activity' events (a tool result). */
  activity?: { kind: string; text: string };
}

export interface ResidentSession {
  /** Write one user turn; resolves on the turn's top-level `result` (or interrupt/close).
   *  Accepts a plain text string or an array of content blocks (text + image) for
   *  multimodal operator messages. `opts.stallTimeoutMs` overrides the per-active-turn
   *  stall budget for THIS turn only (autonomous turns get a deeper budget so a long
   *  read/verify step isn't killed mid-thought); the absolute hard ceiling and interrupt
   *  ladder are unchanged. */
  sendTurn(textOrBlocks: string | Array<Record<string, unknown>>, opts?: { stallTimeoutMs?: number }): Promise<ResidentTurnResult>;
  /** Cooperatively pre-empt the in-flight turn (control_request, SIGINT fallback). */
  interrupt(): void;
  /** Subscribe to parsed stream events (text deltas, tool_use, errors). */
  onEvent(cb: (ev: ResidentSessionEvent) => void): void;
  /** Reset the warm-but-orphaned idle ceiling from external activity (heartbeat /
   *  poll). Keeps a monitor / chat-only session that issues no turns from
   *  self-closing at the idle ceiling while the handler loop is still alive. */
  touch(): void;
  /** Resolve when no turn is active (immediately if idle). */
  waitForIdle(): Promise<void>;
  /** Tear the session down (end stdin, SIGTERM→SIGKILL the process group). */
  close(): void;
  /** The CLI session id captured from the init/system/result events (for --resume). */
  sessionId(): string | null;
  /** False once the process has exited or close() was called. */
  isAlive(): boolean;
  /** The child exit code once it has exited, else null. */
  exitCode(): number | null;
}

export interface ResidentSessionOptions {
  model?: string;
  effort?: string;
  disallowedTools?: string[];
  workspace?: string;
  /** per-active-turn stall budget (ms); default 120s */
  turnTimeoutMs?: number;
  /** ABSOLUTE per-turn hard ceiling (ms), never reset by output; default 15m */
  hardTurnTimeoutMs?: number;
  /** warm-but-orphaned backstop (ms); reset on any activity; default 30m */
  idleCeilingMs?: number;
  /** called once when the child process exits */
  onExit?: (code: number | null) => void;
}

interface ActiveTurn {
  resolve: (r: ResidentTurnResult) => void;
  resultText: string;
  accumulated: string;
  streamedText: boolean;
  sawToolUse: boolean;
  appliedStrategicOp: boolean;
  sentOperatorMessage: boolean;
  /** tool_use ids of `./messages send` invocations still awaiting a tool_result.
   *  sentOperatorMessage flips true only when one of these resolves WITHOUT an
   *  error — so a failed send (bad curl) never suppresses the persistReply
   *  backstop and the operator's durable reply is never silently lost. */
  pendingMessageSendIds: Set<string>;
  interrupted: boolean;
  timedOut: boolean;
  hardTimedOut: boolean;
  sawDeltaThisMessage: boolean;
  /** per-active-turn stall budget for THIS turn (may override the session default). */
  stallTimeoutMs: number;
  stallTimer: NodeJS.Timeout | null;
  forceTimer: NodeJS.Timeout | null;
  hardTimer: NodeJS.Timeout | null;
  interruptTimer: NodeJS.Timeout | null;
  /** monotonic ms timestamp captured at sendTurn() call (for latency accounting). */
  startedAtMs: number;
  /** ms timestamp of the first streamed text token; null until text arrives. */
  firstTokenAtMs: number | null;
  /** count of tool_use calls observed so far this turn. */
  toolCallCount: number;
  /** cumulative ms spent inside tool calls (from tool_use to tool_result)
   *  during this turn. Updated as each tool_result event arrives. */
  totalToolCallDurationMs: number;
  /** map from tool_use_id -> ms timestamp when the tool_use event was received.
   *  Used to compute per-tool-call wall-clock duration at tool_result time. */
  toolCallStartTimes: Map<string, number>;
  /** ms timestamp of the most recently received text token delta; null until
   *  the first text token arrives. Set on every incremental text event so
   *  subsequent deltas can measure the inter-token gap. */
  lastTokenAtMs: number | null;
  /** rolling array of ms intervals between consecutive text deltas (after the
   *  first). Each entry is the time from the PREVIOUS text delta to the current
   *  one. Empty until at least two text tokens have arrived. */
  interTokenIntervalsMs: number[];
  /** inclusive input token count reported by the turn's top-level `result` event;
   *  zero when no usage block was reported (interrupted / pre-result child exit). */
  inputTokens: number;
}

/**
 * Start a resident Claude session. Requires a Claude-compatible provider
 * (anthropic/zai/deepseek);
 * throws for codex/gemini, whose CLIs do not expose the bidirectional stream-json
 * input mode this depends on.
 */
export function startResidentClaudeSession(opts: ResidentSessionOptions = {}): ResidentSession {
  const workspace = opts.workspace ?? WORKSPACE;
  const turnTimeoutMs = opts.turnTimeoutMs ?? DEFAULT_RESIDENT_TURN_TIMEOUT_MS;
  const idleCeilingMs = opts.idleCeilingMs ?? DEFAULT_RESIDENT_IDLE_CEILING_MS;
  // Hard per-turn ceiling. An explicit caller value is honored verbatim (tests
  // set a small one); the default is a generous backstop kept safely above the
  // stall+force-settle chain so it only fires on a genuinely wedged turn, never
  // clipping a legitimately long one.
  const hardTurnTimeoutMs = opts.hardTurnTimeoutMs
    ?? Math.max(DEFAULT_RESIDENT_TURN_HARD_TIMEOUT_MS, turnTimeoutMs + RESIDENT_TURN_FORCE_SETTLE_MS);

  const runner = getProviderRunner();
  const base = runner.buildRunCommand('', opts.model, opts.effort, opts.disallowedTools);
  if (base.outputMode !== 'claude-stream-json') {
    throw new Error(
      `resident session requires a Claude-compatible provider (anthropic/zai/deepseek); got ${runner.provider}/${base.outputMode}`,
    );
  }
  // Clone the one-shot args and append bidirectional streaming input. The one-shot
  // command already carries -p / --output-format stream-json / --verbose /
  // --include-partial-messages / --dangerously-skip-permissions / --model / --effort
  // / --disallowedTools, so we only add the input side.
  const args = [...base.args, '--input-format', 'stream-json'];

  // Guarantee the live token file exists with the CURRENT token before the
  // long-lived subprocess (and its bash helpers) start reading it. Refreshes
  // thereafter keep it current via setToken. Best-effort/no-op when the file is
  // unconfigured or the token is empty (tests).
  writeAgentTokenFile(getAgentToken());

  const child = spawn(base.command, args, {
    cwd: workspace,
    env: base.env,
    detached: true,
    stdio: ['pipe', 'pipe', 'pipe'],
  });

  const listeners: Array<(ev: ResidentSessionEvent) => void> = [];
  const idleWaiters: Array<() => void> = [];
  let sessionId: string | null = null;
  let carry = '';
  let alive = true;
  let closed = false;
  let exitCodeVal: number | null = null;
  let activeTurn: ActiveTurn | null = null;
  let idleTimer: NodeJS.Timeout | null = null;
  // Count of top-level `result` events still owed by turns that were force-settled
  // (resolved) while the child stayed alive. The child processes turns strictly
  // in order, so each such turn still emits exactly one late `result`; without a
  // guard that late result would resolve the WRONG (newer) turn's promise. Each
  // pending stale result is swallowed exactly once before a live result settles
  // the current turn. Reset when the process dies (no more results can arrive).
  let staleResultsPending = 0;
  // Track the last-seen cumulative numTurns from CLI result events so we only
  // record the DELTA per turn, not the accumulating total (which would give
  // N(N+1)/2 after N turns instead of N — see telemetry recordNumTurns).
  let lastSeenNumTurns = 0;
  const pid = child.pid;

  const emit = (ev: ResidentSessionEvent): void => {
    for (const l of listeners) {
      try { l(ev); } catch { /* a listener error must not break the drain */ }
    }
  };

  const armIdleCeiling = (): void => {
    if (!alive || closed) return;
    if (idleTimer) clearTimeout(idleTimer);
    idleTimer = setTimeout(() => {
      log('[resident] idle ceiling reached with no activity — closing warm session');
      close();
    }, idleCeilingMs);
  };

  const resolveIdleWaiters = (): void => {
    while (idleWaiters.length > 0) {
      const w = idleWaiters.shift();
      if (w) { try { w(); } catch { /* ignore */ } }
    }
  };

  const clearTurnTimers = (t: ActiveTurn): void => {
    if (t.stallTimer) { clearTimeout(t.stallTimer); t.stallTimer = null; }
    if (t.forceTimer) { clearTimeout(t.forceTimer); t.forceTimer = null; }
    if (t.hardTimer) { clearTimeout(t.hardTimer); t.hardTimer = null; }
    if (t.interruptTimer) { clearTimeout(t.interruptTimer); t.interruptTimer = null; }
  };

  const settleTurn = (resultText: string, processGone: boolean): void => {
    if (!activeTurn) return;
    const t = activeTurn;
    activeTurn = null;
    clearTurnTimers(t);
    const finalText = resultText || t.resultText || t.accumulated;
    // Fallback: if --include-partial-messages produced no live deltas AND no
    // assistant text was surfaced, still show the operator the final reply.
    if (!t.streamedText && finalText) emit({ kind: 'text', text: finalText });
    armIdleCeiling();
    const nowMs = Date.now();
    const turnMs = nowMs - t.startedAtMs;
    const ttftMs = t.firstTokenAtMs !== null ? t.firstTokenAtMs - t.startedAtMs : null;
    // Mean inter-token latency: average of all inter-token intervals. Only
    // meaningful when at least two text tokens have arrived (≥1 interval).
    const meanInterTokenMs = t.interTokenIntervalsMs.length > 0
      ? Math.round(t.interTokenIntervalsMs.reduce((a, b) => a + b, 0) / t.interTokenIntervalsMs.length)
      : null;
    t.resolve({
      resultText: finalText,
      accumulatedTurnText: t.accumulated,
      interrupted: t.interrupted,
      timedOut: t.timedOut,
      hardTimedOut: t.hardTimedOut,
      appliedStrategicOp: t.appliedStrategicOp,
      sawToolUse: t.sawToolUse,
      sentOperatorMessage: t.sentOperatorMessage,
      processGone,
      ttftMs,
      turnMs,
      toolCallCount: t.toolCallCount,
      totalToolCallDurationMs: t.totalToolCallDurationMs,
      meanInterTokenMs,
      inputTokens: t.inputTokens,
    });
    resolveIdleWaiters();
  };

  const armTurnStall = (): void => {
    const t = activeTurn;
    if (!t) return;
    if (t.stallTimer) clearTimeout(t.stallTimer);
    t.stallTimer = setTimeout(() => {
      if (activeTurn !== t) return;
      log(`[resident] turn stall — no output for >${t.stallTimeoutMs / 1000}s; interrupting`);
      t.timedOut = true;
      interrupt();
      // If the interrupt does not produce a terminal result, force-settle so the
      // handler loop never blocks forever on a wedged turn. The child stays alive
      // and will still emit this turn's real `result` later — mark it stale so it
      // is swallowed and cannot resolve a LATER turn's promise.
      if (!t.forceTimer) {
        t.forceTimer = setTimeout(() => {
          if (activeTurn === t) {
            staleResultsPending += 1;
            settleTurn('', false);
          }
        }, RESIDENT_TURN_FORCE_SETTLE_MS);
      }
    }, t.stallTimeoutMs);
  };

  // ABSOLUTE per-turn deadline. Armed once at turn dispatch and NEVER reset by
  // output, so a child that emits endless keepalive/partial noise without ever
  // returning a top-level `result` cannot limp the turn forever. On expiry:
  // re-issue the cooperative interrupt, then — if the turn is still active after
  // the SIGINT grace — tear the whole session down (close(): SIGTERM→SIGKILL the
  // group) so the still-active turn settles processGone with `hardTimedOut` set,
  // which the handler surfaces as a terminal FAILED (never a silent spin).
  const armTurnHardTimeout = (t: ActiveTurn): void => {
    if (t.hardTimer) clearTimeout(t.hardTimer);
    t.hardTimer = setTimeout(() => {
      if (activeTurn !== t) return;
      log(`[resident] turn hard-timeout — no terminal result within ${Math.round(hardTurnTimeoutMs / 1000)}s; force-killing session`);
      t.timedOut = true;
      t.hardTimedOut = true;
      try { interrupt(); } catch { /* fall through to the kill below */ }
      setTimeout(() => {
        // Still the same active turn after the interrupt grace ⇒ genuinely wedged.
        // close() kills the group and settles this turn with processGone=true.
        if (activeTurn === t) close();
      }, RESIDENT_INTERRUPT_SIGINT_GRACE_MS + 1_000);
    }, hardTurnTimeoutMs);
  };

  const handleEvent = (ev: ResidentStreamEvent): void => {
    if (ev.sessionId && !sessionId) sessionId = ev.sessionId;
    // Superseded-turn suppression. When a turn was force-settled (stall path)
    // while the child stayed alive, that abandoned turn still owes exactly one
    // top-level `result` (staleResultsPending) — and, because the child emits
    // strictly in order, EVERYTHING it streams before that owed result (late
    // text/tool deltas, tool_results, nested sub-agent output) belongs to the
    // abandoned turn, NOT to the new `activeTurn` the loop has since started.
    // Drop those intermediates so a superseded turn's tail can neither stream as
    // the new turn's output nor fold into its result state (appliedStrategicOp /
    // sentOperatorMessage / accumulated text / pending message-send ids). The
    // owed top-level `result` itself is still handled below — it records its
    // token usage and decrements the counter (see the 'result' case). The
    // current turn's own output cannot arrive while this counter is >0 (it comes
    // only AFTER every owed result), so nothing legitimate is ever dropped.
    if (staleResultsPending > 0 && !(ev.kind === 'result' && ev.isTurnResult)) {
      return;
    }
    // Nested (sub-agent) events are sub-progress: surface their text but never
    // let a nested result close the parent's turn.
    if (ev.nested) {
      if (ev.kind === 'text' && ev.text) emit({ kind: 'text', text: ev.text });
      return;
    }
    switch (ev.kind) {
      case 'text': {
        if (!ev.text) break;
        const t = activeTurn;
        if (ev.isAssistantMessage) {
          // Finalized assistant block. Suppress if we already streamed its deltas
          // live (avoid showing the reply twice); otherwise surface it.
          if (t && t.sawDeltaThisMessage) { t.sawDeltaThisMessage = false; break; }
          emit({ kind: 'text', text: ev.text });
          if (t) {
            t.streamedText = true;
            t.accumulated += ev.text;
            if (t.firstTokenAtMs === null) t.firstTokenAtMs = Date.now();
          }
        } else {
          // Incremental delta.
          emit({ kind: 'text', text: ev.text });
          if (t) {
            t.streamedText = true;
            t.sawDeltaThisMessage = true;
            t.accumulated += ev.text;
            const now = Date.now();
            if (t.firstTokenAtMs === null) {
              t.firstTokenAtMs = now;
            } else {
              // Inter-token timing: record the gap since the last delta.
              if (t.lastTokenAtMs !== null) {
                t.interTokenIntervalsMs.push(now - t.lastTokenAtMs);
              }
            }
            t.lastTokenAtMs = now;
          }
        }
        break;
      }
      case 'tool': {
        emit({
          kind: 'tool',
          toolName: ev.toolName,
          text: ev.command,
          activity: ev.activityKind
            ? { kind: ev.activityKind, text: ev.activityText ?? (ev.toolName ?? 'tool') }
            : undefined,
        });
        const t = activeTurn;
        if (t) {
          t.sawToolUse = true;
          t.toolCallCount += 1;
          // Record the tool call start timestamp for per-tool latency tracking.
          // When ev.toolUseId is absent (should never happen), record under a
          // sentinel key so the tool_result handler can still compute duration.
          t.toolCallStartTimes.set(ev.toolUseId ?? `__no_id_${t.toolCallCount}`, Date.now());
          if (isStrategicMutation(ev.command)) t.appliedStrategicOp = true;
          // A `./messages send` is only a CONFIRMED durable reply once its
          // tool_result comes back without an error — track the pending id and
          // let the 'tool_result' case flip sentOperatorMessage on success. When
          // the CLI gave the block no id (should never happen) we cannot confirm,
          // so we leave the flag false and let the handler's persistReply backstop
          // run rather than optimistically assume a send that may have failed.
          if (isMessageSend(ev.command) && ev.toolUseId) t.pendingMessageSendIds.add(ev.toolUseId);
        }
        break;
      }
      case 'tool_result': {
        // Confirm a `./messages send` succeeded: a matching tool_result with no
        // error means the durable operator reply was actually posted. A failed
        // send (is_error) resolves the pending id WITHOUT setting the flag, so the
        // handler still runs its persistReply backstop.
        const t = activeTurn;
        if (t && ev.toolUseId && t.pendingMessageSendIds.has(ev.toolUseId)) {
          t.pendingMessageSendIds.delete(ev.toolUseId);
          if (!ev.isError) t.sentOperatorMessage = true;
        }
        // Per-tool-call latency: compute wall-clock duration from the tool_use
        // event to this tool_result and add it to the running total. When the
        // tool_use arrived before this turn started (stale), or the id is absent,
        // skip gracefully — totalToolCallDurationMs stays at its prior value.
        if (t && ev.toolUseId) {
          const startedAt = t.toolCallStartTimes.get(ev.toolUseId);
          if (startedAt !== undefined) {
            t.totalToolCallDurationMs += Date.now() - startedAt;
          }
        }
        // Surface a compact tool-result one-liner on the LIVE activity strip
        // (ephemeral; never persisted). Only when the parser produced a summary.
        if (ev.activityText) {
          emit({ kind: 'activity', activity: { kind: ev.activityKind ?? 'tool_result', text: ev.activityText } });
        }
        break;
      }
      case 'result': {
        if (!ev.isTurnResult) break;
        // Per-turn token telemetry: record usage on EVERY top-level result (even
        // a swallowed stale one — the tokens were still spent) so a multi-hour
        // session reports real totals instead of 1 call / 0 tokens.
        if (ev.usage) {
          recordLlmTokens(
            ev.usage.inputTokens, ev.usage.outputTokens,
            ev.usage.cacheCreationTokens, ev.usage.cacheReadTokens,
          );
          // Capture the inclusive input token count on the active turn so the
          // handler can detect context-window pressure and trigger compaction.
          if (activeTurn) activeTurn.inputTokens = ev.usage.inputTokens;
        }
        if (ev.numTurns !== undefined) {
          // numTurns is a cumulative count across the whole CLI process;
          // only emit the delta since the last result to avoid double-counting.
          const delta = ev.numTurns - lastSeenNumTurns;
          lastSeenNumTurns = ev.numTurns;
          if (Number.isFinite(delta) && delta > 0) recordNumTurns(delta);
        }
        // Swallow a result owed by an already-force-settled turn so it cannot
        // resolve a newer turn's promise (see staleResultsPending).
        if (staleResultsPending > 0) { staleResultsPending -= 1; break; }
        settleTurn(ev.text ?? '', false);
        break;
      }
      case 'error': {
        emit({ kind: 'error', text: ev.text });
        break;
      }
      default:
        break;
    }
  };

  const handleLine = (line: string): void => {
    for (const ev of parseResidentLine(line)) handleEvent(ev);
  };

  if (typeof pid === 'number') registerChildPid(pid);
  recordLlmCallStart();
  armIdleCeiling();

  // stdio is a literal ['pipe','pipe','pipe'], so Node guarantees non-null
  // stdin/stdout/stderr streams here (ChildProcessWithoutNullStreams).
  child.stdout.on('data', (data: Buffer) => {
    armIdleCeiling();
    if (activeTurn) armTurnStall(); // any output resets the per-turn stall clock
    const split = splitStreamChunk(carry, data.toString());
    carry = split.carry;
    for (const line of split.lines) handleLine(line);
  });
  child.stderr.on('data', (data: Buffer) => {
    const text = sanitize(data.toString());
    if (text.trim()) log(`[resident stderr] ${text.slice(0, 300)}`);
  });

  child.on('error', (err: Error) => {
    alive = false;
    emit({ kind: 'error', text: err.message });
    if (activeTurn) settleTurn('', true);
    resolveIdleWaiters();
  });
  child.on('close', (code: number | null) => {
    alive = false;
    exitCodeVal = code ?? null;
    if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
    // No further results can arrive from a dead child; drop any pending swallows
    // so a subsequent (post-restart) result is never wrongly discarded.
    staleResultsPending = 0;
    if (typeof pid === 'number') unregisterChildPid(pid);
    if (activeTurn) settleTurn('', true);
    resolveIdleWaiters();
    if (opts.onExit) { try { opts.onExit(exitCodeVal); } catch { /* ignore */ } }
  });

  function interrupt(): void {
    if (!alive || closed) return;
    const t = activeTurn;
    if (t) t.interrupted = true;
    let wroteControl = false;
    try {
      if (!child.stdin.destroyed) {
        child.stdin.write(encodeInterrupt(`int-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`));
        wroteControl = true;
      }
    } catch { /* fall through to SIGINT */ }
    const sigint = () => {
      // Never SIGTERM/SIGKILL a resident turn — SIGINT to the claude process only
      // cancels the current turn and keeps the warm session usable.
      try { if (typeof pid === 'number') process.kill(pid, 'SIGINT'); } catch { /* ignore */ }
    };
    if (!wroteControl) {
      sigint();
    } else if (t && !t.interruptTimer) {
      // Fall back to SIGINT if the control_request did not end the turn in time.
      t.interruptTimer = setTimeout(() => { if (activeTurn === t) sigint(); }, RESIDENT_INTERRUPT_SIGINT_GRACE_MS);
    }
  }

  function close(): void {
    if (closed) return;
    closed = true;
    alive = false;
    staleResultsPending = 0;
    if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
    try { child.stdin.end(); } catch { /* ignore */ }
    if (typeof pid === 'number') {
      try { process.kill(-pid, 'SIGTERM'); } catch { try { process.kill(pid, 'SIGTERM'); } catch { /* gone */ } }
      setTimeout(() => {
        try { process.kill(-pid, 'SIGKILL'); } catch { /* already gone */ }
      }, PROCESS_KILL_GRACE_MS);
    }
    if (activeTurn) settleTurn('', true);
    resolveIdleWaiters();
  }

  function sendTurn(textOrBlocks: string | Array<Record<string, unknown>>, opts?: { stallTimeoutMs?: number }): Promise<ResidentTurnResult> {
    if (!alive || closed) {
      return Promise.resolve({
        resultText: '', accumulatedTurnText: '', interrupted: false, timedOut: false, hardTimedOut: false,
        appliedStrategicOp: false, sawToolUse: false, sentOperatorMessage: false,
        processGone: true, ttftMs: null, turnMs: 0, toolCallCount: 0,
        totalToolCallDurationMs: 0, meanInterTokenMs: null, inputTokens: 0,
      });
    }
    if (activeTurn) {
      return Promise.reject(new Error('resident sendTurn called while a turn is already active'));
    }
    armIdleCeiling();
    // A per-turn stall override lets autonomous turns read/verify deeply without
    // the stall watchdog clipping them, while operator turns keep the responsive
    // default. Clamped to the session default floor so an override can only make
    // the budget DEEPER, never shorter than the responsive baseline.
    const stallTimeoutMs = typeof opts?.stallTimeoutMs === 'number' && Number.isFinite(opts.stallTimeoutMs)
      ? Math.max(turnTimeoutMs, opts.stallTimeoutMs)
      : turnTimeoutMs;
    const startedAtMs = Date.now();
    return new Promise<ResidentTurnResult>((resolve) => {
      const turn: ActiveTurn = {
        resolve, resultText: '', accumulated: '', streamedText: false,
        sawToolUse: false, appliedStrategicOp: false, sentOperatorMessage: false,
        pendingMessageSendIds: new Set<string>(),
        interrupted: false, timedOut: false, hardTimedOut: false, sawDeltaThisMessage: false,
        stallTimeoutMs,
        stallTimer: null, forceTimer: null, hardTimer: null, interruptTimer: null,
        startedAtMs, firstTokenAtMs: null, toolCallCount: 0, totalToolCallDurationMs: 0,
        toolCallStartTimes: new Map(), lastTokenAtMs: null, interTokenIntervalsMs: [],
        inputTokens: 0,
      };
      activeTurn = turn;
      try {
        if (child.stdin.destroyed) throw new Error('stdin closed');
        if (typeof textOrBlocks === 'string') {
          child.stdin.write(encodeUserTurn(textOrBlocks));
        } else {
          child.stdin.write(encodeUserTurnBlocks(textOrBlocks));
        }
      } catch (e) {
        activeTurn = null;
        emit({ kind: 'error', text: `failed to write turn: ${String(e)}` });
        resolve({
          resultText: '', accumulatedTurnText: '', interrupted: false, timedOut: false, hardTimedOut: false,
          appliedStrategicOp: false, sawToolUse: false, sentOperatorMessage: false,
          processGone: !alive, ttftMs: null, turnMs: Date.now() - startedAtMs, toolCallCount: 0,
          totalToolCallDurationMs: 0, meanInterTokenMs: null, inputTokens: 0,
        });
        return;
      }
      armTurnStall();
      armTurnHardTimeout(turn);
    });
  }

  return {
    sendTurn,
    interrupt,
    close,
    onEvent: (cb) => { listeners.push(cb); },
    touch: () => { if (alive && !closed) armIdleCeiling(); },
    waitForIdle: () => (activeTurn ? new Promise<void>((res) => { idleWaiters.push(res); }) : Promise.resolve()),
    sessionId: () => sessionId,
    isAlive: () => alive && !closed,
    exitCode: () => exitCodeVal,
  };
}
