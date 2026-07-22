import fs from 'fs';
import { createHash } from 'crypto';
import type WebSocket from 'ws';
import {
  WORKSPACE, strVal, sanitize, sendLog, sendWsFrame,
  apiGet, apiPost, persistAndPostStatus, resolveBaseBranch,
  recordResidentTurnLatency, getResidentTurnLatencyTelemetry,
  recordOperatorDecisionCapture, getOperatorDecisionCaptureTelemetry,
} from '../index.js';
import { restoreOperatorDecisionCaptureTelemetry } from '../telemetry.js';
import { prepareWorkspaceForRole, listWorkspaceTopDirs } from '../workspacePrep.js';
import {
  buildContextPrefix, configureResidentAgentPermissions,
  writePlaybookHelper, writeMessagesHelper, writeSkillHelpers,
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
  formatRecentHandledConversation,
} from './strategic-sections.js';
import {
  residentConfig, initialCursor, advanceCursorFromMessages,
  BoundedSeenSet, admitMessageBatchPage, combineOperatorMessages,
  materializeContentBlocks, buildBootstrapPrompt, buildAutonomousPrompt, resolveTerminalStatus,
  nextAuthorityHeld, autonomousActive, FrameCoalescer,
  MESSAGE_POLL_LIMIT, MAX_POLL_PAGES, OPERATOR_MESSAGE_BATCH_CAP,
  buildPriorDecisionsText, buildRetiredContextText, buildStagedDirectionsText,
  buildSurfaceCatalog, nextFocus, classifyTickOutcome, openFoundLeads, parseTickVerdict,
  classifyOperatorDecisionReview, mergePendingOperatorDecisionReview,
  retainPendingOperatorDecisionReview, advancePendingOperatorDecisionReview, MAX_REVIEW_ATTEMPTS,
  shouldRunAutonomousTurn, EXAM_RECORDS_CAP, FOCUS_LIST_SIZE,
  serializeLedger, restoreLedger, refreshSurfaceCatalog,
  compactionThreshold, formatDirectionGroundingText, buildCompactionCheckpoint,
  decisionsRegisterText, DECISIONS_REGISTER_CAP, isSettledDecisionVerdict, admitDecisionEntry,
  serializeDecisionsReg, restoreDecisionsReg, parseOperatorDecision, OPERATOR_DECISION_VERDICT,
  formatStrategicMemoryNotes, formatCheckpointState, resolveCheckpointGrounding,
  type MessagesPollResponse, type SessionHeartbeatResponse, type ResidentInboundMessage,
  type StreamFrameKind, type StreamFrameStatus, type ExamRecord, type ContentBlock,
  type CompactionCheckpointContext, type DecisionsRegisterEntry,
  type PendingOperatorDecisionReview,
  type CheckpointGrounding, type CheckpointStateResponse,
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
const OPERATOR_PREEMPT_DEADLINE_MS = 10_000;
const RESIDENT_REPLY_KEY_PATH = `${WORKSPACE}/.resident-reply-idempotency-key`;
const RESIDENT_REPLY_RECEIPT_PATH = `${WORKSPACE}/.resident-reply-receipt.json`;
const RESIDENT_STAGED_REPLY_PATH = `${WORKSPACE}/.resident-reply-staged-message.json`;

export interface ResidentReplyReceipt {
  idempotency_key: string;
  covered_message_ids: string[];
}

export interface ResidentStagedReply {
  type: string;
  content: string;
  idempotency_key: string;
  covered_message_ids: string[];
  context?: Record<string, unknown>;
}

/** Read and authenticate the helper's staged intent against this immutable turn receipt. */
export function readResidentStagedReplyAtPath(
  stagedReplyPath: string,
  receipt: ResidentReplyReceipt,
): ResidentStagedReply | null {
  const maxBytes = 65_536;
  let fd: number | null = null;
  let raw: string;
  try {
    const before = fs.lstatSync(stagedReplyPath);
    if (!before.isFile()) throw new Error('staged Resident reply is not a regular file');
    const noFollow = fs.constants.O_NOFOLLOW ?? 0;
    fd = fs.openSync(stagedReplyPath, fs.constants.O_RDONLY | noFollow);
    const opened = fs.fstatSync(fd);
    if (!opened.isFile() || opened.dev !== before.dev || opened.ino !== before.ino) {
      throw new Error('staged Resident reply changed identity during open');
    }
    if (opened.size > maxBytes) throw new Error('staged Resident reply exceeds 64KiB');
    const buffer = Buffer.alloc(opened.size + 1);
    let bytesRead = 0;
    while (bytesRead < buffer.length) {
      const count = fs.readSync(fd, buffer, bytesRead, buffer.length - bytesRead, null);
      if (count === 0) break;
      bytesRead += count;
    }
    const after = fs.fstatSync(fd);
    if (bytesRead !== opened.size || after.size !== opened.size) {
      throw new Error('staged Resident reply changed size during read');
    }
    raw = buffer.subarray(0, bytesRead).toString('utf8');
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code === 'ENOENT') return null;
    throw e;
  } finally {
    if (fd !== null) fs.closeSync(fd);
  }
  const parsed = JSON.parse(raw) as Partial<ResidentStagedReply>;
  const expectedIds = [...receipt.covered_message_ids].sort();
  const rawActualIds = parsed.covered_message_ids;
  const actualIds = Array.isArray(rawActualIds)
    && rawActualIds.every((value): value is string => typeof value === 'string' && Boolean(value))
    ? [...rawActualIds].sort()
    : null;
  if (
    typeof parsed.type !== 'string' || !parsed.type.trim()
    || typeof parsed.content !== 'string' || !parsed.content.trim()
    || parsed.idempotency_key !== receipt.idempotency_key
    || actualIds === null
    || actualIds.length !== expectedIds.length
    || actualIds.some((value, index) => value !== expectedIds[index])
    || (parsed.context !== undefined && (
      parsed.context === null || typeof parsed.context !== 'object' || Array.isArray(parsed.context)
    ))
  ) {
    throw new Error('staged Resident reply does not match the active turn receipt');
  }
  return {
    type: parsed.type.trim(),
    content: parsed.content.trim(),
    idempotency_key: parsed.idempotency_key,
    covered_message_ids: [...receipt.covered_message_ids],
    ...(parsed.context === undefined ? {} : { context: parsed.context }),
  };
}

/**
 * Remove the fixed reply-key path without following it. A previous resident
 * process can die after installing the key but before its per-turn finally
 * removes it, so every new process must clear that stale capability before it
 * starts the model. unlinkSync removes a symlink itself rather than touching
 * its target; errors other than an absent path are fail-closed.
 */
export function removeResidentReplyKeyAtPath(replyKeyPath: string): void {
  try {
    fs.unlinkSync(replyKeyPath);
  } catch (e) {
    if ((e as NodeJS.ErrnoException).code !== 'ENOENT') throw e;
  }
}

/**
 * Install one operator turn's stable reply key safely. Removing the fixed
 * pathname first handles stale regular files and symlinks. Exclusive create
 * then closes the unlink/create race: if anything recreates the pathname, the
 * open fails instead of following or overwriting it and the operator turn does
 * not start.
 */
export function installResidentReplyKeyAtPath(replyKeyPath: string, key: string): void {
  removeResidentReplyKeyAtPath(replyKeyPath);
  fs.writeFileSync(replyKeyPath, key, {
    encoding: 'utf8',
    flag: 'wx',
    mode: 0o600,
  });
}

/** Remove both ambient reply capabilities, attempting both paths fail-closed. */
export function removeResidentReplyArtifactsAtPaths(
  replyKeyPath: string,
  replyReceiptPath: string,
): void {
  let firstError: unknown = null;
  for (const artifactPath of [replyKeyPath, replyReceiptPath]) {
    try {
      removeResidentReplyKeyAtPath(artifactPath);
    } catch (e) {
      firstError ??= e;
    }
  }
  if (firstError !== null) throw firstError;
}

