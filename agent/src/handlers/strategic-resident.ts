import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, sendWsFrame,
  apiGet, apiPost, persistAndPostStatus, resolveBaseBranch,
} from '../index.js';
import { prepareWorkspaceForRole } from '../workspacePrep.js';
import {
  buildContextPrefix, configureAgentPermissions,
  writePlaybookHelper, writeMessagesHelper,
} from '../prompt.js';
import { writeStrategicHelpers } from './strategic-helpers.js';
import {
  startResidentClaudeSession, provisionAmbientHelpers,
  type ResidentSession, type ResidentTurnResult,
} from '../providers/runners.js';
import { buildDirSummaryText, buildOpenMessagesText } from './strategic-sections.js';
import {
  residentConfig, initialCursor, advanceCursorFromMessages,
  BoundedSeenSet, dedupNewMessages, combineOperatorMessages,
  buildBootstrapPrompt, buildAutonomousPrompt, resolveTerminalStatus,
  nextAuthorityHeld, autonomousActive, FrameCoalescer,
  MESSAGE_POLL_LIMIT, MAX_POLL_PAGES,
  type MessagesPollResponse, type SessionHeartbeatResponse, type ResidentInboundMessage,
} from './strategic-resident-lib.js';

// Coalesce streamed assistant deltas to at most ~10 WebSocket frames/sec so the
// live transcript stays well under the backend's 20 msg/s close threshold
// (routers/projects.py) — the same discipline as replayLogBufferRateLimited.
const STREAM_FLUSH_MS = 100;

// Consecutive per-turn stalls (the turn's stall watchdog fired) after which the
// session is considered wedged and stopped as FAILED rather than spun forever.
const MAX_CONSECUTIVE_STALLS = 3;

/** The `kind` vocabulary the backend `strategic_session` delta accepts
 *  (strategic_events.py `_STRATEGIC_SESSION_KINDS`). */
type SessionDeltaKind = 'reasoning' | 'action' | 'tool' | 'status' | 'message';

/**
 * Streams a resident turn's assistant text to the operator as a typed
 * `strategic_session` WebSocket frame:
 *
 *   { type: "strategic_session",
 *     payload: { session_id, task_id, seq, kind, text, ts } }
 *
 * The backend agent WS receiver (routers/projects.py) maps this frame to
 * `emit_strategic_session_delta`, which secret-sanitizes `text`, bounds the
 * payload under the NOTIFY ceiling, and broadcasts a `strategic_session` event
 * the UI's transcript branch (useLogViewer.ts) renders. `seq` is monotonic per
 * session so the UI can order/dedupe deltas. `kind` is one of reasoning /
 * action / tool / status / message; assistant text streams as `message`, tool
 * calls as `tool`, and lifecycle/error lines as `status`.
 *
 * ALL frame kinds — assistant text deltas, tool calls, and status lines — share
 * ONE rate budget: they are enqueued in order (text coalescing into a single
 * frame; a tool/status frame sealing the open text before it) and drained at
 * most once every `STREAM_FLUSH_MS` (≤10/s), so even a burst of tool_use blocks
 * in a single assistant message stays well under the backend's 20 msg/s close
 * threshold. When no live viewer socket is attached, each frame falls back to
 * the buffered `sendLog` transcript so nothing is lost.
 */
export class SessionStreamer {
  private readonly queue = new FrameCoalescer();
  private timer: NodeJS.Timeout | null = null;
  private lastEmitAt = 0;
  private seq = 0;
  constructor(
    private readonly ws: WebSocket | null,
    private readonly sessionId: string,
    private readonly taskId: string,
  ) {}

  private emit(kind: SessionDeltaKind, text: string): void {
    const clean = sanitize(text);
    if (!clean) return;
    const frame = {
      type: 'strategic_session',
      payload: {
        session_id: this.sessionId,
        task_id: this.taskId,
        seq: this.seq++,
        kind,
        text: clean,
        ts: new Date().toISOString(),
      },
    };
    // Prefer the typed frame on a live socket; otherwise keep the delta in the
    // buffered agent-log transcript so a later-attaching viewer still sees it.
    if (!sendWsFrame(this.ws, frame)) {
      sendLog(this.ws, clean.endsWith('\n') ? clean : `${clean}\n`);
    }
  }

