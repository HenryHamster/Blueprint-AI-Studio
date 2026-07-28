import { spawn } from 'child_process';
import { registerChildPid, unregisterChildPid } from '../processTracking.js';
import {
  WORKSPACE, getAgentToken, log, recordLlmCallStart, recordLlmTokens, recordNumTurns, sanitize,
} from '../index.js';
import { writeAgentTokenFile } from '../token-file.js';
import { PROCESS_KILL_GRACE_MS } from '../constants.js';
import {
  parseResidentLine, splitStreamChunk, encodeUserTurn, encodeUserTurnBlocks,
  isStrategicMutation, type ResidentStreamEvent,
} from './resident-stream.js';
import { RESIDENT_REPLY_TOOL_NAME } from './resident-reply-tool.js';
import {
  getProviderRunner, matchesAuthFailure, matchesRateLimit,
  probeClaudeCapabilities, stripUnsupportedClaudeOptionalArgs,
} from './runners.js';

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
const OPERATOR_REPLY_TYPES = new Set(['NOTE', 'QUESTION', 'FEEDBACK', 'BUG_REPORT', 'BLOCKER']);

/** The exact reply arguments observed on the native MCP tool_use event. */
export interface ResidentOperatorReply {
  type: string;
  content: string;
}

function parseOperatorReplyInput(input: unknown): ResidentOperatorReply | null {
  if (input === null || typeof input !== 'object' || Array.isArray(input)) return null;
  const raw = input as Record<string, unknown>;
  const content = typeof raw.content === 'string' ? raw.content.trim() : '';
  const type = raw.message_type === undefined ? 'NOTE' : raw.message_type;
  if (!content || typeof type !== 'string' || !OPERATOR_REPLY_TYPES.has(type)) return null;
  return { content, type };
}

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
  /** the agent invoked the native Workday reply MCP tool during the turn AND
   *  its tool_result confirmed success. Plain assistant text and shell helpers
   *  are deliberately not replies. */
  sentOperatorMessage: boolean;
  /** Exact accepted native-tool payload, retained outside the model-writable
   *  staging file so it cannot be altered after tool success. */
  operatorReply: ResidentOperatorReply | null;
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
  /** A top-level provider error result ended this turn. */
  providerFailed: boolean;
  /** Canonical provider failure when terminal evidence is recognizable. */
  failureCategory: 'rate_limited' | 'dead_auth' | null;
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
   *  read/verify step isn't killed mid-thought). `opts.hardTimeoutMs` overrides the
   *  absolute wall-clock ceiling for this turn, which is never reset by output. */
  sendTurn(textOrBlocks: string | Array<Record<string, unknown>>, opts?: {
    stallTimeoutMs?: number;
    hardTimeoutMs?: number;
  }): Promise<ResidentTurnResult>;
  /** Cooperatively pre-empt the in-flight turn with SIGINT. */
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
  /** Classified terminal provider failure from bounded stderr/stream errors. */
  failureCategory?(): 'rate_limited' | 'dead_auth' | null;
  /**
   * Bounded, redacted diagnostic for a child-process exit when startup fails
   * before the resident can accept a turn.
   */
  exitDiagnostic?(): string | null;
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
  /** Required native MCP reply tool for this Resident session. */
  replyTool?: { mcpConfigPath: string; toolName?: string; allowedTools?: readonly string[] };
  /**
   * Restricted foreground launch supplied by the Call Coordinator.  Unlike the
   * ordinary coding Resident it has an empty cwd, a filtered environment, and
   * no broad-permission CLI arguments.  The caller must have already built the
   * strict local-MCP configuration; this transport remains Claude-compatible.
   */
  foregroundLaunch?: { command: string; args: string[]; env: NodeJS.ProcessEnv; cwd: string };
}

