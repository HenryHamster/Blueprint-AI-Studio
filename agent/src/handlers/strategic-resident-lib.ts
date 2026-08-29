/**
 * Pure, dependency-free helpers for the resident strategic handler
 * (strategic-resident.ts). Kept free of any `../index.js` import so the loop
 * logic — config resolution, message-cursor advancement, operator-message
 * combination, and prompt assembly — is unit-testable in isolation
 * (strategic-resident-lib.test.ts) without mocking the agent's heavy index
 * surface.
 */

import { createHash } from 'crypto';

/**
 * Maximum base64-encoded image data length accepted in a message turn
 * (~10 MB encoded corresponds to ~7.5 MB raw). Longer payloads are silently
 * dropped to prevent excessive token usage and agent stalls.
 */
export const MAX_IMAGE_BASE64_LENGTH = 13_000_000;

/**
 * Maximum number of image attachment blocks extracted per message turn.
 * Beyond this cap, additional images are silently dropped.
 */
export const MAX_IMAGES_PER_TURN = 8;

/**
 * Maximum downloaded image bytes for URL-sourced attachments. This is aligned
 * with MAX_IMAGE_BASE64_LENGTH so a downloaded image cannot expand past the
 * model input cap after base64 encoding.
 */
export const MAX_IMAGE_DOWNLOAD_BYTES = Math.floor(MAX_IMAGE_BASE64_LENGTH * 3 / 4);

/**
 * Maximum bytes to fetch for a URL-sourced text/code file attachment.
 * Larger files are truncated to this limit before being inlined as text.
 */
export const MAX_TEXT_ATTACHMENT_BYTES = 512 * 1024; // 512 KB

/** Whole-request deadline for signed attachment headers and response bodies. */
export const ATTACHMENT_FETCH_TIMEOUT_MS = 15_000;

const MODEL_IMAGE_MEDIA_TYPES = new Set(['image/jpeg', 'image/png', 'image/gif', 'image/webp']);

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
  /** Server-computed canonical subset eligible for the typed Fast claim. */
  fast_message_ids?: string[];
  /** Exact durable Fast-claim membership when a prior process left a turn open. */
  fast_claim_groups?: Array<{
    idempotency_key?: string;
    message_ids?: string[];
    member_count?: number;
    complete?: boolean;
    artifacts_degraded?: boolean;
  }>;
  server_time?: string;
  /** Authoritative MIN(created_at) across all in-scope open user rows. */
  open_message_cursor_floor?: string | null;
  claim_required?: boolean;
}

export interface ConversationClaimResponse {
  claim_id?: string;
  message_ids?: string[];
  lease_expires_at?: string;
  attempt?: number;
  incarnation_supported?: boolean;
  incarnation_id?: string;
  resident_incarnation_id?: string;
}

export interface StrategicWatchReceipt {
  id: string;
  target_kind?: 'task';
  target_task_id?: string;
  purpose?: string;
  state?: string;
  terminal_status?: string | null;
  receipt?: {
    title?: string;
    task_type?: string;
    terminal_status?: string;
    latest_status_message?: string;
    knowledge_document_ids?: string[];
    reason?: string;
    [key: string]: unknown;
  };
}

export interface ResidentWakeBarrierReceipt {
  id: string;
  event_type?: string;
  summary?: string;
  created_at?: string;
  task_id?: string | null;
  context?: Record<string, unknown>;
}

export type ResidentActivityReasonCode =
  | 'consultation_queued'
  | 'consultation_running'
  | 'consultation_delivering';

export type ResidentAutonomyReasonCode =
  | 'authority_not_held'
  | 'foreground_call'
  | 'chat_or_disabled'
  | 'consultation_queued'
  | 'consultation_running'
  | 'consultation_delivering'
  | 'repository_unavailable';

export interface ResidentActivityUpdate {
  phase: 'working' | 'waiting';
  phase_started_at: string;
  last_action: string;
  next_review_at?: string | null;
  /** Bounded machine-readable reason for operator-facing status. Human prose
   * remains diagnostic only and must never be parsed to infer this state. */
  reason_code: ResidentActivityReasonCode | null;
  /** Server-owned public Fast reply id which correlates a request with its
   * eventual continuation. Null outside one exact consultation lifecycle. */
  consultation_id: string | null;
  /** Whether private Continuous Workday can take an autonomous turn now. */
  autonomy_state: 'active' | 'paused';
  /** Machine-readable cause when autonomy_state is paused. */
  autonomy_reason_code: ResidentAutonomyReasonCode | null;
}

export interface SessionHeartbeatResponse {
  authority_held?: boolean;
  leased_until?: string;
  stop_requested?: boolean;
  status?: string;
  conversation_claim_held?: boolean;
  resident_incarnation_held?: boolean;
  resident_incarnation_id?: string;
  admission_status?: 'legacy_grace';
  retry_after_seconds?: number;
  /** Stable fingerprint over actionable strategist-relevant transitions
   *  (operator messages, blockers/failures, terminal cycles, and a drained task
   *  frontier). Routine task heartbeat/progress writes do not change it.
   *  State-gated pacing fires an autonomous turn when this differs from the rev
   *  recorded at the last turn.
   *  ABSENT (older backend that does not populate it) degrades to sparse exploration
   *  pacing — the same forward-compat pattern `authority_held` uses. */
  work_state_rev?: string;
  /** Optional compact counts feeding the tick context (in-flight tasks, terminal
   *  transitions since last look, new operator messages). Advisory only. */
  work_state_summary?: Record<string, unknown>;
  /** Exact task-completion agenda receipts. Unlike work_state_rev (which only
   *  says the world moved), these remain present until Strategic acknowledges
   *  the result through ack-watch. */
  fired_watches?: StrategicWatchReceipt[];
  /** Exact server-authored semantic outcome receipts. A successful autonomous
   *  turn acknowledges the delivered ids through handled_message_ids. */
  fired_barriers?: ResidentWakeBarrierReceipt[];
  /** Serialized exam-ledger echoed from the heartbeat note, or null. Agents
   *  ignore this field; it is only used for roundtrip integrity checks. */
  exam_ledger?: string | null;
  /** Live direction keys (direction_key strings) in the project that are not
   *  yet retired. Used to refresh the surface catalog between restarts so
   *  new/retired directions enter/leave the rotation mid-session. */
  active_direction_ids?: string[];
  /** Refreshable direction grounding: id + text + status for every active
   *  direction. The agent updates its compaction checkpoint context from this
   *  so the resumed session sees current directions rather than session-start
   *  snapshots. Absent on older backends — agent degrades to bootstrap data. */
  direction_grounding?: Array<{id: string; text: string; status: string}>;
  /** Non-sensitive nested Call lease plus a short-lived coordinator transport. */
  call?: unknown;
  call_transport?: unknown;
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
  /**
   * Operator response-progress target (ms). Crossing this threshold only
   * updates the activity status; it never interrupts or replaces a session.
   */
  turnTimeoutMs: number;
  /** false disables the autonomous self-prompt path (e.g. a read-only monitor mode) */
  autonomousEnabled: boolean;
  /** Sparse fallback exploration interval (ms): run a model turn when no semantic
   *  event has arrived. This is distinct from polling/heartbeats and does not keep
   *  an otherwise stale continuation lease alive. Default: 4h. */
  explorationIntervalMs: number;
  /** Per-turn stall budget for AUTONOMOUS turns (ms). Deeper than the operator default
   *  so a long read/verify/investigate step isn't clipped mid-thought. Default: 300s. */
  autonomousTurnTimeoutMs: number;
  /** Max ops to batch in one autonomous turn — surfaced in the self-prompt as a hint.
   *  Default: 3. */
  maxOpsPerTurn: number;
  /** Explicit compaction threshold in input tokens. When set (>0) this overrides the
   *  model-derived threshold. When zero compaction is disabled. When absent (undefined)
   *  the threshold is derived from the model's context-window limit. */
  compactionThresholdTokens?: number;
  /** Minimum wall-clock cooldown between compactions (ms). Prevents double-compacting
   *  when consecutive turns both land above the threshold. Default: 5 min. */
  compactionCooldownMs: number;
  /** Early operator-turn acknowledgment threshold (ms). When an operator turn
   *  exceeds this duration, a durable "on it" message is posted so the operator
   *  gets a progress signal during a slow turn. 0 = disabled. Default: 10s. */
  operatorAckThresholdMs: number;
}

function num(v: unknown, fallback: number): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback;
}

function str(v: unknown, fallback = ''): string {
  return typeof v === 'string' ? v : fallback;
}

function fmtScalar(v: unknown, fallback = '?'): string {
  if (v === undefined || v === null) return fallback;
  if (typeof v === 'string' || typeof v === 'number' || typeof v === 'boolean') return String(v);
  return JSON.stringify(v);
}

export function buildRetiredContextText(
  retiredDirections: Array<Record<string, unknown>> | undefined,
): string {
  if (!retiredDirections || retiredDirections.length === 0) return '';
  const lines: string[] = [
    'These directions were recently retired, completed, or superseded. Use them to avoid reviving landed or intentionally retired work.',
  ];
  for (const d of retiredDirections) {
    const text = str(d.text, '?').slice(0, 200);
    const did = str(d.id, '?');
    const status = str(d.status, 'unknown');
    const cycles = fmtScalar(d.cycles, '?');
    const prsMerged = fmtScalar(d.prs_merged, '?');
    const extras: string[] = [];
    const retirementReason = str(d.retirement_reason).trim();
    const retirementOutcome = str(d.retirement_outcome).trim();
    if (retirementReason) extras.push(`reason=${retirementReason.slice(0, 160)}`);
    if (retirementOutcome) extras.push(`outcome=${retirementOutcome.slice(0, 160)}`);
    const tail = extras.length > 0 ? ` | ${extras.join(' | ')}` : '';
    lines.push(`- [${status}] ${text} (id: ${did}) | cycles=${cycles} | prs_merged=${prsMerged}${tail}`);
  }
  return lines.join('\n');
}

export function buildPriorDecisionsText(
  previousDecisionOutcomes: Record<string, unknown> | undefined,
): string {
  if (!previousDecisionOutcomes || typeof previousDecisionOutcomes !== 'object') return '';
  const created = Array.isArray(previousDecisionOutcomes.created)
    ? previousDecisionOutcomes.created as Array<Record<string, unknown>>
    : [];
  const retired = Array.isArray(previousDecisionOutcomes.retired)
    ? previousDecisionOutcomes.retired as Array<Record<string, unknown>>
    : [];
  if (created.length === 0 && retired.length === 0) return '';

  const lines: string[] = [];
  const cycleId = str(previousDecisionOutcomes.cycle_id);
  const summaryText = str(previousDecisionOutcomes.summary_text).trim();
  const reasoningText = str(previousDecisionOutcomes.cycle_reasoning).trim();
  const cycleBits: string[] = [];
  if (cycleId) cycleBits.push(`cycle=${cycleId}`);
  if (summaryText) cycleBits.push(summaryText.slice(0, 240));
  if (reasoningText) cycleBits.push(`reasoning=${reasoningText.slice(0, 240)}`);
  if (cycleBits.length > 0) {
    lines.push(cycleBits.join(' | '));
  }

  const renderEntry = (label: string, entry: Record<string, unknown>): string => {
    const did = str(entry.direction_id, '?');
    const status = str(entry.status, 'unknown');
    const prsMerged = fmtScalar(entry.prs_merged, '?');
    const rationale = str(entry.rationale).trim();
    const calibrationDelta = entry.calibration_delta;
    const parts = [`${label} ${did}`, `status=${status}`, `prs_merged=${prsMerged}`];
    if (rationale) parts.push(`rationale=${rationale.slice(0, 160)}`);
    if (typeof calibrationDelta === 'number' || typeof calibrationDelta === 'string') {
      parts.push(`calibration_delta=${fmtScalar(calibrationDelta, '?')}`);
    }
    return `- ${parts.join(' | ')}`;
  };

  if (created.length > 0) {
    lines.push('Created directions:');
    for (const entry of created) {
      lines.push(renderEntry('created', entry));
    }
  }
  if (retired.length > 0) {
    lines.push('Retired directions:');
    for (const entry of retired) {
      lines.push(renderEntry('retired', entry));
    }
  }
  return lines.join('\n');
}

export function buildStagedDirectionsText(
  stagedDirections: Array<Record<string, unknown>> | undefined,
): string {
  if (!stagedDirections || stagedDirections.length === 0) return '';
  const lines: string[] = [
    'These directions are already staged for a next version. Use them as queued work, not as fresh proposals.',
  ];
  for (const d of stagedDirections) {
    const did = str(d.id, '?');
    const text = str(d.text, '?').slice(0, 200);
    const priority = str(d.priority, 'unknown');
    const stagedForVersion = fmtScalar(d.staged_for_version, '?');
    const stagedAt = str(d.staged_at).trim();
    const stagedAgeDays = fmtScalar(d.staged_age_days, '?');
    const extras: string[] = [];
    if (stagedAt) extras.push(`staged_at=${stagedAt.slice(0, 64)}`);
    extras.push(`staged_age_days=${stagedAgeDays}`);
    lines.push(`- ${text} (id: ${did}) | priority=${priority} | staged_for_version=${stagedForVersion} | ${extras.join(' | ')}`);
  }
  return lines.join('\n');
}

/**
 * Resolve the resident loop's timing knobs from the strategic payload the
 * backend ships (runtime settings surfaced onto the payload). Defaults: 5s idle
 * (heartbeat) tick, 1.5s inbound-message poll (fast operator pickup), 120s
 * operator response-progress target, daily unchanged-state exploration fallback,
 * 300s autonomous stall budget, 3 ops/turn. The response-progress target is deliberately
 * non-destructive: a slow but healthy reply must remain usable.
 */
export function residentConfig(payload: Record<string, unknown>): ResidentConfig {
  const idleTickSec = num(payload.strategic_session_idle_tick_seconds, 5);
  // Match the backend's explicitly budgeted 80/minute Resident cadence. This
  // removes up to 750 ms of avoidable pickup latency for every typed turn while
  // preserving enough rate-limit headroom for one overlapping replacement.
  const messagePollSec = num(payload.strategic_session_message_poll_seconds, 0.75);
  const turnTimeoutSec = num(payload.strategic_session_turn_timeout_seconds, 120);
  const explorationIntervalSec = num(
    payload.strategic_session_exploration_interval_seconds,
    86_400,
  );
  const autonomousTurnTimeoutSec = num(payload.strategic_session_autonomous_turn_timeout_seconds, 300);
  const maxOpsPerTurn = Math.max(1, Math.round(num(payload.strategic_session_max_ops_per_turn, 3)));
  // interaction_mode === 'monitor' (or an explicit flag) makes the session
  // read-only: it still chats + heartbeats but does not autonomously advance.
  const mode = str(payload.interaction_mode).toLowerCase();
  const autonomousEnabled =
    payload.strategic_session_autonomous === false ? false : mode !== 'monitor';
  const idleTickMs = Math.max(1000, Math.round(idleTickSec * 1000));
  // Compaction threshold: explicit payload override (0 = disabled), else derived
  // from the model's context-window limit below.
  const compactionThresholdTokens = payload.strategic_session_compaction_threshold_tokens !== undefined
    ? num(payload.strategic_session_compaction_threshold_tokens, 0)
    : undefined;
  const compactionCooldownSec = num(payload.strategic_session_compaction_cooldown_seconds, 300);
  // Operator replies must be substantive; canned durable acknowledgements are disabled.
  const operatorAckThresholdMs = 0;
  return {
    idleTickMs,
    // Poll operator messages faster than the heartbeat tick so a turn starts
    // soon after send, but never below a 750ms floor (don't hammer) and never
    // slower than the idle tick.
    messagePollMs: Math.min(idleTickMs, Math.max(750, Math.round(messagePollSec * 1000))),
    turnTimeoutMs: Math.max(15_000, Math.round(turnTimeoutSec * 1000)),
    autonomousEnabled,
    // Zero disables only unchanged-state exploration. Event/state/operator
    // wakes and the first orientation turn remain immediate. Positive values
    // cannot be checked more frequently than the loop itself.
    explorationIntervalMs: explorationIntervalSec <= 0
      ? 0
      : Math.max(idleTickMs, Math.round(explorationIntervalSec * 1000)),
    autonomousTurnTimeoutMs: Math.max(15_000, Math.round(autonomousTurnTimeoutSec * 1000)),
    maxOpsPerTurn,
    compactionThresholdTokens,
    compactionCooldownMs: Math.max(60_000, Math.round(compactionCooldownSec * 1000)),
    operatorAckThresholdMs,
  };
}

// ── Compaction: context-window pressure detection ─────────────────────────

/** Per-model context-window limits (tokens). The resident session tracks
 *  cumulative input tokens and triggers compaction at 80% of the model's
 *  limit, restarting the Claude subprocess with a condensed checkpoint.
 *  Model IDs are matched by substring (case-insensitive) so short aliases
 *  like "sonnet" match "claude-sonnet-5-20251001". */
const MODEL_CONTEXT_LIMITS: ReadonlyArray<{ pattern: string; limit: number }> = [
  // Claude Opus family: 200K context window
  { pattern: 'opus', limit: 200_000 },
  // Claude Sonnet family: 200K context window
  { pattern: 'sonnet', limit: 200_000 },
  // Claude Haiku family: 128K (older), 200K (haiku-4-5+)
  { pattern: 'haiku-4-5', limit: 200_000 },
  { pattern: 'haiku', limit: 128_000 },
  // Claude Fable family: 200K
  { pattern: 'fable', limit: 200_000 },
  // Routed DeepSeek Residents have shown materially higher latency and prompt
  // growth near the generic 200K fallback. Compact conservatively before an
  // interactive turn inherits a six-figure prompt.
  { pattern: 'deepseek', limit: 128_000 },
];

/** Default context-window limit when the model is unrecognized (tokens). */
export const DEFAULT_CONTEXT_LIMIT = 200_000;

/** Fraction of the context-window limit at which compaction triggers. */
export const COMPACTION_THRESHOLD_RATIO = 0.80;

/**
 * Resolve the context-window limit for a model id string. Matches by
 * case-insensitive substring against the known patterns; falls back to
 * {@link DEFAULT_CONTEXT_LIMIT} when no pattern matches.
 */
export function modelContextLimit(model?: string): number {
  if (!model) return DEFAULT_CONTEXT_LIMIT;
  const lower = model.toLowerCase();
  for (const entry of MODEL_CONTEXT_LIMITS) {
    if (lower.includes(entry.pattern)) return entry.limit;
  }
  return DEFAULT_CONTEXT_LIMIT;
}

/**
 * Compute the compaction threshold for a model + explicit override.
 * When `explicitThreshold` is a positive number, it is used directly.
 * When it is zero, compaction is disabled (returns Infinity). When undefined,
 * the threshold is derived as {@link COMPACTION_THRESHOLD_RATIO} of the
 * model's context-window limit.
 */
