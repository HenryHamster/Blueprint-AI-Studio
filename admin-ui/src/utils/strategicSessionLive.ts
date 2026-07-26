/**
 * Pure derivations for the resident "Workday" LIVE session view — split out of
 * MessagesPanel so the component file stays under its size cap and these
 * reconciliation helpers can be unit-tested in isolation.
 *
 * A resident-session delta (one `strategic_session` WS frame) carries either the
 * assistant's REPLY text (the only content that reaches the durable transcript,
 * once the agent `./messages send`s it) or a LIVE, EPHEMERAL ACTIVITY one-liner
 * (a command / file edit / read / spawned sub-agent / tool result). These helpers
 * split those apart: the reply builds the streaming preview bubble; the activity
 * builds the ephemeral strip. Neither is ever persisted.
 */
import type { ProjectMessage } from '../types/messages';
import type { StrategicSessionDelta } from '../hooks/useLogViewer';
import type { StrategicResidentSessionSnapshot } from '../strategicStateApi';
import { parseTimestamp } from './time';

/** One classified run of contiguous streamed reply/reasoning text for a turn.
 *  `reply` = the resident's actual answer (message / legacy kind-less deltas);
 *  `reasoning` = ephemeral train-of-thought (reasoning-kind deltas), rendered set
 *  apart so it is never mistaken for the reply. */
export type StrategicTurnSegmentKind = 'reply' | 'reasoning';
export interface StrategicTurnSegment {
  kind: StrategicTurnSegmentKind;
  text: string;
}

export interface StrategicStreamingTurn {
  /** Stable React key for the streaming bubble: session id + first visible seq. */
  key: string;
  sessionId: string;
  /** Reply-only concatenation (scroll anchor + emptiness check). Excludes reasoning. */
  text: string;
  /** Ordered runs of reply vs reasoning text, contiguous same-kind fragments merged. */
  segments: StrategicTurnSegment[];
}

export interface StrategicActivityItem {
  id: number;
  kind: string;
  text: string;
  /** Epoch ms when this activity item started (from d.ts). */
  ts?: number;
  /** True when this is the last item of an in-flight turn (lifecycle working/responding). */
  inFlight?: boolean;
}

export interface StrategicLiveActivity {
  /** The most recent activity one-liners for the current in-flight turn. */
  items: StrategicActivityItem[];
  /** The latest lifecycle marker seen for the current turn (or null).
   *  `responding` = operator turn in flight; `working` = autonomous turn in
   *  flight; `done` = turn ended (strip cleared); `null` = no lifecycle seen. */
  latestLifecycle: 'responding' | 'working' | 'done' | null;
}

// Activity kinds mirror strategic_events.py `_STRATEGIC_SESSION_KINDS`.
const STRATEGIC_ACTIVITY_KINDS = new Set([
  'tool', 'command', 'file', 'read', 'subagent', 'tool_result', 'action',
]);

/** Reply text = message / reasoning (and legacy kind-less) deltas — never the
 *  activity one-liners, so command noise stays out of the streaming reply. */
export function isStrategicReplyDelta(kind: string | undefined): boolean {
  return !kind || kind === 'message' || kind === 'reasoning';
}

/** Newest durable strategic reply time — the reconciliation high-water mark: a
 *  streamed delta older than this has already been persisted (shown as a durable
 *  bubble) so it is filtered out of the live preview / activity strip. */
export function strategicReplyWatermark(conversationMessages: ProjectMessage[]): number {
  let watermark = Number.NEGATIVE_INFINITY;
  for (const msg of conversationMessages) {
    const ft = (msg.from_type || '').toLowerCase();
    if ((ft === 'strategic' || ft === 'strategist') && msg.created_at) {
      const t = parseTimestamp(msg.created_at).getTime();
      if (!Number.isNaN(t) && t > watermark) watermark = t;
    }
  }
  return watermark;
}

/** Count of durable strategist replies — the clock-skew-proof baseline the panel
 *  compares against to detect that a fresh reply has landed. */
