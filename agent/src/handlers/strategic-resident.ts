import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, sendWsFrame,
  apiGet, apiPost, persistAndPostStatus, resolveBaseBranch,
  recordResidentTurnLatency, getResidentTurnLatencyTelemetry,
} from '../index.js';
import { prepareWorkspaceForRole, listWorkspaceTopDirs } from '../workspacePrep.js';
import {
  buildContextPrefix, configureResidentAgentPermissions,
  writePlaybookHelper, writeMessagesHelper,
} from '../prompt.js';
import { writeStrategicHelpers } from './strategic-helpers.js';
import { provisionAmbientHelpers } from '../providers/runners.js';
import {
  writeGitTokenFile, setupGitCredentialHelper, GIT_TOKEN_REFRESH_MS,
} from '../git-credential.js';
import {
  startResidentClaudeSession,
  type ResidentSession, type ResidentSessionEvent, type ResidentTurnResult,
} from '../providers/resident-session.js';
import {
  buildDirSummaryText,
  buildOpenMessagesText,
} from './strategic-sections.js';
import {
  residentConfig, initialCursor, advanceCursorFromMessages,
  BoundedSeenSet, dedupNewMessages, combineOperatorMessages,
  materializeContentBlocks, buildBootstrapPrompt, buildAutonomousPrompt, resolveTerminalStatus,
  nextAuthorityHeld, autonomousActive, FrameCoalescer,
  MESSAGE_POLL_LIMIT, MAX_POLL_PAGES,
  buildPriorDecisionsText, buildRetiredContextText, buildStagedDirectionsText,
  buildSurfaceCatalog, nextFocus, classifyTickOutcome, openFoundLeads,
  shouldRunAutonomousTurn, EXAM_RECORDS_CAP, FOCUS_LIST_SIZE,
  serializeLedger, restoreLedger, refreshSurfaceCatalog,
  compactionThreshold, buildCompactionCheckpoint,
  type MessagesPollResponse, type SessionHeartbeatResponse, type ResidentInboundMessage,
  type StreamFrameKind, type StreamFrameStatus, type ExamRecord, type ContentBlock,
  type CompactionCheckpointContext,
} from './strategic-resident-lib.js';
import { buildFetchGuidance, STRATEGIC_FETCH_VERBS } from './strategic-helpers.js';

const _RESIDENT_STRATEGIC_TASK_TYPE = 'strategic_review' as const;

// Coalesce streamed assistant deltas to at most ~10 WebSocket frames/sec so the
// live transcript stays well under the backend's 20 msg/s close threshold
// (routers/projects.py) — the same discipline as replayLogBufferRateLimited.
const STREAM_FLUSH_MS = 100;

// Consecutive per-turn stalls (the turn's stall watchdog fired) after which the
// session is considered wedged and stopped as FAILED rather than spun forever.
const MAX_CONSECUTIVE_STALLS = 3;


/** The `kind` vocabulary the backend `strategic_session` delta accepts
 *  (strategic_events.py `_STRATEGIC_SESSION_KINDS`) — reused from the lib so the
 *  emit + parse sides can never drift. */