interface ActiveTurn {
  resolve: (r: ResidentTurnResult) => void;
  resultText: string;
  accumulated: string;
  streamedText: boolean;
  sawToolUse: boolean;
  appliedStrategicOp: boolean;
  sentOperatorMessage: boolean;
  operatorReply: ResidentOperatorReply | null;
  /** Native reply inputs, keyed by their tool-use id until tool_result arrives. */
  pendingOperatorReplies: Map<string, ResidentOperatorReply>;
  interrupted: boolean;
  timedOut: boolean;
  hardTimedOut: boolean;
  providerFailed: boolean;
  failureCategory: 'rate_limited' | 'dead_auth' | null;
  sawDeltaThisMessage: boolean;
  /** per-active-turn stall budget for THIS turn (may override the session default). */
  stallTimeoutMs: number;
  /** absolute wall-clock ceiling for THIS turn; output never extends it. */
  hardTimeoutMs: number;
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
  const runnerBase = runner.buildRunCommand('', opts.model, opts.effort, opts.disallowedTools);
  const base = opts.foregroundLaunch
    ? {
      command: opts.foregroundLaunch.command,
      args: opts.foregroundLaunch.args,
      env: opts.foregroundLaunch.env,
      outputMode: runnerBase.outputMode,
    }
    : runnerBase;
  if (base.outputMode !== 'claude-stream-json') {
    throw new Error(
      `resident session requires a Claude-compatible provider (anthropic/zai/deepseek); got ${runner.provider}/${base.outputMode}`,
    );
  }
  // Clone the one-shot args and append bidirectional streaming input. The one-shot
  // command already carries -p / --output-format stream-json / --verbose /
  // --include-partial-messages / --dangerously-skip-permissions / --model / --effort
  // / --disallowedTools, so we only add the input side.
  // Keep resident launch compatible with the installed Claude binary just like
  // the ordinary one-shot runner path.
  const capabilities = probeClaudeCapabilities(base.command, base.env);
  const compatibleBaseArgs = opts.foregroundLaunch
    ? [...base.args]
    : stripUnsupportedClaudeOptionalArgs(base.args, capabilities);
  const args = opts.foregroundLaunch || compatibleBaseArgs.includes('--input-format')
    ? compatibleBaseArgs
    : [...compatibleBaseArgs, '--input-format', 'stream-json'];
  if (opts.replyTool) {
    if (capabilities && (
      !capabilities.has('--mcp-config')
      || !capabilities.has('--strict-mcp-config')
      || !capabilities.has('--allowedTools')
    )) {
      throw new Error('resident native reply tool requires Claude --mcp-config, --strict-mcp-config, and --allowedTools support');
    }
    args.push('--mcp-config', opts.replyTool.mcpConfigPath, '--strict-mcp-config');
    for (const toolName of opts.replyTool.allowedTools ?? [opts.replyTool.toolName ?? RESIDENT_REPLY_TOOL_NAME]) {
      args.push('--allowedTools', toolName);
    }
  }

  // Guarantee the live token file exists with the CURRENT token before the
  // long-lived subprocess (and its bash helpers) start reading it. Refreshes
  // thereafter keep it current via setToken. Best-effort/no-op when the file is
  // unconfigured or the token is empty (tests).
  // The normal Resident keeps an agent token file for its project helpers.
  // A foreground Call Resident has no helpers and must not create or inherit
  // that backend credential in its isolated environment.
  if (!opts.foregroundLaunch) writeAgentTokenFile(getAgentToken());

  const child = spawn(base.command, args, {
    cwd: opts.foregroundLaunch?.cwd ?? workspace,
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
  let exitSignalVal: NodeJS.Signals | null = null;
  let terminalFailureCategory: 'rate_limited' | 'dead_auth' | null = null;
  let failureTextTail = '';
  const appendFailureText = (text: string): void => {
    if (!text) return;
    failureTextTail = (failureTextTail + '\n' + text).slice(-16_000);
  };
  const classifyFailure = (text: string): 'rate_limited' | 'dead_auth' | null => {
    if (matchesRateLimit(text, runner.provider, runner.authMode)) return 'rate_limited';
    if (matchesAuthFailure(text, runner.provider)) return 'dead_auth';
    return null;
  };
  // Claude's OAuth weekly-limit banner is sometimes emitted as an ordinary
  // assistant text frame before the CLI exits. Keep this deliberately narrow:
  // a model discussing rate limits must not be mistaken for an operational
  // failure, but the provider's exact "You've hit your weekly limit" wording
  // must never become a Strategic reply.
  const isOAuthQuotaBanner = (text: string): boolean => (
    matchesRateLimit(text, runner.provider, runner.authMode)
    && /\byou(?:'|’)ve hit your (?:weekly|usage) limit\b|\byou have hit your (?:weekly|usage) limit\b/i.test(text)
  );
  const clearFailureEvidence = (): void => {
    failureTextTail = '';
    terminalFailureCategory = null;
  };
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
      operatorReply: t.operatorReply,
      processGone,
      ttftMs,
      turnMs,
      toolCallCount: t.toolCallCount,
      totalToolCallDurationMs: t.totalToolCallDurationMs,
      meanInterTokenMs,
      inputTokens: t.inputTokens,
      providerFailed: t.providerFailed,
      failureCategory: t.failureCategory,
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
      log(`[resident] turn hard-timeout — no terminal result within ${Math.round(t.hardTimeoutMs / 1000)}s; force-killing session`);
      t.timedOut = true;
      t.hardTimedOut = true;
      try { interrupt(); } catch { /* fall through to the kill below */ }
      setTimeout(() => {
        // Still the same active turn after the interrupt grace ⇒ genuinely wedged.
        // close() kills the group and settles this turn with processGone=true.
        if (activeTurn === t) close();
      }, RESIDENT_INTERRUPT_SIGINT_GRACE_MS + 1_000);
    }, t.hardTimeoutMs);
  };