  // Schedule the drain so it emits at most one queued frame per STREAM_FLUSH_MS
  // window (measured from the last real emit) — the shared ≤10/s rate budget.
  private scheduleDrain(): void {
    if (this.timer || this.queue.isEmpty) return;
    const elapsed = Date.now() - this.lastEmitAt;
    const wait = elapsed >= STREAM_FLUSH_MS ? 0 : STREAM_FLUSH_MS - elapsed;
    this.timer = setTimeout(() => {
      this.timer = null;
      const item = this.queue.next();
      if (item) { this.emit(item.kind, item.text); this.lastEmitAt = Date.now(); }
      this.scheduleDrain();
    }, wait);
  }

  delta(text: string): void {
    this.queue.pushText(text);
    this.scheduleDrain();
  }

  // Turn/session boundary flush. Seals the ending turn's open text item (so its
  // tail can't merge into the next turn's first delta) and hands the residual to
  // the SAME rate-limited drain the hot path uses. It deliberately does NOT
  // synchronously empty the queue: a turn that produced frames faster than the
  // ≤10/s drain (e.g. a burst of tool_use blocks in one assistant message) can
  // leave a large residual, and emitting it all in one tick here would blow past
  // the backend's 20 msg/s WS-close threshold (routers/projects.py) — the very
  // cap scheduleDrain exists to respect. Ordering and delivery are preserved:
  // scheduleDrain emits every queued frame, in order, one per STREAM_FLUSH_MS.
  flush(): void {
    this.queue.seal();
    this.scheduleDrain();
  }

  status(text: string): void {
    this.queue.pushFrame('status', `[strategic-session] ${text}`);
    this.scheduleDrain();
  }

  tool(name: string | undefined, command: string | undefined): void {
    const cmd = command ? ` ${command.slice(0, 160)}` : '';
    this.queue.pushFrame('tool', `[tool: ${name || 'tool'}]${cmd}`);
    this.scheduleDrain();
  }
}

const sleep = (ms: number): Promise<void> => new Promise((r) => { setTimeout(r, ms); });

const TURN_DONE = Symbol('resident-turn-done');

type WatchOutcome =
  | { kind: 'done' }
  | { kind: 'stop' }
  | { kind: 'suspend' }
  | { kind: 'message'; messages: ResidentInboundMessage[] };

/**
 * Resident strategic handler — the standing, chattable, streaming strategist.
 *
 * Entered when a claimed `strategic_review` task carries the resident flag
 * (payload.resident_session === true OR context._resident_session === true; see
 * handlers/index.ts dispatch). Instead of the one-shot thinker→challenge→executor
 * pass, it clones the workspace, provisions the same `./strategic` / `./fetch` /
 * `./design-doc` / `./messages` tools as handleStrategic, then starts ONE
 * persistent Claude session (runners.startResidentClaudeSession) and runs a
 * two-speed loop until the backend signals stop:
 *   - fast path:  a new operator message pre-empts any running autonomous turn,
 *                 then streams a reply and renews the lease (progress=true).
 *   - autonomous: a bounded "advance the project" self-prompt, interruptible by
 *                 the fast path, renewing the lease only when a strategic op ran.
 * The normal per-task heartbeat (index.startHeartbeat) keeps running in parallel
 * so the task stays non-terminal; this loop's SESSION heartbeat renews the
 * authority lease and is the stop channel. Terminal status is posted only once
 * the session actually stops.
 */