type SessionDeltaKind = StreamFrameKind;

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

  private emit(kind: SessionDeltaKind, text: string, status?: StreamFrameStatus): void {
    const clean = sanitize(text);
    // A lifecycle frame (responding/done) is allowed to carry empty text; every
    // other frame with no text after sanitizing is dropped.
    if (!clean && !status) return;
    const payload: Record<string, unknown> = {
      session_id: this.sessionId,
      task_id: this.taskId,
      seq: this.seq++,
      kind,
      text: clean,
      ts: new Date().toISOString(),
    };
    if (status) payload.status = status;
    const frame = { type: 'strategic_session', payload };
    // Prefer the typed frame on a live socket; otherwise keep the delta in the
    // buffered agent-log transcript so a later-attaching viewer still sees it.
    if (!sendWsFrame(this.ws, frame)) {
      if (clean) sendLog(this.ws, clean.endsWith('\n') ? clean : `${clean}\n`);
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
      if (item) { this.emit(item.kind, item.text, item.status); this.lastEmitAt = Date.now(); }
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

  /**
   * A LIVE, EPHEMERAL activity one-liner for the operator's Workday activity
   * strip — a Bash command, file edit, read, spawned sub-agent, or tool result.
   * Carried on the same rate-limited queue as text so a burst of tool_use blocks
   * in one assistant message stays under the backend WS-close cap. NEVER
   * persisted (only the assistant's `./messages send` reply lands in the durable
   * transcript, as a ProjectMessage).
   */
  activity(kind: StreamFrameKind, text: string): void {
    this.queue.pushFrame(kind, text);
    this.scheduleDrain();
  }

  /** Mark the START of an operator turn — drives the UI "Workday's on it" state. */
  responding(): void {
    this.queue.pushFrame('status', '[strategic-session] responding', 'responding');
    this.scheduleDrain();
  }

  /** Mark the START of an autonomous turn — drives the UI "Workday: advancing" state. */
  working(): void {
    this.queue.pushFrame('status', '[strategic-session] working', 'working');
    this.scheduleDrain();
  }

  /** Mark the END of any turn — clears the UI activity strip. Ordered after all of
   *  the turn's text/activity frames (the queue drains in order). */
  done(): void {
    this.queue.pushFrame('status', '[strategic-session] done', 'done');
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

  // ── Git read-credential refresh setup ────────────────────────────────────────
  // Write the initial token then switch the remote URL from the baked-in
  // token-embedded form to a plain HTTPS URL backed by a credential helper that
  // reads a refreshed token file. This prevents `git fetch origin` from failing
  // after the 60-minute GitHub App installation token TTL.
  writeGitTokenFile(ghToken);
  try {
    setupGitCredentialHelper(owner, repo, WORKSPACE);
    sendLog(ws, '[resident] git credential helper configured (read-only refresh every 40 min)\n');
  } catch (e) {
    sendLog(ws, `[resident] WARNING: git credential helper setup failed (git fetch may expire): ${String(e)}\n`);
  }

  writePlaybookHelper(taskId);
  writeMessagesHelper(taskId);
  const disallowedTools = configureResidentAgentPermissions(_RESIDENT_STRATEGIC_TASK_TYPE);
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
  const retiredDirections = Array.isArray(payload.retired_directions_summary)
    ? payload.retired_directions_summary as Array<Record<string, unknown>> : [];
  const stagedDirections = Array.isArray(payload.staged_directions)
    ? payload.staged_directions as Array<Record<string, unknown>> : [];
  const previousDecisionOutcomes = payload.previous_decision_outcomes
    && typeof payload.previous_decision_outcomes === 'object'
    ? payload.previous_decision_outcomes as Record<string, unknown>
    : undefined;

  // Read the session summary from the prior session's set-session-summary output
  // (carried forward by the backend on restart). The bootstrap will include it as
  // a "Prior session context" section so the agent knows what was in-flight.
  const priorSessionContextText = strVal(ctx._session_summary) || '';

  // Format strategic memory notes for the bootstrap prompt. Each entry is
  // {note, set_at}; render as "- [set_at] note" so the timestamp provides
  // ordering context. Absent or non-array degrades to empty string.
  const strategicMemoryNotesText = ((): string => {
    const raw = payload.strategic_memory_notes;
    if (!Array.isArray(raw) || raw.length === 0) return '';
    return raw
      .filter((e): e is Record<string, unknown> => e !== null && typeof e === 'object' && !Array.isArray(e))
      .map((e) => {
        const note = typeof e.note === 'string' ? e.note.trim() : '';
        const setAt = typeof e.set_at === 'string' ? e.set_at.trim() : '';
        if (!note) return '';
        return setAt ? `- [${setAt}] ${note}` : `- ${note}`;
      })
      .filter(Boolean)
      .join('\n');
  })();

  const bootstrapPrompt = buildBootstrapPrompt({
    contextPrefix,
    projectName: strVal(payload.project_name, 'this project'),
    problemModelJson,
    runningUpdate: typeof payload.running_update === 'string' ? payload.running_update : '',
    directionsText: buildDirSummaryText(directionsSummary, authoritativeActive),
    stagedDirectionsText: buildStagedDirectionsText(stagedDirections),
    retiredDirectionsText: buildRetiredContextText(retiredDirections),
    previousDecisionOutcomesText: buildPriorDecisionsText(previousDecisionOutcomes),
    strategicMemoryNotesText,
    visionJson,
    openMessagesText: buildOpenMessagesText(openMessages),
    sessionId,
    fetchGuidanceText: buildFetchGuidance(STRATEGIC_FETCH_VERBS),
    priorSessionContextText,
  });

  const cfg = residentConfig(payload);
  let cursor = initialCursor(task, payload);
  // Bounded set of message ids already handled — with `created_at >= since` poll
  // semantics the boundary message re-appears each poll and is dropped here.
  const seen = new BoundedSeenSet();

  // ── Evidence-gated autonomous tick: examination ledger + rotating focus ──────
  // Code-owned cross-tick memory the model cannot be trusted to keep. The catalog
  // enumerates the project's examinable surfaces (directions, diagnostic fetch
  // verbs, messages/knowledge/problem-model/design-doc, and code:<top-level-dir>
  // seeded by scanning the full-clone workspace — project-shape-agnostic). Each
  // tick focuses a rotating slice (never-examined first, then LRU); the parsed
  // TICK-VERDICT records what was examined so an idle claim can never be a global
  // "nothing to do" and can never rest on unexamined surfaces. No quotas exist.
  let surfaceCatalog = buildSurfaceCatalog(payload, listWorkspaceTopDirs(WORKSPACE));
  // Restore the exam ledger from the persisted context (written by the heartbeat
  // note round-trip). A clean restart continues rotation rather than wiping it.
  const restoredLedger = restoreLedger(
    typeof ctx._exam_ledger === 'string' ? ctx._exam_ledger : undefined,
  );
  const examRecords: ExamRecord[] = restoredLedger?.records ?? [];
  let examTick = restoredLedger?.tick ?? 0;
  // Set after an unevidenced idle claim (CLEAR with no tool use, or a missing/
  // garbled marker): the next tick opens with an examine-before-CLEAR correction.
  let correctiveNextTick = restoredLedger?.corrective ?? false;
  // The previous autonomous turn ended AGENDA-CONTINUE → re-tick promptly.
  let previousTurnAgendaContinue = false;

  // ── Compaction state ─────────────────────────────────────────────────────
  // Track cumulative input tokens (latest turn) and the last compaction time
  // so the session detects context-window pressure and compacts before the
  // model's context limit is reached.
  let compactionCount = 0;
  let lastCompactionAt = 0;
  const projectName = strVal(payload.project_name, 'this project');

  /** Build the compaction checkpoint context from current handler state. */
  const buildCheckpointContext = (): CompactionCheckpointContext => {
    const roleIdentity = [
      `You are the resident strategist for ${projectName}.`,
      'You operate at two speeds — interactive (operator chat) and autonomous',
      '(between messages). Your tools: ./strategic, ./fetch, ./design-doc,',
      './messages send. All strategic state you write describes the MANAGED',
      'project — never this execution environment or this prompt.',
    ].join(' ');

    const examLedgerJson = serializeLedger(examRecords, examTick, correctiveNextTick);

    const recentRecords = examRecords.slice(-10);
    const recentInteractions = recentRecords.length > 0
      ? `Recent examination activity (last ${recentRecords.length} ticks):\n${recentRecords.map(r => `- [${r.verdict}] ${r.surfaceKey}: ${r.note}`).join('\n')}`
      : '(no examination records yet)';

    const currentFocus = nextFocus(surfaceCatalog, examRecords, FOCUS_LIST_SIZE);
    const openFound = openFoundLeads(examRecords);
    const autonomousState = [
      `Examination tick: ${examTick}`,
      `Focus rotation: ${currentFocus.map(f => f.key).join(', ') || '(none)'}`,
      openFound.length > 0 ? `Open leads (${openFound.length}): ${openFound.map(f => `${f.surfaceKey}: ${f.note.slice(0, 120)}`).join('; ')}` : '',
      `Corrective next tick: ${correctiveNextTick}`,
      `Previous turn agenda-continue: ${previousTurnAgendaContinue}`,
      `Compaction count: ${compactionCount + 1}`,
    ].filter(Boolean).join('\n');

    return { roleIdentity, examLedgerJson, recentInteractions, autonomousState };
  };
  // An operator turn just completed → run an autonomous turn to act on it.
  let operatorTurnJustCompleted = false;
  // Pacing state for shouldRunAutonomousTurn: 0 = never run (floor fires first).
  let lastAutonomousTurnAt = 0;
  let lastAutonomousWorkStateRev: string | null = null;
  // Latest work_state_rev observed on a heartbeat (undefined until the backend
  // that populates it lands — absence degrades to floor-tick pacing).
  let currentWorkStateRev: string | null = null;
  // Pacing for the "watching" ephemeral status line emitted while gated-idle so
  // deliberate quiet looks different from a dark/crashed session.
  let lastWatchingEmitAt = 0;

  // ── Session + streaming wiring ──────────────────────────────────────────────
  const streamer = new SessionStreamer(ws, sessionId, taskId);
  const onSessionEvent = (ev: ResidentSessionEvent): void => {
    if (ev.kind === 'text' && ev.text) {
      // Assistant reply text — the only content that reaches the durable
      // transcript (once the agent `./messages send`s it). Streams as `message`.
      streamer.delta(ev.text);
    } else if (ev.kind === 'tool' || ev.kind === 'activity') {
      // A tool call / tool result — a LIVE, ephemeral activity one-liner. Prefer
      // the parser's compact `{kind,text}`; fall back to a bare tool label.
      if (ev.activity) {
        streamer.activity(ev.activity.kind as StreamFrameKind, ev.activity.text);
      } else if (ev.kind === 'tool') {
        streamer.tool(ev.toolName, ev.text);
      }
    } else if (ev.kind === 'error' && ev.text) {
      streamer.status(`error: ${ev.text}`);
    }
  };
  let session: ResidentSession = startResidentClaudeSession({
    model, effort, disallowedTools, workspace: WORKSPACE, turnTimeoutMs: cfg.turnTimeoutMs,
  });
  session.onEvent(onSessionEvent);

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

  /**
   * Convert ContentBlock[] to Array<Record<string, unknown>> for the session layer.
   * Validates required fields at runtime so malformed blocks are caught early
   * rather than silently producing garbled NDJSON. Each block is reconstructed
   * as a plain object so TypeScript can widen it past the discriminated union.
   */
  function toRecordBlocks(blocks: ContentBlock[]): Array<Record<string, unknown>> {
    const result: Array<Record<string, unknown>> = [];
    for (const block of blocks) {
      if (block.type === 'text') {
        result.push({ type: 'text' as const, text: block.text });
      } else if (block.type === 'image') {
        result.push({ type: 'image' as const, source: block.source });
      }
      // text_url blocks are resolved by materializeContentBlocks before this point;
      // if one slips through (a bug), skip it silently rather than crashing.
    }
    return result;
  }

  const runTurn = async (content: string | ContentBlock[], operator: boolean): Promise<ResidentTurnResult> => {
    const full: string | ContentBlock[] = typeof content === 'string'
      ? (firstTurnSent ? content : `${bootstrapPrompt}\n\n---\n\n${content}`)
      : (firstTurnSent ? content : [{ type: 'text', text: `${bootstrapPrompt}\n\n---\n\n` }, ...content]);
    firstTurnSent = true;
    statusRef.value = operator ? 'Responding to operator...' : 'Advancing project (autonomous)...';
    // Bracket BOTH operator and autonomous turns with lifecycle frames so the UI
    // can distinguish in-flight activity from deliberate quiet:
    //   - operator turns: responding … done  (drives "Workday's on it" indicator)
    //   - autonomous turns: working … done   (drives "Workday: advancing" treatment)
    // done always clears the ephemeral activity strip.
    if (operator) {
      streamer.responding();
    } else {
      streamer.working();
    }
    // Autonomous turns get a deeper per-turn stall budget so a long read/verify
    // step isn't killed mid-thought; operator turns keep the responsive default.
    const r = await session.sendTurn(
      Array.isArray(full) ? toRecordBlocks(await materializeContentBlocks(full)) : full,
      operator ? undefined : { stallTimeoutMs: cfg.autonomousTurnTimeoutMs });
    streamer.flush();
    streamer.done();
    recordResidentTurnLatency({
      kind: operator ? 'operator' : 'autonomous',
      turnMs: r.turnMs,
      ttftMs: r.ttftMs,
      interrupted: r.interrupted,
      toolCallCount: r.toolCallCount,
    });
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

  // ── Compaction: context-window pressure detection + subprocess restart ──
  // After each turn, check whether the latest input token count exceeds the
  // compaction threshold. If it does (and the cooldown has elapsed), build a
  // condensed checkpoint, close the old Claude subprocess, spawn a fresh one,
  // wire up the same event listeners, and prime it with the checkpoint as an
  // internal turn. The session continues coherently — the exam ledger, message
  // cursor, and operator conversation context survive the restart.
  const maybeCompact = async (r: ResidentTurnResult): Promise<boolean> => {
    const threshold = compactionThreshold(model, cfg.compactionThresholdTokens);
    if (r.inputTokens <= 0 || r.inputTokens < threshold) return false;
    const now = Date.now();
    if (lastCompactionAt > 0 && now - lastCompactionAt < cfg.compactionCooldownMs) {
      sendLog(ws, `[resident] compaction skipped — within cooldown (${Math.round((now - lastCompactionAt) / 1000)}s since last)\n`);
      return false;
    }
    sendLog(ws, `[resident] context-window pressure detected (${r.inputTokens} input tokens >= ${threshold} threshold) — compacting session\n`);
    streamer.status('session compacting — context reset to preserve signal');

    // Build the checkpoint prompt from current handler state.
    const checkpoint = buildCompactionCheckpoint(buildCheckpointContext());

    // Close the old subprocess and spawn a fresh one with the same params.
    session.close();
    lastCompactionAt = now;
    compactionCount += 1;

    const newSession = startResidentClaudeSession({
      model, effort, disallowedTools, workspace: WORKSPACE, turnTimeoutMs: cfg.turnTimeoutMs,
    });
    // Wire the same event listeners so the streamer keeps receiving deltas.
    newSession.onEvent(onSessionEvent);
    session = newSession;

    // Prime the new session with the checkpoint as a standalone internal turn.
    // This delivers the condensed context so the session remembers its role,
    // exam ledger, recent interactions, and autonomous state. It is NOT an
    // operator or autonomous turn — it carries no lifecycle frames.
    streamer.status('priming compacted session with checkpoint');
    try {
      const primeResult = await newSession.sendTurn(checkpoint);
      if (primeResult.processGone) {
        sendLog(ws, '[resident] FATAL: new session died during checkpoint priming\n');
        stopReason = 'compaction_prime_failed';
        return true; // caller must stop the loop
      }
      streamer.status(`session compacted (compaction #${compactionCount}) — context reset to preserve signal`);
      sendLog(ws, `[resident] compaction #${compactionCount} complete — new session primed with checkpoint (${checkpoint.length} chars)\n`);
    } catch (e) {
      sendLog(ws, `[resident] FATAL: checkpoint priming failed: ${String(e)}\n`);
      stopReason = 'compaction_prime_failed';
      return true; // caller must stop the loop
    }
    return false; // session continues
  };
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
        // session_id scopes the poll to THIS session's conversation server-side
        // (messages stamped for another session are excluded; legacy/cadence
        // NULL-session rows stay visible). Omitted-value degrades to project-wide.
        const sessionScope = sessionId ? `&session_id=${encodeURIComponent(sessionId)}` : '';
        const q = `since=${encodeURIComponent(cursor)}&from_type=user&status=open&limit=${MESSAGE_POLL_LIMIT}${sessionScope}`;
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
  // handledIds: message ids consumed by the resident turn — marked handled on the
  // backend so they stop counting as open backlog and aren't re-delivered on restart.
  const heartbeat = async (
    progress: boolean,
    handledIds?: string[],
  ): Promise<SessionHeartbeatResponse | null> => {
    // A heartbeat is proof the loop is alive — reset the warm session's idle
    // ceiling so a chat-only / monitor session (which issues no turns to reset
    // it) never self-terminates while the loop keeps polling.
    session.touch();
    // Serialize the current exam ledger into the note so the backend persists it;
    // a restart will restore it from task.context._exam_ledger.
    const note = serializeLedger(examRecords, examTick, correctiveNextTick);
    const body: Record<string, unknown> = {
      session_id: sessionId, task_id: taskId, progress, note,
      // Send the current cursor so the backend persists it; a restart will
      // restore it from task.context.message_cursor instead of falling back to
      // the 2-minute window that re-delivers already-answered messages.
      message_cursor: cursor,
      // Report compaction metadata so the backend persists it in the session
      // blob — surfaced in the snapshot for operator visibility.
      compaction_count: compactionCount,
      last_compaction_at: lastCompactionAt > 0 ? new Date(lastCompactionAt).toISOString() : null,
    };
    if (handledIds && handledIds.length > 0) {
      body.handled_message_ids = handledIds;
    }
    const latencySummary = getResidentTurnLatencyTelemetry();
    if (latencySummary !== null) {
      body.turn_latency_summary = latencySummary;
    }
    try {
      const resp = await apiPost(`/api/agent/projects/${projectId}/strategic-session/heartbeat`, body) as SessionHeartbeatResponse;
      // Fold the lease state so the loop's autonomous gate tracks the backend.
      authorityHeld = nextAuthorityHeld(authorityHeld, resp);
      // Track the work-state revision (when the backend ships it) so state-gated
      // pacing can fire on a real world change; absence leaves it null (floor tick).
      if (typeof resp.work_state_rev === 'string') currentWorkStateRev = resp.work_state_rev;
      // Refresh the surface catalog from the backend's live direction list so
      // newly created or retired directions enter/leave the rotation mid-session.
      if (Array.isArray(resp.active_direction_ids)) {
        surfaceCatalog = refreshSurfaceCatalog(surfaceCatalog, resp.active_direction_ids, payload);
      }
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

  // Liveness-heartbeat pacing. Operator messages are polled at the fast
  // cfg.messagePollMs cadence (prompt pickup), but a bare liveness heartbeat
  // (progress=false) fires only every cfg.idleTickMs so the faster poll does not
  // hammer the backend. Progress heartbeats (operator interaction / applied op)
  // are always sent directly and are unaffected by this gate.
  let lastLivenessHeartbeatAt = 0;
  const livenessHeartbeatDue = (): boolean =>
    Date.now() - lastLivenessHeartbeatAt >= cfg.idleTickMs;
  const livenessHeartbeat = async (): Promise<SessionHeartbeatResponse | null> => {
    const hb = await heartbeat(false);
    lastLivenessHeartbeatAt = Date.now();
    return hb;
  };

  // Poll + heartbeat while an autonomous turn runs; resolves the moment the turn
  // finishes, a stop is requested, or an operator message arrives (→ interrupt).
  // Returns a discriminated outcome so the loop needs no async-mutated flags.
  // Ticks at the fast message-poll cadence so an operator message pre-empts the
  // autonomous turn promptly; the liveness heartbeat inside stays paced to
  // cfg.idleTickMs.
  const watchTurn = async (turnPromise: Promise<ResidentTurnResult>): Promise<WatchOutcome> => {
    const doneSignal: Promise<typeof TURN_DONE> = turnPromise.then(() => TURN_DONE);
    for (;;) {
      const raced = await Promise.race([doneSignal, sleep(cfg.messagePollMs).then(() => 'tick' as const)]);
      if (raced === TURN_DONE) return { kind: 'done' };
      if (livenessHeartbeatDue()) {
        const hb = await livenessHeartbeat(); // liveness only while working
        if (hb?.stop_requested) return { kind: 'stop' };
        // Authority lapsed mid-turn (backend unattended cap): suspend the
        // autonomous path — pre-empt this turn and go passive rather than let it
        // finish mutating strategic state on a lapsed lease.
        if (!authorityHeld) return { kind: 'suspend' };
      }
      const newMsgs = await pollMessages();
      if (newMsgs.length > 0) return { kind: 'message', messages: newMsgs };
    }
  };

  // ── Git token background refresh (push-capable) ──────────────────────────────
  // Periodically calls /api/agent/github/token and updates the credential
  // helper's token file so `git fetch origin` and `git push` keep working
  // beyond the 60-minute GitHub App installation token TTL. Uses the
  // push-capable endpoint (not the read-only /api/agent/git-read-token) so
  // in-session writes (strategic ops that push or use the credential) do not
  // get a stale read-only token after the first 8-minute refresh cycle.
  // Self-scheduling named function expression so the timer reference is always
  // non-null and cleared cleanly in the finally.
  let gitTokenRefreshTimer: ReturnType<typeof setTimeout>;
  function gitReadTokenRefreshTick(): void {
    void (async () => {
      try {
        const result = await apiGet(`/api/agent/github/token?task_id=${encodeURIComponent(taskId)}&force_refresh=true`) as { data?: { token?: string } } | null;
        const newToken = result?.data?.token;
        if (newToken) {
          writeGitTokenFile(newToken);
          sendLog(ws, '[resident] git token refreshed (push-capable)\n');
        } else {
          sendLog(ws, '[resident] git token refresh: empty response (keeping existing token)\n');
        }
      } catch (e) {
        sendLog(ws, `[resident] git token refresh failed (keeping existing token): ${String(e)}\n`);
      }
      gitTokenRefreshTimer = setTimeout(gitReadTokenRefreshTick, GIT_TOKEN_REFRESH_MS);
    })();
  }
  gitTokenRefreshTimer = setTimeout(gitReadTokenRefreshTick, GIT_TOKEN_REFRESH_MS);

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
        // Check context-window pressure after the turn: compact if needed.
        if (await maybeCompact(r)) break;
        // Collect the ids of the messages we just consumed so the backend can
        // mark them handled — they stop counting as open backlog and won't be
        // re-delivered to a restarted session.
        const consumedIds = msgs.map(m => m.id).filter((id): id is string => typeof id === 'string' && id.length > 0);
        const hb = await heartbeat(true, consumedIds); // operator interaction always renews the lease
        if (hb?.stop_requested) break;
        if (r.processGone) { stopReason = 'session_process_exited'; break; }
        // A just-finished operator turn arms one autonomous follow-through tick.
        operatorTurnJustCompleted = true;
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
        // Poll operator messages at the fast cadence (the fast path at the top of
        // the loop does the actual pickup); heartbeat only when the paced liveness
        // interval is due so passive mode doesn't hammer the backend.
        if (livenessHeartbeatDue()) {
          const hb = await livenessHeartbeat();
          if (hb?.stop_requested) break;
        }
        await sleep(cfg.messagePollMs);
        continue;
      }

      // 2. Autonomous path: state-gated so ticks fire on real signals rather than
      // loop speed. Run when the world moved (heartbeat work_state_rev differs
      // from the rev at the last turn), the agent declared more work
      // (AGENDA-CONTINUE), an operator turn just completed, or the floor tick
      // elapsed. Otherwise wait like passive mode — no self-prompt against
      // unchanged state.
      if (!shouldRunAutonomousTurn({
        currentWorkStateRev,
        lastTurnWorkStateRev: lastAutonomousWorkStateRev,
        previousTurnAgendaContinue,
        operatorTurnJustCompleted,
        msSinceLastAutonomousTurn: lastAutonomousTurnAt === 0
          ? Number.POSITIVE_INFINITY : Date.now() - lastAutonomousTurnAt,
        floorTickMs: cfg.minTickMs,
      })) {
        if (livenessHeartbeatDue()) {
          const hb = await livenessHeartbeat();
          if (hb?.stop_requested) break;
          // Emit an ephemeral "watching" line at the liveness-heartbeat cadence so
          // deliberate gated-idle is visibly different from a dark/crashed session.
          const msSinceWatching = Date.now() - lastWatchingEmitAt;
          if (msSinceWatching >= cfg.idleTickMs * 2) {
            const rawInFlight = hb?.work_state_summary?.in_flight;
            const inFlight = typeof rawInFlight === 'number' ? rawInFlight : null;
            const msUntilFloor = lastAutonomousTurnAt === 0
              ? 0
              : Math.max(0, cfg.minTickMs - (Date.now() - lastAutonomousTurnAt));
            const nextReview = msUntilFloor < 60_000
              ? 'soon'
              : `~${Math.round(msUntilFloor / 60_000)}min`;
            const taskNote = inFlight !== null ? `${inFlight} task${inFlight === 1 ? '' : 's'} in flight; ` : '';
            streamer.status(`watching — ${taskNote}next review ${nextReview} or on state change`);
            lastWatchingEmitAt = Date.now();
          }
        }
        await sleep(cfg.messagePollMs);
        continue;
      }

      // Build the dynamic, ledger-driven self-prompt: rotating focus
      // (never-examined first, then LRU), recent records so the turn goes
      // somewhere new/deeper, open FOUND leads to restate, and a corrective flag
      // set after an unevidenced idle claim.
      examTick += 1;
      operatorTurnJustCompleted = false;
      const focus = nextFocus(surfaceCatalog, examRecords, FOCUS_LIST_SIZE);
      const recentRecords = examRecords.slice(-6);
      const openFound = openFoundLeads(examRecords);
      const corrective = correctiveNextTick;
      lastAutonomousTurnAt = Date.now();
      const turnPromise = runTurn(buildAutonomousPrompt(focus, recentRecords, openFound, corrective, cfg.maxOpsPerTurn), false);
      const watch = await watchTurn(turnPromise);
      if (watch.kind !== 'done') session.interrupt();

      // Await the (possibly interrupted) autonomous turn, then renew the lease
      // ONLY if a strategic op was applied (progress) AND we still hold authority
      // — never renew a lease that lapsed mid-turn (watch === 'suspend'), and
      // never renew on bare examination (looking never renews freshness — the
      // dead-man's switch stays progress-gated).
      const r = await turnPromise;
      if (turnWedgedSession(r)) break;
      // Check context-window pressure after the turn: compact if needed.
      if (await maybeCompact(r)) break;
      const hb = await heartbeat(r.appliedStrategicOp && authorityHeld);
      // Record the pacing baseline AFTER the post-turn heartbeat so the loop's own
      // writes are already folded into the rev we compare against next iteration.
      // Recording it pre-turn would leave lastAutonomousWorkStateRev behind the
      // heartbeat's updated currentWorkStateRev, causing the gate to fire again on
      // the loop's own applied ops (self-echo).
      lastAutonomousWorkStateRev = currentWorkStateRev;
      if (hb?.stop_requested || watch.kind === 'stop') break;
      if (r.processGone) { stopReason = 'session_process_exited'; break; }

      // Update the examination ledger from the turn's TICK-VERDICT. Interrupted
      // turns (operator preemption via watch.kind === 'message', authority suspend
      // via watch.kind === 'suspend', or stall watchdog) never emit a TICK-VERDICT
      // marker; classifyTickOutcome records them as `interrupted` (non-corrective)
      // so the ledger captures the event without raising a false violation.
      const upd = classifyTickOutcome({
        resultText: r.resultText,
        sawToolUse: r.sawToolUse,
        focus,
        atTick: examTick,
        at: new Date().toISOString(),
        interrupted: r.interrupted,
      });
      examRecords.push(upd.record);
      if (examRecords.length > EXAM_RECORDS_CAP) {
        examRecords.splice(0, examRecords.length - EXAM_RECORDS_CAP);
      }
      correctiveNextTick = upd.corrective;
      previousTurnAgendaContinue = upd.agendaContinue;
      if (upd.statusNote) streamer.status(upd.statusNote);

      // Authority lapsed mid-turn ⇒ fall through to passive mode next iteration.
      if (watch.kind === 'suspend') continue;

      // Handle any operator message that interrupted the autonomous turn.
      if (watch.kind === 'message') {
        streamer.status('operator message received (interrupted autonomous work)');
        const or = await runTurn(combineOperatorMessages(watch.messages), true);
        if (turnWedgedSession(or)) break;
        await persistReply(or);
        // Check context-window pressure after the turn: compact if needed.
        if (await maybeCompact(or)) break;
        // Extract the ids of the messages we just consumed so the backend can mark
        // them handled — same as the fast-path operator-message pre-emption above.
        const consumedIds = watch.messages.map(m => m.id).filter((id): id is string => typeof id === 'string' && id.length > 0);
        const hb2 = await heartbeat(true, consumedIds);
        if (hb2?.stop_requested) break;
        if (or.processGone) { stopReason = 'session_process_exited'; break; }
        operatorTurnJustCompleted = true;
        continue;
      }

      // Non-advancing turn (no strategic op applied) → pace so we don't hot-loop
      // the API, at the fast message-poll cadence so a just-arrived operator
      // message is still picked up promptly. Extended from the old zero-tool
      // condition to ALL non-advancing turns: an examination tick uses tools but
      // applies no op and must still pace.
      if (!r.appliedStrategicOp) {
        await sleep(cfg.messagePollMs);
      }
    }
  } catch (e) {
    stopReason = `loop_error: ${String(e)}`;
    sendLog(ws, `[resident] loop error: ${String(e)}\n`);
  } finally {
    clearTimeout(gitTokenRefreshTimer);
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