export function compactionThreshold(
  model?: string,
  explicitThreshold?: number,
): number {
  if (explicitThreshold !== undefined) {
    return explicitThreshold > 0 ? explicitThreshold : Number.POSITIVE_INFINITY;
  }
  return Math.floor(modelContextLimit(model) * COMPACTION_THRESHOLD_RATIO);
}

/**
 * Format a compact directions section for the compaction checkpoint from
 * heartbeat-refreshed direction_grounding data. Outputs a simple markdown
 * list with fetch hints — the full directions_summary fields (cycles, PRs,
 * etc.) are not available from the heartbeat, so the checkpoint shows a
 * simpler view with a note to fetch full detail.
 */
export function formatDirectionGroundingText(
  directions: Array<{id: string; text: string; status: string}> | null | undefined,
): string {
  if (!directions || directions.length === 0) return '';
  const entries = directions.map(d => {
    const statusNote = d.status !== 'active' ? ` [${d.status.toUpperCase()}]` : '';
    const textNote = d.text ? ` — "${d.text.slice(0, 120)}"` : '';
    return `- \`./fetch direction ${d.id}\`${textNote}${statusNote}`;
  });
  return `(refreshed from heartbeat)\n${entries.join('\n')}`;
}

/**
 * Format strategic memory notes for the bootstrap / checkpoint prompt.
 *
 * Handles two shapes backward-compatibly:
 * - Legacy `{note, set_at}` entries: rendered as `- [set_at] note`.
 * - KnowledgeDocument index `{title, summary, topic, created_at, ...}` entries:
 *   rendered as `- [created_at] title — summary` (gracefully omits absent fields).
 *
 * Absent, non-array, or empty input degrades to an empty string without throwing.
 * Shared by the session-start bootstrap and the compaction refresh so both paths
 * format identically. Pure — safe to unit-test.
 */
export function formatStrategicMemoryNotes(raw: unknown): string {
  if (!Array.isArray(raw) || raw.length === 0) return '';
  return raw
    .filter((e): e is Record<string, unknown> => e !== null && typeof e === 'object' && !Array.isArray(e))
    .map((e) => {
      // Legacy shape: {note, set_at}
      if (typeof e.note === 'string') {
        const note = e.note.trim();
        const setAt = typeof e.set_at === 'string' ? e.set_at.trim() : '';
        if (!note) return '';
        return setAt ? `- [${setAt}] ${note}` : `- ${note}`;
      }
      // KnowledgeDocument index shape: {title, summary, topic, created_at, ...}
      const title = typeof e.title === 'string' ? e.title.trim() : '';
      const summary = typeof e.summary === 'string' ? e.summary.trim() : '';
      const createdAt = typeof e.created_at === 'string' ? e.created_at.trim() : '';
      const text = title && summary ? `${title} — ${summary}` : title || summary;
      if (!text) return '';
      return createdAt ? `- [${createdAt}] ${text}` : `- ${text}`;
    })
    .filter(Boolean)
    .join('\n');
}

/** Pretty-print a record-shaped value as 2-space JSON, or '' when absent/empty. */
function formatRecordJson(value: unknown): string {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    && Object.keys(value).length > 0
    ? JSON.stringify(value, null, 2)
    : '';
}

/** Format the backend-resolved effective release policy from a bootstrap payload. */
export function formatCurrentEffectivePolicyJson(payload: Record<string, unknown>): string {
  return formatRecordJson(payload.current_effective_policy);
}

/**
 * The response of GET
 * `/api/agent/projects/{id}/strategic-session/checkpoint-state`. Read-only
 * mirror of the backend contract — do NOT extend without changing the endpoint.
 */
export interface CheckpointStateResponse {
  problem_model?: unknown;
  strategic_memory_notes?: unknown;
  running_update?: unknown;
  effective_purpose?: unknown;
  /** Fresh project-aware release policy resolved at compaction time. */
  current_effective_policy?: unknown;
  /** Current open operator messages (same shape as the bootstrap payload's
   *  open_messages). Present on backends that have shipped this field; absent
   *  on older responses — caller degrades gracefully. */
  open_messages?: unknown;
  /** Recently-answered operator conversation tail (status handled/resolved).
   *  Present on backends that have shipped this field; absent on older
   *  responses — caller degrades gracefully. */
  recent_handled_conversation?: unknown;
}

/**
 * Resolve the resident's project-purpose grounding from the bootstrap payload.
 *
 * Binds to `effective_purpose` — the single intent field the backend actually
 * emits (the legacy `vision` body was removed in #16973). Never read
 * `payload.vision` here: the backend no longer ships it, so a vision-bound read
 * would render a silently-empty grounding section that only activates when a
 * stale backend still carries the old key. Returns a trimmed string, or '' when
 * the payload carries no purpose (callers log + render an explicit marker).
 */
export function resolveResidentPurposeText(payload: Record<string, unknown>): string {
  return typeof payload.effective_purpose === 'string' ? payload.effective_purpose.trim() : '';
}

/** The self-model grounding fields the compaction checkpoint carries. */
export interface CheckpointGrounding {
  problemModelJson: string;
  /** Current project purpose text (payload.effective_purpose). */
  purposeText: string;
  strategicMemoryNotesText: string;
  runningUpdate: string;
  /** Pretty-printed current_effective_policy, when the backend supplies it. */
  currentEffectivePolicyJson?: string;
  /** Formatted open operator messages text (from open_messages). Absent when
   *  not yet computed (older path) or when there are no open messages. */
  openMessagesText?: string;
  /** Formatted recently-answered operator conversation tail (from
   *  recent_handled_conversation). Absent when not yet computed (older path) or
   *  when there is no recent handled conversation. */
  recentHandledConversationText?: string;
}

/**
 * Map a checkpoint-state endpoint response into the refreshed grounding fields,
 * formatted identically to the session-start bootstrap (problem_model → pretty
 * JSON, effective_purpose → trimmed text, notes → shared formatter,
 * running_update → string). Tolerant of missing / mistyped keys so a partial
 * response never throws.
 *
 * Note: `openMessagesText` is left empty here because the formatting function
 * (`buildOpenMessagesText`) lives in strategic-sections.ts which imports the
 * heavy index surface. Callers in strategic-resident.ts fill this field after
 * calling formatCheckpointState by formatting `resp.open_messages` directly.
 */
export function formatCheckpointState(resp: CheckpointStateResponse): CheckpointGrounding {
  return {
    problemModelJson: formatRecordJson(resp.problem_model),
    purposeText: typeof resp.effective_purpose === 'string' ? resp.effective_purpose.trim() : '',
    strategicMemoryNotesText: formatStrategicMemoryNotes(resp.strategic_memory_notes),
    runningUpdate: typeof resp.running_update === 'string' ? resp.running_update : '',
    currentEffectivePolicyJson: formatRecordJson(resp.current_effective_policy),
    openMessagesText: '',
    recentHandledConversationText: '',
  };
}

/**
 * Resolve the grounding sub-fields for the compaction checkpoint, preferring
 * grounding refreshed at compaction time over the session-start snapshot.
 *
 * When `refreshed` is absent (the checkpoint-state fetch failed or never ran)
 * the snapshot is used wholesale. When present, the fields resolve with a
 * DELIBERATE ASYMMETRY — do NOT "simplify" this back into a whole-object
 * `refreshed ?? snapshot` switch:
 *
 *  - STRUCTURAL fields (problemModelJson, purposeText, strategicMemoryNotesText,
 *    runningUpdate) fall back FIELD-BY-FIELD: a refreshed value that is
 *    empty/blank is treated as a transient degraded server read (e.g.
 *    _agent_view_problem_model formatting to empty) and the session-start
 *    snapshot value is used instead. Losing the problem model or purpose to a
 *    momentary empty refetch is a coherence loss we refuse to take; a genuine
 *    mid-session edit still refreshes because it returns a non-empty value.
 *  - openMessagesText and recentHandledConversationText are the EXCEPTION: a
 *    refreshed EMPTY value must WIN over the stale bootstrap snapshot. Messages
 *    the operator's turn just handled should DISAPPEAR from the open feed (and
 *    appear in the fresh recently-answered tail), so here empty-refreshed beats
 *    snapshot rather than falling back to it.
 *
 * Empty strings collapse to `undefined` so the checkpoint omits that
 * sub-section, matching the existing `refreshedDirectionGrounding` fallback.
 */
export function resolveCheckpointGrounding(
  snapshot: CheckpointGrounding,
  refreshed: CheckpointGrounding | null | undefined,
): {
  problemModelJson?: string;
  purposeText?: string;
  strategicMemoryNotesText?: string;
  runningUpdate?: string;
  currentEffectivePolicyJson?: string;
  openMessagesText?: string;
  recentHandledConversationText?: string;
} {
  // No refetch → whole-snapshot fallback (unchanged behavior).
  if (refreshed == null) {
    return {
      problemModelJson: snapshot.problemModelJson || undefined,
      purposeText: snapshot.purposeText || undefined,
      strategicMemoryNotesText: snapshot.strategicMemoryNotesText || undefined,
      runningUpdate: snapshot.runningUpdate || undefined,
      currentEffectivePolicyJson: snapshot.currentEffectivePolicyJson || undefined,
      openMessagesText: snapshot.openMessagesText || undefined,
      recentHandledConversationText: snapshot.recentHandledConversationText || undefined,
    };
  }
  // Refetch succeeded. Structural fields fall back to the snapshot when the
  // refreshed value is blank (transient degraded read); openMessagesText lets a
  // refreshed empty WIN (see the asymmetry note above).
  const structural = (
    refreshedVal: string | undefined,
    snapshotVal: string | undefined,
  ): string | undefined =>
    (refreshedVal && refreshedVal.trim() ? refreshedVal : snapshotVal) || undefined;
  return {
    problemModelJson: structural(refreshed.problemModelJson, snapshot.problemModelJson),
    purposeText: structural(refreshed.purposeText, snapshot.purposeText),
    strategicMemoryNotesText: structural(refreshed.strategicMemoryNotesText, snapshot.strategicMemoryNotesText),
    runningUpdate: structural(refreshed.runningUpdate, snapshot.runningUpdate),
    currentEffectivePolicyJson: structural(
      refreshed.currentEffectivePolicyJson,
      snapshot.currentEffectivePolicyJson,
    ),
    openMessagesText: refreshed.openMessagesText || undefined,
    // Same rule as openMessagesText: the refreshed recently-answered tail is the
    // current source of truth, so let it WIN over the session-start snapshot
    // (including when a successful refetch returns an empty tail).
    recentHandledConversationText: refreshed.recentHandledConversationText || undefined,
  };
}

export interface CompactionCheckpointContext {
  /** @deprecated Identity is process-level; accepted only for old callers and never rendered. */
  roleIdentity?: string;
  /** Current exam ledger serialized as JSON. */
  examLedgerJson: string;
  /** Human-readable summary of recent operator interactions and key decisions. */
  recentInteractions: string;
  /** Current autonomous state snapshot (agenda, focus rotation, open leads). */
  autonomousState: string;
  // ── Durable self-model grounding (carried from the bootstrap prompt) ──────
  // These fields deliver the same grounding the bootstrap delivers on a fresh
  // start so the restarted session does NOT re-derive settled facts, re-read
  // settled directions, or re-propose corrections the operator already made.
  /** Current problem model JSON (payload.problem_model). */
  problemModelJson?: string;
  /** Current project purpose text (payload.effective_purpose). */
  purposeText?: string;
  /** Formatted standing behavioral lessons (payload.strategic_memory_notes). */
  strategicMemoryNotesText?: string;
  /** Formatted open operator messages text (payload.open_messages). */
  openMessagesText?: string;
  /** Formatted recently-answered operator conversation tail
   *  (payload.recent_handled_conversation). Read-only, non-actionable. */
  recentHandledConversationText?: string;
  /** Prior session context text (ctx._session_summary). */
  priorSessionContextText?: string;
  /** Formatted active directions summary (payload.directions_summary). */
  directionsText?: string;
  /** Running update text (payload.running_update). */
  runningUpdate?: string;
  /** Formatted decisions register text (durable cross-tick decision memory). */
  decisionsRegisterText?: string;
  /** Backend-resolved release policy projection carried as a labeled snapshot. */
  currentEffectivePolicyJson?: string;
  // ── Static session-start grounding (same snapshot forward every checkpoint) ─
  // These mirror three bootstrap sections that would otherwise vanish after a
  // compaction restart, letting the resumed session re-derive settled facts or
  // re-propose already-retired/staged directions. They are session-start values
  // (not server-mutable grounding), so no refetch is needed — the same snapshot
  // carries forward on every checkpoint.
  /** Formatted recently retired / completed directions (buildRetiredContextText). */
  retiredDirectionsText?: string;
  /** Formatted already-staged (next-version) directions (buildStagedDirectionsText). */
  stagedDirectionsText?: string;
  /** Formatted prior-cycle decision outcomes (buildPriorDecisionsText). */
  previousDecisionOutcomesText?: string;
  /**
   * True when the self-model grounding was re-fetched from the checkpoint-state
   * endpoint at compaction time (so it reflects current, not session-start,
   * state). Softens the "session-start snapshot" note. Defaults to false.
   */
  groundingRefreshed?: boolean;
  // ── Session-static bootstrap sections (operator purpose + tool grounding) ──
  // These are NOT server-mutable mid-session so they never go through the
  // checkpoint-state refresh path — the same session-start values carry forward
  // on every checkpoint.  Without them a compacted session loses the operator
  // purpose/constraints preamble, project skills, and tool-content context that
  // every autonomous tick starts with.
  /** Operator purpose, current focus, and constraints preamble (buildContextPrefix). */
  contextPrefix?: string;
  /** Formatted project skills from `.blueprint/tools.json`. */
  skillsText?: string;
  /** Formatted built-in tool content (operator notes, whiteboard). */
  toolContentText?: string;
}

function buildCurrentEffectivePolicySection(currentEffectivePolicyJson: string): string {
  return `## Current effective release policy
The backend-resolved \`current_effective_policy\` below is authoritative for this snapshot. It takes precedence over historical attempt snapshots, prior summaries, repository/on-disk policy files, and source-code defaults. If a newer \`./fetch qualification\` or \`./fetch version-phase\` returns a different \`current_effective_policy\`, the newer projection wins.

\`\`\`json
${currentEffectivePolicyJson.trim()}
\`\`\``;
}

/**
 * Build the compaction checkpoint prompt — a condensed context package
 * delivered to a FRESH subprocess so the session continues coherently
 * after a context-window compaction restart.
 *
 * This is NOT the full bootstrap prompt. The canonical role is supplied again
 * through the fresh subprocess's system prompt; this user turn carries state
 * only so identity is neither duplicated nor weakened after compaction.
 *
 * Pure and unit-testable — no imports from index.js.
 */