export function countStrategicReplies(conversationMessages: ProjectMessage[]): number {
  let n = 0;
  for (const msg of conversationMessages) {
    const ft = (msg.from_type || '').toLowerCase();
    if (ft === 'strategic' || ft === 'strategist') n += 1;
  }
  return n;
}

/** Deltas of the CURRENT (latest) session strictly newer than the durable-reply
 *  water mark — the shared un-persisted window for both live views. */
function visibleSessionDeltas(
  deltas: StrategicSessionDelta[],
  latestSessionId: string | undefined,
  watermark: number,
): StrategicSessionDelta[] {
  return deltas.filter(d => {
    if (latestSessionId && d.session_id && d.session_id !== latestSessionId) return false;
    if (watermark === Number.NEGATIVE_INFINITY) return true;
    if (!d.ts) return true; // no timestamp → treat as a fresh, unpersisted delta
    const t = parseTimestamp(d.ts).getTime();
    return Number.isNaN(t) ? true : t > watermark;
  });
}

/** Seq of the last `done` lifecycle frame in `visible` (or NEGATIVE_INFINITY if
 *  none). A `done` frame is always the LAST frame emitted for a turn (the agent
 *  calls `streamer.done()` only after the turn's text/tool deltas have flushed),
 *  so its seq exceeds every reply/activity delta of that completed turn — including
 *  any post-send "Sent — here is a summary…" narration. Anything at or before it
 *  belongs to a finished turn whose reply is now durable, and is dropped from both
 *  live views. */
function lastDoneSeq(visible: StrategicSessionDelta[]): number {
  let seq = Number.NEGATIVE_INFINITY;
  for (const d of visible) {
    if (d.status === 'done') {
      const s = d.seq ?? d.id;
      if (s > seq) seq = s;
    }
  }
  return seq;
}

/**
 * Derive the live streaming turn from the resident-session transcript deltas.
 *
 * Dedup / reconciliation uses TWO gates. (1) The newest DURABLE strategic
 * message's `created_at` is a high-water mark: only deltas from the most recent
 * session whose `ts` is strictly newer than it are considered. (2) The last `done`
 * lifecycle frame marks the end of a completed turn: reply deltas at or before it
 * are dropped. Gate (2) is what kills the "reply shown twice" bug — after the
 * strategist sends its durable reply it may keep narrating in the live stream
 * ("Sent — here is a summary of what I said…"), and those post-send reply deltas
 * carry a `ts` that is NOT strictly older than the durable message's `created_at`,
 * so the time-only water mark lets them survive as a second bubble. They always
 * precede the turn's `done` frame, so gating on `lastDoneSeq` removes them and only
 * the durable persisted message remains. Only REPLY-kind deltas (message /
 * reasoning) build the preview — command / tool one-liners render in the ephemeral
 * strip instead. A cadence / non-resident turn streams nothing, so this returns
 * null and only the persisted message renders.
 */
export function computeStrategicStreamingTurn(
  transcript: StrategicSessionDelta[] | undefined,
  conversationMessages: ProjectMessage[],
): StrategicStreamingTurn | null {
  const deltas = transcript ?? [];
  if (deltas.length === 0) return null;
  const latestSessionId = deltas[deltas.length - 1]?.session_id;
  const watermark = strategicReplyWatermark(conversationMessages);
  const visible = visibleSessionDeltas(deltas, latestSessionId, watermark);
  const doneSeq = lastDoneSeq(visible);

  const replyDeltas = visible.filter(
    d => isStrategicReplyDelta(d.kind) && (d.seq ?? d.id) > doneSeq,
  );
  if (replyDeltas.length === 0) return null;

  // Each WS frame is a rate-limited flush fragment of ONE continuous text run, so
  // contiguous fragments of the SAME classification (reply vs reasoning) are
  // concatenated WITHOUT any injected separator — a fragment's own newlines (real
  // paragraph breaks, bullets, fenced code) are the only newlines that survive.
  const rawSegments: StrategicTurnSegment[] = [];
  for (const d of replyDeltas) {
    if (!d.text) continue;
    const segKind: StrategicTurnSegmentKind = d.kind === 'reasoning' ? 'reasoning' : 'reply';
    const last = rawSegments[rawSegments.length - 1];
    if (last && last.kind === segKind) last.text += d.text;
    else rawSegments.push({ kind: segKind, text: d.text });
  }
  // Trim only each run's outer whitespace (streaming boundary artifacts); internal
  // newlines/blank lines are preserved. Drop runs that trim to nothing.
  const segments = rawSegments
    .map(s => ({ kind: s.kind, text: s.text.trim() }))
    .filter(s => s.text.length > 0);
  if (segments.length === 0) return null;

  const text = segments.filter(s => s.kind === 'reply').map(s => s.text).join('\n').trim();

  const firstSeq = replyDeltas[0].seq ?? replyDeltas[0].id;
  const sessionId = latestSessionId ?? 'session';
  return { key: `${sessionId}:${firstSeq}`, sessionId, text, segments };
}