/** Install the immutable per-turn key and covered-message receipt before model work. */
export function installResidentReplyReceiptAtPaths(
  replyKeyPath: string,
  replyReceiptPath: string,
  receipt: ResidentReplyReceipt,
): void {
  removeResidentReplyArtifactsAtPaths(replyKeyPath, replyReceiptPath);
  try {
    fs.writeFileSync(replyReceiptPath, JSON.stringify(receipt), {
      encoding: 'utf8', flag: 'wx', mode: 0o600,
    });
    fs.writeFileSync(replyKeyPath, receipt.idempotency_key, {
      encoding: 'utf8', flag: 'wx', mode: 0o600,
    });
  } catch (e) {
    try { removeResidentReplyArtifactsAtPaths(replyKeyPath, replyReceiptPath); } catch { /* preserve setup error */ }
    throw e;
  }
}

/** Stable identity for one logical operator reply across retry batches. */
export function oldestStableInboundMessageId(
  messages: ResidentInboundMessage[],
): string | null {
  let chosen: { id: string; createdAt: number; index: number } | null = null;
  for (const [index, message] of messages.entries()) {
    const id = typeof message.id === 'string' ? message.id.trim() : '';
    if (!id) continue;
    const parsed = typeof message.created_at === 'string'
      ? new Date(message.created_at).getTime()
      : Number.NaN;
    const createdAt = Number.isFinite(parsed) ? parsed : Number.POSITIVE_INFINITY;
    if (
      chosen === null
      || createdAt < chosen.createdAt
      || (createdAt === chosen.createdAt && index < chosen.index)
    ) {
      chosen = { id, createdAt, index };
    }
  }
  return chosen?.id ?? null;
}


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
/**
 * Refresh the resident's ambient git credential with a READ-SCOPED token.
 *
 * SECURITY (audit finding #10). The resident is the longest-lived process in the
 * fleet, so whatever token sits in its credential file is AMBIENT — reachable by
 * anything the strategist runs, for as long as the session lives. This refresh
 * therefore mints from `/api/agent/git-read-token` (a `contents:read` installation
 * token) and NOT from the push-capable `/api/agent/github/token`. Refreshing from
 * the latter kept a standing, freshly-minted PUSH-capable installation token in
 * that file every 8 minutes for the whole session — a standing privilege
 * escalation.
 *
 * The resident does not need push: it only runs `git fetch origin` (automain
 * verification). Push credentials are per-task and short-lived by design. A
 * strategic op that genuinely needs to push must mint a push-capable token AT THE
 * MOMENT OF USE and must not park it in this ambient file.
 *
 * Exported so the read-scoping is a directly testable property rather than an
 * incidental detail of a background timer.
 *
 * Never throws: a failed refresh keeps the existing token (the credential is
 * still valid until the 60-minute installation-token TTL expires).
 */
export async function refreshResidentGitReadToken(ws: WebSocket | null): Promise<void> {
  try {
    const result = await apiGet('/api/agent/git-read-token') as { token?: string } | null;
    if (result?.token) {
      writeGitTokenFile(result.token);
      sendLog(ws, '[resident] git read token refreshed (read-scoped)\n');
    } else {
      sendLog(ws, '[resident] git read token refresh: empty response (keeping existing token)\n');
    }
  } catch (e) {
    sendLog(ws, `[resident] git read token refresh failed (keeping existing token): ${String(e)}\n`);
  }
}

/**
 * Re-clone the resident workspace after it vanished mid-session (OOM-kill, /tmp
 * cleaner), restoring it to EXACTLY the state the initial setup leaves it in.
 *
 * SECURITY + CORRECTNESS (audit finding, this PR). The guard used to re-clone with
 * the `cloneUrl` / `ghToken` captured at session START, which is wrong twice over:
 *
 *  * STALE. A GitHub App installation token lives 60 minutes; the resident is a
 *    multi-hour "workday". Any re-clone after the first hour authenticated with an
 *    EXPIRED token — the clone throws, the throw escapes the turn loop, and the
 *    whole resident session dies. The guard was least likely to work exactly when
 *    it was most needed (deep into a long session).
 *  * WRONG-SCOPED. `ghToken` is the PUSH-capable per-task token. Re-cloning with it
 *    re-embedded a push-capable credential in the workspace remote and re-wrote it
 *    into the ambient credential file — silently undoing the read-scoping the
 *    8-minute background refresh exists to maintain (the resident never pushes).
 *    The re-clone also dropped the credential helper, so the remote reverted to the
 *    token-embedded form and stopped consulting the refreshed token file at all —
 *    re-breaking `git fetch` at the next TTL boundary.
 *
 * So: mint a FRESH READ-SCOPED token at the moment of use, clone with it, then
 * re-establish the credential helper (plain HTTPS remote + refreshable token file)
 * exactly as `handleStrategicResident` does at startup. Consistent with the refresh
 * loop, and correct at any point in the session.
 *
 * Throws when no read token can be minted — a re-clone with no valid credential
 * cannot succeed anyway, and failing loudly beats silently re-parking a
 * push-capable token in the ambient file.
 */