export function buildCompactionCheckpoint(ctx: CompactionCheckpointContext): string {
  const sections: string[] = [];

  // The fresh subprocess already has the canonical process-level identity.
  sections.push(`# Session checkpoint — compaction restart

The resident session's Claude subprocess was restarted because the context
window approached its limit. This is a CHECKPOINT restore, not a fresh start.
Your previous session was compacted. Continue operating exactly where you left off.
This internal restore turn is not an operator message. Do not call reply_to_operator
or send any user-facing message while absorbing this checkpoint.`);

  // (ii) Exam ledger state.
  if (ctx.examLedgerJson.trim()) {
    sections.push(`## Examination ledger (carried forward)
\`\`\`json
${ctx.examLedgerJson.trim()}
\`\`\``);
  }

  // (iii) Recent operator interactions and decisions.
  if (ctx.recentInteractions.trim()) {
    sections.push(`## Recent context (carried forward)
${ctx.recentInteractions.trim()}`);
  }

  // (iv) Autonomous state.
  if (ctx.autonomousState.trim()) {
    sections.push(`## Current state
${ctx.autonomousState.trim()}`);
  }

  // (v) Durable self-model grounding — preserved from the bootstrap so the
  // restarted session retains its identity, problem context, and operator
  // relationship rather than re-deriving settled facts across the restart.
  const grounding: string[] = [];
  if (ctx.problemModelJson?.trim()) {
    grounding.push(`### Problem model\n\`\`\`json\n${ctx.problemModelJson.trim()}\n\`\`\``);
  }
  if (ctx.directionsText?.trim()) {
    grounding.push(`### Active directions\n${ctx.directionsText.trim()}`);
  }
  if (ctx.purposeText?.trim()) {
    grounding.push(`### Vision (project purpose)\n${ctx.purposeText.trim()}`);
  } else if (ctx.purposeText !== undefined) {
    grounding.push(`### Vision (project purpose)\n_(no project purpose on record — the operator has not seeded intent.purpose / user_purpose. Ground decisions on the problem model and directions above instead.)_`);
  }
  if (ctx.openMessagesText?.trim()) {
    grounding.push(`### Open operator messages\n${ctx.openMessagesText.trim()}`);
  }
  if (ctx.strategicMemoryNotesText?.trim()) {
    grounding.push(`### Standing behavioral lessons\n${ctx.strategicMemoryNotesText.trim()}`);
  }
  if (ctx.priorSessionContextText?.trim()) {
    grounding.push(`### Prior session context\n${ctx.priorSessionContextText.trim()}`);
  }
  if (ctx.runningUpdate?.trim()) {
    grounding.push(`### Running update\n${ctx.runningUpdate.trim()}`);
  }
  if (grounding.length > 0) {
    if (ctx.groundingRefreshed) {
      const note = `> **Refreshed note:** The data below was re-fetched from current project
> state at compaction time. If in doubt you can still confirm via
> \`./fetch problem-model\`, \`./fetch running-update\`, \`./fetch direction <id>\`,
> and \`./fetch message\`.`;
      sections.push(`## Self-model grounding (refreshed at compaction)\n${note}\n\n${grounding.join('\n\n')}`);
    } else {
      const note = `> **Snapshot note:** The data below was captured when the session started
> and may not reflect current project state. Use \`./fetch problem-model\`,
> \`./fetch running-update\`, \`./fetch direction <id>\`, and \`./fetch message\` to retrieve
> the latest state.`;
      sections.push(`## Self-model grounding (session-start snapshot)\n${note}\n\n${grounding.join('\n\n')}`);
    }
  }

  if (ctx.currentEffectivePolicyJson?.trim()) {
    sections.push(buildCurrentEffectivePolicySection(ctx.currentEffectivePolicyJson));
  }

  // (v-b) Recently-answered operator conversation tail — READ-ONLY. These
  // messages are already handled/resolved; they are here only so a correction
  // the operator made that was never captured as an OPERATOR-DECISION survives
  // this restart. Do NOT re-answer or re-act on them.
  if (ctx.recentHandledConversationText?.trim()) {
    sections.push(`## Recently answered operator conversation (read-only — already handled, do NOT re-answer)
If any of these settled or corrected a course but is NOT already reflected in your decisions register above, treat it as settled — do not re-propose it.

${ctx.recentHandledConversationText.trim()}`);
  }

  // (vi) Durable decisions register — preserved across restarts.
  if (ctx.decisionsRegisterText?.trim()) {
    sections.push(ctx.decisionsRegisterText.trim());
  }

  // (vii) Static session-start grounding the bootstrap renders but the
  // checkpoint previously dropped — so the resumed session does NOT re-derive
  // settled facts or re-propose already-retired/staged directions. Wording
  // mirrors buildBootstrapPrompt exactly.
  if (ctx.stagedDirectionsText?.trim()) {
    sections.push(`## Already-staged (next-version) directions\n${ctx.stagedDirectionsText.trim()}`);
  }
  if (ctx.retiredDirectionsText?.trim()) {
    sections.push(`## Recently retired / completed directions\n${ctx.retiredDirectionsText.trim()}`);
  }
  if (ctx.previousDecisionOutcomesText?.trim()) {
    sections.push(`## Prior-cycle decision outcomes\n${ctx.previousDecisionOutcomesText.trim()}`);
  }

  // (viii) Session-static bootstrap sections — operator purpose + tool grounding.
  // These mirror the bootstrap sections that carry operator purpose, constraints,
  // project skills, and tool content.  Without them a post-compaction session
  // operates without foundational operator grounding every tick normally sees.
  if (ctx.contextPrefix?.trim()) {
    sections.push(ctx.contextPrefix.trim());
  }
  if (ctx.skillsText?.trim()) {
    sections.push(`## Project Skills\n${ctx.skillsText.trim()}`);
  }
  if (ctx.toolContentText?.trim()) {
    sections.push(`## Project Tool Content
(Live content the operator jotted in this project's built-in tools — notes and whiteboard sketches. Treat it as operator input, not authoritative state. Pull the latest with \`./fetch tools\` if you need to re-read it mid-session.)

${ctx.toolContentText.trim()}`);
  }

  sections.push(`Resume from this checkpoint. Your next turn will arrive as a
normal operator or autonomous prompt — this checkpoint is informational only.`);

  return sections.join('\n\n');
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
/**
 * One backend poll page is one atomic operator turn. Keeping the cap equal to
 * the page size lets handled rows fall out of the next `status=open` query, so
 * even 201 messages sharing one timestamp drain without a time-only-cursor stall.
 */
export const OPERATOR_MESSAGE_BATCH_CAP = MESSAGE_POLL_LIMIT;
/** Generated helpers bound receipt reads to 16KiB; one page fits with ample headroom. */
export const RESIDENT_REPLY_RECEIPT_MAX_BYTES = 16_384;
/** Safety bound on re-polls within one poll cycle (drains a backlog, never loops forever). */
export const MAX_POLL_PAGES = 100;

/**
 * The high-water cursor for the inbound-message poll. Prefer the session's
 * persisted `message_cursor`; else start a short window in the past so a message
 * sent moments before the session started (often the one that triggered it) is
 * still picked up on the first poll.
 *
 * A fresh worker can also be handed older, still-open operator messages in its
 * bootstrap payload. Rewind to the earliest of those explicit rows so the
 * canonical status=open poll can admit and claim them. Merely rendering them in
 * the bootstrap prompt is insufficient in on-demand mode because Workday does
 * not run a turn until a message is admitted.
 */
export function rewindCursorToOpenMessageFloor(
  cursor: string,
  rawFloor: unknown,
): string {
  const floor = str(rawFloor);
  const cursorMs = Date.parse(cursor);
  const floorMs = Date.parse(floor);
  if (Number.isNaN(floorMs)) return cursor;
  if (Number.isNaN(cursorMs) || floorMs < cursorMs) return floor;
  return cursor;
}

export interface OpenMessageFloorTraversal {
  cursor: string;
  traversedFloor: string | null;
  rewound: boolean;
}

/**
 * Apply a live authoritative open-message floor without oscillating between
 * that floor and the end of every full page. `traversedFloor` is local to one
 * paginated poll traversal: once a request began at or before an advertised
 * floor, later pages with that same (or a newer) floor are already covered.
 * A newly advertised older floor still forces an immediate rewind.
 */
export function applyOpenMessageCursorFloor(
  cursor: string,
  rawFloor: unknown,
  traversedFloor: string | null,
): OpenMessageFloorTraversal {
  const floor = str(rawFloor);
  const floorMs = Date.parse(floor);
  if (Number.isNaN(floorMs)) {
    return { cursor, traversedFloor, rewound: false };
  }

  const traversedMs = traversedFloor === null
    ? Number.NaN
    : Date.parse(traversedFloor);
  const alreadyTraversed = !Number.isNaN(traversedMs) && traversedMs <= floorMs;
  const rewoundCursor = rewindCursorToOpenMessageFloor(cursor, floor);
  if (rewoundCursor !== cursor && !alreadyTraversed) {
    return { cursor: rewoundCursor, traversedFloor: floor, rewound: true };
  }

  // If this request started at/before the floor, it has traversed that floor
  // even when no rewind was necessary (the bootstrap cursor commonly equals
  // the floor exactly). Preserve the earliest floor covered in this traversal.
  const cursorMs = Date.parse(cursor);
  const requestCoveredFloor = !Number.isNaN(cursorMs) && cursorMs <= floorMs;
  const nextTraversedFloor = requestCoveredFloor && !alreadyTraversed
    ? floor
    : traversedFloor;
  return { cursor, traversedFloor: nextTraversedFloor, rewound: false };
}

export function initialCursor(
  task: Record<string, unknown>,
  payload: Record<string, unknown>,
  now: number = Date.now(),
): string {
  const ctx = task.context && typeof task.context === 'object'
    ? task.context as Record<string, unknown>
    : {};
  const fromCtx = str(ctx.message_cursor);
  const fromPayload = str(payload.message_cursor);
  const base = fromCtx || fromPayload || new Date(now - 2 * 60_000).toISOString();
  let earliest = base;
  let earliestMs = Date.parse(base);
  if (Number.isNaN(earliestMs)) earliestMs = Infinity;
  // The bounded open_messages bootstrap can omit an older ordinary row when a
  // burst of higher-priority critical rows fills the pack. The backend ships an
  // independent authoritative MIN(open operator created_at) in both task
  // context and payload so mixed-version/recovery paths can still rewind far
  // enough to retrieve that omitted row from the paginated poll endpoint.
  const cursorFloors: unknown[] = [
    str(ctx.open_message_cursor_floor),
    str(payload.open_message_cursor_floor),
  ];
  for (const floor of cursorFloors) {
    const rewound = rewindCursorToOpenMessageFloor(earliest, floor);
    if (rewound !== earliest) {
      earliest = rewound;
      earliestMs = Date.parse(rewound);
    }
  }
  const openMessages = Array.isArray(payload.open_messages) ? payload.open_messages : [];
  for (const raw of openMessages) {
    if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) continue;
    const createdAt = str((raw as Record<string, unknown>).created_at);
    const createdAtMs = Date.parse(createdAt);
    if (!Number.isNaN(createdAtMs) && createdAtMs < earliestMs) {
      earliest = createdAt;
      earliestMs = createdAtMs;
    }
  }
  return earliest;
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

  delete(id: string): boolean {
    if (!this.set.delete(id)) return false;
    const index = this.order.indexOf(id);
    if (index >= 0) this.order.splice(index, 1);
    return true;
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

/**
 * Admit at most `remaining` unseen rows from one page. Only admitted ids enter
 * the global seen-set. `fullyAdmitted=false` means a deferred unseen row was
 * encountered, so callers must not advance their cursor across the whole page.
 */
export function admitMessageBatchPage(
  msgs: ResidentInboundMessage[] | null | undefined,
  seen: BoundedSeenSet,
  remaining: number,
  remainingRequiredImages: number = MAX_IMAGES_PER_TURN,
): {
  admitted: ResidentInboundMessage[];
  fullyAdmitted: boolean;
  requiredImagesAdmitted: number;
} {
  const admitted: ResidentInboundMessage[] = [];
  const capacity = Math.max(0, Math.floor(remaining));
  const requiredImageCapacity = Math.max(0, Math.floor(remainingRequiredImages));
  let requiredImagesAdmitted = 0;
  for (const message of Array.isArray(msgs) ? msgs : []) {
    const id = typeof message.id === 'string' ? message.id : '';
    if (id && seen.has(id)) continue;
    if (admitted.length >= capacity) {
      return { admitted, fullyAdmitted: false, requiredImagesAdmitted };
    }
    const requiredImages = requiredArtifactImageCount(message);
    if (requiredImagesAdmitted + requiredImages > requiredImageCapacity) {
      return { admitted, fullyAdmitted: false, requiredImagesAdmitted };
    }
    if (id) seen.add(id);
    admitted.push(message);
    requiredImagesAdmitted += requiredImages;
  }
  return { admitted, fullyAdmitted: true, requiredImagesAdmitted };
}

/** Count exact-revision ArtifactRef images that must survive into the model turn. */
export function requiredArtifactImageCount(message: ResidentInboundMessage): number {
  const attachments = message.context?.attachments;
  if (!Array.isArray(attachments)) return 0;
  return attachments.filter((attachment) => {
    if (!attachment || typeof attachment !== 'object' || Array.isArray(attachment)) return false;
    const record = attachment as Record<string, unknown>;
    return str(record.type) === 'image' && str(record.artifact_ref_id).trim().length > 0;
  }).length;
}

export function rollbackMessageBatchAdmission(
  seen: BoundedSeenSet,
  cursorBefore: string,
  messages: ResidentInboundMessage[],
): string {
  for (const message of messages) {
    const id = typeof message.id === 'string' ? message.id : '';
    if (id) seen.delete(id);
  }
  return cursorBefore;
}

/** Content block types for multimodal message delivery. */
export interface TextBlock { type: 'text'; text: string; }
export interface ImageBlockBase64Source { type: 'base64'; media_type: string; data: string; }
export interface ImageBlockUrlSource {
  type: 'url';
  url: string;
  media_type: string;
  name?: string;
  artifact_ref_id?: string;
  image_checksum?: string;
}
export type ImageBlockSource = ImageBlockBase64Source | ImageBlockUrlSource;
export interface ImageBlock { type: 'image'; source: ImageBlockSource; }
/** Internal transport block: a text/code file to be fetched from a signed GCS URL by materializeContentBlocks. */
export interface TextUrlBlock { type: 'text_url'; url: string; name: string; mime_type: string; }
export type ContentBlock = TextBlock | ImageBlock | TextUrlBlock;

const TEXT_LIKE_APP_MIMES = new Set([
  'application/json', 'application/javascript', 'application/xml',
  'application/yaml', 'application/x-yaml', 'application/toml',
  'application/sql', 'application/x-sh', 'application/x-shellscript',
]);

/**
 * Whether a MIME type represents human-readable text that can be decoded and presented inline.
 */
function isTextLikeMime(mimeType: string): boolean {
  if (typeof mimeType !== 'string') return false;
  if (mimeType.startsWith('text/')) return true;
  return TEXT_LIKE_APP_MIMES.has(mimeType.split(';')[0].trim().toLowerCase());
}

/**
 * Build a one-line reference block for an opaque (non-text, non-image) file attachment.
 */
function buildFileReference(att: Record<string, unknown>): string {
  const name = str(att.name, 'file');
  const mimeType = str(att.mime_type, 'application/octet-stream');
  const sizeBytes = typeof att.size_bytes === 'number' ? att.size_bytes : null;
  const sizeStr = sizeBytes !== null ? `, ${sizeBytes} bytes` : '';
  return `[File attachment: ${name} (${mimeType}${sizeStr})]`;
}
export interface AttachmentFetchResponse {
  ok: boolean;
  status: number;
  headers: { get(name: string): string | null };
  arrayBuffer(): Promise<ArrayBuffer>;
}

export interface AttachmentFetchInit {
  signal?: AbortSignal;
}

export type AttachmentFetch = (
  url: string,
  init?: AttachmentFetchInit,
) => Promise<AttachmentFetchResponse>;

class AttachmentFetchTimeoutError extends Error {
  constructor() {
    super('attachment download timed out');
    this.name = 'AttachmentFetchTimeoutError';
  }
}

async function withAttachmentFetchDeadline<T>(
  url: string,
  fetchImpl: AttachmentFetch,
  consume: (response: AttachmentFetchResponse) => Promise<T>,
): Promise<T> {
  const controller = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const deadline = new Promise<never>((_resolve, reject) => {
    timer = setTimeout(() => {
      controller.abort();
      reject(new AttachmentFetchTimeoutError());
    }, ATTACHMENT_FETCH_TIMEOUT_MS);
  });
  try {
    const response = await Promise.race([
      fetchImpl(url, { signal: controller.signal }),
      deadline,
    ]);
    return await Promise.race([consume(response), deadline]);
  } finally {
    if (timer !== undefined) clearTimeout(timer);
  }
}

export class RetryableArtifactAttachmentError extends Error {
  readonly retryable = true;

  constructor(readonly artifactRefId: string, reason: string) {
    super(`Required ArtifactRef ${artifactRefId} could not be loaded: ${reason}`);
    this.name = 'RetryableArtifactAttachmentError';
  }
}

export function isRetryableArtifactAttachmentError(
  error: unknown,
): error is RetryableArtifactAttachmentError {
  return error instanceof RetryableArtifactAttachmentError;
}

function normalizeImageMediaType(value: unknown): string {
  const mediaType = str(value).split(';', 1)[0].trim().toLowerCase();
  return MODEL_IMAGE_MEDIA_TYPES.has(mediaType) ? mediaType : '';
}

function attachmentName(value: unknown): string {
  return str(value).trim().slice(0, 200);
}

export function isAllowedAttachmentDownloadUrl(value: string): boolean {
  try {
    const url = new URL(value);
    if (url.protocol !== 'https:') return false;
    if (url.username || url.password || url.port) return false;
    const host = url.hostname.toLowerCase();
    const gcsHost = host === 'storage.googleapis.com' || host.endsWith('.storage.googleapis.com');
    if (!gcsHost) return false;
    const signatures = url.searchParams.getAll('X-Goog-Signature');
    return signatures.length === 1 && signatures[0].trim().length > 0;
  } catch {
    return false;
  }
}

function attachmentUnavailableBlock(source: ImageBlockUrlSource, reason: string): TextBlock {
  const label = source.name ? ` "${source.name}"` : '';
  return { type: 'text', text: `[Attached image${label} could not be loaded: ${reason}.]` };
}

function requiredArtifactIdentity(
  attachment: Record<string, unknown>,
): { artifactRefId: string; imageChecksum: string } | null {
  const artifactRefId = str(attachment.artifact_ref_id).trim();
  if (!artifactRefId) return null;
  const imageChecksum = str(attachment.image_checksum).trim().toLowerCase();
  if (!/^[a-f0-9]{64}$/.test(imageChecksum)) {
    throw new RetryableArtifactAttachmentError(
      artifactRefId,
      'missing or invalid image checksum',
    );
  }
  return { artifactRefId, imageChecksum };
}

/**
 * Extract message content as content blocks (text + image attachments from
 * context.attachments). Images can arrive as inline base64 data or as GCS
 * signed URLs (the scalable path for operator-uploaded files). Non-image
 * attachments with text/* MIME types are decoded and emitted as text blocks;
 * opaque files (PDF, binary, URL-only) are emitted as a one-line reference
 * block. Text-only messages produce a single text block.
 */
export function extractMessageTurn(msg: ResidentInboundMessage): ContentBlock[] {
  const blocks: ContentBlock[] = [];
  const text = str(msg.content).trim();
  if (text) {
    blocks.push({ type: 'text', text });
  }
  if (msg.context) {
    const raw = msg.context.attachments;
    if (Array.isArray(raw)) {
      const attachments = raw as Array<Record<string, unknown>>;
      const requiredImageIds = attachments
        .filter(attachment => str(attachment.type) === 'image')
        .map(attachment => str(attachment.artifact_ref_id).trim())
        .filter(Boolean);
      if (requiredImageIds.length > MAX_IMAGES_PER_TURN) {
        throw new RetryableArtifactAttachmentError(
          requiredImageIds[0],
          'required image attachment limit exceeded',
        );
      }
      const ordinaryImageBudget = MAX_IMAGES_PER_TURN - requiredImageIds.length;
      let imageCount = 0;
      let ordinaryImageCount = 0;
      for (const att of attachments) {
        if (str(att.type) === 'image') {
          const requiredArtifact = requiredArtifactIdentity(att);
          if (!requiredArtifact && ordinaryImageCount >= ordinaryImageBudget) {
            continue;
          }
          if (imageCount >= MAX_IMAGES_PER_TURN) {
            if (requiredArtifact) {
              throw new RetryableArtifactAttachmentError(
                requiredArtifact.artifactRefId,
                'image attachment limit exceeded',
              );
            }
            continue;
          }
          const mediaType = normalizeImageMediaType(att.mime_type);
          if (!mediaType) {
            if (requiredArtifact) {
              throw new RetryableArtifactAttachmentError(
                requiredArtifact.artifactRefId,
                'invalid image media type',
              );
            }
            continue;
          }
          if (requiredArtifact) {
            const url = typeof att.url === 'string' ? att.url.trim() : '';
            if (!url || !isAllowedAttachmentDownloadUrl(url)) {
              throw new RetryableArtifactAttachmentError(
                requiredArtifact.artifactRefId,
                'unsupported download URL',
              );
            }
            blocks.push({
              type: 'image',
              source: {
                type: 'url',
                url,
                media_type: mediaType,
                name: attachmentName(att.name) || undefined,
                artifact_ref_id: requiredArtifact.artifactRefId,
                image_checksum: requiredArtifact.imageChecksum,
              },
            });
            imageCount++;
            continue;
          }
          // Inline base64 data — preferred when available.
          if (typeof att.data === 'string' && att.data && att.data.length <= MAX_IMAGE_BASE64_LENGTH) {
            blocks.push({
              type: 'image',
              source: { type: 'base64', media_type: mediaType, data: att.data },
            });
            imageCount++;
            ordinaryImageCount++;
          } else if (typeof att.url === 'string' && att.url) {
            const url = att.url.trim();
            // GCS signed URL — fallback when data is absent or oversized. The URL
            // is an internal transport detail; materializeContentBlocks fetches it
            // and converts it to base64 before the model sees the turn.
            if (isAllowedAttachmentDownloadUrl(url)) {
              blocks.push({
                type: 'image',
                source: {
                  type: 'url',
                  url,
                  media_type: mediaType,
                  name: attachmentName(att.name) || undefined,
                },
              });
              imageCount++;
              ordinaryImageCount++;
            } else {
              blocks.push(attachmentUnavailableBlock(
                { type: 'url', url: '', media_type: mediaType, name: attachmentName(att.name) || undefined },
                'unsupported download URL',
              ));
          }
          }
        } else {
          // Non-image attachment: decode text-like content or emit a reference block.
          const attData = typeof att.data === 'string' ? att.data : '';
          const mimeType = str(att.mime_type);
          if (attData && isTextLikeMime(mimeType)) {
            try {
              const decoded = Buffer.from(attData, 'base64').toString('utf-8');
              const name = str(att.name, 'file');
              blocks.push({ type: 'text', text: `[Attachment: ${name}]\n${decoded}` });
            } catch {
              blocks.push({ type: 'text', text: buildFileReference(att) });
            }
          } else if (!attData && typeof att.url === 'string' && att.url && isTextLikeMime(mimeType)) {
            // URL-only text/code file (uploaded to GCS) — defer fetch to materializeContentBlocks.
            const url = att.url.trim();
            if (isAllowedAttachmentDownloadUrl(url)) {
              blocks.push({ type: 'text_url', url, name: str(att.name, 'file'), mime_type: mimeType });
            } else {
              blocks.push({ type: 'text', text: buildFileReference(att) });
            }
          } else {
            blocks.push({ type: 'text', text: buildFileReference(att) });
          }
        }
      }
    }
  }
  return blocks;
}

/**
 * Convert internal URL-sourced image blocks into model-ready base64 image
 * blocks. Signed URLs are never forwarded into the resident model input.
 * Ordinary download failures degrade to a text notice. Required ArtifactRef
 * failures throw so the operator message can be retried without a model turn.
 */
export async function materializeContentBlocks(
  blocks: ContentBlock[],
  fetchImpl: AttachmentFetch = async (url: string, init?: AttachmentFetchInit) => {
    if (typeof globalThis.fetch !== 'function') throw new Error('fetch unavailable');
    return globalThis.fetch(url, { signal: init?.signal });
  },
): Promise<ContentBlock[]> {
  const result: ContentBlock[] = [];
  for (const block of blocks) {
    if (block.type === 'text_url') {
      if (!isAllowedAttachmentDownloadUrl(block.url)) {
        result.push({ type: 'text', text: `[Attachment "${block.name}" could not be loaded: unsupported download URL.]` });
        continue;
      }
      try {
        const materialized = await withAttachmentFetchDeadline(
          block.url,
          fetchImpl,
          async (resp): Promise<TextBlock> => {
            if (!resp.ok) {
              return { type: 'text', text: `[Attachment "${block.name}" could not be loaded: download failed (${resp.status}).]` };
            }
            const bytes = Buffer.from(await resp.arrayBuffer());
            const sliced = bytes.length > MAX_TEXT_ATTACHMENT_BYTES ? bytes.subarray(0, MAX_TEXT_ATTACHMENT_BYTES) : bytes;
            const text = sliced.toString('utf-8');
            const truncNote = bytes.length > MAX_TEXT_ATTACHMENT_BYTES ? '\n[... truncated]' : '';
            return { type: 'text', text: `[Attachment: ${block.name}]\n${text}${truncNote}` };
          },
        );
        result.push(materialized);
      } catch (error) {
        const reason = error instanceof AttachmentFetchTimeoutError ? 'download timed out' : 'download failed';
        result.push({ type: 'text', text: `[Attachment "${block.name}" could not be loaded: ${reason}.]` });
      }
      continue;
    }
    if (block.type !== 'image' || block.source.type !== 'url') {
      result.push(block);
      continue;
    }

    const source = block.source;
    const requiredArtifactId = source.artifact_ref_id;
    const requiredChecksum = source.image_checksum;
    const failRequired = (reason: string): never => {
      throw new RetryableArtifactAttachmentError(
        requiredArtifactId || 'unknown',
        reason,
      );
    };
    if (!isAllowedAttachmentDownloadUrl(source.url)) {
      if (requiredArtifactId) failRequired('unsupported download URL');
      result.push(attachmentUnavailableBlock(source, 'unsupported download URL'));
      continue;
    }
    try {
      const materialized = await withAttachmentFetchDeadline(
        source.url,
        fetchImpl,
        async (resp): Promise<ContentBlock> => {
          if (!resp.ok) {
            if (requiredArtifactId) failRequired(`download failed (${resp.status})`);
            return attachmentUnavailableBlock(source, `download failed (${resp.status})`);
          }
          const lengthHeader = resp.headers.get('content-length');
          const contentLength = lengthHeader ? Number.parseInt(lengthHeader, 10) : 0;
          if (Number.isFinite(contentLength) && contentLength > MAX_IMAGE_DOWNLOAD_BYTES) {
            if (requiredArtifactId) failRequired('image is too large');
            return attachmentUnavailableBlock(source, 'image is too large');
          }
          const bytes = Buffer.from(await resp.arrayBuffer());
          if (bytes.length > MAX_IMAGE_DOWNLOAD_BYTES) {
            if (requiredArtifactId) failRequired('image is too large');
            return attachmentUnavailableBlock(source, 'image is too large');
          }
          const data = bytes.toString('base64');
          if (data.length > MAX_IMAGE_BASE64_LENGTH) {
            if (requiredArtifactId) failRequired('image is too large');
            return attachmentUnavailableBlock(source, 'image is too large');
          }
          if (requiredArtifactId) {
            if (!requiredChecksum || !/^[a-f0-9]{64}$/.test(requiredChecksum)) {
              failRequired('missing or invalid image checksum');
            }
            const actualChecksum = createHash('sha256').update(bytes).digest('hex');
            if (actualChecksum !== requiredChecksum) {
              failRequired('image checksum mismatch');
            }
          }
          const headerMediaType = normalizeImageMediaType(resp.headers.get('content-type'));
          return {
            type: 'image',
            source: {
              type: 'base64',
              media_type: headerMediaType || source.media_type,
              data,
            },
          };
        },
      );
      result.push(materialized);
    } catch (error) {
      if (isRetryableArtifactAttachmentError(error)) throw error;
      if (error instanceof AttachmentFetchTimeoutError) {
        if (requiredArtifactId) failRequired('download timed out');
        result.push(attachmentUnavailableBlock(source, 'download timed out'));
        continue;
      }
      if (requiredArtifactId) failRequired('download failed');
      result.push(attachmentUnavailableBlock(source, 'download failed'));
    }
  }
  return result;
}

/**
 * Combine one or more pending operator messages into a single turn payload of
 * content blocks. When several arrive between polls they are concatenated in
 * order with a short header so the agent sees them as a batch rather than
 * losing all but the last.
 */
export function combineOperatorMessages(msgs: ResidentInboundMessage[]): ContentBlock[] {
  const nonEmpty = msgs.filter(m => {
    if ((m.content || '').trim()) return true;
    if (m.context && Array.isArray(m.context.attachments) && m.context.attachments.length > 0) return true;
    return false;
  });
  if (nonEmpty.length === 0) return [];
  if (nonEmpty.length === 1) return extractMessageTurn(nonEmpty[0]);
  const result: ContentBlock[] = [];
  for (let i = 0; i < nonEmpty.length; i++) {
    if (i > 0) result.push({ type: 'text', text: '\n\n' });
    result.push({ type: 'text', text: `[operator message ${i + 1}/${nonEmpty.length}]\n` });
    result.push(...extractMessageTurn(nonEmpty[i]));
  }
  const requiredImages = result.filter(block => (
    block.type === 'image'
    && block.source.type === 'url'
    && typeof block.source.artifact_ref_id === 'string'
    && block.source.artifact_ref_id.length > 0
  ));
  if (requiredImages.length > MAX_IMAGES_PER_TURN) {
    const first = requiredImages[0];
    const artifactRefId = first.type === 'image' && first.source.type === 'url'
      ? first.source.artifact_ref_id || 'unknown'
      : 'unknown';
    throw new RetryableArtifactAttachmentError(
      artifactRefId,
      'required image attachment limit exceeded',
    );
  }
  let ordinaryImagesRemaining = MAX_IMAGES_PER_TURN - requiredImages.length;
  return result.filter((block) => {
    if (block.type !== 'image') return true;
    if (
      block.source.type === 'url'
      && typeof block.source.artifact_ref_id === 'string'
      && block.source.artifact_ref_id.length > 0
    ) return true;
    if (ordinaryImagesRemaining <= 0) return false;
    ordinaryImagesRemaining -= 1;
    return true;
  });
}

export interface BootstrapContext {
  contextPrefix: string;
  projectName: string;
  problemModelJson: string;
  runningUpdate: string;
  /** Backend-resolved release policy projection (payload.current_effective_policy). */
  currentEffectivePolicyJson?: string;
  directionsText: string;
  stagedDirectionsText?: string;
  retiredDirectionsText: string;
  previousDecisionOutcomesText: string;
  /** Formatted strategic memory notes text (one line per entry, e.g. "- [ts] note").
   *  Empty string or whitespace → section omitted. Absent or omitted defaults to ''. */
  strategicMemoryNotesText?: string;
  /** Current project purpose text (payload.effective_purpose). Rendered as the
   *  `## Vision (project purpose)` grounding section. Empty string renders an
   *  explicit "no project purpose on record" marker so an absent purpose is
   *  observable, never silently indistinguishable from a dropped section. */
  purposeText: string;
  openMessagesText: string;
  /** Formatted recently-answered operator conversation tail
   *  (payload.recent_handled_conversation). Rendered as a read-only,
   *  non-actionable section so a marker-less operator correction survives a full
   *  session restart. Empty string or whitespace → section omitted. Absent or
   *  omitted defaults to ''. */
  recentHandledConversationText?: string;
  /** Formatted operator attention census (payload.operator_attention): what the
   *  workbench rail would be telling the operator needs a look. Rendered as a
   *  `## Operator attention census` section when non-empty; refresh it any time
   *  with `./fetch attention`. Absent or omitted defaults to ''. */
  operatorAttentionText?: string;
  sessionId: string;
  /** Conditional guidance for the provisioned compute-artifact helper. */
  computeArtifactGuidanceText?: string;
  /** @deprecated Fetch help is now discovered on demand via `./fetch help`. */
  fetchGuidanceText?: string;
  priorSessionContextText?: string;
  /** Formatted project skills text from `.blueprint/tools.json`. Rendered as a
   *  `## Project Skills` section when non-empty. Absent or omitted defaults to ''. */
  skillsText?: string;
  /** Formatted built-in tool content (operator's notes markdown, whiteboard
   *  summary). Rendered as a `## Project Tool Content` section when non-empty.
   *  Absent or omitted defaults to '' (no-op section). */
  toolContentText?: string;
  /** Pre-rendered durable decisions register (from `decisionsRegisterText`),
   *  restored from persisted `task.context._decisions_reg` on a full session
   *  restart. Carries its own `## Decisions register …` header. Empty string or
   *  whitespace → section omitted (clean-restart no-op). Absent → defaults to ''. */
  decisionsRegisterText?: string;

}

/**
 * Assemble project state and live capability grounding delivered on the FIRST
 * turn. Stable identity and behavior live in STRATEGIC_RESIDENT_SYSTEM_PROMPT,
 * so this prompt stays state-oriented and can be refreshed independently.
 */
export function buildBootstrapPrompt(ctx: BootstrapContext): string {
  const sections: string[] = [];
  if (ctx.contextPrefix.trim()) sections.push(ctx.contextPrefix.trim());

  sections.push(`# Resident session state — ${ctx.projectName || 'this project'}

Session id: ${ctx.sessionId}

## Provisioned capabilities
- \`./strategic\` mutates the problem model, portfolio, tasks, watches, status,
  and durable memory. Run \`./strategic help\` or \`./strategic schema <op>\`
  before using an unfamiliar operation.
- \`./fetch\` reads project, portfolio, task, pipeline, message, and knowledge
  state. Run \`./fetch help\` or \`./fetch help <verb>\` for the live catalog.
- \`./design-doc <get|list|set|delete> <key>\` — read/update design-doc sections.
- \`reply_to_operator\` — native interactive reply path.
- \`./messages send\` — generic project messages outside an active reply.`);

  if (ctx.computeArtifactGuidanceText?.trim()) {
    sections.push(ctx.computeArtifactGuidanceText.trim());
  }

  if (ctx.problemModelJson.trim()) {
    sections.push(`## Current problem model\n\`\`\`json\n${ctx.problemModelJson.trim()}\n\`\`\``);
  } else {
    sections.push('## Current problem model\n_(empty — bootstrap it from the directions, vision, and open operator messages below plus the codebase.)_');
  }
  if (ctx.runningUpdate.trim()) {
    sections.push(`## Current running update (operator-facing snapshot)\n${ctx.runningUpdate.trim()}`);
  }
  if (ctx.currentEffectivePolicyJson?.trim()) {
    sections.push(buildCurrentEffectivePolicySection(ctx.currentEffectivePolicyJson));
  }
  if (ctx.directionsText.trim()) {
    sections.push(`## Active directions\n${ctx.directionsText.trim()}`);
  }
  if (ctx.stagedDirectionsText?.trim()) {
    sections.push(`## Already-staged (next-version) directions\n${ctx.stagedDirectionsText.trim()}`);
  }
  if (ctx.retiredDirectionsText.trim()) {
    sections.push(`## Recently retired / completed directions\n${ctx.retiredDirectionsText.trim()}`);
  }
  if (ctx.previousDecisionOutcomesText.trim()) {
    sections.push(`## Prior-cycle decision outcomes\n${ctx.previousDecisionOutcomesText.trim()}`);
  }
  if (ctx.strategicMemoryNotesText?.trim()) {
    sections.push(`## Standing behavioral lessons
(Accumulated across sessions — operator preferences, effective strategies, and lessons the project has learned about itself. Persisted by the strategist via \`./strategic set-memory\`.)

${ctx.strategicMemoryNotesText.trim()}`);
  }
  if (ctx.skillsText?.trim()) {
    sections.push(`## Project Skills\n${ctx.skillsText.trim()}`);
  }
  if (ctx.toolContentText?.trim()) {
    sections.push(`## Project Tool Content
(Live content the operator jotted in this project's built-in tools — notes and whiteboard sketches. Treat it as operator input, not authoritative state. Pull the latest with \`./fetch tools\` if you need to re-read it mid-session.)

${ctx.toolContentText.trim()}`);
  }
  if (ctx.purposeText.trim()) {
    sections.push(`## Vision (project purpose)\n${ctx.purposeText.trim()}`);
  } else {
    sections.push(`## Vision (project purpose)\n_(no project purpose on record — the operator has not seeded intent.purpose / user_purpose. Ground decisions on the problem model, directions, and open operator messages below.)_`);
  }
  if (ctx.priorSessionContextText?.trim()) {
    sections.push(`## Prior session context\n${ctx.priorSessionContextText.trim()}`);
  }
  if (ctx.openMessagesText.trim()) {
    sections.push(`## Open operator messages\n${ctx.openMessagesText.trim()}`);
  }
  // Operator attention census — the backlog the workbench rail shows the
  // operator. Distinct from the inbox above: these are conditions (stale tasks,
  // stranded approved PRs, error alerts), not messages addressed to you. It is a
  // point-in-time snapshot taken at spawn; re-read it with `./fetch attention`.
  if (ctx.operatorAttentionText?.trim()) {
    sections.push(`## Operator attention census (snapshot at session start — re-read with \`./fetch attention\`)
${ctx.operatorAttentionText.trim()}`);
  }
  // Recently-answered operator conversation tail — READ-ONLY. These messages are
  // already handled/resolved, so they are NOT an inbox and must not be
  // re-answered. Rendered so a correction the operator made that was never
  // captured as an OPERATOR-DECISION still survives this full restart and the
  // strategist stops re-proposing what the operator already settled.
  if (ctx.recentHandledConversationText?.trim()) {
    sections.push(`## Recently answered operator conversation (read-only — already handled, do NOT re-answer)
If any of these settled or corrected a course but is NOT already reflected in your decisions register below, treat it as settled — do not re-propose it.

${ctx.recentHandledConversationText.trim()}`);
  }
  // Durable decisions register — settled cross-tick decisions restored from the
  // persisted context on a full session restart. Rendered here (mirroring the
  // compaction checkpoint) so the FIRST post-restart turn — usually a live
  // operator reply — is not generated blind to already-settled decisions. The
  // text carries its own `## Decisions register …` header. Empty on a clean
  // restart, in which case the section is omitted (byte-for-byte no-op).
  if (ctx.decisionsRegisterText?.trim()) {
    sections.push(ctx.decisionsRegisterText.trim());
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
export function resolveTerminalStatus(
  stopReason: string,
  exitCode: number | null,
  providerFailureCategory: 'rate_limited' | 'dead_auth' | null = null,
): TerminalStatusDecision {
  if (CLEAN_STOP_REASONS.has(stopReason)) return { status: 'COMPLETED' };
  if (providerFailureCategory) return { status: 'FAILED', failureCategory: providerFailureCategory };
  if (stopReason === 'consultation_abandoned_process_exit') {
    // The CLI child died (SIGINT-on-abandon exits the whole process, not just
    // the turn) while a typed Workday consultation turn was in flight, and
    // in-place recovery from checkpoint (recoverFromConsultationAbandonedExit)
    // also failed to keep the session alive. This is never a clean end of
    // work — reporting COMPLETED here previously masked consultation churn
    // (the reconciler would see "success" and respawn, repeating the loop).
    return { status: 'FAILED', failureCategory: 'consultation_abandoned' };
  }
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

// Safety margin added to the server's configured autonomous turn timeout when
// deriving the typed-consultation client abandon-deadline (ms).
export const CONSULTATION_DEADLINE_MARGIN_MS = 60_000;

// Floor for the typed-consultation client abandon-deadline (ms), independent
// of the configured autonomous turn timeout.
export const CONSULTATION_DEADLINE_FLOOR_MS = 240_000;

/**
 * Derive the typed-consultation client abandon-deadline from the server's
 * configured autonomous turn timeout (`cfg.autonomousTurnTimeoutMs`).
 *
 * Previously hardcoded to 240s while the server's default autonomous turn
 * timeout is 300s: the client gave up on (and SIGINT'd) a turn the server
 * still considered live, and `session.interrupt()` kills the whole Claude
 * Code child process, not just the active turn — so every such abandon also
 * tore down the resident session. The client must never abandon before the
 * server would, plus a margin for renewal/network latency.
 */
export function consultationAbandonDeadlineMs(autonomousTurnTimeoutMs: number): number {
  return Math.max(CONSULTATION_DEADLINE_FLOOR_MS, autonomousTurnTimeoutMs + CONSULTATION_DEADLINE_MARGIN_MS);
}

/** The `kind` vocabulary the backend `strategic_session` delta accepts
 *  (strategic_events.py `_STRATEGIC_SESSION_KINDS`). The five activity kinds
 *  (command / file / read / subagent / tool_result) drive the LIVE, ephemeral
 *  Workday activity strip; only `message` reaches the durable transcript, and
 *  only as a ProjectMessage — never via a stream frame. */
export type StreamFrameKind =
  | 'reasoning' | 'action' | 'tool' | 'status' | 'message'
  | 'command' | 'file' | 'read' | 'subagent' | 'tool_result';

/** Optional lifecycle status on a frame: `responding` opens an OPERATOR turn
 *  (drives the working indicator), `working` opens an AUTONOMOUS turn (advancing/
 *  watching indicator), `done` closes any turn (clears the activity strip). */
export type StreamFrameStatus = 'responding' | 'working' | 'done';

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

// ── Evidence-gated autonomous tick: examination ledger + rotating focus ───────
//
// Replaces the old constant "take ONE small step, or say nothing needs doing"
// self-prompt. The loop cannot conclude idle without naming ONE project surface
// it actually examined this tick (the TICK-VERDICT contract), and a code-owned
// ledger rotates a focus hint across an enumerable catalog of surfaces so each
// tick's look lands somewhere new. There are NO quotas: a project may be CLEAR
// on every surface forever — the only demand is that the claim is earned
// per-surface, per-tick. See design docs 24fd9f78 (evidence-gated idle) and
// 9f769438 (state-gated work units).

export type ExamSurfaceKind =
  | 'direction' | 'code' | 'fetch' | 'messages' | 'knowledge' | 'design-doc' | 'problem-model';

/** One examinable project surface. `key` is the stable ledger identity
 *  (`direction:d-21efb120` | `code:backend` | `qualification` | …); `hint` is a
 *  default way to examine it, surfaced in the prompt — a suggestion, not a cage. */
export interface ExamSurface {
  key: string;
  kind: ExamSurfaceKind;
  label: string;
  hint: string;
}

/** One recorded look at a surface. `verdict` is the parsed tick outcome, or
 *  `unevidenced` when a CLEAR was claimed on a turn that examined nothing (no
 *  tool use) or the marker was missing/garbled — both corrected next tick.
 *  `interrupted` is set when the turn was cut short before completing (operator
 *  preemption, authority suspend, stall watchdog) — not a violation, no
 *  corrective instruction. */
export interface ExamRecord {
  surfaceKey: string;
  atTick: number;
  at: string;
  verdict: 'clear' | 'found' | 'advanced' | 'unevidenced' | 'interrupted';
  note: string;
  /** Compact change-fingerprint of the surface AT the moment of this exam, used
   *  by the re-exam change-gate (see directionChangeSignal / nextFocus). Present
   *  only for surfaces that expose a cheap change signal (currently directions,
   *  from directions_summary counters); undefined for surfaces with no derivable
   *  signal. A later tick compares the live fingerprint against this baseline: an
   *  unchanged fingerprint on a settled `clear` surface means "nothing moved
   *  since I cleared it", so the surface is skipped until its cooldown elapses. */
  signal?: string;
}

/** Cap on retained exam records (bounds a multi-hour session's memory). */
export const EXAM_RECORDS_CAP = 200;

/** Re-exam cooldown (ms): the max staleness after which a settled-`clear`,
 *  unchanged surface becomes due for a fresh look again even with NO change
 *  signal — the escape hatch that stops the change-gate from starving any
 *  surface forever. Measured against the exam record's wall-clock `at` (not a
 *  tick count) so it keeps advancing during all-quiet stretches where no tick
 *  runs. 4h is ~24 floor ticks (AUTONOMOUS_FLOOR_TICK_MS = 10min): a settled
 *  surface that would otherwise be re-read every floor tick is now re-read at
 *  most ~once per 4h absent real movement, while staleness stays bounded. */
export const REEXAM_COOLDOWN_MS = 4 * 60 * 60 * 1000;

export interface DecisionsRegisterEntry {
  atTick: number;
  at: string;
  surfaceKey: string;
  verdict: string;
  /** Full-fidelity note — NOT truncated like exam record notes. */
  note: string;
}
/** Cap on decisions register entries (durable cross-tick decision memory). */
export const DECISIONS_REGISTER_CAP = 50;

/** The tick verdicts that represent a SETTLED decision worth remembering across
 *  ticks: an evidenced CLEAR (nothing actionable on the examined surface), a
 *  FOUND lead, or an ADVANCED mutation. `interrupted` (operator preemption /
 *  authority suspend / stall — no verdict emitted) and `unevidenced` (idle CLEAR
 *  with no tool use, or a missing/garbled marker) are NON-decisions: they carry
 *  no durable conclusion and must NOT occupy the fixed decisions-register window,
 *  or they evict real decisions — worst at the autonomous<->operator boundary
 *  where interrupts cluster. The exam ledger still records those outcomes; only
 *  the durable register filters them out. */
/** The verdict tag stamped on a decisions-register entry that captures a
 *  correction/decision the operator settled in live chat (as opposed to an
 *  autonomous tick verdict). Included in SETTLED_DECISION_VERDICTS so an
 *  operator-settled decision is treated as durable just like an autonomous one —
 *  this is what stops the strategist re-proposing a course the operator already
 *  corrected after a session restart. */
export const OPERATOR_DECISION_VERDICT = 'operator';

const SETTLED_DECISION_VERDICTS: ReadonlySet<string> = new Set([
  'advanced', 'found', 'clear', OPERATOR_DECISION_VERDICT,
]);

/**
 * Whether a tick verdict represents a settled decision that belongs in the
 * durable decisions register. True for `advanced` | `found` | `clear` and the
 * operator-settled `operator` verdict; false for `unevidenced`, `interrupted`,
 * and any unrecognized string. Pure — unit-testable in isolation.
 */
export function isSettledDecisionVerdict(verdict: string): boolean {
  return SETTLED_DECISION_VERDICTS.has(verdict);
}

/** Verdicts evicted FIRST when the decisions register is over its cap. A
 *  `clear` ("nothing actionable on the examined surface") is the designed-common
 *  autonomous outcome, so a high-volume flood of them must not crowd out and
 *  evict the scarce operator/advanced/found settlements the register exists to
 *  preserve — worst at the autonomous<->operator boundary this guards. Any
 *  verdict NOT in this set (operator/advanced/found) is protected: it is only
 *  evicted once no low-priority entry remains. */
const EVICT_FIRST_VERDICTS: ReadonlySet<string> = new Set(['clear']);

/**
 * Index of the next entry to evict when the decisions register is over cap:
 * the oldest (lowest-index) low-priority (`clear`) entry, or index 0 (oldest
 * overall) when no low-priority entry remains. Callers must guard on a
 * non-empty register. Pure.
 */
function decisionEvictIndex(register: DecisionsRegisterEntry[]): number {
  const idx = register.findIndex(e => EVICT_FIRST_VERDICTS.has(e.verdict));
  return idx >= 0 ? idx : 0;
}

/**
 * Admit `entry` into the durable decisions register with priority-aware
 * eviction. Appends the entry, then — while the register exceeds `cap` —
 * evicts the oldest low-priority (`clear`) entry first, falling back to the
 * oldest entry overall only when no low-priority entry remains. This keeps
 * scarce operator/advanced/found settlements from being crowded out by routine
 * clears (the FIFO the register previously used evicted purely by age, so a
 * flood of clears silently dropped operator corrections). Mutates and returns
 * `register` in place. Pure aside from that mutation.
 */
export function admitDecisionEntry(
  register: DecisionsRegisterEntry[],
  entry: DecisionsRegisterEntry,
  cap: number = DECISIONS_REGISTER_CAP,
): DecisionsRegisterEntry[] {
  register.push(entry);
  while (register.length > cap) {
    register.splice(decisionEvictIndex(register), 1);
  }
  return register;
}
/** Default number of focus surfaces surfaced in the prompt each tick. */
export const FOCUS_LIST_SIZE = 3;
/** Default interval for a deliberately sparse model-driven exploration turn when
 * no semantic event has arrived. Daily keeps autonomous discovery available
 * without turning ordinary Resident idleness into a fixed upper-tier tax. */
export const AUTONOMOUS_EXPLORATION_INTERVAL_MS = 86_400_000;
export const EXAM_NOTE_CAP = 240;
/**
 * Byte budget for the serialized exam ledger. Must stay below the backend's
 * _EXAM_LEDGER_MAX_BYTES (20_000) so the stored value is always valid JSON.
 * Set to 16_000 to leave headroom for the surrounding heartbeat note envelope.
 */
export const EXAM_LEDGER_MAX_BYTES = 16_000;

export interface PendingOperatorDecisionReview {
  status: 'pending';
  messageIds: string[];
  exchangeText?: string;
  exchangeTextTruncated?: true;
  startedAt: string;
  attempts: number;
  queuedExchanges?: Array<{
    messageIds: string[];
    exchangeText?: string;
    exchangeTextTruncated?: true;
    startedAt: string;
    attempts: number;
  }>;
  overflowed?: true;
  overflowExchanges?: number;
}

const OPERATOR_DECISION_REVIEW_MESSAGE_CAP = 20;
const OPERATOR_DECISION_REVIEW_TEXT_CAP = 384;

export function mergePendingOperatorDecisionReview(
  current: PendingOperatorDecisionReview | null,
  messageIds: string[],
  startedAt: string,
  exchangeText = '',
): PendingOperatorDecisionReview {
  const ids = messageIds
    .filter((id, index, all) => typeof id === 'string' && id.length > 0 && all.indexOf(id) === index)
    .slice(-OPERATOR_DECISION_REVIEW_MESSAGE_CAP);
  if (current) {
    const knownIds = new Set([
      ...current.messageIds,
      ...(current.queuedExchanges ?? []).flatMap(exchange => exchange.messageIds),
    ]);
    const newIds = ids.filter(id => !knownIds.has(id));
    if (newIds.length === 0) return current;
    const queuedExchanges = current.queuedExchanges ?? [];
    if (current.overflowed) {
      return {
        ...current,
        overflowed: true,
        overflowExchanges: (current.overflowExchanges ?? 0) + 1,
      };
    }
    const normalizedText = exchangeText.trim();
    const boundedText = normalizedText.slice(0, OPERATOR_DECISION_REVIEW_TEXT_CAP);
    return {
      ...current,
      queuedExchanges: [
        ...queuedExchanges,
        {
          messageIds: newIds,
          ...(boundedText ? { exchangeText: boundedText } : {}),
          ...(normalizedText.length > boundedText.length ? { exchangeTextTruncated: true } : {}),
          startedAt,
          attempts: 0,
        },
      ],
    };
  }
  const normalizedText = exchangeText.trim();
  const boundedText = normalizedText.slice(0, OPERATOR_DECISION_REVIEW_TEXT_CAP);
  return {
    status: 'pending',
    messageIds: ids,
    ...(boundedText ? { exchangeText: boundedText } : {}),
    ...(normalizedText.length > boundedText.length ? { exchangeTextTruncated: true } : {}),
    startedAt,
    attempts: 0,
  };
}

/**
 * Number of consecutive failed autonomous review turns (missing/malformed marker)
 * after which a stalled operator-decision review is force-admitted into the durable
 * register as an exchange text fallback — so the original operator correction is
 * not silently lost at the next restart or compaction. The caller checks
 * `retained.attempts >= MAX_REVIEW_ATTEMPTS` immediately after retaining.
 */
export const MAX_REVIEW_ATTEMPTS = 3;

export function retainPendingOperatorDecisionReview(
  current: PendingOperatorDecisionReview,
): PendingOperatorDecisionReview {
  return { ...current, attempts: current.attempts + 1 };
}

export function advancePendingOperatorDecisionReview(
  current: PendingOperatorDecisionReview,
  options: { coversOverflow?: boolean } = {},
): PendingOperatorDecisionReview | null {
  const queued = current.queuedExchanges ?? [];
  if (queued.length > 0) {
    const [next, ...remaining] = queued;
    return {
      status: 'pending',
      messageIds: next.messageIds,
      ...(next.exchangeText ? { exchangeText: next.exchangeText } : {}),
      ...(next.exchangeTextTruncated ? { exchangeTextTruncated: true } : {}),
      startedAt: next.startedAt,
      attempts: next.attempts,
      ...(remaining.length > 0 ? { queuedExchanges: remaining } : {}),
      ...(current.overflowed ? { overflowed: true } : {}),
      ...(current.overflowExchanges ? { overflowExchanges: current.overflowExchanges } : {}),
    };
  }
  if (current.overflowed) {
    if (current.messageIds.length === 0) return options.coversOverflow ? null : current;
    return {
      status: 'pending',
      messageIds: [],
      exchangeTextTruncated: true,
      startedAt: current.startedAt,
      attempts: 0,
      overflowed: true,
      ...(current.overflowExchanges ? { overflowExchanges: current.overflowExchanges } : {}),
    };
  }
  return null;
}

// The diagnostic read verbs (a subset of STRATEGIC_FETCH_VERBS in
// strategic-helpers.ts) that make good STANDING examination surfaces. Kept as a
// local literal so this lib stays dependency-free (importing strategic-helpers
// would pull in the heavy index surface and defeat the pure-helper testability).
const EXAM_FETCH_SURFACES: ReadonlyArray<{ key: string; hint: string }> = [
  { key: 'qualification', hint: './fetch qualification — read the open version findings/sub-scores; is any finding actionable now?' },
  { key: 'version-phase', hint: './fetch version-phase — confirm the phase and any blockers to the next promotion' },
  { key: 'ci-compliance', hint: './fetch ci-compliance — scan for a recurring failure class worth a direction' },
  { key: 'efficiency-snapshot', hint: './fetch efficiency-snapshot — look for an input-token / cost regression' },
  { key: 'vm-cost', hint: './fetch vm-cost — check the spend trend for anything anomalous' },
  { key: 'compute-sweeps', hint: './fetch compute-sweeps — review sweep outcomes for a lead' },
  { key: 'capability-trials', hint: './fetch capability-trials — review trial verdicts for a follow-up' },
  // Pipeline-visibility surfaces. These describe the state of the pipeline
  // itself rather than the resident's own lineage; without them on the
  // examination rotation the autonomous tick never looks at the pipeline it is
  // supposed to be steering.
  { key: 'attention', hint: './fetch attention — read the operator attention census; what is waiting on a human, and is any of it something you can clear?' },
  { key: 'task-census', hint: './fetch task-census — project-wide in-flight work across every created_by; look at the oldest non-terminal tasks for anything stranded' },
  { key: 'autonomy-debug', hint: './fetch autonomy-debug — is autonomy spawning? check per-direction block_reason_code for a direction stranded on a clearable blocker' },
  { key: 'alerts', hint: './fetch alerts — look at the alert mix by event_type for a recurring condition worth a direction, not at individual notices' },
  { key: 'deploy-state', hint: './fetch deploy-state — confirm what SHA is actually serving and whether deploys are paused or rolled back' },
];

/**
 * Enumerate the project's examinable surfaces from what the resident already
 * reaches: active directions (from the payload's `directions_summary` ids), the
 * diagnostic read verbs, the standing messages/knowledge/problem-model/design-doc
 * surfaces, and one `code:<dir>` per top-level directory of the full-clone
 * workspace (project-shape-agnostic — no hardcoded project types). Pure: rebuild
 * it whenever the payload changes and new directions enter as never-examined,
 * retired ones drop out (nextFocus ignores records whose surface is gone).
 */
export function buildSurfaceCatalog(
  payload: Record<string, unknown>,
  workspaceTopDirs: string[],
): ExamSurface[] {
  const out: ExamSurface[] = [];
  const seenKeys = new Set<string>();
  const push = (s: ExamSurface): void => {
    if (seenKeys.has(s.key)) return;
    seenKeys.add(s.key);
    out.push(s);
  };

  const dirs = Array.isArray(payload.directions_summary)
    ? payload.directions_summary as Array<Record<string, unknown>> : [];
  for (const d of dirs) {
    const id = str(d.id);
    if (!id) continue;
    const text = str(d.text).trim().slice(0, 80);
    push({
      key: `direction:${id}`,
      kind: 'direction',
      label: text ? `direction ${id} — ${text}` : `direction ${id}`,
      hint: `./fetch direction ${id} — judge its recent outcomes/churn; is it still productive or done?`,
    });
  }

  for (const f of EXAM_FETCH_SURFACES) {
    push({ key: f.key, kind: 'fetch', label: f.key, hint: f.hint });
  }

  push({ key: 'messages:recent', kind: 'messages', label: 'recent operator messages', hint: './fetch message open — re-read open + recently resolved operator messages for anything unaddressed' });
  push({ key: 'knowledge:recent', kind: 'knowledge', label: 'recent knowledge / design outputs', hint: './fetch knowledge — scan unadopted design / web_research outputs for something to act on' });
  push({ key: 'problem-model', kind: 'problem-model', label: 'problem model', hint: './fetch problem-model — compare its claims against the code/directions for drift' });
  push({ key: 'design-doc', kind: 'design-doc', label: 'design-doc sections', hint: './design-doc list then read a section — check drift vs the code it describes' });

  for (const dir of Array.isArray(workspaceTopDirs) ? workspaceTopDirs : []) {
    const name = str(dir).trim();
    if (!name) continue;
    push({
      key: `code:${name}`,
      kind: 'code',
      label: `code/${name}`,
      hint: `read a module under ${name}/ with fresh eyes for correctness / config-drift / UX bugs`,
    });
  }

  return out;
}

/**
 * Compact change-fingerprint of one direction's `directions_summary` entry: a
 * stable string over the fields that MOVE when the direction does real work or
 * churns — its status plus the monotonic outcome/cycle/PR/churn counters. Used
 * as the per-surface change signal by the re-exam gate: an unchanged fingerprint
 * on a surface already found CLEAR means "nothing moved since I cleared it", so
 * re-reading it re-derives a settled fact. Pure over the fields it reads; a
 * missing field folds to 0/'' so the fingerprint is defined for any shape.
 *
 * NOTE on freshness: these counters ship in the session-start payload's
 * `directions_summary` and refresh only at the next bootstrap — the mid-session
 * heartbeat carries `work_state_rev`, `active_direction_ids`, and
 * `direction_grounding` (id/text/status) but NOT the counters. So within one
 * session a direction's fingerprint is stable (the cooldown drives re-exam) and
 * counter deltas surface across a restart/bootstrap, where the persisted exam
 * ledger's stored signal becomes the baseline to diff against. This is a
 * conservative, no-false-positive signal aligned with "don't re-read every tick".
 */
export function directionChangeSignal(dir: Record<string, unknown>): string {
  const n = (v: unknown): number => (typeof v === 'number' && Number.isFinite(v) ? v : 0);
  return [
    str(dir.status),
    n(dir.total_cycles),
    n(dir.prs_merged),
    n(dir.prs_rejected),
    n(dir.tasks_failed),
    n(dir.confirmed_finding_count),
  ].join('|');
}

/**
 * Build the live per-surface change-signal map (`direction:<id>` → fingerprint)
 * from a payload's `directions_summary`. Only direction surfaces expose a cheap
 * signal; a surface absent from the map has no derivable signal and is gated by
 * the cooldown alone. Pure.
 */
export function buildDirectionChangeSignals(
  payload: Record<string, unknown>,
): Map<string, string> {
  const out = new Map<string, string>();
  const dirs = Array.isArray(payload.directions_summary)
    ? payload.directions_summary as Array<Record<string, unknown>> : [];
  for (const d of dirs) {
    const id = str(d.id);
    if (id) out.set(`direction:${id}`, directionChangeSignal(d));
  }
  return out;
}

/** Inputs to the re-exam change-gate. All optional: with none supplied,
 *  `nextFocus` degrades to the legacy never-first-then-LRU rotation and every
 *  examined surface is treated as due. */
export interface ReexamGateOptions {
  /** Live per-surface change signals (`direction:<id>` → fingerprint) from the
   *  current payload's directions_summary (see buildDirectionChangeSignals). An
   *  absent entry means "no derivable signal" → the surface is cooldown-gated. */
  currentSignals?: Map<string, string>;
  /** Wall-clock now (ms) for the cooldown escape hatch; omit to disable it. */
  nowMs?: number;
  /** Max staleness (ms) before a settled-clear, unchanged surface is due again
   *  regardless of change signal. Default REEXAM_COOLDOWN_MS. */
  cooldownMs?: number;
}

/** Most-recent exam record per surface key (highest atTick wins; array order
 *  breaks ties). */
function latestRecordBySurface(records: ExamRecord[]): Map<string, ExamRecord> {
  const latest = new Map<string, ExamRecord>();
  for (const r of records) {
    const prev = latest.get(r.surfaceKey);
    if (prev === undefined || r.atTick >= prev.atTick) latest.set(r.surfaceKey, r);
  }
  return latest;
}

/**
 * Whether a surface is DUE for (re-)examination under the change-gate. A surface
 * is due when it has NEVER been examined, when its most-recent verdict is not a
 * settled `clear` (found/advanced/unevidenced/interrupted all still want a
 * look), or when — being settled clear — it has a change signal since that clear
 * OR its cooldown has elapsed. A settled-clear surface with no change signal and
 * an un-elapsed cooldown is NOT due (skipped this tick). Pure.
 */
export function surfaceDue(
  surface: ExamSurface,
  latest: ExamRecord | undefined,
  opts: ReexamGateOptions,
): boolean {
  if (latest === undefined) return true;      // never examined → always due
  if (latest.verdict !== 'clear') return true; // unsettled → keep working it
  // Settled clear: due only if the surface moved or the cooldown elapsed.
  const current = opts.currentSignals?.get(surface.key);
  // A comparable live signal that differs from the baseline recorded at the
  // clear exam ⇒ real movement ⇒ due. `current === undefined` (no derivable
  // signal for this surface) skips this check and falls through to the cooldown.
  // A legacy clear record with no stored `signal` (baseline undefined) but a
  // live signal present ⇒ differs ⇒ due (re-examined once to seed a baseline).
  if (current !== undefined && current !== latest.signal) return true;
  // Cooldown escape hatch so nothing is starved forever.
  const cooldownMs = opts.cooldownMs ?? REEXAM_COOLDOWN_MS;
  if (typeof opts.nowMs === 'number' && latest.at) {
    const at = Date.parse(latest.at);
    if (Number.isFinite(at) && opts.nowMs - at >= cooldownMs) return true;
  }
  return false;
}

/**
 * Whether ANY catalog surface is due for examination under the change-gate.
 * When this is false, every surface is settled-clear-and-unchanged within its
 * cooldown — the "all-quiet" case, where the caller should idle cheaply rather
 * than deep-re-read an arbitrary already-settled surface. Pure.
 */
export function hasDueSurface(
  catalog: ExamSurface[],
  records: ExamRecord[],
  opts: ReexamGateOptions,
): boolean {
  const latest = latestRecordBySurface(records);
  for (const s of catalog) {
    if (surfaceDue(s, latest.get(s.key), opts)) return true;
  }
  return false;
}

/**
 * The next focus surfaces for this tick. Never-examined surfaces come first (in
 * catalog order, so freshly-added directions are auto-prioritized); then, when a
 * change-gate `opts` is supplied, surfaces that are DUE (a change signal since
 * their last clear, an unsettled last verdict, or an elapsed cooldown) ahead of
 * surfaces that are settled-clear-and-unchanged ("skip" tier) — both ordered
 * least-recently-examined. Skip-tier surfaces are still returned (never dropped)
 * so a forced turn always has a surface to name, just deprioritized so an
 * unchanged, recently-cleared surface is not re-read while anything real is due.
 * Without `opts`, this degrades to the legacy never-first-then-LRU rotation.
 * Records whose surface is no longer in the catalog are ignored (pruned).
 * Returns at most `n` surfaces.
 */
export function nextFocus(
  catalog: ExamSurface[],
  records: ExamRecord[],
  n: number = FOCUS_LIST_SIZE,
  opts?: ReexamGateOptions,
): ExamSurface[] {
  const latest = latestRecordBySurface(records);
  const never: ExamSurface[] = [];
  const due: ExamSurface[] = [];
  const skip: ExamSurface[] = [];
  for (const s of catalog) {
    const rec = latest.get(s.key);
    if (rec === undefined) { never.push(s); continue; }
    // Legacy path (no opts): every examined surface is "due" → preserves the
    // exact never-first-then-LRU ordering the gate-free rotation produced.
    if (opts && !surfaceDue(s, rec, opts)) skip.push(s);
    else due.push(s);
  }
  const lru = (arr: ExamSurface[]): ExamSurface[] =>
    arr.sort((a, b) => (latest.get(a.key)?.atTick ?? 0) - (latest.get(b.key)?.atTick ?? 0));
  return [...never, ...lru(due), ...lru(skip)].slice(0, Math.max(0, n));
}

/**
 * Strip leading markdown decoration (blockquote `>`, one list bullet `- * + •`,
 * and an emphasis-open `** __ * _`) plus a trailing emphasis-close from a
 * candidate marker line, so a model that renders `**TICK-VERDICT: CLEAR ...**`,
 * `- OPERATOR-DECISION: ...`, or `> TICK-VERDICT: FOUND ...` still parses. Only
 * line-leading list/quote/emphasis tokens are removed — an inline mention like
 * "you asked about OPERATOR-DECISION:" keeps its leading prose, so the tight
 * `^\s*<LABEL>:` anchors below still reject it (mid-reply mentions stay ignored).
 */
export function stripMarkerDecoration(line: string): string {
  return line
    // leading blockquote markers, then at most one list bullet
    .replace(/^\s*(?:>\s*)*(?:[-*+•]\s+)?/, '')
    // leading emphasis-open (**, __, *, _) left after the bullet, if any
    .replace(/^\s*[*_]{1,3}\s*/, '')
    // trailing emphasis-close (**, __, *, _)
    .replace(/\s*[*_]{1,3}\s*$/, '');
}

/**
 * Parse the trailing TICK-VERDICT marker line from a turn's result text. Scans
 * from the LAST line so a mid-turn mention cannot be mistaken for the verdict.
 * ADVANCED carries no surface (it is about applied ops); FOUND/CLEAR name the ONE
 * surface examined. Markdown decoration around the marker (bold/bullet/blockquote)
 * is tolerated via stripMarkerDecoration. Returns null when no valid marker is
 * present (missing/garbled → treated as a contract violation by classifyTickOutcome).
 */
export function parseTickVerdict(
  resultText: string,
): { verdict: 'advanced' | 'found' | 'clear'; surfaceKey: string; note: string } | null {
  if (typeof resultText !== 'string') return null;
  const lines = resultText.split('\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = /^\s*TICK-VERDICT:\s*(ADVANCED|FOUND|CLEAR)\b\s*(.*)$/i.exec(stripMarkerDecoration(lines[i]));
    if (!m) continue;
    const verdict = m[1].toLowerCase() as 'advanced' | 'found' | 'clear';
    const rest = m[2].trim();
    if (verdict === 'advanced') {
      return { verdict, surfaceKey: '', note: rest.replace(/^[—–-]\s*/, '').trim() };
    }
    // FOUND / CLEAR: "<surface-key> — <note>" (dash surrounded by spaces so a
    // hyphenated key like d-21efb120 is never split), or "<surface-key> <note>".
    let surfaceKey = '';
    let note = '';
    const dashIdx = rest.search(/\s[—–-]\s/);
    if (dashIdx >= 0) {
      surfaceKey = rest.slice(0, dashIdx).trim();
      note = rest.slice(dashIdx).replace(/^\s*[—–-]\s*/, '').trim();
    } else {
      const sp = rest.indexOf(' ');
      if (sp >= 0) { surfaceKey = rest.slice(0, sp).trim(); note = rest.slice(sp + 1).trim(); }
      else surfaceKey = rest;
    }
    return { verdict, surfaceKey, note };
  }
  return null;
}

/**
 * Parse the trailing OPERATOR-DECISION marker line from an operator-turn's
 * result text. The Resident system prompt instructs it to end a
 * reply with `OPERATOR-DECISION: <settled fact>` whenever the operator corrects
 * or settles a course in live chat. Scans from the LAST line so a mid-reply
 * mention cannot be mistaken for the marker. Markdown decoration around the
 * marker (bold/bullet/blockquote) is tolerated via stripMarkerDecoration.
 * Returns the settled-fact note (leading dash stripped), or null when no marker
 * is present or its note is empty. Pure — unit-testable in isolation.
 */
export function parseOperatorDecision(resultText: string): string | null {
  if (typeof resultText !== 'string') return null;
  const lines = resultText.split('\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = /^\s*OPERATOR-DECISION:\s*(.*)$/i.exec(stripMarkerDecoration(lines[i]));
    if (!m) continue;
    const note = m[1].replace(/^[—–-]\s*/, '').trim();
    return note.length > 0 ? note : null;
  }
  return null;
}

export type OperatorDecisionReviewResolution = 'no-decision' | 'unresolved';

export interface OperatorDecisionReviewOutcome {
  kind: 'recovered' | 'no-decision' | 'pending';
  note?: string;
  coversOverflow?: boolean;
  statusNote?: string;
}

export function classifyOperatorDecisionReview(params: {
  resultText: string;
  accumulatedTurnText?: string;
  interrupted?: boolean;
}): OperatorDecisionReviewOutcome {
  if (params.interrupted) {
    return {
      kind: 'pending',
      statusNote: 'operator-decision review interrupted — durable review remains pending',
    };
  }

  type ReviewMarker =
    | { kind: 'decision'; note: string | null; normalized: string }
    | {
      kind: 'review';
      resolution: OperatorDecisionReviewResolution | 'overflow-no-decision' | 'overflow-decision' | null;
      note?: string | null;
      normalized: string;
    };
  const markersIn = (text: string): ReviewMarker[] => {
    if (typeof text !== 'string') return [];
    const markers: ReviewMarker[] = [];
    for (const line of text.split('\n')) {
      const normalized = stripMarkerDecoration(line).trim();
      const decision = /^\s*OPERATOR-DECISION:\s*(.*)$/i.exec(normalized);
      if (decision) {
        const note = decision[1].replace(/^[—–-]\s*/, '').trim();
        markers.push({ kind: 'decision', note: note || null, normalized: normalized.toLowerCase() });
        continue;
      }
      const review = /^\s*OPERATOR-DECISION-REVIEW:\s*(.*)$/i.exec(normalized);
      if (review) {
        const value = review[1].trim().toUpperCase();
        const overflowDecision = /^OVERFLOW-DECISION\s*:\s*(.*)$/i.exec(review[1].trim());
        const resolution = /^OVERFLOW-NO-DECISION\b/.test(value) ? 'overflow-no-decision'
          : overflowDecision ? 'overflow-decision'
          : /^NO-DECISION\b/.test(value) ? 'no-decision'
          : /^UNRESOLVED\b/.test(value) ? 'unresolved' : null;
        const note = overflowDecision?.[1].replace(/^[—–-]\s*/, '').trim() || null;
        markers.push({ kind: 'review', resolution, note, normalized: normalized.toLowerCase() });
      }
    }
    return markers;
  };

  const resultMarkers = markersIn(params.resultText);
  const accumulatedMarkers = markersIn(params.accumulatedTurnText ?? '');
  const bySource = [resultMarkers, accumulatedMarkers];
  const markerCounts = new Map<string, { marker: ReviewMarker; count: number }>();
  for (const markers of bySource) {
    const sourceCounts = new Map<string, { marker: ReviewMarker; count: number }>();
    for (const marker of markers) {
      const entry = sourceCounts.get(marker.normalized);
      sourceCounts.set(marker.normalized, { marker, count: (entry?.count ?? 0) + 1 });
    }
    for (const [key, entry] of sourceCounts) {
      if (entry.count > (markerCounts.get(key)?.count ?? 0)) markerCounts.set(key, entry);
    }
  }
  const markers = [...markerCounts.values()].flatMap(({ marker, count }) =>
    Array.from({ length: count }, () => marker));

  if (markers.length === 1) {
    const [marker] = markers;
    if (marker.kind === 'decision' && marker.note !== null) {
      return { kind: 'recovered', note: marker.note };
    }
    if (marker.kind === 'review' && marker.resolution === 'no-decision') {
      return { kind: 'no-decision' };
    }
    if (marker.kind === 'review' && marker.resolution === 'overflow-no-decision') {
      return { kind: 'no-decision', coversOverflow: true };
    }
    if (marker.kind === 'review' && marker.resolution === 'overflow-decision' && marker.note) {
      return { kind: 'recovered', note: marker.note, coversOverflow: true };
    }
    if (marker.kind === 'review' && marker.resolution === 'unresolved') {
      return {
        kind: 'pending',
        statusNote: 'operator-decision review unresolved — durable review remains pending',
      };
    }
  }

  return {
    kind: 'pending',
    statusNote: markers.length === 0
      ? 'operator-decision review missing a valid resolution marker — durable review remains pending'
      : 'operator-decision review emitted conflicting, duplicate, or malformed outcome markers — durable review remains pending',
  };
}

/** Whether a turn declared more actionable work remains (a literal AGENDA-CONTINUE
 *  line), gating an immediate re-tick in shouldRunAutonomousTurn. Tolerates the
 *  same leading markdown decoration (blockquote/bullet/emphasis) as the verdict
 *  and operator-decision markers so a decorated line is not silently dropped. */
export function parseAgendaContinue(resultText: string): boolean {
  return /^\s*(?:>\s*)*(?:[-*+•]\s+)?(?:[*_]{1,3}\s*)?AGENDA-CONTINUE\b/im.test(
    typeof resultText === 'string' ? resultText : '',
  );
}

/** The FOUND leads still open — the latest record per surface that is a FOUND
 *  (a later clear/advanced/unevidenced on the same surface closes it). Restated
 *  in the next tick's prompt until acted on or cleared. */
export function openFoundLeads(records: ExamRecord[]): ExamRecord[] {
  const latest = new Map<string, ExamRecord>();
  for (const r of records) latest.set(r.surfaceKey, r);
  return [...latest.values()].filter(r => r.verdict === 'found');
}

export interface TickLedgerUpdate {
  /** the record to append to the ledger */
  record: ExamRecord;
  /** open the next tick with a corrective examine-before-CLEAR instruction */
  corrective: boolean;
  /** the turn declared more actionable work (AGENDA-CONTINUE) */
  agendaContinue: boolean;
  /** a status stream line to surface a contract violation (undefined = none) */
  statusNote?: string;
}

/**
 * Classify a completed (or interrupted) autonomous turn into a ledger update. A
 * CLEAR on a turn that used no tool (`sawToolUse === false`) is physically
 * unevidenced; a missing/garbled marker is a contract violation. Both are recorded
 * `unevidenced` and set the corrective flag so the next tick opens with "examine
 * <stalest> before concluding CLEAR" — never failing the session. ADVANCED/FOUND
 * and an evidenced CLEAR record their verdict against the named surface (falling
 * back to the stalest focus surface when the marker omitted a key, e.g. ADVANCED).
 *
 * When `interrupted` is true the turn was cut short before it could produce a
 * TICK-VERDICT marker (operator preemption, authority suspend, stall watchdog).
 * This is NOT a contract violation: it is recorded as `interrupted` with
 * `corrective: false` and `agendaContinue: false` so the ledger and rotation are
 * unaffected and no false corrective frame is raised.
 */
export function classifyTickOutcome(params: {
  resultText: string;
  /** Streamed text retained separately from the authoritative terminal result.
   *  Consult it only when a given marker is absent from resultText. */
  accumulatedTurnText?: string;
  sawToolUse: boolean;
  focus: ExamSurface[];
  atTick: number;
  at: string;
  /** True when the turn was cut short before completion (preemption / suspend / stall). */
  interrupted?: boolean;
}): TickLedgerUpdate {
  const stalest = params.focus[0]?.key ?? '';
  const base = { atTick: params.atTick, at: params.at };

  if (params.interrupted) {
    return {
      record: { surfaceKey: stalest, ...base, verdict: 'interrupted', note: 'turn interrupted before completion' },
      corrective: false,
      agendaContinue: false,
    };
  }

  const verdict = parseTickVerdict(params.resultText)
    ?? parseTickVerdict(params.accumulatedTurnText ?? '');
  const agendaContinue = parseAgendaContinue(params.resultText)
    || parseAgendaContinue(params.accumulatedTurnText ?? '');

  if (!verdict) {
    return {
      record: { surfaceKey: stalest, ...base, verdict: 'unevidenced', note: 'no valid TICK-VERDICT marker line' },
      corrective: true,
      agendaContinue,
      statusNote: 'tick ended without a valid TICK-VERDICT marker — will examine a surface before concluding CLEAR next tick',
    };
  }

  const surfaceKey = verdict.surfaceKey || stalest;
  const note = verdict.note.slice(0, EXAM_NOTE_CAP);

  if (verdict.verdict === 'clear' && !params.sawToolUse) {
    return {
      record: { surfaceKey, ...base, verdict: 'unevidenced', note: note || 'CLEAR claimed with no tool use' },
      corrective: true,
      agendaContinue,
      statusNote: `CLEAR on ${surfaceKey || 'a surface'} examined nothing this turn (no tool use) — recorded unevidenced`,
    };
  }

  return {
    record: { surfaceKey, ...base, verdict: verdict.verdict, note },
    corrective: false,
    agendaContinue,
  };
}

export interface AutonomousTurnGateState {
  /** work_state_rev from the latest heartbeat (undefined/null = backend does not
   *  populate it → this signal is skipped and pacing degrades to exploration). */
  currentWorkStateRev?: string | null;
  /** the rev recorded at the last autonomous turn */
  lastTurnWorkStateRev?: string | null;
  /** the previous autonomous turn ended AGENDA-CONTINUE */
  previousTurnAgendaContinue: boolean;
  /** a code-owned corrective/review obligation requires another real turn */
  followupTurnPending?: boolean;
  /** an operator turn just completed (act on fresh conversation) */
  operatorTurnJustCompleted: boolean;
  /** stable identity of currently fired/expired watch receipts */
  currentFiredWatchRev?: string;
  /** fired-watch identity recorded after the last completed autonomous turn */
  lastTurnFiredWatchRev?: string;
  /** true while owned tasks or pending/unacknowledged watches remain */
  ownedWorkPending?: boolean;
  /** ms since the last autonomous turn (Infinity before the first) */
  msSinceLastAutonomousTurn: number;
  /** unchanged-state exploration interval; default AUTONOMOUS_EXPLORATION_INTERVAL_MS */
  explorationIntervalMs?: number;
  /** re-exam change-gate verdict: is ANY surface due for a real look this tick
   *  (never-examined, unsettled, changed since its clear, or cooldown elapsed)?
   *  Consulted ONLY on the unchanged-state exploration path — when the interval
   *  has elapsed but every surface is settled-clear-and-unchanged within its
   *  cooldown (`false`), the exploration turn is suppressed so the caller idles
   *  cheaply instead of deep-re-reading a settled surface. `undefined` (gate not
   *  wired) preserves legacy exploration pacing. Never gates operator/agenda/
   *  followup/watch/world-change wakes — those always run. */
  anyDue?: boolean;
  /** has THIS session already completed at least one autonomous examination turn
   *  (→ it has made strategic API activity)? Empty-session guard: the all-quiet
   *  `anyDue === false` suppression must NEVER fire before the session's first
   *  autonomous turn, or a freshly-spawned session that inherits an all-clear,
   *  within-cooldown carried ledger would make ZERO API calls and then lapse at
   *  its TTL — which the backend scores as an empty-session strategic failure
   *  (3 consecutive → the strategic_failures_high freeze). `false` forces the
   *  first turn to run regardless of `anyDue`; `true` allows all-quiet
   *  suppression; `undefined` (gate not wired) preserves legacy pacing. */
  hasRunAutonomousTurnThisSession?: boolean;
}

/**
 * Pure pacing predicate. Operator work, an explicit immediate agenda, code-owned
 * correction, new watch receipt, or an actionable world-state change all run.
 * The first turn also runs so a fresh/recovered session can orient. After that,
 * unchanged-state exploration is disabled while owned work/watches are pending:
 * the backend waits cheaply and the next semantic event wakes the model.
 */
export function shouldRunAutonomousTurn(state: AutonomousTurnGateState): boolean {
  if (state.operatorTurnJustCompleted) return true;
  if (state.previousTurnAgendaContinue) return true;
  if (state.followupTurnPending) return true;
  if (
    state.currentFiredWatchRev
    && state.currentFiredWatchRev !== state.lastTurnFiredWatchRev
  ) return true;
  const rev = state.currentWorkStateRev;
  if (rev !== undefined && rev !== null && rev !== state.lastTurnWorkStateRev) return true;
  if (state.msSinceLastAutonomousTurn === Number.POSITIVE_INFINITY) return true;
  if (state.ownedWorkPending) return false;
  const explorationInterval = state.explorationIntervalMs
    ?? AUTONOMOUS_EXPLORATION_INTERVAL_MS;
  if (explorationInterval <= 0) return false;
  if (state.msSinceLastAutonomousTurn < explorationInterval) return false;
  // Empty-session guard: the all-quiet suppression below must never keep a
  // session that has NOT yet run an autonomous turn from running one — a
  // freshly-spawned session inheriting an all-clear, within-cooldown carried
  // ledger would otherwise make zero strategic API calls and lapse at its TTL,
  // which the backend scores as an empty-session strategic failure. The first
  // autonomous turn always runs (→ API activity). The `msSinceLastAutonomousTurn
  // === Infinity` short-circuit above already covers the current caller wiring
  // (lastAutonomousTurnAt is per-session), but this makes the invariant explicit
  // and independent of that sentinel. Only an explicit `false` forces the run;
  // `undefined` (not wired) preserves legacy pacing.
  if (state.hasRunAutonomousTurnThisSession === false) return true;
  // Exploration interval elapsed. Spend a real (expensive) turn only when a
  // surface is actually due under the re-exam change-gate. When everything is
  // settled-clear-and-unchanged within its cooldown (anyDue === false), suppress
  // the exploration turn so the caller idles cheaply rather than deep-re-reading
  // an already-settled surface. `undefined` (gate not wired) preserves legacy
  // exploration pacing by running.
  return state.anyDue !== false;
}

/** Operator-facing projection for the next unchanged-state exploration wake. */
export function explorationWakeProjection(
  lastAutonomousTurnAt: number,
  explorationIntervalMs: number,
  ownedWorkPending: boolean,
  nowMs = Date.now(),
): { nextReviewAt: string | null; nextReview: string } {
  if (ownedWorkPending || explorationIntervalMs <= 0) {
    return { nextReviewAt: null, nextReview: 'on an actionable state change' };
  }
  const nextReviewMs = lastAutonomousTurnAt === 0
    ? nowMs
    : lastAutonomousTurnAt + explorationIntervalMs;
  const msUntilExploration = Math.max(0, nextReviewMs - nowMs);
  return {
    nextReviewAt: new Date(nextReviewMs).toISOString(),
    nextReview: msUntilExploration < 60_000
      ? 'soon'
      : `~${Math.round(msUntilExploration / 60_000)}min`,
  };
}

/** The work note in the idle "watching — ..." line.
 *
 * The heartbeat summary carries TWO scopes and conflating them is the whole
 * point of keeping them apart. The unprefixed keys are the Resident's OWN
 * lineage (`created_by='STRATEGIC_AGENT'` and its descendants); the `project_*`
 * keys are the whole project across every author. The orchestrator writes
 * nearly all real pipeline work, so a line built from the lineage count alone
 * announced "0 tasks in flight" while the fleet was saturated — a quiet system
 * that was not quiet.
 *
 * Both numbers are shown, labelled, and the project figure is only added when
 * it actually differs from the owned one, so an idle project still reads as one
 * short clause. Purely cosmetic: it schedules nothing.
 */
export function formatWatchingWorkNote(
  summary: Record<string, unknown> | null | undefined,
): string {
  const count = (key: string): number | null => {
    const value = summary?.[key];
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  };
  const owned = count('in_flight');
  if (owned === null) return '';
  const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const parts = [`${plural(owned, 'task')} in flight`];
  const projectInFlight = count('project_in_flight');
  if (projectInFlight !== null && projectInFlight !== owned) {
    const blocked = count('project_blocked') ?? 0;
    const parked = count('project_parked') ?? 0;
    const stalled = blocked + parked;
    parts.push(
      `${projectInFlight} project-wide`
      + (stalled > 0 ? `, ${stalled} blocked/parked` : ''),
    );
  }
  return `${parts.join('; ')}; `;
}

/** Stable, order-independent identity for the actionable watch receipts in one
 * heartbeat. Receipt bodies may gain additive fields; ids/state are the agenda
 * identity and therefore the only pacing inputs. */
export function firedWatchRevision(watches: StrategicWatchReceipt[]): string {
  return watches
    .map((watch) => `${watch.id}:${watch.state ?? 'fired'}`)
    .sort()
    .join('|');
}

export function wakeBarrierRevision(
  barriers: ResidentWakeBarrierReceipt[],
): string {
  return barriers.map((barrier) => barrier.id).sort().join('|');
}

/** Coalesce successful parallel child receipts at the drained-frontier barrier.
 * Direct Strategic spawns register a watch by default, so waking on every
 * COMPLETED child would otherwise defeat semantic task coalescing. Expiry,
 * failure, cancellation, and unknown receipt shapes remain immediately
 * actionable; successful receipts stay durable and all appear in the prompt
 * when the sibling frontier drains or another genuine turn occurs. */
export function actionableFiredWatchRevision(
  watches: StrategicWatchReceipt[],
  summary: Record<string, unknown> | null | undefined,
): string {
  const inFlight = summary?.in_flight;
  if (typeof inFlight !== 'number' || !Number.isFinite(inFlight) || inFlight <= 0) {
    return firedWatchRevision(watches);
  }
  return firedWatchRevision(watches.filter((watch) => {
    if (watch.state === 'expired') return true;
    const terminal = watch.terminal_status
      ?? (typeof watch.receipt?.terminal_status === 'string'
        ? watch.receipt.terminal_status
        : null);
    return terminal !== 'COMPLETED';
  }));
}

/** Whether unchanged-state exploration turns should stay suppressed. The compact
 * backend summary is advisory and may contain unknown additive keys. */
export function hasPendingOwnedWork(
  summary: Record<string, unknown> | null | undefined,
): boolean {
  const positive = (key: string): boolean => {
    const value = summary?.[key];
    return typeof value === 'number' && Number.isFinite(value) && value > 0;
  };
  // Fired watches and barrier messages have their own exact one-shot wake
  // identities. They stay durable/open until explicitly handled, so treating
  // their continued presence as pending execution would suppress exploration
  // forever if a turn failed to acknowledge/resolve them. Only work that can
  // still move on its own keeps the model asleep between semantic events.
  return positive('in_flight') || positive('pending_watches');
}

/**
 * Build the DYNAMIC autonomous self-prompt for one tick. Parameterized by the
 * rotating focus surfaces, recent ledger records (so the turn goes somewhere new
 * or deeper rather than re-polling), open FOUND leads (restated until acted on),
 * and a corrective flag (set after an unevidenced idle claim). Contract: complete
 * ONE meaningful unit of progress (may chain investigate→decide→act→verify and
 * batch a few ops), keep a standing agenda, and END with exactly one TICK-VERDICT
 * marker line. CLEAR is scoped to the ONE surface examined this turn — there is
 * no global "nothing to do", and cheap delta signals can never justify it.
 */
export function buildAutonomousPrompt(
  focus: ExamSurface[],
  recentRecords: ExamRecord[],
  openFound: ExamRecord[],
  corrective: boolean,
  maxOpsPerTurn = 3,
  decisionsRegisterText = '',
  operatorDecisionReview: PendingOperatorDecisionReview | boolean = false,
  firedWatches: StrategicWatchReceipt[] = [],
  firedBarriers: ResidentWakeBarrierReceipt[] = [],
): string {
  const lines: string[] = [];

  if (corrective && focus.length > 0) {
    lines.push(
      `CORRECTION: your last idle conclusion examined nothing (no fetch/read ran, or the verdict marker was missing). Examine ${focus[0].key} now — ${focus[0].hint} — and cite what you actually read BEFORE you may conclude CLEAR.`,
      '',
    );
  }

  if (operatorDecisionReview) {
    const review = typeof operatorDecisionReview === 'object' ? operatorDecisionReview : null;
    lines.push(
      'OPERATOR-DECISION REVIEW (required before normal autonomous work): a recent',
      'operator exchange ended without a classification marker. Review the recent',
      'handled operator conversation and emit EXACTLY ONE of these review outcomes:',
      '  OPERATOR-DECISION: <durable fact> — the exchange settled/corrected a course',
      '  OPERATOR-DECISION-REVIEW: NO-DECISION — routine Q&A/chat settled nothing',
      '  OPERATOR-DECISION-REVIEW: UNRESOLVED — evidence is unavailable/ambiguous',
      'If the metadata says an overflow batch remains, ordinary outcomes classify only',
      'the current exchange. Clear that batch only after reviewing ALL of its exchanges:',
      '  OPERATOR-DECISION-REVIEW: OVERFLOW-NO-DECISION — every omitted exchange was routine',
      '  OPERATOR-DECISION-REVIEW: OVERFLOW-DECISION: <complete durable facts for every omitted exchange>',
      'Do not guess. Only a recovered/NO-DECISION outcome for an explicit exchange, or',
      'one of the explicit OVERFLOW outcomes for the complete omitted batch, resolves it;',
      'UNRESOLVED, malformed output, or interruption keeps it armed for a later turn.',
      review
        ? `Pending review metadata: current exchange has ${review.messageIds.length} message id(s) and ${review.attempts} prior attempt(s); ${review.queuedExchanges?.length ?? 0} later exchange(s) remain explicitly queued${review.overflowed ? `, plus ${review.overflowExchanges ?? 1} omitted overflow exchange(s)` : ''}. Classify ONLY the current exchange unless issuing one of the explicit OVERFLOW outcomes for the complete omitted batch.`
        : '',
      review && review.messageIds.length > 0
        ? `Current exchange message id(s): ${review.messageIds.join(', ')}. If the excerpt is incomplete, run ./fetch message <id> before classifying it.`
        : '',
      review?.exchangeText
        ? `Current exchange excerpt (durable across compaction${review.exchangeTextTruncated ? '; truncated' : ''}): ${JSON.stringify(review.exchangeText)}`
        : '',
      (review?.overflowed && review.messageIds.length === 0) || review?.exchangeTextTruncated
        ? 'Some durable context was compacted. Use the excerpt, message identities, and any handled conversation context still available; emit UNRESOLVED if that evidence is insufficient. A valid recovered/NO-DECISION classification may resolve this review.'
        : '',
      '',
    );
  }

  if (firedWatches.length > 0) {
    lines.push(
      'FIRED TASK WATCHES (handle before normal rotating examination):',
      'These are durable tasks you explicitly waited on. Inspect the result and',
      'documents, report or act as appropriate, then acknowledge each receipt with',
      '`./strategic ack-watch --json \'{"watch_id":"<id>","disposition":"reported|acted|no_action|superseded","note":"<brief reason>"}\'`.',
    );
    for (const watch of firedWatches.slice(0, 20)) {
      const receipt = watch.receipt ?? {};
      const title = typeof receipt.title === 'string' ? receipt.title : 'task';
      const status = watch.terminal_status
        ?? (typeof receipt.terminal_status === 'string' ? receipt.terminal_status : watch.state ?? 'ready');
      const purpose = typeof watch.purpose === 'string' && watch.purpose
        ? `; purpose=${watch.purpose}`
        : '';
      const documents = Array.isArray(receipt.knowledge_document_ids) && receipt.knowledge_document_ids.length > 0
        ? `; documents=${receipt.knowledge_document_ids.join(',')}`
        : '';
      lines.push(`- watch=${watch.id}; task=${watch.target_task_id ?? 'unknown'}; ${title}; status=${status}${purpose}${documents}`.slice(0, 1200));
    }
    lines.push('');
  }

  if (firedBarriers.length > 0) {
    lines.push(
      'SEMANTIC OUTCOME RECEIPTS (handle before normal rotating examination):',
      'These exact server-authored receipts woke this turn. Inspect and act on the',
      'decision boundary; they are acknowledged automatically only after this turn completes.',
    );
    for (const barrier of firedBarriers.slice(0, 20)) {
      const eventType = barrier.event_type || 'outcome';
      const summary = barrier.summary || 'Outcome receipt ready';
      const context = barrier.context && Object.keys(barrier.context).length > 0
        ? `; context=${JSON.stringify(barrier.context)}`
        : '';
      lines.push(`- receipt=${barrier.id}; event=${eventType}; ${summary}${context}`.slice(0, 1800));
    }
    lines.push('');
  }

  lines.push(
    'No operator message is pending. Complete ONE meaningful unit of progress this',
    `turn. A unit may chain investigate → decide → act → verify, and may batch up to`,
    `${maxOpsPerTurn} applied ./strategic ops when they form one coherent unit. Depth is`,
    'welcome: read code, task outcomes, a direction, or the qualification findings',
    'before deciding — do not stop at revision numbers or counts.',
    '',
    'Tasks run concurrently after spawn_task. When bounded work is',
    'genuinely independent, dispatch parallel TASKS under the owning direction.',
    'Do not create directions to parallelize implementation or consume capacity;',
    'a direction is a durable outcome lane, and activating one creates recurring attention.',
    'When a specific result matters in a later turn, register it once with',
    '`./strategic watch-task --json \'{"target_task_id":"<task-id>","purpose":"<what to do when it finishes>"}\'`.',
    'Do not spend turns polling it: its terminal receipt will be placed ahead of normal',
    'agenda work until you handle it and call `ack-watch`.',
    '',
    'Maintain a standing agenda in your session memory; act on the highest-leverage',
    'item actionable NOW. If more actionable items remain after this turn, end with a',
    'literal line: AGENDA-CONTINUE',
    '',
    'Rotating examination focus (prefer the first; pick another ONLY with a concrete',
    'lead — your verdict MUST name the surface you actually examined):',
  );
  // Durable decisions register — full-fidelity record of recent tick verdicts,
  // rendered here so the model sees settled decisions before picking a surface.
  if (decisionsRegisterText) {
    lines.push('', decisionsRegisterText, '');
  }
  for (const s of focus) lines.push(`- ${s.key} — ${s.hint}`);

  if (recentRecords.length > 0) {
    lines.push('', 'Recently examined (go somewhere new or deeper — do not just re-poll these):');
    for (const r of recentRecords) {
      lines.push(`- ${r.surfaceKey} [${r.verdict}] ${r.note}`.slice(0, 200));
    }
  }

  if (openFound.length > 0) {
    lines.push('', 'Open leads (FOUND but not yet acted on — act on one or explicitly clear it):');
    for (const r of openFound) {
      lines.push(`- ${r.surfaceKey}: ${r.note}`.slice(0, 200));
    }
  }

  lines.push(
    '',
    'Operator visibility: do NOT send unsolicited durable messages. Use `./messages',
    "send` ONLY to reply to the operator or to surface something operator-actionable",
    '(a question, blocker, or real finding). Operator-facing status goes to',
    '`set-running-update` — pass a FILE PATH, not inline markdown:',
    '  write your markdown to /tmp/running_update.md first, then',
    '  `./strategic set-running-update /tmp/running_update.md`.',
    'The helper reads the file and wraps it as JSON; passing markdown as argv fails silently.',
    'Idle is stated here in the stream, never in the inbox.',
    '',
    'A surface you have NOT yet examined cannot be declared CLEAR on cheap delta',
    'signals alone — revision numbers, attempt counts, `git fetch`, unchanged payload',
    'fields: a first-time CLEAR must rest on what you actually opened and read.',
    'But a surface you ALREADY examined and found CLEAR, with no change signal since',
    '(no new outcome/cycle/PR/churn on a direction; no update elsewhere), may be',
    'SKIPPED this tick — trust the prior verdict, do not re-derive it. Re-examine it',
    'only when a change signal appears or its re-exam cooldown elapses; the rotation',
    'already deprioritizes settled-clear, unchanged surfaces so you can spend the',
    'turn where something actually moved.',
    '',
    'Proactive memory ops: call `./strategic set-memory <note>` when you discover a',
    'durable behavioral lesson, operator preference, project-specific pitfall, or',
    'reusable strategy that would benefit future sessions. Call `./strategic',
    'set-session-summary <summary>` to capture session state (current focus, open',
    'leads, in-flight context) so continuity survives a restart. Do NOT wait for an',
    'explicit operator request — if you learned something durable this turn, persist',
    'it now. Keep entries concise and specific; avoid journaling one-off observations.',
    '',
    'End this turn with EXACTLY ONE trailing marker line, one of:',
    '  TICK-VERDICT: ADVANCED — <what op(s) you applied and why>',
    '  TICK-VERDICT: FOUND <surface-key> — <the lead worth acting on next>',
    '  TICK-VERDICT: CLEAR <surface-key> — <what you actually opened/read, and why nothing there is actionable>',
    'CLEAR is scoped to the ONE surface you examined this turn — there is no global',
    '"nothing to do". An examination that ends CLEAR is a fully successful turn.',
  );

  return lines.join('\n');
}

// ── Durable exam-ledger helpers ───────────────────────────────────────────────
// The ledger lives only in process memory; these helpers serialize it as a
// bounded JSON string for the heartbeat ``note`` field and restore it from
// ``task.context._exam_ledger`` on startup, so a routine restart (deploy,
// runner restart) does not wipe open FOUND leads or reset tick rotation.

/**
 * Serialize the exam ledger for the heartbeat note. Caps note fields at
 * EXAM_NOTE_CAP, then drops oldest records until the JSON serialization fits
 * within EXAM_LEDGER_MAX_BYTES so the backend never receives a truncated blob
 * that would cause JSON.parse to throw on restart.
 */
export function serializeLedger(
  records: ExamRecord[],
  tick: number,
  corrective: boolean,
  operatorDecisionCorrective: boolean = false,
  operatorDecisionReview: PendingOperatorDecisionReview | null = null,
  operatorDecisionCapture: { total: number; captured: number } | null = null,
): string {
  const truncated = records.slice(-EXAM_RECORDS_CAP).map(r => ({
    ...r,
    note: r.note.slice(0, EXAM_NOTE_CAP),
  }));
  const encode = (selectedRecords: ExamRecord[], review: PendingOperatorDecisionReview | null): string =>
    JSON.stringify({
      records: selectedRecords,
      tick,
      corrective,
      operatorDecisionCorrective: operatorDecisionCorrective || review !== null,
      operatorDecisionReview: review,
      operatorDecisionCapture,
    });

  // Drop oldest records until the serialized byte length fits the budget.
  let start = 0;
  while (start <= truncated.length) {
    const candidate = encode(truncated.slice(start), operatorDecisionReview);
    if (Buffer.byteLength(candidate, 'utf8') <= EXAM_LEDGER_MAX_BYTES) {
      return candidate;
    }
    start++;
  }

  if (operatorDecisionReview !== null) {
    const compactExchange = (exchange: PendingOperatorDecisionReview): PendingOperatorDecisionReview => ({
      status: 'pending',
      messageIds: exchange.messageIds.slice(-1),
      ...(exchange.exchangeText ? { exchangeText: exchange.exchangeText } : {}),
      ...(exchange.exchangeTextTruncated || exchange.messageIds.length > 1
        ? { exchangeTextTruncated: true as const } : {}),
      startedAt: exchange.startedAt,
      attempts: exchange.attempts,
    });
    const compactedCurrent = compactExchange(operatorDecisionReview);
    const compactedQueue = (operatorDecisionReview.queuedExchanges ?? []).map(exchange => ({
      messageIds: exchange.messageIds.slice(-1),
      ...(exchange.exchangeTextTruncated || exchange.exchangeText || exchange.messageIds.length > 1
        ? { exchangeTextTruncated: true as const } : {}),
      startedAt: exchange.startedAt,
      attempts: exchange.attempts,
    }));
    let queueLength = compactedQueue.length;
    while (queueLength >= 0) {
      const review: PendingOperatorDecisionReview = {
        ...compactedCurrent,
        ...(queueLength > 0 ? { queuedExchanges: compactedQueue.slice(0, queueLength) } : {}),
        ...(operatorDecisionReview.overflowed || queueLength < compactedQueue.length
          ? { overflowed: true } : {}),
        ...(operatorDecisionReview.overflowExchanges || queueLength < compactedQueue.length
          ? { overflowExchanges: (operatorDecisionReview.overflowExchanges ?? 0) + (compactedQueue.length - queueLength) }
          : {}),
      };
      const candidate = encode([], review);
      if (Buffer.byteLength(candidate, 'utf8') <= EXAM_LEDGER_MAX_BYTES) return candidate;
      queueLength--;
    }

    const sentinel: PendingOperatorDecisionReview = {
      status: 'pending',
      messageIds: [],
      exchangeTextTruncated: true,
      startedAt: '',
      attempts: operatorDecisionReview.attempts,
      overflowed: true,
      overflowExchanges: Math.max(
        1,
        (operatorDecisionReview.overflowExchanges ?? 0)
          + (operatorDecisionReview.messageIds.length > 0 ? 1 : 0)
          + (operatorDecisionReview.queuedExchanges?.length ?? 0),
      ),
    };
    const candidate = encode([], sentinel);
    if (Buffer.byteLength(candidate, 'utf8') <= EXAM_LEDGER_MAX_BYTES) return candidate;
  }

  // Fixed scalar fields are bounded in normal operation. Keep a defensive
  // minimal valid checkpoint if a future caller supplies pathological values.
  return JSON.stringify({
    records: [], tick: 0, corrective: true,
    operatorDecisionCorrective: operatorDecisionCorrective || operatorDecisionReview !== null,
    operatorDecisionReview: operatorDecisionReview === null ? null : {
      status: 'pending', messageIds: [], exchangeTextTruncated: true,
      startedAt: '', attempts: 0, overflowed: true, overflowExchanges: 1,
    },
    operatorDecisionCapture: null,
  });
}

/**
 * Restore the exam ledger from a persisted string (``task.context._exam_ledger``
 * or the echoed ``exam_ledger`` heartbeat field). Returns null on any parse
 * error so the caller falls back to a clean slate.
 */
export function restoreLedger(
  value: string | undefined | null,
): {
  records: ExamRecord[];
  tick: number;
  corrective: boolean;
  operatorDecisionCorrective: boolean;
  operatorDecisionReview: PendingOperatorDecisionReview | null;
  operatorDecisionCapture: { total: number; captured: number } | null;
} | null {
  if (!value || typeof value !== 'string') return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
    const p = parsed as Record<string, unknown>;
    const rawRecords = Array.isArray(p.records) ? p.records : [];
    const records: ExamRecord[] = rawRecords
      .filter((r): r is Record<string, unknown> => r !== null && typeof r === 'object' && !Array.isArray(r))
      .filter(r => typeof r.surfaceKey === 'string')
      .slice(0, EXAM_RECORDS_CAP)
      .map(r => ({
        surfaceKey: String(r.surfaceKey),
        atTick: typeof r.atTick === 'number' ? r.atTick : 0,
        at: typeof r.at === 'string' ? r.at : '',
        verdict: (['clear', 'found', 'advanced', 'unevidenced', 'interrupted'].includes(String(r.verdict))
          ? r.verdict : 'unevidenced') as ExamRecord['verdict'],
        note: typeof r.note === 'string' ? r.note.slice(0, EXAM_NOTE_CAP) : '',
        // Preserve the change-gate baseline fingerprint across restart so a
        // settled surface stays skippable (and its cross-restart delta stays
        // detectable) rather than restoring to a signal-less record that would
        // force one redundant re-read. Omitted when absent (legacy blobs).
        ...(typeof r.signal === 'string' && r.signal ? { signal: r.signal } : {}),
      }));
    const tick = typeof p.tick === 'number' && Number.isFinite(p.tick) ? p.tick : 0;
    const corrective = typeof p.corrective === 'boolean' ? p.corrective : false;
    // Legacy blobs (written before this field existed) lack the key and restore
    // to false so an old checkpoint never spuriously raises the corrective frame.
    const operatorDecisionCorrective = typeof p.operatorDecisionCorrective === 'boolean'
      ? p.operatorDecisionCorrective : false;
    const rawReview = p.operatorDecisionReview;
    let operatorDecisionReview: PendingOperatorDecisionReview | null = null;
    if (rawReview && typeof rawReview === 'object' && !Array.isArray(rawReview)) {
      const review = rawReview as Record<string, unknown>;
      const rawIds = Array.isArray(review.messageIds) ? review.messageIds : [];
      const parsedQueuedExchanges = Array.isArray(review.queuedExchanges)
        ? review.queuedExchanges
          .filter((exchange): exchange is Record<string, unknown> =>
            exchange !== null && typeof exchange === 'object' && !Array.isArray(exchange))
          .map(exchange => ({
            messageIds: (Array.isArray(exchange.messageIds) ? exchange.messageIds : [])
              .filter((id): id is string => typeof id === 'string' && id.length > 0)
              .slice(-OPERATOR_DECISION_REVIEW_MESSAGE_CAP),
            ...(typeof exchange.exchangeText === 'string' && exchange.exchangeText.trim()
              ? { exchangeText: exchange.exchangeText.trim().slice(0, OPERATOR_DECISION_REVIEW_TEXT_CAP) }
              : {}),
            ...(exchange.exchangeTextTruncated === true
              || (typeof exchange.exchangeText === 'string'
                && exchange.exchangeText.trim().length > OPERATOR_DECISION_REVIEW_TEXT_CAP)
              ? { exchangeTextTruncated: true as const } : {}),
            startedAt: typeof exchange.startedAt === 'string' ? exchange.startedAt.slice(0, 64) : '',
            attempts: typeof exchange.attempts === 'number' && Number.isFinite(exchange.attempts)
              ? Math.max(0, Math.floor(exchange.attempts)) : 0,
          }))
        : [];
      const queuedExchanges = parsedQueuedExchanges;
      const overflowed = review.overflowed === true
        || (typeof review.overflowExchanges === 'number' && review.overflowExchanges > 0)
        || parsedQueuedExchanges.length > queuedExchanges.length;
      const overflowExchanges = Math.max(
        typeof review.overflowExchanges === 'number' && Number.isFinite(review.overflowExchanges)
          ? Math.floor(review.overflowExchanges) : 0,
        parsedQueuedExchanges.length - queuedExchanges.length,
        overflowed ? 1 : 0,
      );
      operatorDecisionReview = {
        status: 'pending',
        messageIds: rawIds
          .filter((id): id is string => typeof id === 'string' && id.length > 0)
          .slice(-OPERATOR_DECISION_REVIEW_MESSAGE_CAP),
        ...(typeof review.exchangeText === 'string' && review.exchangeText.trim()
          ? { exchangeText: review.exchangeText.trim().slice(0, OPERATOR_DECISION_REVIEW_TEXT_CAP) }
          : {}),
        ...(review.exchangeTextTruncated === true
          || (typeof review.exchangeText === 'string'
            && review.exchangeText.trim().length > OPERATOR_DECISION_REVIEW_TEXT_CAP)
          ? { exchangeTextTruncated: true } : {}),
        startedAt: typeof review.startedAt === 'string' ? review.startedAt.slice(0, 64) : '',
        attempts: typeof review.attempts === 'number' && Number.isFinite(review.attempts)
          ? Math.max(0, Math.floor(review.attempts)) : 0,
        ...(queuedExchanges.length > 0 ? { queuedExchanges } : {}),
        ...(overflowed ? { overflowed: true } : {}),
        ...(overflowed ? { overflowExchanges } : {}),
      };
    } else if (operatorDecisionCorrective) {
      operatorDecisionReview = {
        status: 'pending',
        messageIds: [],
        startedAt: '',
        attempts: 0,
      };
    }
    const rawCapture = p.operatorDecisionCapture;
    const capture = rawCapture as Record<string, unknown> | null;
    const operatorDecisionCapture = capture && typeof capture === 'object' && !Array.isArray(capture)
      ? {
        total: typeof capture.total === 'number' && Number.isFinite(capture.total)
          ? Math.max(0, Math.floor(capture.total)) : 0,
        captured: typeof capture.captured === 'number' && Number.isFinite(capture.captured)
          ? Math.max(0, Math.floor(capture.captured)) : 0,
      }
      : null;
    return {
      records,
      tick,
      corrective,
      operatorDecisionCorrective: operatorDecisionReview !== null,
      operatorDecisionReview,
      operatorDecisionCapture,
    };
  } catch {
    return null;
  }
}

/**
 * Refresh the surface catalog from a heartbeat's ``active_direction_ids``:
 * add direction surfaces for newly active directions, prune surfaces for
 * retired ones, and leave all non-direction surfaces (fetch verbs, code dirs,
 * etc.) untouched. Preserves catalog ordering (directions first, then fetch,
 * then code/knowledge/etc.).
 */
export function refreshSurfaceCatalog(
  catalog: ExamSurface[],
  activeDirectionIds: string[],
  payload: Record<string, unknown>,
): ExamSurface[] {
  const activeSet = new Set(activeDirectionIds);

  // Prune direction surfaces for retired/absent directions.
  const pruned = catalog.filter(
    s => s.kind !== 'direction' || activeSet.has(s.key.slice('direction:'.length)),
  );

  // Build label map from payload's directions_summary (best-effort enrichment).
  const payloadDirs = Array.isArray(payload.directions_summary)
    ? payload.directions_summary as Array<Record<string, unknown>> : [];
  const payloadDirText = new Map<string, string>();
  for (const d of payloadDirs) {
    const id = typeof d.id === 'string' ? d.id : '';
    const text = typeof d.text === 'string' ? d.text.trim().slice(0, 80) : '';
    if (id) payloadDirText.set(id, text);
  }

  // Determine which direction keys are already in the (pruned) catalog.
  const existingKeys = new Set(pruned.map(s => s.key));

  // Build new direction surfaces in the order activeDirectionIds provides.
  const newDirs: ExamSurface[] = [];
  for (const id of activeDirectionIds) {
    const key = `direction:${id}`;
    if (!existingKeys.has(key)) {
      const text = payloadDirText.get(id) ?? '';
      newDirs.push({
        key,
        kind: 'direction',
        label: text ? `direction ${id} — ${text}` : `direction ${id}`,
        hint: `./fetch direction ${id} — judge its recent outcomes/churn; is it still productive or done?`,
      });
    }
  }

  if (newDirs.length === 0) return pruned;

  // Insert new directions before the first non-direction surface to maintain
  // the ordering buildSurfaceCatalog establishes (directions always first).
  const firstNonDir = pruned.findIndex(s => s.kind !== 'direction');
  if (firstNonDir === -1) return [...pruned, ...newDirs];
  return [...pruned.slice(0, firstNonDir), ...newDirs, ...pruned.slice(firstNonDir)];
}

// ── Durable decisions register: cross-tick decision memory ──────────────────
// Unlike the exam ledger (which truncates notes and caps retained history in
// the prompt to 6 entries), the decisions register stores full-fidelity tick
// verdicts and is rendered into BOTH the autonomous tick prompt and the
// compaction checkpoint. This prevents the strategist from re-deriving
// settled facts or re-proposing already-executed corrections tick over tick
// and across session restarts.

/**
 * Format the decisions register for inclusion in the autonomous prompt or
 * compaction checkpoint. Returns a markdown section header followed by one
 * line per entry (most recent last). Returns empty string when the register
 * is empty, so the caller can omit the section cleanly.
 */
export function decisionsRegisterText(
  register: DecisionsRegisterEntry[],
  limit = 20,
): string {
  if (register.length === 0) return '';
  const slice = register.slice(-limit);
  const lines: string[] = ['## Decisions register (durable cross-tick memory)'];
  for (const entry of slice) {
    const noteStr = entry.note ? ` — ${entry.note}` : '';
    lines.push(`- tick-${entry.atTick} [${entry.verdict}] ${entry.surfaceKey || '(none)'}${noteStr} (${entry.at})`);
  }
  return lines.join('\n');
}

/** Max byte size for the serialized decisions register persisted in the heartbeat. */
export const DECISIONS_REG_MAX_BYTES = 32_000;

/**
 * Serialize the decisions register for heartbeat persistence.
 * The result is stored as ``_decisions_reg`` in task.context and restored on
 * full session restart so the strategist retains its settled-decision memory
 * across process death/VM recycles (not just across in-process compaction).
 */
export function serializeDecisionsReg(register: DecisionsRegisterEntry[]): string {
  // Priority-aware cap: evict the oldest `clear` before any protected
  // operator/advanced/found entry, so persistence never front-drops a scarce
  // operator settlement just because it is the oldest entry.
  const working = register.slice();
  while (working.length > DECISIONS_REGISTER_CAP) {
    working.splice(decisionEvictIndex(working), 1);
  }
  let candidate = JSON.stringify({ entries: working });
  if (Buffer.byteLength(candidate, 'utf8') <= DECISIONS_REG_MAX_BYTES) {
    return candidate;
  }
  // Still over the byte budget: keep evicting (clears first, then oldest
  // overall) until it fits.
  while (working.length > 0) {
    working.splice(decisionEvictIndex(working), 1);
    candidate = JSON.stringify({ entries: working });
    if (Buffer.byteLength(candidate, 'utf8') <= DECISIONS_REG_MAX_BYTES) {
      return candidate;
    }
  }
  return JSON.stringify({ entries: [] });
}

/**
 * Restore the decisions register from a persisted string
 * (``task.context._decisions_reg``). Returns null on parse error so the
 * caller falls back to a clean empty register.
 */
export function restoreDecisionsReg(
  value: string | undefined | null,
): DecisionsRegisterEntry[] | null {
  if (!value || typeof value !== 'string') return null;
  try {
    const parsed: unknown = JSON.parse(value);
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
    const p = parsed as Record<string, unknown>;
    const rawEntries = Array.isArray(p.entries) ? p.entries : [];
    const entries: DecisionsRegisterEntry[] = rawEntries
      .filter((e): e is Record<string, unknown> => e !== null && typeof e === 'object' && !Array.isArray(e))
      .filter(e => typeof e.surfaceKey === 'string')
      .slice(0, DECISIONS_REGISTER_CAP)
      .map(e => ({
        atTick: typeof e.atTick === 'number' ? e.atTick : 0,
        at: typeof e.at === 'string' ? e.at : '',
        surfaceKey: String(e.surfaceKey),
        verdict: typeof e.verdict === 'string' ? e.verdict : '',
        note: typeof e.note === 'string' ? e.note : '',
      }));
    return entries;
  } catch {
    return null;
  }
}