/**
 * Derive the LIVE, EPHEMERAL activity feed (commands / tool calls / sub-agents)
 * for the in-flight resident turn. Shares the streaming bubble's watermark so it
 * only ever shows activity NOT yet superseded by a durable strategic reply, and
 * drops everything up to and including the last `done` frame (that belongs to a
 * completed turn). Returns at most the most recent handful so the strip stays
 * compact. NEVER persisted — purely a live view of the transcript deltas.
 */
export function computeStrategicSessionActivity(
  transcript: StrategicSessionDelta[] | undefined,
  conversationMessages: ProjectMessage[],
): StrategicLiveActivity {
  const deltas = transcript ?? [];
  if (deltas.length === 0) return { items: [], latestLifecycle: null };
  const latestSessionId = deltas[deltas.length - 1]?.session_id;
  const watermark = strategicReplyWatermark(conversationMessages);
  const visible = visibleSessionDeltas(deltas, latestSessionId, watermark);

  let latestLifecycle: 'responding' | 'working' | 'done' | null = null;
  for (const d of visible) {
    if (d.status === 'responding' || d.status === 'working' || d.status === 'done') latestLifecycle = d.status;
  }
  const doneSeq = lastDoneSeq(visible);

  const inFlight = latestLifecycle === 'working' || latestLifecycle === 'responding';
  const rawItems: StrategicActivityItem[] = visible
    .filter(d => STRATEGIC_ACTIVITY_KINDS.has(d.kind ?? '') && Boolean(d.text) && (d.seq ?? d.id) > doneSeq)
    .map(d => {
      const tsMs = d.ts ? parseTimestamp(d.ts).getTime() : undefined;
      return { id: d.id, kind: d.kind ?? 'tool', text: d.text, ts: Number.isNaN(tsMs) ? undefined : tsMs };
    });
  if (inFlight && rawItems.length > 0) {
    rawItems[rawItems.length - 1] = { ...rawItems[rawItems.length - 1], inFlight: true };
  }
  const items = rawItems;

  const MAX_ACTIVITY = 8;
  return {
    items: items.length > MAX_ACTIVITY ? items.slice(-MAX_ACTIVITY) : items,
    latestLifecycle,
  };
}

/** One of the three truthful states of the persistent resident live-activity
 *  widget. `executing` is the ONLY phase whose elapsed timer ticks. */
export type ResidentActivityPhase = 'executing' | 'idle' | 'dead';

export interface ResidentActivityState {
  phase: ResidentActivityPhase;
  /** Durable backend lifecycle phase. `phase` remains the timer/presentation
   * compatibility shape; this field carries the exact operator contract. */
  durablePhase?: 'working' | 'waiting' | 'retrying' | 'stopping' | 'ended';
  /** The shell command / tool one-liner currently (or last) executing. */
  commandText?: string;
  /** Epoch ms the executing command started — drives the LIVE counter (executing only). */
  startMs?: number;
  /** Epoch ms to FREEZE any counter at (idle/dead) — last known heartbeat / end time. */
  endMs?: number;
  /** Short human-readable label for the state. */
  label: string;
  detail?: string;
  idleRemainingSeconds?: number | null;
  pendingOperatorMessages?: number;
  pendingWatches?: number;
  firedWatches?: number;
}