  const handleEvent = (ev: ResidentStreamEvent): void => {
    const textQuotaBanner = ev.kind === 'text'
      && typeof ev.text === 'string'
      && isOAuthQuotaBanner(ev.text);
    if (
      staleResultsPending === 0
      && !ev.nested
      // OAuth quota exhaustion can arrive before the first input turn as a
      // bare stdout line rather than a stream-json result/error frame. Treat
      // that raw provider output as terminal evidence too; otherwise the
      // child exits and the resident is retried generically against the same
      // exhausted account.
      && (ev.kind === 'error'
        || ev.kind === 'other'
        || textQuotaBanner
        || (ev.kind === 'result' && ev.isError && ev.isTurnResult)) && ev.text
    ) {
      appendFailureText(ev.text);
      const category = classifyFailure(failureTextTail);
      if (category) terminalFailureCategory = category;
      if (activeTurn && category) {
        activeTurn.providerFailed = true;
        activeTurn.failureCategory = category;
      }
    }
    if (ev.sessionId && !sessionId) sessionId = ev.sessionId;
    if (textQuotaBanner) {
      // The banner is a provider failure, not conversation content. Settling
      // immediately lets the resident handler publish `rate_limited` and
      // release/blacklist the account without waiting for the CLI to exit.
      if (activeTurn) settleTurn('', false);
      return;
    }
    // Superseded-turn suppression. When a turn was force-settled (stall path)
    // while the child stayed alive, that abandoned turn still owes exactly one
    // top-level `result` (staleResultsPending) — and, because the child emits
    // strictly in order, EVERYTHING it streams before that owed result (late
    // text/tool deltas, tool_results, nested sub-agent output) belongs to the
    // abandoned turn, NOT to the new `activeTurn` the loop has since started.
    // Drop those intermediates so a superseded turn's tail can neither stream as
    // the new turn's output nor fold into its result state (appliedStrategicOp /
    // sentOperatorMessage / accepted native reply / accumulated text / pending
    // reply-tool ids). The
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
          // Only the explicit MCP reply tool carries the delivery contract.
          // Bash `./messages send`, `./strategic create-message`, and normal
          // assistant text are not accepted as an operator reply.
          if (ev.toolName === (opts.replyTool?.toolName ?? RESIDENT_REPLY_TOOL_NAME) && ev.toolUseId) {
            const reply = parseOperatorReplyInput(ev.input);
            if (reply) t.pendingOperatorReplies.set(ev.toolUseId, reply);
          }
        }
        break;
      }
      case 'tool_result': {
        // A staged native reply counts only after its matching MCP tool_result
        // confirms success. This guards against a tool call that failed to write
        // the receipt-bound staging file.
        const t = activeTurn;
        if (t && ev.toolUseId) {
          const reply = t.pendingOperatorReplies.get(ev.toolUseId);
          t.pendingOperatorReplies.delete(ev.toolUseId);
          if (!ev.isError && reply) {
            t.sentOperatorMessage = true;
            t.operatorReply = reply;
          }
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
        // Claude's OAuth usage-limit banner is sometimes delivered as a
        // successful-looking top-level result (rather than an error event or
        // stderr) and then the CLI stays alive. Treat that exact provider
        // evidence as terminal before the ordinary-success path below clears
        // the failure tail; otherwise a resident keeps accepting turns from an
        // exhausted account and the pool never gets a rate-limit signal to
        // rotate away from it.
        const resultFailure = ev.text ? classifyFailure(ev.text) : null;
        if (resultFailure) {
          appendFailureText(ev.text ?? '');
          terminalFailureCategory = resultFailure;
          if (activeTurn) {
            activeTurn.providerFailed = true;
            activeTurn.failureCategory = resultFailure;
          }
        }
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
        // A successful result proves earlier warnings were transient. Scope the
        // evidence to the current terminal failure so an old warning cannot
        // poison an unrelated process exit later in the session.
        if (!ev.isError && !resultFailure) clearFailureEvidence();
        if (ev.isError && activeTurn) {
          activeTurn.providerFailed = true;
          activeTurn.failureCategory = classifyFailure(failureTextTail);
        }
        if (resultFailure && !ev.isError) {
          // Do not render the quota banner as an assistant reply. It is an
          // operational error, and SessionStreamer surfaces this as status text
          // while the handler terminates with provider_rate_limited.
          emit({ kind: 'error', text: ev.text });
          settleTurn('', false);
          break;
        }
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
    appendFailureText(text);
    if (text.trim()) log(`[resident stderr] ${text.slice(0, 300)}`);
  });

  child.on('error', (err: Error) => {
    appendFailureText(err.message);
    alive = false;
    emit({ kind: 'error', text: err.message });
    terminalFailureCategory = classifyFailure(failureTextTail);
    if (activeTurn) settleTurn('', true);
    resolveIdleWaiters();
  });
  child.on('close', (code: number | null, signal: NodeJS.Signals | null) => {
    // Some Claude CLI failures (notably the OAuth weekly-limit banner) are
    // written as bare stdout without a final newline. Feed that final fragment
    // through the ordinary parser before classifying this process exit.
    if (carry.trim()) {
      const trailing = carry;
      carry = '';
      handleLine(trailing);
    }
    alive = false;
    exitCodeVal = code ?? null;
    exitSignalVal = signal ?? null;
    if (idleTimer) { clearTimeout(idleTimer); idleTimer = null; }
    // No further results can arrive from a dead child; drop any pending swallows
    // so a subsequent (post-restart) result is never wrongly discarded.
    if (!closed) {
      terminalFailureCategory = classifyFailure(failureTextTail);
      if (activeTurn) activeTurn.failureCategory = terminalFailureCategory;
    }
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
    // Claude's stream-json input accepts only user frames. Sending a synthetic
    // control_request makes the CLI exit instead of cancelling the active
    // turn, so always use the process-level cooperative interrupt.
    try { if (typeof pid === 'number') process.kill(pid, 'SIGINT'); } catch { /* ignore */ }
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

  function sendTurn(textOrBlocks: string | Array<Record<string, unknown>>, opts?: {
    stallTimeoutMs?: number;
    hardTimeoutMs?: number;
  }): Promise<ResidentTurnResult> {
    if (!alive || closed) {
      return Promise.resolve({
        resultText: '', accumulatedTurnText: '', interrupted: false, timedOut: false, hardTimedOut: false,
        appliedStrategicOp: false, sawToolUse: false, sentOperatorMessage: false, operatorReply: null,
        processGone: true, ttftMs: null, turnMs: 0, toolCallCount: 0,
        totalToolCallDurationMs: 0, meanInterTokenMs: null, inputTokens: 0,
        providerFailed: false, failureCategory: terminalFailureCategory,
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
    // An interactive turn must not extend its wall-clock ceiling merely because
    // it keeps producing tool activity. Autonomous callers deliberately omit
    // this override and retain the session's deeper default hard ceiling.
    const turnHardTimeoutMs = typeof opts?.hardTimeoutMs === 'number' && Number.isFinite(opts.hardTimeoutMs)
      ? Math.max(1, Math.round(opts.hardTimeoutMs))
      : hardTurnTimeoutMs;
    const startedAtMs = Date.now();
    return new Promise<ResidentTurnResult>((resolve) => {
      const turn: ActiveTurn = {
        resolve, resultText: '', accumulated: '', streamedText: false,
        sawToolUse: false, appliedStrategicOp: false, sentOperatorMessage: false, operatorReply: null,
        pendingOperatorReplies: new Map<string, ResidentOperatorReply>(),
        interrupted: false, timedOut: false, hardTimedOut: false, sawDeltaThisMessage: false,
        stallTimeoutMs, hardTimeoutMs: turnHardTimeoutMs,
        stallTimer: null, forceTimer: null, hardTimer: null, interruptTimer: null,
        startedAtMs, firstTokenAtMs: null, toolCallCount: 0, totalToolCallDurationMs: 0,
        providerFailed: false, failureCategory: null,
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
          appliedStrategicOp: false, sawToolUse: false, sentOperatorMessage: false, operatorReply: null,
          processGone: !alive, ttftMs: null, turnMs: Date.now() - startedAtMs, toolCallCount: 0,
          totalToolCallDurationMs: 0, meanInterTokenMs: null, inputTokens: 0,
          providerFailed: false, failureCategory: terminalFailureCategory,
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
    failureCategory: () => terminalFailureCategory ?? classifyFailure(failureTextTail),
    exitDiagnostic: () => {
      if (alive) return null;
      const parts = [`exit_code=${exitCodeVal ?? 'null'}`];
      if (exitSignalVal) parts.push(`exit_signal=${exitSignalVal}`);
      const evidence = sanitize(failureTextTail).trim();
      if (evidence) parts.push(`provider_output_tail=${evidence.slice(-2_000)}`);
      return parts.join('; ');
    },
  };
}
