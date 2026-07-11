/**
 * Pure, dependency-free helpers for the resident strategic handler
 * (strategic-resident.ts). Kept free of any `../index.js` import so the loop
 * logic — config resolution, message-cursor advancement, operator-message
 * combination, and prompt assembly — is unit-testable in isolation
 * (strategic-resident-lib.test.ts) without mocking the agent's heavy index
 * surface.
 */

import { promptOps } from './strategic-ops-registry.js';

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
  server_time?: string;
}

export interface SessionHeartbeatResponse {
  authority_held?: boolean;
  leased_until?: string;
  stop_requested?: boolean;
  status?: string;
  /** Monotonic marker over strategist-relevant change sources (task transitions,
   *  operator messages, direction/cycle mutations). State-gated pacing fires an
   *  autonomous turn when this differs from the rev recorded at the last turn.
   *  ABSENT (older backend that does not populate it) degrades to floor-tick
   *  pacing — the same forward-compat pattern `authority_held` uses. */
  work_state_rev?: string;
  /** Optional compact counts feeding the tick context (in-flight tasks, terminal
   *  transitions since last look, new operator messages). Advisory only. */
  work_state_summary?: Record<string, unknown>;
  /** Serialized exam-ledger echoed from the heartbeat note, or null. Agents
   *  ignore this field; it is only used for roundtrip integrity checks. */
  exam_ledger?: string | null;
  /** Live direction keys (direction_key strings) in the project that are not
   *  yet retired. Used to refresh the surface catalog between restarts so
   *  new/retired directions enter/leave the rotation mid-session. */
  active_direction_ids?: string[];
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
  /** Floor-tick interval for autonomous turns (ms): fire even with no state change so
   *  the session never goes fully dark. Default: AUTONOMOUS_FLOOR_TICK_MS (600s). */
  minTickMs: number;
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
 * per-turn timeout, 600s floor tick, 300s autonomous stall budget, 3 ops/turn.
 */
export function residentConfig(payload: Record<string, unknown>): ResidentConfig {
  const idleTickSec = num(payload.strategic_session_idle_tick_seconds, 5);
  const messagePollSec = num(payload.strategic_session_message_poll_seconds, 1.5);
  const turnTimeoutSec = num(payload.strategic_session_turn_timeout_seconds, 120);
  const minTickSec = num(payload.strategic_session_min_tick_seconds, 600);
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
  return {
    idleTickMs,
    // Poll operator messages faster than the heartbeat tick so a turn starts
    // soon after send, but never below a 750ms floor (don't hammer) and never
    // slower than the idle tick.
    messagePollMs: Math.min(idleTickMs, Math.max(750, Math.round(messagePollSec * 1000))),
    turnTimeoutMs: Math.max(15_000, Math.round(turnTimeoutSec * 1000)),
    autonomousEnabled,
    // Never poll slower than the idle tick (600s floor keeps the session lively).
    minTickMs: Math.max(idleTickMs, Math.round(minTickSec * 1000)),
    autonomousTurnTimeoutMs: Math.max(15_000, Math.round(autonomousTurnTimeoutSec * 1000)),
    maxOpsPerTurn,
    compactionThresholdTokens,
    compactionCooldownMs: Math.max(60_000, Math.round(compactionCooldownSec * 1000)),
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

export interface CompactionCheckpointContext {
  /** Condensed role identity section (from the original bootstrap). */
  roleIdentity: string;
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
  /** Current vision JSON (payload.vision). */
  visionJson?: string;
  /** Formatted standing behavioral lessons (payload.strategic_memory_notes). */
  strategicMemoryNotesText?: string;
  /** Formatted open operator messages text (payload.open_messages). */
  openMessagesText?: string;
  /** Prior session context text (ctx._session_summary). */
  priorSessionContextText?: string;
  /** Formatted active directions summary (payload.directions_summary). */
  directionsText?: string;
  /** Running update text (payload.running_update). */
  runningUpdate?: string;
}

/**
 * Build the compaction checkpoint prompt — a condensed context package
 * delivered to a FRESH subprocess so the session continues coherently
 * after a context-window compaction restart.
 *
 * This is NOT the full bootstrap prompt. It preserves high-signal content:
 * (i) resident role identity and tool descriptions, (ii) the current exam
 * ledger state, (iii) a brief summary of recent operator interactions and
 * key decisions made, (iv) the current autonomous state.
 *
 * Pure and unit-testable — no imports from index.js.
 */
export function buildCompactionCheckpoint(ctx: CompactionCheckpointContext): string {
  const sections: string[] = [];

  // (i) Condensed role identity — the session is resuming, not starting fresh.
  sections.push(`# Session checkpoint — compaction restart

The resident session's Claude subprocess was restarted because the context
window approached its limit. This is a CHECKPOINT restore, not a fresh start.
Your previous session was compacted; you retain your role, tools, and all
durable state. Continue operating exactly where you left off.

${ctx.roleIdentity}`);

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
  if (ctx.visionJson?.trim()) {
    grounding.push(`### Vision\n\`\`\`json\n${ctx.visionJson.trim()}\n\`\`\``);
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
    sections.push(`## Self-model grounding\n${grounding.join('\n\n')}`);
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

/** Content block types for multimodal message delivery. */
export interface TextBlock { type: 'text'; text: string; }
export interface ImageBlockBase64Source { type: 'base64'; media_type: string; data: string; }
export interface ImageBlockUrlSource { type: 'url'; url: string; media_type: string; name?: string; }
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

export type AttachmentFetch = (url: string) => Promise<AttachmentFetchResponse>;

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
    const host = url.hostname.toLowerCase();
    const gcsHost = host === 'storage.googleapis.com' || host.endsWith('.storage.googleapis.com');
    if (!gcsHost) return false;
    return url.searchParams.has('X-Goog-Signature');
  } catch {
    return false;
  }
}

function attachmentUnavailableBlock(source: ImageBlockUrlSource, reason: string): TextBlock {
  const label = source.name ? ` "${source.name}"` : '';
  return { type: 'text', text: `[Attached image${label} could not be loaded: ${reason}.]` };
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
      let imageCount = 0;
      for (const att of raw as Array<Record<string, unknown>>) {
        if (imageCount >= MAX_IMAGES_PER_TURN) break;
        if (str(att.type) === 'image') {
          const mediaType = normalizeImageMediaType(att.mime_type);
          if (!mediaType) continue;
          // Inline base64 data — preferred when available.
          if (typeof att.data === 'string' && att.data && att.data.length <= MAX_IMAGE_BASE64_LENGTH) {
            blocks.push({
              type: 'image',
              source: { type: 'base64', media_type: mediaType, data: att.data },
            });
            imageCount++;
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
 * Download failures degrade to a text notice so the operator turn still runs.
 */
export async function materializeContentBlocks(
  blocks: ContentBlock[],
  fetchImpl: AttachmentFetch = async (url: string) => {
    if (typeof globalThis.fetch !== 'function') throw new Error('fetch unavailable');
    return globalThis.fetch(url);
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
        const resp = await fetchImpl(block.url);
        if (!resp.ok) {
          result.push({ type: 'text', text: `[Attachment "${block.name}" could not be loaded: download failed (${resp.status}).]` });
          continue;
        }
        const bytes = Buffer.from(await resp.arrayBuffer());
        const sliced = bytes.length > MAX_TEXT_ATTACHMENT_BYTES ? bytes.subarray(0, MAX_TEXT_ATTACHMENT_BYTES) : bytes;
        const text = sliced.toString('utf-8');
        const truncNote = bytes.length > MAX_TEXT_ATTACHMENT_BYTES ? '\n[... truncated]' : '';
        result.push({ type: 'text', text: `[Attachment: ${block.name}]\n${text}${truncNote}` });
      } catch {
        result.push({ type: 'text', text: `[Attachment "${block.name}" could not be loaded: download failed.]` });
      }
      continue;
    }
    if (block.type !== 'image' || block.source.type !== 'url') {
      result.push(block);
      continue;
    }

    const source = block.source;
    if (!isAllowedAttachmentDownloadUrl(source.url)) {
      result.push(attachmentUnavailableBlock(source, 'unsupported download URL'));
      continue;
    }
    try {
      const resp = await fetchImpl(source.url);
      if (!resp.ok) {
        result.push(attachmentUnavailableBlock(source, `download failed (${resp.status})`));
        continue;
      }
      const lengthHeader = resp.headers.get('content-length');
      const contentLength = lengthHeader ? Number.parseInt(lengthHeader, 10) : 0;
      if (Number.isFinite(contentLength) && contentLength > MAX_IMAGE_DOWNLOAD_BYTES) {
        result.push(attachmentUnavailableBlock(source, 'image is too large'));
        continue;
      }
      const bytes = Buffer.from(await resp.arrayBuffer());
      if (bytes.length > MAX_IMAGE_DOWNLOAD_BYTES) {
        result.push(attachmentUnavailableBlock(source, 'image is too large'));
        continue;
      }
      const data = bytes.toString('base64');
      if (data.length > MAX_IMAGE_BASE64_LENGTH) {
        result.push(attachmentUnavailableBlock(source, 'image is too large'));
        continue;
      }
      const headerMediaType = normalizeImageMediaType(resp.headers.get('content-type'));
      result.push({
        type: 'image',
        source: {
          type: 'base64',
          media_type: headerMediaType || source.media_type,
          data,
        },
      });
    } catch {
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
  return result;
}

export interface BootstrapContext {
  contextPrefix: string;
  projectName: string;
  problemModelJson: string;
  runningUpdate: string;
  directionsText: string;
  stagedDirectionsText?: string;
  retiredDirectionsText: string;
  previousDecisionOutcomesText: string;
  /** Formatted strategic memory notes text (one line per entry, e.g. "- [ts] note").
   *  Empty string or whitespace → section omitted. Absent or omitted defaults to ''. */
  strategicMemoryNotesText?: string;
  visionJson: string;
  openMessagesText: string;
  sessionId: string;
  fetchGuidanceText?: string;
  priorSessionContextText?: string;

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
  conversational answer to the operator with \`./messages send '<answer>'\` or,
  for apostrophe-heavy or multiline replies, \`./messages send --stdin\` with a
  heredoc or pipe. The live stream is a transcript, but the durable chat reply
  the operator reads is the message you send. Every reply to an operator
  message REQUIRES a \`./messages send\`; do not answer only in the stream.
- **Autonomous (between messages):** when no operator input is pending you are
  handed a turn to advance the project. Complete ONE meaningful unit of progress —
  refine the problem model, open/refine/retire a direction, spawn needed work,
  refine the running update, OR examine one project surface for work worth doing.
  An examination that ends CLEAR (the one surface you looked at holds nothing
  actionable right now) is a fully successful turn — looking is real progress.
  Busy-work means unneeded MUTATION; examination is never busy-work. Then stop so
  the loop can check for operator input again. Your autonomous turn can be
  interrupted the moment the operator speaks.

Dispatched work runs concurrently and independently of your own turn-by-turn
reasoning: once you \`spawn_task\`, \`spawn_fix\`, or \`create_direction\` (and the
autonomous research pipeline behind each active direction), that work executes
in the pipeline regardless of your cadence — you do NOT have to finish or watch
one before opening the next. When two pieces of intended work are genuinely
independent (no shared files, no logical dependency, no real conflict risk),
dispatching BOTH now is the DEFAULT — do not stage one behind the other merely
to avoid context-switching, since there is no context to switch: the pipeline
carries them in parallel. Sequence work only when there is a real dependency or
conflict (the \`conflicts_with\` field marks that case).

Never wait on the operator to proceed. Never claim the session is "done" — it
ends only when the operator stops it.

## Your tools (bash helpers already provisioned in this workspace)
- \`./strategic <op> <json|file>\` — apply strategic operations (${promptOps().map(e => e.alias).join(', ')}).
  These are your MUTATING actions on the cloud source of truth.
- \`./fetch <kind> <id>\` — pull full detail when context was trimmed. The
  canonical verb list and per-verb guidance are below.
- \`./design-doc <get|list|set|delete> <key>\` — read/update design-doc sections.
- \`./messages send '<text>'\` / \`./messages send --stdin\` / \`./messages resolve <ref> '<how>'\` — talk to the
  operator and close addressed messages. Use \`--stdin\` for apostrophe-heavy or
  multiline replies.

${ctx.fetchGuidanceText?.trim() ? `${ctx.fetchGuidanceText.trim()}\n\n` : ''}

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
  if (ctx.visionJson.trim()) {
    sections.push(`## Vision\n\`\`\`json\n${ctx.visionJson.trim()}\n\`\`\``);
  }
  if (ctx.priorSessionContextText?.trim()) {
    sections.push(`## Prior session context\n${ctx.priorSessionContextText.trim()}`);
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
}

/** Cap on retained exam records (bounds a multi-hour session's memory). */
export const EXAM_RECORDS_CAP = 200;
/** Default number of focus surfaces surfaced in the prompt each tick. */
export const FOCUS_LIST_SIZE = 3;
/** Default floor-tick interval (ms): even with no state change, review at least
 *  this often. Deliberately under the continuation-lease TTL so a session never
 *  goes fully dark, and matches design doc 9f769438's 600s floor. */
export const AUTONOMOUS_FLOOR_TICK_MS = 600_000;
export const EXAM_NOTE_CAP = 240;
/**
 * Byte budget for the serialized exam ledger. Must stay below the backend's
 * _EXAM_LEDGER_MAX_BYTES (20_000) so the stored value is always valid JSON.
 * Set to 16_000 to leave headroom for the surrounding heartbeat note envelope.
 */
export const EXAM_LEDGER_MAX_BYTES = 16_000;

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
 * The next focus surfaces for this tick: never-examined surfaces first (in
 * catalog order, so freshly-added directions are auto-prioritized), then
 * least-recently-examined (LRU by the max tick a record touched the surface).
 * Records whose surface is no longer in the catalog are ignored (pruned).
 * Returns at most `n` surfaces.
 */
export function nextFocus(
  catalog: ExamSurface[],
  records: ExamRecord[],
  n: number = FOCUS_LIST_SIZE,
): ExamSurface[] {
  const lastTick = new Map<string, number>();
  for (const r of records) {
    const prev = lastTick.get(r.surfaceKey);
    if (prev === undefined || r.atTick > prev) lastTick.set(r.surfaceKey, r.atTick);
  }
  const never: ExamSurface[] = [];
  const examined: ExamSurface[] = [];
  for (const s of catalog) {
    if (lastTick.has(s.key)) examined.push(s);
    else never.push(s);
  }
  examined.sort((a, b) => (lastTick.get(a.key) ?? 0) - (lastTick.get(b.key) ?? 0));
  return [...never, ...examined].slice(0, Math.max(0, n));
}

/**
 * Parse the trailing TICK-VERDICT marker line from a turn's result text. Scans
 * from the LAST line so a mid-turn mention cannot be mistaken for the verdict.
 * ADVANCED carries no surface (it is about applied ops); FOUND/CLEAR name the ONE
 * surface examined. Returns null when no valid marker is present (missing/garbled
 * → treated as a contract violation by classifyTickOutcome).
 */
export function parseTickVerdict(
  resultText: string,
): { verdict: 'advanced' | 'found' | 'clear'; surfaceKey: string; note: string } | null {
  if (typeof resultText !== 'string') return null;
  const lines = resultText.split('\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    const m = /^\s*TICK-VERDICT:\s*(ADVANCED|FOUND|CLEAR)\b\s*(.*)$/i.exec(lines[i]);
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

/** Whether a turn declared more actionable work remains (a literal AGENDA-CONTINUE
 *  line), gating an immediate re-tick in shouldRunAutonomousTurn. */
export function parseAgendaContinue(resultText: string): boolean {
  return /^\s*AGENDA-CONTINUE\b/im.test(typeof resultText === 'string' ? resultText : '');
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

  const verdict = parseTickVerdict(params.resultText);
  const agendaContinue = parseAgendaContinue(params.resultText);

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
   *  populate it → this signal is skipped and pacing degrades to the floor tick). */
  currentWorkStateRev?: string | null;
  /** the rev recorded at the last autonomous turn */
  lastTurnWorkStateRev?: string | null;
  /** the previous autonomous turn ended AGENDA-CONTINUE */
  previousTurnAgendaContinue: boolean;
  /** an operator turn just completed (act on fresh conversation) */
  operatorTurnJustCompleted: boolean;
  /** ms since the last autonomous turn (Infinity before the first) */
  msSinceLastAutonomousTurn: number;
  /** floor-tick interval; default AUTONOMOUS_FLOOR_TICK_MS */
  floorTickMs?: number;
}

/**
 * Pure pacing predicate: run an autonomous turn when ANY of — the world moved
 * (heartbeat work_state_rev differs from the rev at the last turn), the agent
 * declared more work (AGENDA-CONTINUE), an operator turn just completed, or the
 * floor tick elapsed. ABSENT work_state_rev degrades to floor-tick pacing.
 */
export function shouldRunAutonomousTurn(state: AutonomousTurnGateState): boolean {
  if (state.operatorTurnJustCompleted) return true;
  if (state.previousTurnAgendaContinue) return true;
  const rev = state.currentWorkStateRev;
  if (rev !== undefined && rev !== null && rev !== state.lastTurnWorkStateRev) return true;
  const floor = state.floorTickMs ?? AUTONOMOUS_FLOOR_TICK_MS;
  return state.msSinceLastAutonomousTurn >= floor;
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
): string {
  const lines: string[] = [];

  if (corrective && focus.length > 0) {
    lines.push(
      `CORRECTION: your last idle conclusion examined nothing (no fetch/read ran, or the verdict marker was missing). Examine ${focus[0].key} now — ${focus[0].hint} — and cite what you actually read BEFORE you may conclude CLEAR.`,
      '',
    );
  }

  lines.push(
    'No operator message is pending. Complete ONE meaningful unit of progress this',
    `turn. A unit may chain investigate → decide → act → verify, and may batch up to`,
    `${maxOpsPerTurn} applied ./strategic ops when they form one coherent unit. Depth is`,
    'welcome: read code, task outcomes, a direction, or the qualification findings',
    'before deciding — do not stop at revision numbers or counts.',
    '',
    'Dispatched work runs concurrently: once you spawn_task, spawn_fix, or',
    'create_direction, that work executes in the pipeline regardless of your cadence.',
    'When two pieces of work are genuinely independent (no shared files, no logical',
    'dependency), dispatch BOTH now rather than staging one behind the other.',
    '',
    'Maintain a standing agenda in your session memory; act on the highest-leverage',
    'item actionable NOW. If more actionable items remain after this turn, end with a',
    'literal line: AGENDA-CONTINUE',
    '',
    'Rotating examination focus (prefer the first; pick another ONLY with a concrete',
    'lead — your verdict MUST name the surface you actually examined):',
  );
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
    'Cheap delta signals — revision numbers, attempt counts, `git fetch`, unchanged',
    'payload fields — may be checked but can NEVER justify a CLEAR: "no change since',
    'the last tick" is the expected steady state, not evidence a surface is clear.',
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
): string {
  const truncated = records.slice(-EXAM_RECORDS_CAP).map(r => ({
    ...r,
    note: r.note.slice(0, EXAM_NOTE_CAP),
  }));
  // Drop oldest records until the serialized byte length fits the budget.
  let start = 0;
  while (start < truncated.length) {
    const candidate = JSON.stringify({ records: truncated.slice(start), tick, corrective });
    if (Buffer.byteLength(candidate, 'utf8') <= EXAM_LEDGER_MAX_BYTES) {
      return candidate;
    }
    start++;
  }
  return JSON.stringify({ records: [], tick, corrective });
}

/**
 * Restore the exam ledger from a persisted string (``task.context._exam_ledger``
 * or the echoed ``exam_ledger`` heartbeat field). Returns null on any parse
 * error so the caller falls back to a clean slate.
 */
export function restoreLedger(
  value: string | undefined | null,
): { records: ExamRecord[]; tick: number; corrective: boolean } | null {
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
      }));
    const tick = typeof p.tick === 'number' && Number.isFinite(p.tick) ? p.tick : 0;
    const corrective = typeof p.corrective === 'boolean' ? p.corrective : false;
    return { records, tick, corrective };
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