/** Heartbeat older than this (relative to `nowMs`) means the resident session is
 *  presumed dead even without an explicit `ended`/`stop_requested` snapshot — the
 *  backstop that keeps a counter from ticking forever when no `done` frame ever
 *  arrives. The resident heartbeats on a slow cadence, so the threshold is generous. */
export const RESIDENT_HEARTBEAT_STALE_MS = 90_000;

function retryWaitLabel(
  reason: string | null | undefined,
  retryUntil: string | null | undefined,
  nowMs: number,
): string {
  const cleanReason = reason?.trim() || 'Provider temporarily unavailable';
  const retryAtMs = retryUntil ? parseTimestamp(retryUntil).getTime() : NaN;
  if (Number.isNaN(retryAtMs)) return `${cleanReason} — waiting to retry`;
  const remainingSeconds = Math.max(0, Math.ceil((retryAtMs - nowMs) / 1000));
  if (remainingSeconds < 60) return `${cleanReason} — retrying in ${remainingSeconds}s`;
  const remainingMinutes = Math.ceil(remainingSeconds / 60);
  if (remainingMinutes < 60) return `${cleanReason} — retrying in ${remainingMinutes}m`;
  const hours = Math.floor(remainingMinutes / 60);
  const minutes = remainingMinutes % 60;
  return `${cleanReason} — retrying in ${hours}h${minutes ? ` ${minutes}m` : ''}`;
}

/** Activity kinds that represent genuine in-progress work. A trailing `tool_result`
 *  (or a read/file completion) means the prior command already returned, so it does
 *  NOT count as still-executing. */
const EXECUTING_ACTIVITY_KINDS = new Set(['command', 'tool', 'subagent', 'action']);

/**
 * Derive the truthful three-state resident live-activity status for the persistent
 * chat-view widget from ACTUAL feed data — never solely the last WS lifecycle frame
 * (which may never arrive when the session dies).
 *
 * - `dead`  — the resident session is explicitly gone, restart failed, or its
 *   heartbeat is stale enough to require repair. Explicitly ended sessions say
 *   "Session ended"; recoverable failures say "Workday needs restart". The counter
 *   is frozen at the last known time — it must NEVER keep ticking for either case.
 * - `executing` — the session is alive AND the live activity feed's last item is an
 *   in-flight running command (not superseded by a `tool_result` / newer activity /
 *   `done`). Only this phase carries a live `startMs` so the timer runs.
 * - `idle` — the session is alive but nothing is currently executing (between
 *   commands, or the turn is `done` while the session stays up). No ticking timer.
 *
 * Returns `null` when there is no resident session at all (nothing to render).
 */