export async function recloneResidentWorkspace(
  ws: WebSocket | null, owner: string, repo: string, baseBranch: string,
): Promise<void> {
  const result = await apiGet('/api/agent/git-read-token') as { token?: string } | null;
  const token = result?.token;
  if (!token) {
    throw new Error(
      'resident workspace re-clone failed: /api/agent/git-read-token returned no token',
    );
  }
  sendLog(ws, '[resident] re-clone: minted a fresh read-scoped git token\n');
  await prepareWorkspaceForRole({
    workspace: WORKSPACE,
    repoUrl: `https://x-access-token:${token}@github.com/${owner}/${repo}.git`,
    githubToken: token,
    branch: baseBranch, baseBranch, depth: 0,
  });
  // Restore the refreshable credential: plain HTTPS remote + helper reading the
  // token file. Without this the re-cloned workspace keeps a token-embedded remote
  // that the background refresh cannot update, so fetches break at the next TTL.
  writeGitTokenFile(token);
  setupGitCredentialHelper(owner, repo, WORKSPACE);
}

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

  // A hard process/VM exit bypasses the operator turn's finally block. Clear
  // any reply key it left behind before starting a new model process; otherwise
  // the first autonomous turn could invoke `./messages send` with another
  // turn's key. Any cleanup error is fatal so model work never begins with an
  // ambiguous ambient reply capability.
  try {
    removeResidentReplyArtifactsAtPaths(
      RESIDENT_REPLY_KEY_PATH,
      RESIDENT_REPLY_RECEIPT_PATH,
    );
    removeResidentReplyKeyAtPath(RESIDENT_STAGED_REPLY_PATH);
  } catch (e) {
    sendLog(ws, `[resident] stale idempotency key cleanup failed; refusing to start: ${String(e)}\n`);
    throw e;
  }

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
  writeSkillHelpers(task.skills as Record<string, unknown>[] | undefined);
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
  const workerGenerationRaw = Number(ctx._resident_worker_generation);
  const workerGeneration = Number.isSafeInteger(workerGenerationRaw) && workerGenerationRaw > 0
    ? workerGenerationRaw : 1;

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
  // Recently-answered operator conversation tail (status handled/resolved).
  // Read-only, non-actionable — surfaced so a marker-less operator correction
  // survives a full session restart. Absent on older backends → [] (no section).
  const recentHandledConversation = Array.isArray(payload.recent_handled_conversation)
    ? payload.recent_handled_conversation as Array<Record<string, unknown>> : [];
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
  // ordering context. Absent or non-array degrades to empty string. Shared with
  // the compaction-refresh path so both format identically.
  const strategicMemoryNotesText = formatStrategicMemoryNotes(payload.strategic_memory_notes);

  // Format project skills for the bootstrap context.
  const skillsText = ((): string => {
    const raw = task.skills;
    if (!Array.isArray(raw) || raw.length === 0) return '';
    return raw
      .filter((e): e is Record<string, unknown> => e !== null && typeof e === 'object' && !Array.isArray(e))
      .map((e) => {
        const slug = strVal(e.slug);
        const title = strVal(e.title) || slug;
        const command = strVal(e.command);
        const description = strVal(e.description).trim();
        const parts = [`- **${title}** (\`skill-${slug}\`) — \`${command}\``];
        if (description) parts.push(`  ${description}`);
        return parts.join('\n');
      })
      .filter(Boolean)
      .join('\n');
  })();

  // Format the project's built-in tool content (notes markdown, whiteboard
  // summary) for the bootstrap context. Mirrors skillsText: reads the
  // best-effort `project_tools` array the backend claim injects (each entry is
  // {slug, title, kind, text}); empty/absent → '' so the section is a no-op.
  const toolContentText = ((): string => {
    const raw = task.project_tools;
    if (!Array.isArray(raw) || raw.length === 0) return '';
    return raw
      .filter((e): e is Record<string, unknown> => e !== null && typeof e === 'object' && !Array.isArray(e))
      .map((e) => {
        const slug = strVal(e.slug);
        const title = strVal(e.title) || slug;
        const kind = strVal(e.kind);
        const text = strVal(e.text).trim();
        if (!text) return '';
        const heading = `### ${title}${kind ? ` (${kind})` : ''}`;
        return `${heading}\n${text}`;
      })
      .filter(Boolean)
      .join('\n\n');
  })();

  // ── Durable decisions register ──────────────────────────────────────────
  // Full-fidelity cross-tick memory of settled decisions, preserved across
  // compaction AND full session restarts (VM recycles, process crashes) so
  // the strategist stops re-deriving settled facts and re-proposing
  // corrections tick over tick. Restored from task.context._decisions_reg
  // (written by the heartbeat round-trip, same pattern as the exam ledger).
  // Restored HERE — above the bootstrap assembly — so the register can be
  // rendered into the bootstrap prompt: on a full restart the first turn is
  // usually a live operator reply, and it must not be generated blind to
  // settled decisions (compaction restart already carries it via the checkpoint).
  const decisionsReg: DecisionsRegisterEntry[] = restoreDecisionsReg(
    typeof ctx._decisions_reg === 'string' ? ctx._decisions_reg : undefined,
  ) ?? [];

  // Static session-start grounding texts. Hoisted to consts so the SAME value
  // feeds both the bootstrap prompt and the compaction checkpoint (see
  // buildCheckpointContext) — the checkpoint would otherwise drop these three
  // sections, letting a post-compaction session re-derive settled facts or
  // re-propose already-retired/staged directions.
  const stagedDirectionsText = buildStagedDirectionsText(stagedDirections);
  const retiredDirectionsText = buildRetiredContextText(retiredDirections);
  const previousDecisionOutcomesText = buildPriorDecisionsText(previousDecisionOutcomes);

  const bootstrapPrompt = buildBootstrapPrompt({
    contextPrefix,
    projectName: strVal(payload.project_name, 'this project'),
    problemModelJson,
    runningUpdate: typeof payload.running_update === 'string' ? payload.running_update : '',
    directionsText: buildDirSummaryText(directionsSummary, authoritativeActive),
    stagedDirectionsText,
    retiredDirectionsText,
    previousDecisionOutcomesText,
    strategicMemoryNotesText,
    visionJson,
    openMessagesText: buildOpenMessagesText(openMessages),
    recentHandledConversationText: formatRecentHandledConversation(recentHandledConversation),
    sessionId,
    fetchGuidanceText: buildFetchGuidance(STRATEGIC_FETCH_VERBS),
    priorSessionContextText,
    skillsText,
    toolContentText,
    decisionsRegisterText: decisionsRegisterText(decisionsReg, DECISIONS_REGISTER_CAP),
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
  // Markerless replies create a durable review; legacy boolean blobs restore
  // into the same state via restoreLedger.
  let operatorDecisionReview: PendingOperatorDecisionReview | null =
    restoredLedger?.operatorDecisionReview ?? null;
  restoreOperatorDecisionCaptureTelemetry(restoredLedger?.operatorDecisionCapture);
  // The previous autonomous turn ended AGENDA-CONTINUE → re-tick promptly.
  let previousTurnAgendaContinue = false;

  // ── Compaction state ─────────────────────────────────────────────────────
  // Track cumulative input tokens (latest turn) and the last compaction time
  // so the session detects context-window pressure and compacts before the
  // model's context limit is reached.
  let compactionCount = 0;
  let lastCompactionAt = 0;
  const projectName = strVal(payload.project_name, 'this project');

  // Refreshable direction grounding from the heartbeat. Initialized as null
  // (degrading to the bootstrap directionsSummary) and updated on every
  // heartbeat that returns direction_grounding; the compaction checkpoint
  // uses this so it shows live directions rather than session-start snapshots.
  let refreshedDirectionGrounding: Array<{id: string; text: string; status: string}> | null = null;

  // Refreshable self-model grounding (problem model, vision, memory notes,
  // running update) fetched from the checkpoint-state endpoint at compaction
  // time. Initialized null (degrading to the session-start snapshots) and set
  // just before a compaction checkpoint is built so the checkpoint carries
  // current state rather than stale bootstrap data. Mirrors the direction
  // refresh above. See maybeCompact / buildCheckpointContext.
  let refreshedGrounding: CheckpointGrounding | null = null;

  /** Build the compaction checkpoint context from current handler state. */
  const buildCheckpointContext = (): CompactionCheckpointContext => {
    const roleIdentity = [
      `You are the resident strategist for ${projectName}.`,
      'You operate at two speeds — interactive (operator chat) and autonomous',
      '(between messages). Your tools: ./strategic, ./fetch, ./design-doc,',
      './messages send. All strategic state you write describes the MANAGED',
      'project — never this execution environment or this prompt.',
    ].join(' ');

    const examLedgerJson = serializeLedger(
      examRecords,
      examTick,
      correctiveNextTick,
      operatorDecisionReview !== null,
      operatorDecisionReview,
      getOperatorDecisionCaptureTelemetry(),
    );

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
      operatorDecisionReview
        ? `Operator-decision review pending: ${1 + (operatorDecisionReview.queuedExchanges?.length ?? 0)} explicit exchange(s)${operatorDecisionReview.overflowed ? ' plus unresolved overflow' : ''}; current has ${operatorDecisionReview.messageIds.length} message id(s), ${operatorDecisionReview.attempts} attempt(s)`
        : '',
      `Previous turn agenda-continue: ${previousTurnAgendaContinue}`,
      `Compaction count: ${compactionCount + 1}`,
    ].filter(Boolean).join('\n');

    // Durable self-model grounding — carried from the bootstrap across compaction
    // so the resumed session does NOT re-derive settled facts, re-read settled
    // directions, or re-propose corrections the operator already made. Prefer
    // grounding refreshed at compaction time (refreshedGrounding, fetched from
    // the checkpoint-state endpoint) over the session-start snapshot.
    // Pass the bootstrap open messages text in the snapshot so
    // resolveCheckpointGrounding can fall back to it when the checkpoint-state
    // fetch fails, but let a refreshed empty feed WIN (operator resolved all
    // messages mid-session) rather than reverting to the stale bootstrap text.
    const grounding = resolveCheckpointGrounding(
      {
        problemModelJson,
        visionJson,
        strategicMemoryNotesText,
        runningUpdate: strVal(payload.running_update),
        openMessagesText: buildOpenMessagesText(openMessages),
        recentHandledConversationText: formatRecentHandledConversation(recentHandledConversation),
      },
      refreshedGrounding,
    );
    return {
      roleIdentity, examLedgerJson, recentInteractions, autonomousState,
      problemModelJson: grounding.problemModelJson,
      visionJson: grounding.visionJson,
      strategicMemoryNotesText: grounding.strategicMemoryNotesText,
      openMessagesText: grounding.openMessagesText,
      recentHandledConversationText: grounding.recentHandledConversationText,
      priorSessionContextText: priorSessionContextText || undefined,
      directionsText: refreshedDirectionGrounding
        ? formatDirectionGroundingText(refreshedDirectionGrounding) || undefined
        : buildDirSummaryText(directionsSummary, authoritativeActive) || undefined,
      runningUpdate: grounding.runningUpdate,
      decisionsRegisterText: decisionsRegisterText(decisionsReg, DECISIONS_REGISTER_CAP),
      // Static session-start grounding — same snapshot forward every checkpoint
      // (no refetch): retired/staged/prior-decision context the bootstrap shows.
      retiredDirectionsText: retiredDirectionsText || undefined,
      stagedDirectionsText: stagedDirectionsText || undefined,
      previousDecisionOutcomesText: previousDecisionOutcomesText || undefined,
      groundingRefreshed: refreshedGrounding !== null,
      // Session-static bootstrap sections: operator purpose/constraints preamble,
      // project skills, and tool content.  These are never server-refreshed so
      // the same session-start values carry forward on every checkpoint.
      contextPrefix: contextPrefix || undefined,
      skillsText: skillsText || undefined,
      toolContentText: toolContentText || undefined,
    };
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
  // Set true when workspace vanish has been handled for the current vanish event
  // so the guard does not restart the subprocess again on the NEXT iteration
  // (which would still see no .git if the recovery clone hasn't fully landed yet).
  // Reset to false when .git is observed, arming the guard for the next event.
  let workspaceVanishHandled = false;

  // ── Session + streaming wiring ──────────────────────────────────────────────
  const streamer = new SessionStreamer(ws, sessionId, taskId);
  const handleSessionEvent = (ev: ResidentSessionEvent): void => {
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
  // Every subprocess gets a distinct listener epoch. Invalidate the old epoch
  // BEFORE closing a worker for forced restart: child stdout can drain after
  // close(), and those late events must never enter the fresh worker's stream.
  let sessionEventEpoch = 0;
  const bindSessionEvents = (target: ResidentSession): void => {
    const epoch = ++sessionEventEpoch;
    target.onEvent((ev) => {
      if (epoch !== sessionEventEpoch) return;
      handleSessionEvent(ev);
    });
  };
  const invalidateSessionEvents = (): void => { sessionEventEpoch += 1; };
  let session: ResidentSession = startResidentClaudeSession({
    model, effort, disallowedTools, workspace: WORKSPACE, turnTimeoutMs: cfg.turnTimeoutMs,
  });
  bindSessionEvents(session);

  let firstTurnSent = false;
  let stopReason = 'stop_requested';
  let terminalProviderFailure: 'rate_limited' | 'dead_auth' | null = null;
  let pendingRestartCheckpoint: string | null = null;
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

  const runTurn = async (content: string | ContentBlock[], operator: boolean, pickupLagMs?: number | null): Promise<ResidentTurnResult> => {
    let turnContent = content;
    if (pendingRestartCheckpoint !== null) {
      const checkpointPrefix = `${pendingRestartCheckpoint}\n\n---\n\n`;
      turnContent = typeof content === 'string'
        ? `${checkpointPrefix}${content}`
        : [{ type: 'text', text: checkpointPrefix }, ...content];
      pendingRestartCheckpoint = null;
    }
    const full: string | ContentBlock[] = typeof turnContent === 'string'
      ? (firstTurnSent ? turnContent : `${bootstrapPrompt}\n\n---\n\n${turnContent}`)
      : (firstTurnSent ? turnContent : [{ type: 'text', text: `${bootstrapPrompt}\n\n---\n\n` }, ...turnContent]);
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
      toolCallDurationMs: r.totalToolCallDurationMs,
      meanInterTokenMs: r.meanInterTokenMs,
      pickupLagMs: pickupLagMs ?? null,
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
  const providerTurnFailed = (r: ResidentTurnResult): boolean => {
    // Child-close paths can classify the provider failure from their buffered
    // stderr tail even when no explicit provider-failed terminal event arrived.
    // Treat either signal as canonical so prompt-exit preemption never restarts
    // and masks a rate-limit/dead-auth failure as a benign SIGINT boundary.
    if (!r.providerFailed && r.failureCategory === null) return false;
    terminalProviderFailure = r.failureCategory;
    stopReason = r.failureCategory === 'rate_limited'
      ? 'provider_rate_limited'
      : r.failureCategory === 'dead_auth'
        ? 'provider_dead_auth'
        : 'provider_turn_failed';
    return true;
  };
  const checkpointPrimeFailed = (
    r: ResidentTurnResult,
    fallbackStopReason: 'compaction_prime_failed' | 'workspace_recovery_prime_failed',
    label: string,
  ): boolean => {
    if (providerTurnFailed(r)) {
      sendLog(ws, `[resident] FATAL: ${label} failed (${stopReason})\n`);
      return true;
    }
    if (r.interrupted || r.timedOut || r.hardTimedOut || r.processGone) {
      // A prime timeout is a canonical timeout terminal, not a generic Claude
      // process failure. Other incomplete exits retain the call-site reason.
      stopReason = r.timedOut || r.hardTimedOut
        ? 'turn_hard_timeout'
        : fallbackStopReason;
      const flags = [
        r.interrupted ? 'interrupted' : '',
        r.timedOut ? 'timed_out' : '',
        r.hardTimedOut ? 'hard_timed_out' : '',
        r.processGone ? 'process_gone' : '',
      ].filter(Boolean).join(',');
      sendLog(ws, `[resident] FATAL: ${label} incomplete (${flags})\n`);
      return true;
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
  const restartSessionFromCheckpoint = (reason: string): boolean => {
    const checkpoint = buildCompactionCheckpoint(buildCheckpointContext());
    invalidateSessionEvents();
    session.close();
    try {
      const newSession = startResidentClaudeSession({
        model, effort, disallowedTools, workspace: WORKSPACE, turnTimeoutMs: cfg.turnTimeoutMs,
      });
      bindSessionEvents(newSession);
      session = newSession;
      pendingRestartCheckpoint = checkpoint;
      streamer.status(`restarting resident worker (${reason})`);
      sendLog(ws, `[resident] worker restarted after ${reason}; checkpoint queued for next turn\n`);
      return true;
    } catch (e) {
      sendLog(ws, `[resident] worker restart failed after ${reason}: ${String(e)}\n`);
      stopReason = 'preemption_restart_failed';
      return false;
    }
  };

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

    // Refresh the self-model grounding from CURRENT project state before building
    // the checkpoint, so a session that compacts after mid-session mutations
    // (set-problem-model, set-memory, set-running-update) carries fresh data
    // rather than the stale session-start snapshot. NON-FATAL: any failure
    // degrades to the snapshot so compaction still proceeds — same degrade
    // pattern as the heartbeat/poll paths.
    try {
      const state = await apiGet(
        `/api/agent/projects/${projectId}/strategic-session/checkpoint-state`,
      ) as CheckpointStateResponse | null;
      if (state) {
        refreshedGrounding = formatCheckpointState(state);
        // Format the refreshed open messages using the same function as the
        // bootstrap. buildOpenMessagesText lives in strategic-sections.ts
        // (imports ../index.js), so it cannot be called from the pure lib.
        const refreshedOpenMsgs = Array.isArray(state.open_messages)
          ? state.open_messages as Array<Record<string, unknown>>
          : [];
        refreshedGrounding.openMessagesText = buildOpenMessagesText(refreshedOpenMsgs);
        // Same treatment for the recently-answered conversation tail: format from
        // the fresh checkpoint-state response so a correction the operator made
        // mid-session (now handled) survives the compaction restart.
        const refreshedRecentHandled = Array.isArray(state.recent_handled_conversation)
          ? state.recent_handled_conversation as Array<Record<string, unknown>>
          : [];
        refreshedGrounding.recentHandledConversationText = formatRecentHandledConversation(refreshedRecentHandled);
        sendLog(ws, '[resident] compaction grounding refreshed from checkpoint-state endpoint\n');
      }
    } catch (e) {
      sendLog(ws, `[resident] checkpoint-state refresh failed (non-fatal, using session-start snapshot): ${String(e)}\n`);
    }

    // Build the checkpoint prompt from current handler state.
    const checkpoint = buildCompactionCheckpoint(buildCheckpointContext());

    // Close the old subprocess and spawn a fresh one with the same params.
    invalidateSessionEvents();
    session.close();
    lastCompactionAt = now;
    compactionCount += 1;

    const newSession = startResidentClaudeSession({
      model, effort, disallowedTools, workspace: WORKSPACE, turnTimeoutMs: cfg.turnTimeoutMs,
    });
    // Wire the same event listeners so the streamer keeps receiving deltas.
    bindSessionEvents(newSession);
    session = newSession;

    // Prime the new session with the checkpoint as a standalone internal turn.
    // This delivers the condensed context so the session remembers its role,
    // exam ledger, recent interactions, and autonomous state. It is NOT an
    // operator or autonomous turn — it carries no lifecycle frames.
    streamer.status('priming compacted session with checkpoint');
    try {
      const primeResult = await newSession.sendTurn(checkpoint);
      if (checkpointPrimeFailed(primeResult, 'compaction_prime_failed', 'checkpoint prime')) {
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
  const residentMessageKey = (kind: 'ack' | 'reply', anchorMessageId: string): string => {
    const digest = createHash('sha256')
      .update(`${sessionId}:${anchorMessageId}`)
      .digest('hex');
    return `resident-${kind}-${digest}`;
  };
  const persistReply = async (
    r: ResidentTurnResult,
    anchorMessageId: string,
    coveredMessageIds: string[],
    stagedReply: ResidentStagedReply | null,
    residentTurnState: Record<string, unknown>,
  ): Promise<boolean> => {
    const text = stagedReply?.content.trim() || r.resultText.trim();
    if (!text) return false;
    try {
      await apiPost('/api/agent/strategic', {
        task_id: taskId,
        op: 'create_message',
        data: {
          type: stagedReply?.type ?? 'NOTE',
          content: text,
          idempotency_key: residentMessageKey('reply', anchorMessageId),
          covered_message_ids: coveredMessageIds,
          ...(stagedReply?.context === undefined ? {} : { context: stagedReply.context }),
          resident_turn_state: residentTurnState,
        },
      });
      return true;
    } catch (e) {
      sendLog(ws, `[resident] reply persistence failed; leaving operator message open: ${String(e)}\n`);
      return false;
    }
  };

  // Capture an operator-settled correction into the durable decisions register.
  // Operator turns (fast path + interrupt path) never emit a TICK-VERDICT, so
  // the autonomous decisions-register push at the bottom of the loop never sees
  // them; without this, a correction the operator makes in live chat drops out of
  // open_messages once handled and survives nowhere durable — so a restarted
  // session re-proposes exactly what the operator already settled. The strategist
  // is instructed (bootstrap prompt) to end such a turn with an OPERATOR-DECISION
  // marker; when present we record it here BEFORE the operator-turn heartbeat so
  // it is serialized into task.context._decisions_reg on that same heartbeat.
  const recordOperatorDecision = (
    r: ResidentTurnResult,
    messageIds: string[],
    exchangeText: string,
    stagedReplyText?: string,
  ): void => {
    if (r.interrupted || r.timedOut || r.hardTimedOut || r.processGone) {
      operatorDecisionReview = mergePendingOperatorDecisionReview(
        operatorDecisionReview,
        messageIds,
        new Date().toISOString(),
        exchangeText,
      );
      streamer.status('operator turn incomplete — durable decision review remains pending');
      return;
    }
    const note = (stagedReplyText ? parseOperatorDecision(stagedReplyText) : null)
      ?? parseOperatorDecision(r.resultText)
      ?? parseOperatorDecision(r.accumulatedTurnText);
    if (note === null) {
      recordOperatorDecisionCapture('markerless');
      operatorDecisionReview = mergePendingOperatorDecisionReview(
        operatorDecisionReview,
        messageIds,
        new Date().toISOString(),
        exchangeText,
      );
      streamer.status('operator turn awaits durable decision/no-decision review');
      return;
    }
    recordOperatorDecisionCapture('captured');
    admitDecisionEntry(decisionsReg, {
      atTick: examTick,
      at: new Date().toISOString(),
      surfaceKey: 'operator:message',
      verdict: OPERATOR_DECISION_VERDICT,
      note,
    }, DECISIONS_REGISTER_CAP);
    streamer.status('operator correction recorded to durable decisions register');
  };

  const pollMessages = async (): Promise<ResidentInboundMessage[]> => {
    const collected: ResidentInboundMessage[] = [];
    try {
      for (let page = 0; page < MAX_POLL_PAGES; page++) {
        const prevCursor = cursor;
        // session_id scopes the poll to THIS session's conversation server-side
        // (messages stamped for another session are excluded; legacy/cadence
        // NULL-session rows stay visible). Omitted-value degrades to project-wide.
        const sessionScope = sessionId ? `&session_id=${encodeURIComponent(sessionId)}&worker_generation=${workerGeneration}` : '';
        const q = `since=${encodeURIComponent(cursor)}&from_type=user&status=open&limit=${MESSAGE_POLL_LIMIT}${sessionScope}`;
        const resp = await apiGet(`/api/agent/projects/${projectId}/messages?${q}`) as MessagesPollResponse;
        const raw = Array.isArray(resp.messages) ? resp.messages : [];
        const admission = admitMessageBatchPage(
          raw, seen, OPERATOR_MESSAGE_BATCH_CAP - collected.length,
        );
        collected.push(...admission.admitted);
        // Advance across the whole page only when every unseen row was admitted.
        // If the atomic turn cap cut the page, advancing across deferred rows (or
        // marking them seen) would suppress them forever on the next poll.
        cursor = advanceCursorFromMessages(
          admission.fullyAdmitted ? raw : admission.admitted,
          cursor,
        );
        // Short page ⇒ tail drained. A full page that did not move the cursor (a
        // whole page sharing one timestamp) can't be drained by re-polling, so
        // stop to avoid an infinite loop.
        if (collected.length >= OPERATOR_MESSAGE_BATCH_CAP) break;
        if (!admission.fullyAdmitted) break;
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
    const note = serializeLedger(
      examRecords,
      examTick,
      correctiveNextTick,
      operatorDecisionReview !== null,
      operatorDecisionReview,
      getOperatorDecisionCaptureTelemetry(),
    );
    const body: Record<string, unknown> = {
      session_id: sessionId, task_id: taskId, progress, note,
      worker_generation: workerGeneration,
      // Send the current cursor so the backend persists it; a restart will
      // restore it from task.context.message_cursor instead of falling back to
      // the 2-minute window that re-delivers already-answered messages.
      message_cursor: cursor,
      // Report compaction metadata so the backend persists it in the session
      // blob — surfaced in the snapshot for operator visibility.
      compaction_count: compactionCount,
      last_compaction_at: lastCompactionAt > 0 ? new Date(lastCompactionAt).toISOString() : null,
      // Persist the decisions register so a full restart (VM recycle, process
      // crash) can restore settled decisions from task.context._decisions_reg.
      decisions_reg: decisionsReg.length > 0 ? serializeDecisionsReg(decisionsReg) : null,
    };
    if (handledIds && handledIds.length > 0) {
      body.handled_message_ids = handledIds;
    }
    const latencySummary = getResidentTurnLatencyTelemetry();
    if (latencySummary !== null) {
      body.turn_latency_summary = latencySummary;
    }
    const opDecisionSummary = getOperatorDecisionCaptureTelemetry();
    if (opDecisionSummary !== null) {
      body.operator_decision_summary = opDecisionSummary;
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
      // Refresh direction grounding from the heartbeat so compaction
      // checkpoints receive live direction text/status rather than the
      // session-start snapshot baked into directionsSummary.
      if (Array.isArray(resp.direction_grounding)) {
        refreshedDirectionGrounding = resp.direction_grounding;
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

  // ── Git READ-token background refresh ────────────────────────────────────────
  // Periodically calls /api/agent/git-read-token and updates the credential
  // helper's token file so `git fetch origin` keeps working beyond the 60-minute
  // GitHub App installation token TTL.
  //
  // SECURITY (audit finding #10) — this MUST stay the read-scoped endpoint.
  // The resident is a long-lived session (a multi-hour "workday"), so whatever
  // token sits in its credential file is AMBIENT: reachable by anything the
  // strategist runs, for as long as the session lives. Refreshing from the
  // push-capable /api/agent/github/token?force_refresh=true instead kept a
  // standing, freshly-minted push-capable installation token in that file every
  // 8 minutes for the life of the session — a standing privilege escalation in
  // the longest-lived process in the fleet.
  //
  // The resident does not need it: it only ever runs `git fetch origin` (automain
  // verification) and never pushes — push credentials are per-task and
  // short-lived, by design (see the endpoint docstring on agent_git_read_token).
  // Reading read-scoped here also means the push-capable ghToken written at clone
  // time (writeGitTokenFile above, which the clone itself needs) is REPLACED by a
  // read-scoped one on the first tick: the ambient credential DECAYS to read-only
  // within GIT_TOKEN_REFRESH_MS instead of being re-minted push-capable forever.
  //
  // If a strategic op ever genuinely needs to push, it must mint a push-capable
  // token AT THE MOMENT OF USE and must not park it in the ambient credential
  // file. (Dropping force_refresh also stops re-minting an installation token
  // every 8 minutes against the shared 5000/hr GitHub App budget; the read-token
  // endpoint is cache-backed.)
  //
  // Self-scheduling named function expression so the timer reference is always
  // non-null and cleared cleanly in the finally.
  let gitTokenRefreshTimer: ReturnType<typeof setTimeout>;
  function gitReadTokenRefreshTick(): void {
    void (async () => {
      await refreshResidentGitReadToken(ws);
      gitTokenRefreshTimer = setTimeout(gitReadTokenRefreshTick, GIT_TOKEN_REFRESH_MS);
    })();
  }
  gitTokenRefreshTimer = setTimeout(gitReadTokenRefreshTick, GIT_TOKEN_REFRESH_MS);

  // Pickup lag for an operator turn: ms from the MOST RECENT operator message's
  // server-side created_at to now. Server-absolute timestamps avoid agent-VM /
  // backend clock drift inflating the value. Returns null when no usable
  // created_at is present. Shared by every operator entry point so the metric is
  // computed identically wherever a reply starts.
  const computePickupLagMs = (msgs: ResidentInboundMessage[]): number | null => {
    const now = Date.now();
    let maxCreatedMs = 0;
    for (const m of msgs) {
      if (typeof m.created_at === 'string') {
        const t = new Date(m.created_at).getTime();
        if (Number.isFinite(t) && t > maxCreatedMs) maxCreatedMs = t;
      }
    }
    if (maxCreatedMs <= 0) return null;
    const lag = now - maxCreatedMs;
    return lag >= 0 ? lag : null;
  };

  // Run one operator turn end-to-end with OPERATOR-FIRST ordering: stream the
  // reply, record any settled decision, atomically persist reply + decision
  // state + exact handled receipt, heartbeat authority, and only THEN pay any
  // context-compaction cost.
  // Shared by all THREE operator entry points — the top-of-loop fast path, an
  // autonomous-turn interrupt, and a message that lands during a turn's tail — so
  // none of them can regress into compacting ahead of the operator's reply. That
  // ordering is what removes the measured compaction-restart pickup penalty: the
  // operator no longer waits a full Claude-subprocess restart before their reply
  // even starts. Returns 'stop' when the loop must break, 'continue' otherwise.
  const handleOperatorTurn = async (
    msgs: ResidentInboundMessage[],
  ): Promise<'stop' | 'continue'> => {
    const pickupLagMs = computePickupLagMs(msgs);
    const coveredMessageIds = msgs
      .map(message => typeof message.id === 'string' ? message.id.trim() : '')
      .filter(Boolean);
    const replyAnchorId = oldestStableInboundMessageId(msgs);
    if (replyAnchorId === null || coveredMessageIds.length !== msgs.length) {
      stopReason = 'operator_reply_missing_stable_message_id';
      sendLog(ws, '[resident] operator batch has no stable message id; leaving it open for retry\n');
      return 'stop';
    }
    const replyReceipt: ResidentReplyReceipt = {
      idempotency_key: residentMessageKey('reply', replyAnchorId),
      covered_message_ids: coveredMessageIds,
    };

    // Threshold-gated early acknowledgment: start a timer that fires after
    // cfg.operatorAckThresholdMs so the operator sees a progress signal during
    // slow turns. If the turn completes before the threshold, the timer is
    // cancelled and no ack is posted. A threshold of 0 disables the ack.
    let ackTimer: ReturnType<typeof setTimeout> | null = null;
    if (cfg.operatorAckThresholdMs > 0) {
      ackTimer = setTimeout(() => {
        const ackText = msgs.length === 1
          ? `On it — working on your message (this may take a moment).`
          : `On it — working on ${msgs.length} messages (this may take a moment).`;
        apiPost('/api/agent/strategic', {
          task_id: taskId,
          op: 'create_message',
          data: {
            type: 'NOTE',
            content: ackText,
            idempotency_key: residentMessageKey('ack', replyAnchorId),
          },
        }).catch((e: unknown) => {
          sendLog(ws, `[resident] early acknowledgment failed (non-fatal): ${String(e)}\n`);
        });
      }, cfg.operatorAckThresholdMs);
    }
    let r: ResidentTurnResult;
    let cleanupError: unknown = null;
    try {
      removeResidentReplyKeyAtPath(RESIDENT_STAGED_REPLY_PATH);
      installResidentReplyReceiptAtPaths(
        RESIDENT_REPLY_KEY_PATH,
        RESIDENT_REPLY_RECEIPT_PATH,
        replyReceipt,
      );
    } catch (e) {
      if (ackTimer !== null) clearTimeout(ackTimer);
      try {
        removeResidentReplyArtifactsAtPaths(
          RESIDENT_REPLY_KEY_PATH,
          RESIDENT_REPLY_RECEIPT_PATH,
        );
      } catch { /* setup already failed; leave both for startup cleanup */ }
      stopReason = 'operator_reply_idempotency_setup_failed';
      sendLog(ws, `[resident] idempotency key setup failed; leaving operator message open: ${String(e)}\n`);
      return 'stop';
    }
    try {
      r = await runTurn(combineOperatorMessages(msgs), true, pickupLagMs);
    } finally {
      if (ackTimer !== null) clearTimeout(ackTimer);
      try {
        removeResidentReplyArtifactsAtPaths(
          RESIDENT_REPLY_KEY_PATH,
          RESIDENT_REPLY_RECEIPT_PATH,
        );
      } catch (e) {
        cleanupError = e;
        sendLog(ws, `[resident] reply receipt cleanup failed; stopping after this turn: ${String(e)}\n`);
      }
    }
    if (providerTurnFailed(r)) return 'stop';
    if (turnWedgedSession(r)) return 'stop';
    if (r.interrupted || r.timedOut || r.hardTimedOut || r.processGone) {
      stopReason = r.processGone ? 'session_process_exited' : 'operator_turn_interrupted';
      return 'stop';
    }
    let stagedReply: ResidentStagedReply | null;
    try {
      stagedReply = readResidentStagedReplyAtPath(RESIDENT_STAGED_REPLY_PATH, replyReceipt);
    } catch (e) {
      stopReason = 'operator_reply_staging_invalid';
      sendLog(ws, `[resident] staged reply validation failed; leaving operator message open: ${String(e)}\n`);
      try { removeResidentReplyKeyAtPath(RESIDENT_STAGED_REPLY_PATH); } catch { /* startup cleanup retries */ }
      return 'stop';
    }
    // Capture any operator-settled correction BEFORE the sole reply write. The
    // serialized state below then lands in the same backend transaction as the
    // reply and covered-message updates, closing the crash-before-heartbeat gap.
    recordOperatorDecision(
      r,
      coveredMessageIds,
      msgs.map(message => message.content?.trim() ?? '').filter(Boolean).join('\n\n'),
      stagedReply?.content,
    );
    const residentTurnState: Record<string, unknown> = {
      exam_ledger: serializeLedger(
        examRecords,
        examTick,
        correctiveNextTick,
        operatorDecisionReview !== null,
        operatorDecisionReview,
        getOperatorDecisionCaptureTelemetry(),
      ),
      decisions_reg: decisionsReg.length > 0 ? serializeDecisionsReg(decisionsReg) : null,
      message_cursor: cursor,
      operator_decision_summary: getOperatorDecisionCaptureTelemetry(),
    };
    let replyPersisted = false;
    try {
      replyPersisted = await persistReply(
        r, replyAnchorId, coveredMessageIds, stagedReply, residentTurnState,
      );
    } finally {
      try {
        removeResidentReplyKeyAtPath(RESIDENT_STAGED_REPLY_PATH);
      } catch (e) {
        cleanupError ??= e;
        sendLog(ws, `[resident] staged reply cleanup failed; stopping after this turn: ${String(e)}\n`);
      }
    }
    if (!replyPersisted) {
      stopReason = 'operator_reply_not_persisted';
      return 'stop';
    }
    // The create_message transaction already marked exactly this receipt's
    // covered IDs handled and persisted the restart-critical turn metadata.
    // The heartbeat only renews authority and reports ancillary session state;
    // it must never expand the covered set after a crash/retry.
    const hb = await heartbeat(true);
    if (hb === null) {
      stopReason = 'operator_post_reply_heartbeat_failed';
      sendLog(ws, '[resident] post-reply heartbeat failed; stopping on a fresh worker boundary\n');
      return 'stop';
    }
    if (hb?.stop_requested) return 'stop';
    if (cleanupError !== null) {
      stopReason = 'operator_reply_receipt_cleanup_failed';
      return 'stop';
    }
    if (r.processGone) { stopReason = 'session_process_exited'; return 'stop'; }
    // Compaction runs ONLY after the reply is streamed and the handled-marking
    // heartbeat has landed — so a compaction on this turn primes the fresh session
    // WITH the settled decision and WITHOUT the just-answered message, and the
    // operator never waits a subprocess restart before their reply starts.
    if (await maybeCompact(r)) return 'stop';
    // A just-finished operator turn arms one autonomous follow-through tick.
    operatorTurnJustCompleted = true;
    return 'continue';
  };

  // ── Two-speed loop ──────────────────────────────────────────────────────────
  try {
    for (;;) {
      // Guard: verify the workspace is still present before each turn. An
      // external event (OOM-kill, /tmp cleaner) can delete it mid-session.
      //
      // The re-clone mints a FRESH READ-SCOPED token at the moment of use rather
      // than reusing the session-start `cloneUrl`/`ghToken`: that token is push-
      // capable (which the resident must not hold ambiently) and expires after 60
      // minutes (so a re-clone deep into a multi-hour session would just throw).
      // See recloneResidentWorkspace.
      if (!fs.existsSync(`${WORKSPACE}/.git`)) {
        if (!workspaceVanishHandled) {
          workspaceVanishHandled = true;
          sendLog(ws, '[resident] workspace vanished — re-cloning before next turn\n');
          streamer.status('workspace vanished — re-cloning');
          await recloneResidentWorkspace(ws, owner, repo, baseBranch);
          sendLog(ws, '[resident] workspace restored\n');

          // Re-provision all generated helper tools into the recreated checkout.
          // They were written into the deleted directory inode and are gone.
          writePlaybookHelper(taskId);
          writeMessagesHelper(taskId);
          writeSkillHelpers(task.skills as Record<string, unknown>[] | undefined);
          configureResidentAgentPermissions(_RESIDENT_STRATEGIC_TASK_TYPE);
          writeStrategicHelpers(taskId, projectId, WORKSPACE);
          provisionAmbientHelpers(taskId, WORKSPACE);
          sendLog(ws, '[resident] workspace recovery — helper tools re-provisioned\n');

          // Restart the subprocess so its cwd points to the new directory inode.
          // The live subprocess is still attached to the deleted inode.
          invalidateSessionEvents();
          session.close();
          const recoverySession = startResidentClaudeSession({
            model, effort, disallowedTools, workspace: WORKSPACE, turnTimeoutMs: cfg.turnTimeoutMs,
          });
          bindSessionEvents(recoverySession);
          session = recoverySession;

          // Prime with the compaction checkpoint to carry pre-loss decision context.
          streamer.status('workspace recovery — priming restarted subprocess with checkpoint');
          try {
            const primeResult = await recoverySession.sendTurn(
              buildCompactionCheckpoint(buildCheckpointContext()),
            );
            if (checkpointPrimeFailed(
              primeResult,
              'workspace_recovery_prime_failed',
              'workspace recovery checkpoint prime',
            )) {
              break;
            }
            sendLog(ws, '[resident] workspace recovery complete — subprocess restarted and primed\n');
          } catch (e) {
            sendLog(ws, `[resident] FATAL: workspace recovery prime threw: ${String(e)}\n`);
            stopReason = 'workspace_recovery_prime_failed';
            break;
          }
        }
      } else {
        workspaceVanishHandled = false;
      }

      if (!session.isAlive()) {
        stopReason = 'session_process_exited';
        break;
      }

      // 1. Fast path: any pending operator message pre-empts everything. The
      // operator reply is streamed and its lease-renewing heartbeat lands BEFORE
      // any context compaction (handleOperatorTurn enforces that ordering), so a
      // compaction never sits on the critical path ahead of the reply.
      const msgs = await pollMessages();
      if (msgs.length > 0) {
        streamer.status('operator message received — responding');
        if (await handleOperatorTurn(msgs) === 'stop') break;
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
      // Only a recovered decision or NO-DECISION may clear this snapshot.
      const reviewForTurn = operatorDecisionReview;
      lastAutonomousTurnAt = Date.now();
      const turnPromise = runTurn(
        buildAutonomousPrompt(
          focus,
          recentRecords,
          openFound,
          corrective,
          cfg.maxOpsPerTurn,
          decisionsRegisterText(decisionsReg),
          reviewForTurn ?? false,
        ),
        false,
      );
      const watch = await watchTurn(turnPromise);
      let r: ResidentTurnResult;
      if (watch.kind !== 'done') {
        session.interrupt();
        const settledBeforeDeadline = await Promise.race([
          turnPromise.then(() => true),
          new Promise<boolean>((resolve) => setTimeout(() => resolve(false), OPERATOR_PREEMPT_DEADLINE_MS)),
        ]);
        if (!settledBeforeDeadline) {
          invalidateSessionEvents();
          session.close();
          const abandoned = await turnPromise;
          if (
            watch.kind !== 'stop'
            && !restartSessionFromCheckpoint('operator preemption deadline')
          ) break;
          r = { ...abandoned, processGone: false, interrupted: true };
        } else {
          r = await turnPromise;
        }
      } else {
        r = await turnPromise;
      }

      // Await the (possibly interrupted) autonomous turn, then renew the lease
      // ONLY if a strategic op was applied (progress) AND we still hold authority
      // — never renew a lease that lapsed mid-turn (watch === 'suspend'), and
      // never renew on bare examination (looking never renews freshness — the
      // dead-man's switch stays progress-gated).
      if (providerTurnFailed(r)) break;
      if (watch.kind === 'message' && r.processGone) {
        // SIGINT commonly makes the old CLI exit promptly. That is a successful
        // operator preemption boundary, not a Resident terminal, provided the
        // result was not a canonical provider failure (handled immediately
        // above). Queue the checkpoint into a fresh subprocess before replying.
        if (!restartSessionFromCheckpoint('operator preemption process exit')) break;
        r = { ...r, processGone: false, interrupted: true };
      }
      if (turnWedgedSession(r)) break;
      if (r.processGone) { stopReason = 'session_process_exited'; break; }
      // ResidentSession reports a child that closed without a terminal result as
      // processGone while leaving interrupted false and returning any partial
      // streamed text. Treat both signals identically for classification: partial
      // markers from a dead child are not completed autonomous outcomes.
      const turnInterrupted = r.interrupted || r.timedOut || r.hardTimedOut || r.processGone;

      // Update the examination ledger from the turn's TICK-VERDICT. Interrupted
      // turns (operator preemption via watch.kind === 'message', authority suspend
      // via watch.kind === 'suspend', stall watchdog, or subprocess exit) do not
      // have a completed TICK-VERDICT; classifyTickOutcome records them as
      // `interrupted` (non-corrective) so the ledger captures the event without
      // raising a false violation or admitting partial output.
      const upd = classifyTickOutcome({
        resultText: r.resultText,
        accumulatedTurnText: r.accumulatedTurnText,
        sawToolUse: r.sawToolUse,
        focus,
        atTick: examTick,
        at: new Date().toISOString(),
        interrupted: turnInterrupted,
      });
      examRecords.push(upd.record);
      if (examRecords.length > EXAM_RECORDS_CAP) {
        examRecords.splice(0, examRecords.length - EXAM_RECORDS_CAP);
      }
      // Build and push to the durable decisions register (full-fidelity notes),
      // but ONLY for SETTLED decisions (advanced/found/clear). Interrupted turns
      // (operator preemption / authority suspend / stall) and unevidenced idle
      // CLEARs carry no durable conclusion; recording them here would crowd the
      // fixed register window and evict real decisions — worst at the
      // autonomous<->operator boundary where interrupts cluster. They remain in
      // the exam ledger (examRecords) above; only the durable register skips them.
      if (isSettledDecisionVerdict(upd.record.verdict)) {
        const fullTickVerdict = parseTickVerdict(r.resultText)
          ?? parseTickVerdict(r.accumulatedTurnText);
        admitDecisionEntry(decisionsReg, {
          atTick: upd.record.atTick,
          at: upd.record.at,
          surfaceKey: upd.record.surfaceKey,
          verdict: upd.record.verdict,
          note: fullTickVerdict?.note ?? upd.record.note,
        }, DECISIONS_REGISTER_CAP);
      }
      correctiveNextTick = upd.corrective;
      previousTurnAgendaContinue = upd.agendaContinue;
      if (upd.statusNote) streamer.status(upd.statusNote);

      // Resolve the operator-decision review requested by THIS turn. A recovered
      // decision is admitted before the heartbeat. Marker-coverage telemetry was
      // already recorded exactly once on the original operator turn; corrective
      // outcomes must not rewrite or double-count it. Every unresolved result
      // retains and advances the durable pending review.
      if (reviewForTurn !== null) {
        const reviewOutcome = classifyOperatorDecisionReview({
          resultText: r.resultText,
          accumulatedTurnText: r.accumulatedTurnText,
          interrupted: turnInterrupted,
        });
        // A normal outcome covers exactly one explicit exchange. Once the
        // queue has compacted into the id-less overflow sentinel, accept only
        // an explicit whole-batch outcome; otherwise one recovered fact could
        // silently discard every omitted operator exchange.
        const activeOverflowSentinel = reviewForTurn.overflowed === true
          && reviewForTurn.messageIds.length === 0
          && (reviewForTurn.queuedExchanges?.length ?? 0) === 0;
        // Batch markers classify only the id-less overflow sentinel. In
        // particular, never let one consume an explicit current exchange while
        // leaving the batch itself armed behind it.
        const resolvesReview = reviewOutcome.coversOverflow === true
          ? activeOverflowSentinel
          : reviewForTurn.messageIds.length > 0 || reviewForTurn.overflowed !== true;
        if (reviewOutcome.kind === 'recovered' && reviewOutcome.note && resolvesReview) {
          admitDecisionEntry(decisionsReg, {
            atTick: examTick,
            at: upd.record.at,
            surfaceKey: 'operator:message',
            verdict: OPERATOR_DECISION_VERDICT,
            note: reviewOutcome.note,
          }, DECISIONS_REGISTER_CAP);
          operatorDecisionReview = advancePendingOperatorDecisionReview(reviewForTurn, {
            coversOverflow: reviewOutcome.coversOverflow,
          });
          streamer.status('operator decision recovered into durable decisions register');
        } else if (reviewOutcome.kind === 'no-decision' && resolvesReview) {
          operatorDecisionReview = advancePendingOperatorDecisionReview(reviewForTurn, {
            coversOverflow: reviewOutcome.coversOverflow,
          });
          streamer.status('operator exchange confirmed routine/no-decision — review cleared');
        } else {
          const retained = retainPendingOperatorDecisionReview(reviewForTurn);
          if (retained.attempts >= MAX_REVIEW_ATTEMPTS) {
            // The model has repeatedly failed to emit a valid classification marker.
            // Rather than silently losing the operator's exchange at restart or
            // compaction, admit the available exchange text as a best-effort entry
            // and advance past this review so the queue can drain.
            //
            // Id-less overflow sentinels have no stable message ids so the same
            // sentinel can re-trip this branch on every failed autonomous turn.
            // Guard against crowding the register with duplicate eviction-protected
            // entries: skip admission if an equivalent fallback entry is already
            // present (identified by the sentinel's startedAt timestamp in the note).
            const sentinelKey = activeOverflowSentinel ? `sentinel:${reviewForTurn.startedAt}` : null;
            const alreadyAdmitted = sentinelKey !== null
              && decisionsReg.some(e => e.verdict === OPERATOR_DECISION_VERDICT && e.note.includes(sentinelKey));
            const fallbackNote = reviewForTurn.exchangeText
              ? `[unclassified after ${retained.attempts} attempts${sentinelKey ? ` ${sentinelKey}` : ''}] ${reviewForTurn.exchangeText}`
              : `[unclassified after ${retained.attempts} attempts${sentinelKey ? ` ${sentinelKey}` : ''} — exchange text unavailable]`;
            if (!alreadyAdmitted) {
              admitDecisionEntry(decisionsReg, {
                atTick: examTick,
                at: upd.record.at,
                surfaceKey: 'operator:message',
                verdict: OPERATOR_DECISION_VERDICT,
                note: fallbackNote,
              }, DECISIONS_REGISTER_CAP);
            }
            // Pass coversOverflow:true when the current review IS the id-less
            // overflow sentinel (activeOverflowSentinel) — advancePending returns
            // `current` unchanged when messageIds is empty and coversOverflow is
            // falsy, so without this the sentinel never clears and every subsequent
            // autonomous turn re-trips the same escape hatch.
            operatorDecisionReview = advancePendingOperatorDecisionReview(reviewForTurn, {
              coversOverflow: activeOverflowSentinel || reviewOutcome.coversOverflow,
            });
            streamer.status(`operator-decision review admitted after ${retained.attempts} failed attempts — exchange text recorded as fallback`);
          } else {
            operatorDecisionReview = retained;
            streamer.status(reviewOutcome.statusNote
              ?? 'operator-decision overflow needs an explicit whole-batch classification — durable review remains pending');
          }
        }
      }

      // The post-turn heartbeat is the commit point for the complete autonomous
      // state transition: tick verdict, exam ledger, decisions register, review,
      // agenda, corrective state, and capture telemetry are all classified first.
      // Compaction remains later so the operator-first ordering is unchanged.
      const hb = await heartbeat(r.appliedStrategicOp && authorityHeld);
      // Record the pacing baseline AFTER the post-turn heartbeat so the loop's own
      // writes are already folded into the rev we compare against next iteration.
      // Recording it pre-turn would leave lastAutonomousWorkStateRev behind the
      // heartbeat's updated currentWorkStateRev, causing the gate to fire again on
      // the loop's own applied ops (self-echo).
      lastAutonomousWorkStateRev = currentWorkStateRev;
      if (hb?.stop_requested || watch.kind === 'stop') break;
      if (r.processGone) { stopReason = 'session_process_exited'; break; }

      // Authority lapsed mid-turn ⇒ fall through to passive mode next iteration.
      if (watch.kind === 'suspend') continue;

      // OPERATOR-FIRST: answer any operator message BEFORE paying the compaction
      // restart. Two message sources are handled the same way:
      //   - watch.kind === 'message': a message that pre-empted this autonomous
      //     turn (caught by watchTurn's fast poll and already interrupted above);
      //   - a message that landed during the turn's tail / the heartbeat above,
      //     caught by this extra poll.
      // handleOperatorTurn streams the reply and lands its lease-renewing
      // heartbeat FIRST, then compacts — so the operator never waits a full Claude
      // subprocess restart before their reply starts. Compacting here (as the loop
      // used to, right after the autonomous turn) was the dominant contributor to
      // the called-out operator pickup lag.
      const pendingMsgs = watch.kind === 'message'
        ? watch.messages
        : await pollMessages();
      if (pendingMsgs.length > 0) {
        streamer.status(watch.kind === 'message'
          ? 'operator message received (interrupted autonomous work)'
          : 'operator message received — responding');
        if (await handleOperatorTurn(pendingMsgs) === 'stop') break;
        continue;
      }

      // No operator waiting → safe to pay the compaction cost now. Deferred to
      // here (from immediately after the turn) so the operator-message check above
      // always wins the race for the critical path.
      if (await maybeCompact(r)) break;

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
    invalidateSessionEvents();
    session.close();
  }

  // ── Terminal status ─────────────────────────────────────────────────────────
  // Consult the ACTUAL stop reason + process exit code: a crash / wedge is a
  // FAILED terminal, COMPLETED only on a clean operator/task-driven stop. A
  // session that limped or died must not report success.
  statusRef.value = 'Resident strategic session ended';
  sendLog(ws, `Resident strategic session ${sessionId} ended (${stopReason}).\n`);

  const { status, failureCategory } = resolveTerminalStatus(
    stopReason,
    session.exitCode(),
    terminalProviderFailure ?? session.failureCategory?.() ?? null,
  );
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