export async function handleStrategicResident(
  taskId: string, payload: Record<string, unknown>, task: Record<string, unknown>,
  ghToken: string, owner: string, repo: string, ws: WebSocket | null, statusRef: { value: string },
  model?: string, effort?: string,
): Promise<void> {
  const baseBranch = resolveBaseBranch(payload, task);
  statusRef.value = 'Starting resident strategic session...';
  sendLog(ws, 'Resident strategic session starting...\n');

  // ── Workspace + tool provisioning (mirror handleStrategic's setup) ──────────
  const cloneUrl = `https://x-access-token:${ghToken}@github.com/${owner}/${repo}.git`;
  await prepareWorkspaceForRole({
    workspace: WORKSPACE, repoUrl: cloneUrl, githubToken: ghToken,
    branch: baseBranch, baseBranch, depth: 0,
  });
  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const researchMode = strVal(payload.research_mode, 'research');
  const disallowedTools = configureAgentPermissions(researchMode);
  const contextPrefix = buildContextPrefix(task);

  const projectMetadata = task.project_metadata as Record<string, unknown> | undefined;
  const projectId = strVal(projectMetadata?.project_id);
  writeStrategicHelpers(taskId, projectId, WORKSPACE);
  provisionAmbientHelpers(taskId, WORKSPACE);

  const ctx = task.context && typeof task.context === 'object'
    ? task.context as Record<string, unknown>
    : {};
  const sessionId = strVal(ctx.strategic_session_id)
    || strVal(payload.strategic_session_id)
    || taskId;

  if (!projectId) {
    sendLog(ws, '[resident] ERROR: no project_id in task.project_metadata — cannot poll/heartbeat.\n');
    await persistAndPostStatus(taskId, {
      task_id: taskId, status: 'FAILED', artifact_type: 'ERROR_LOG',
      failure_category: 'claude_code_failed',
      logs: 'Resident strategic session could not resolve project_id.',
    });
    return;
  }

  // ── Bootstrap context (delivered on the FIRST turn, retained in-session) ────
  const vision = payload.vision as Record<string, unknown> | undefined;
  const visionJson = vision && Object.keys(vision).length > 0 ? JSON.stringify(vision, null, 2) : '';
  const problemModel = payload.problem_model as Record<string, unknown> | undefined;
  const problemModelJson = problemModel && Object.keys(problemModel).length > 0
    ? JSON.stringify(problemModel, null, 2) : '';
  const directionsSummary = Array.isArray(payload.directions_summary)
    ? payload.directions_summary as Array<Record<string, unknown>> : [];
  const directionCounts = payload.direction_counts && typeof payload.direction_counts === 'object'
    ? payload.direction_counts as Record<string, unknown> : undefined;
  const authoritativeActive = directionCounts && typeof directionCounts.active === 'number'
    ? directionCounts.active : undefined;
  const openMessages = Array.isArray(payload.open_messages)
    ? payload.open_messages as Array<Record<string, unknown>> : [];

  const bootstrapPrompt = buildBootstrapPrompt({
    contextPrefix,
    projectName: strVal(payload.project_name, 'this project'),
    problemModelJson,
    runningUpdate: typeof payload.running_update === 'string' ? payload.running_update : '',
    directionsText: buildDirSummaryText(directionsSummary, authoritativeActive),
    visionJson,
    openMessagesText: buildOpenMessagesText(openMessages),
    sessionId,
  });

  const cfg = residentConfig(payload);
  let cursor = initialCursor(task, payload);
  // Bounded set of message ids already handled — with `created_at >= since` poll
  // semantics the boundary message re-appears each poll and is dropped here.
  const seen = new BoundedSeenSet();

  // ── Session + streaming wiring ──────────────────────────────────────────────
  const streamer = new SessionStreamer(ws, sessionId, taskId);
  const session: ResidentSession = startResidentClaudeSession({
    model, effort, disallowedTools, workspace: WORKSPACE, turnTimeoutMs: cfg.turnTimeoutMs,
  });
  session.onEvent((ev) => {
    if (ev.kind === 'text' && ev.text) streamer.delta(ev.text);
    else if (ev.kind === 'tool') streamer.tool(ev.toolName, ev.text);
    else if (ev.kind === 'error' && ev.text) streamer.status(`error: ${ev.text}`);
  });

  let firstTurnSent = false;
  let stopReason = 'stop_requested';
  // Consecutive turns whose per-turn stall watchdog fired. N in a row ⇒ the
  // session is wedged (not merely slow) ⇒ fail it rather than spin forever.
  let consecutiveStalls = 0;
  // Whether we currently hold the strategic-write AUTHORITY lease. Assumed held
  // at start (an operator/strategist just armed the session) and updated from
  // every heartbeat: the backend lapses it (authority_held:false) once an
  // unattended session exceeds its cap, and re-arms it (authority_held:true)
  // when a new operator message re-engages. While false the loop suspends the
  // autonomous path — no self-prompting, no strategic mutations — and goes
  // passive (chat + heartbeat only) so it never becomes a second concurrent
  // writer alongside the resumed cloud strategist. Losing authority NEVER ends
  // the session; only stop_requested / 401 does.
  let authorityHeld = true;

  const runTurn = async (text: string, operator: boolean): Promise<ResidentTurnResult> => {
    const full = firstTurnSent ? text : `${bootstrapPrompt}\n\n---\n\n${text}`;
    firstTurnSent = true;
    statusRef.value = operator ? 'Responding to operator...' : 'Advancing project (autonomous)...';
    const r = await session.sendTurn(full);
    streamer.flush();
    return r;
  };

  // Evaluate a completed turn for a wedged/stalled session. Returns true when the
  // session must stop as FAILED: an ABSOLUTE hard timeout fired (process was
  // force-killed), or too many turns stalled back-to-back. Advances the
  // consecutive-stall counter as a side effect (an interrupt is NOT a stall).
  const turnWedgedSession = (r: ResidentTurnResult): boolean => {
    if (r.hardTimedOut) { stopReason = 'turn_hard_timeout'; return true; }
    if (r.timedOut) {
      consecutiveStalls += 1;
      if (consecutiveStalls >= MAX_CONSECUTIVE_STALLS) {
        stopReason = 'session_stalled';
        return true;
      }
    } else {
      consecutiveStalls = 0;
    }
    return false;
  };

  // Persist an operator-facing reply as a durable strategic ProjectMessage
  // (from_type=strategic) so it lands in the chat transcript, in ADDITION to the
  // live stream. The turn prompt REQUIRES the agent to `./messages send` its
  // answer; this is the backstop for when it did not. Autonomous turns carry no
  // operator reply, so callers only invoke it for operator turns.
  const persistReply = async (r: ResidentTurnResult): Promise<void> => {
    if (r.sentOperatorMessage) return; // agent already posted it itself
    const text = r.resultText.trim();
    if (!text) return;
    try {
      await apiPost('/api/agent/strategic', {
        task_id: taskId,
        op: 'create_message',
        data: { type: 'NOTE', content: text },
      });
    } catch (e) {
      sendLog(ws, `[resident] reply persistence failed (non-fatal): ${String(e)}\n`);
    }
  };

  const pollMessages = async (): Promise<ResidentInboundMessage[]> => {
    const collected: ResidentInboundMessage[] = [];
    try {
      for (let page = 0; page < MAX_POLL_PAGES; page++) {
        const prevCursor = cursor;
        const q = `since=${encodeURIComponent(cursor)}&from_type=user&status=open&limit=${MESSAGE_POLL_LIMIT}`;
        const resp = await apiGet(`/api/agent/projects/${projectId}/messages?${q}`) as MessagesPollResponse;
        const raw = Array.isArray(resp.messages) ? resp.messages : [];
        collected.push(...dedupNewMessages(raw, seen));
        // Advance to the MAX returned created_at; the >= boundary message
        // re-appears next poll and the seen-set drops it.
        cursor = advanceCursorFromMessages(raw, cursor);
        // Short page ⇒ tail drained. A full page that did not move the cursor (a
        // whole page sharing one timestamp) can't be drained by re-polling, so
        // stop to avoid an infinite loop.
        if (raw.length < MESSAGE_POLL_LIMIT) break;
        if (cursor === prevCursor) break;
      }
    } catch (e) {
      sendLog(ws, `[resident] message poll failed (non-fatal): ${String(e)}\n`);
    }
    return collected;
  };

  // progress=true renews the authority lease (applied op / operator interaction);
  // progress=false is bare liveness (keeps the session fresh for the reaper only).
  const heartbeat = async (progress: boolean): Promise<SessionHeartbeatResponse | null> => {
    // A heartbeat is proof the loop is alive — reset the warm session's idle
    // ceiling so a chat-only / monitor session (which issues no turns to reset
    // it) never self-terminates while the loop keeps polling.
    session.touch();
    try {
      const resp = await apiPost(`/api/agent/projects/${projectId}/strategic-session/heartbeat`, {
        session_id: sessionId, task_id: taskId, progress,
      }) as SessionHeartbeatResponse;
      // Fold the lease state so the loop's autonomous gate tracks the backend.
      authorityHeld = nextAuthorityHeld(authorityHeld, resp);
      return resp;
    } catch (e) {
      const msg = String(e);
      // 401 / "no longer active" ⇒ the task went terminal ⇒ stop the session.
      if (/\(401\)/.test(msg) || /no longer active/i.test(msg)) {
        stopReason = 'task_terminal';
        authorityHeld = false;
        return { stop_requested: true, status: 'ended', authority_held: false };
      }
      sendLog(ws, `[resident] heartbeat failed (non-fatal): ${msg}\n`);
      return null;
    }
  };

  // Poll + heartbeat while an autonomous turn runs; resolves the moment the turn
  // finishes, a stop is requested, or an operator message arrives (→ interrupt).
  // Returns a discriminated outcome so the loop needs no async-mutated flags.
  const watchTurn = async (turnPromise: Promise<ResidentTurnResult>): Promise<WatchOutcome> => {
    const doneSignal: Promise<typeof TURN_DONE> = turnPromise.then(() => TURN_DONE);
    for (;;) {
      const raced = await Promise.race([doneSignal, sleep(cfg.idleTickMs).then(() => 'tick' as const)]);
      if (raced === TURN_DONE) return { kind: 'done' };
      const hb = await heartbeat(false); // liveness only while working
      if (hb?.stop_requested) return { kind: 'stop' };
      // Authority lapsed mid-turn (backend unattended cap): suspend the autonomous
      // path — pre-empt this turn and go passive rather than let it finish
      // mutating strategic state on a lapsed lease.
      if (!authorityHeld) return { kind: 'suspend' };
      const newMsgs = await pollMessages();
      if (newMsgs.length > 0) return { kind: 'message', messages: newMsgs };
    }
  };

  // ── Two-speed loop ──────────────────────────────────────────────────────────
  try {
    for (;;) {
      if (!session.isAlive()) {
        stopReason = 'session_process_exited';
        break;
      }

      // 1. Fast path: any pending operator message pre-empts everything.
      const msgs = await pollMessages();
      if (msgs.length > 0) {
        streamer.status('operator message received — responding');
        const r = await runTurn(combineOperatorMessages(msgs), true);
        if (turnWedgedSession(r)) break;
        await persistReply(r);
        const hb = await heartbeat(true); // operator interaction always renews the lease
        if (hb?.stop_requested) break;
        if (r.processGone) { stopReason = 'session_process_exited'; break; }
        continue;
      }

      // Passive mode: chat-only, no self-driven work. Entered either for a
      // read-only monitor session (autonomous disabled) OR when the authority
      // lease has lapsed (backend's unattended cap → authority_held:false). We
      // keep polling operator messages (the fast path above) and heartbeating;
      // the heartbeat here touches the session so its idle ceiling never fires,
      // and folds any re-arm (authority_held:true — set when a new operator
      // message re-engages) so the NEXT iteration reopens the autonomous path.
      // We never self-prompt or mutate strategic state on a lapsed lease.
      if (!autonomousActive(cfg.autonomousEnabled, authorityHeld)) {
        const hb = await heartbeat(false);
        if (hb?.stop_requested) break;
        await sleep(cfg.idleTickMs);
        continue;
      }

      // 2. Autonomous path: a bounded self-prompt, interruptible by the fast path.
      const turnPromise = runTurn(buildAutonomousPrompt(), false);
      const watch = await watchTurn(turnPromise);
      if (watch.kind !== 'done') session.interrupt();

      // Await the (possibly interrupted) autonomous turn, then renew the lease
      // ONLY if a strategic op was applied (progress) AND we still hold authority
      // — never renew a lease that lapsed mid-turn (watch === 'suspend'), and
      // never renew on bare work.
      const r = await turnPromise;
      if (turnWedgedSession(r)) break;
      const hb = await heartbeat(r.appliedStrategicOp && authorityHeld);
      if (hb?.stop_requested || watch.kind === 'stop') break;
      if (r.processGone) { stopReason = 'session_process_exited'; break; }
      // Authority lapsed mid-turn ⇒ fall through to passive mode next iteration.
      if (watch.kind === 'suspend') continue;

      // Handle any operator message that interrupted the autonomous turn.
      if (watch.kind === 'message') {
        streamer.status('operator message received (interrupted autonomous work)');
        const or = await runTurn(combineOperatorMessages(watch.messages), true);
        if (turnWedgedSession(or)) break;
        await persistReply(or);
        const hb2 = await heartbeat(true);
        if (hb2?.stop_requested) break;
        if (or.processGone) { stopReason = 'session_process_exited'; break; }
        continue;
      }

      // Nothing applied and nothing done → pace so we don't hot-loop the API.
      if (!r.appliedStrategicOp && !r.sawToolUse) {
        await sleep(cfg.idleTickMs);
      }
    }
  } catch (e) {
    stopReason = `loop_error: ${String(e)}`;
    sendLog(ws, `[resident] loop error: ${String(e)}\n`);
  } finally {
    streamer.flush();
    session.close();
  }

  // ── Terminal status ─────────────────────────────────────────────────────────
  // Consult the ACTUAL stop reason + process exit code: a crash / wedge is a
  // FAILED terminal, COMPLETED only on a clean operator/task-driven stop. A
  // session that limped or died must not report success.
  statusRef.value = 'Resident strategic session ended';
  sendLog(ws, `Resident strategic session ${sessionId} ended (${stopReason}).\n`);

  const { status, failureCategory } = resolveTerminalStatus(stopReason, session.exitCode());
  try {
    await persistAndPostStatus(taskId, {
      task_id: taskId,
      status,
      artifact_type: status === 'FAILED' ? 'ERROR_LOG' : 'RESEARCH_FINDINGS',
      ...(failureCategory ? { failure_category: failureCategory } : {}),
      logs: `Resident strategic session ${sessionId} ended (${stopReason}).`,
    });
  } catch (e) {
    sendLog(ws, `[ERROR] Failed to post resident terminal status: ${String(e)}\n`);
  }
}