export function computeResidentActivityState(
  transcript: StrategicSessionDelta[] | undefined,
  conversationMessages: ProjectMessage[],
  residentSession: StrategicResidentSessionSnapshot | null | undefined,
  nowMs: number = Date.now(),
): ResidentActivityState | null {
  if (!residentSession) return null;

  const heartbeat = residentSession.last_progress_at || residentSession.updated_at || null;
  const heartbeatMs = heartbeat ? parseTimestamp(heartbeat).getTime() : NaN;
  const endedAtMs = residentSession.ended_at ? parseTimestamp(residentSession.ended_at).getTime() : NaN;
  const stale = !Number.isNaN(heartbeatMs) && nowMs - heartbeatMs > RESIDENT_HEARTBEAT_STALE_MS;
  const retryWaiting = residentSession.worker_state === 'retry_wait';

  const explicitlyEnded =
    (residentSession.status ?? '').toLowerCase() === 'ended'
    || Boolean(residentSession.stop_requested)
    || Boolean(residentSession.ended_at);

  if (explicitlyEnded) {
    const frozen = !Number.isNaN(endedAtMs)
      ? endedAtMs
      : (!Number.isNaN(heartbeatMs) ? heartbeatMs : undefined);
    return {
      phase: 'dead', durablePhase: 'ended', endMs: frozen, label: 'Session ended',
      detail: residentSession.activity?.last_action ?? undefined,
      idleRemainingSeconds: residentSession.activity?.idle_remaining_seconds,
    };
  }

  // A provider-enforced retry hold is an intentional dormant state. The worker
  // cannot heartbeat while it is not claimable, so its old progress timestamp
  // must never make the still-owned Workday session look dead.
  if (retryWaiting) {
    return {
      phase: 'idle',
      durablePhase: 'retrying',
      endMs: !Number.isNaN(heartbeatMs) ? heartbeatMs : undefined,
      label: retryWaitLabel(
        residentSession.worker_retry_reason,
        residentSession.worker_retry_until,
        nowMs,
      ),
      detail: residentSession.activity?.last_action ?? undefined,
      idleRemainingSeconds: residentSession.activity?.idle_remaining_seconds,
    };
  }

  // A freshly ensured worker can legitimately retain an old session heartbeat
  // until its first claim. Treat the backend's explicit starting state as a
  // recoverable transition, never as the contradictory "Active / Session ended".
  if (residentSession.worker_state === 'starting') {
    return {
      phase: 'idle',
      durablePhase: 'waiting',
      endMs: !Number.isNaN(heartbeatMs) ? heartbeatMs : undefined,
      label: 'Reconnecting Workday…',
      idleRemainingSeconds: residentSession.activity?.idle_remaining_seconds,
    };
  }

  // A stale ready worker is repaired by the reload ensure path. Until that
  // authoritative repair completes, make the required action explicit instead
  // of claiming the operator intentionally ended the conversation.
  if (residentSession.worker_state === 'restart_failed' || stale) {
    return {
      phase: 'dead',
      durablePhase: 'ended',
      endMs: !Number.isNaN(heartbeatMs) ? heartbeatMs : undefined,
      label: 'Workday needs restart',
      detail: residentSession.activity?.last_action ?? undefined,
      idleRemainingSeconds: residentSession.activity?.idle_remaining_seconds,
    };
  }

  const durable = residentSession.activity;
  const durablePhase = durable?.phase;
  if (
    durable
    && (
      durablePhase === 'working'
      || durablePhase === 'waiting'
      || durablePhase === 'retrying'
      || durablePhase === 'stopping'
    )
  ) {
    const phaseStartedMs = durable.phase_started_at
      ? parseTimestamp(durable.phase_started_at).getTime()
      : NaN;
    return {
      phase: durablePhase === 'working' ? 'executing' : 'idle',
      durablePhase,
      startMs: durablePhase === 'working' && !Number.isNaN(phaseStartedMs)
        ? phaseStartedMs
        : undefined,
      endMs: durablePhase === 'working' || Number.isNaN(heartbeatMs)
        ? undefined
        : heartbeatMs,
      label: {
        working: 'Working',
        waiting: 'Waiting',
        retrying: 'Retrying',
        stopping: 'Stopping soon',
      }[durablePhase],
      detail: durable.last_action ?? undefined,
      idleRemainingSeconds: durable.idle_remaining_seconds,
      pendingOperatorMessages: durable.pending_operator_messages,
      pendingWatches: durable.pending_watches,
      firedWatches: durable.fired_watches,
    };
  }

  // Session is alive — inspect the live feed for a genuinely-running command.
  const activity = computeStrategicSessionActivity(transcript, conversationMessages);
  const last = activity.items[activity.items.length - 1];
  const executing =
    Boolean(last?.inFlight)
    && EXECUTING_ACTIVITY_KINDS.has(last?.kind ?? '')
    && activity.latestLifecycle !== 'done';

  if (executing && last) {
    return {
      phase: 'executing', durablePhase: 'working', commandText: last.text,
      startMs: last.ts, label: 'Working',
    };
  }

  return {
    phase: 'idle',
    durablePhase: 'waiting',
    endMs: !Number.isNaN(heartbeatMs) ? heartbeatMs : undefined,
    label: 'Waiting for direction',
  };
}
