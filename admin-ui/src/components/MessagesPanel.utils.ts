import { apiFetch, API_BASE_URL } from '../api';

// Canonical set of escalation event types emitted by backend/app/notification_dispatch.py.
// One source of truth: high-signal classification, error toast severity, and ContextBadges
// all read from this set, so a new escalation type added here automatically gets surfaced
// across all three behaviors. Keep in sync with notification_dispatch.py escalation emitters.
//
// EVENT TYPE INVARIANT: See backend/testing/event_type_snapshot.json for the canonical list
// of all 21+ event_types emitted by the backend. The invariant test in
// MessagesPanel.invariant.test.ts validates that every event_type appears in either
// HIGH_SIGNAL_NOTE_EVENTS or ROUTINE_EVENT_LABELS below. When adding a new event_type:
// 1. Update backend/testing/event_type_snapshot.json with (name, classification, source_fn)
// 2. Update HIGH_SIGNAL_NOTE_EVENTS, ROUTINE_EVENT_LABELS, or ESCALATION_EVENT_TYPES below
// 3. If escalation, add a badge label to ESCALATION_BADGE_LABELS
// The invariant test will fail and tell you exactly what's missing.
export const ESCALATION_EVENT_TYPES: ReadonlySet<string> = new Set([
  'auto_fix_escalated',
  'residual_fix_escalated',
  'residual_fix_audit_deferred',
  'remediation_escalated',
  'direction_burn_pause',
  'direction_churn_pause',
]);

// Per-event-type label and Tailwind class for the ContextBadges escalation badge.
// Adding a new entry to ESCALATION_EVENT_TYPES without a corresponding entry here
// is caught by the invariant test in MessagesPanel.utils.test.ts.
export const ESCALATION_BADGE_LABELS: Readonly<Record<string, string>> = {
  auto_fix_escalated: 'Auto-fix Escalated',
  residual_fix_escalated: 'Residual Fix Escalated',
  residual_fix_audit_deferred: 'Audit Deferred',
  remediation_escalated: 'Remediation Escalated',
  direction_burn_pause: 'Burn Pause',
  direction_churn_pause: 'Churn Pause',
};

export const HIGH_SIGNAL_NOTE_EVENTS: ReadonlySet<string> = new Set([
  'failure_alert', 'rollback_recommended', 'fix_loop_divergence_regressed',
  'release_calcifying', 'direction_outcome_stalled', 'release_build_stalled',
  'release_stabilize_stalled', 'promotion_readiness_reached',
  ...ESCALATION_EVENT_TYPES,
]);

export const CRITICAL_NOTE_EVENTS: ReadonlySet<string> = new Set([
  'failure_alert', 'rollback_recommended', 'fix_loop_divergence_regressed',
  'release_calcifying', 'direction_outcome_stalled', 'release_build_stalled',
  'release_stabilize_stalled', 'promotion_readiness_reached',
  ...ESCALATION_EVENT_TYPES,
]);

export const ROUTINE_EVENT_LABELS: Record<string, string> = {
  pr_merged: 'PR merges',
  direction_milestone: 'milestones',
  cycle_summary: 'cycle summaries',
  account_model_regression_quarantined: 'account quarantines',
  account_model_regression_recovered: 'account recoveries',
  auto_fix_spawned: 'auto-fix spawns',
  auto_fix_result: 'auto-fix results',
  residual_fix_spawned: 'residual fix spawns',
  residual_fix_result: 'residual fix results',
  residual_fix_unsuspended: 'residual unsuspensions',
  residual_case_auto_resolved: 'auto-resolutions',
  issue_implementation_spawned: 'issue spawns',
  review_feedback: 'review feedback',
  feedback_acknowledgment: 'feedback acks',
};

export type EventPref = 'mute' | 'routine' | 'surfaced';

export const EVENT_PREFS_KEY = 'messages_panel_event_prefs';

// Legacy global key for migration only
const LEGACY_EVENT_PREFS_KEY = 'messages_panel_event_prefs';

export function eventPrefsKey(userId: string | undefined, projectId: string | undefined): string {
  return `messages_panel_event_prefs:${userId || 'anonymous'}:${projectId || 'default'}`;
}

export function loadEventPrefs(userId?: string | null, projectId?: string | null): Record<string, EventPref> {
  try {
    const key = eventPrefsKey(userId || undefined, projectId || undefined);
    const stored = localStorage.getItem(key);
    if (stored) return JSON.parse(stored);

    // Migration: if no project-scoped key exists, try legacy global key
    const legacy = localStorage.getItem(LEGACY_EVENT_PREFS_KEY);
    if (legacy) return JSON.parse(legacy);

    return {};
  } catch {
    return {};
  }
}

export function saveEventPrefs(
  userId: string | undefined | null,
  projectId: string | undefined | null,
  prefs: Record<string, EventPref>,
): void {
  try {
    const key = eventPrefsKey(userId || undefined, projectId || undefined);
    if (Object.keys(prefs).length === 0) {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, JSON.stringify(prefs));
    }
  } catch { /* ignore */ }
}

// ── Saved Views (operator filter combos) ────────────────────────────────────
// Persists to localStorage under a per-user-per-project key so each operator's
// named views travel with their account but stay isolated per project. Schema
// is versioned: load() coerces missing fields to defaults and ignores unknown
// fields so future panel state (e.g. dateRange) can join SavedView additively.

export const SAVED_VIEWS_CAP = 10;

export type SavedView = {
  id: string;
  name: string;
  description?: string;
  query: string;
  prefs: Record<string, EventPref>;
  sort: 'importance' | 'recent';
  showResolved: boolean;
  routineExpanded: boolean;
  showOnlyNew: boolean;
  createdAt: number;
};

export type SavedViewsFile = { version: 1; views: SavedView[] };

export function savedViewsKey(userId?: string | null, projectId?: string | null): string {
  return `messages_saved_views:${userId || 'anonymous'}:${projectId || 'default'}`;
}

function sanitizeSavedView(raw: unknown): SavedView | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const id = typeof r.id === 'string' && r.id ? r.id : null;
  const name = typeof r.name === 'string' && r.name ? r.name : null;
  if (!id || !name) return null;
  const prefsRaw = r.prefs && typeof r.prefs === 'object' ? r.prefs as Record<string, unknown> : {};
  const prefs: Record<string, EventPref> = {};
  for (const [k, v] of Object.entries(prefsRaw)) {
    if (v === 'mute' || v === 'routine' || v === 'surfaced') prefs[k] = v;
  }
  return {
    id,
    name,
    description: typeof r.description === 'string' ? r.description : undefined,
    query: typeof r.query === 'string' ? r.query : '',
    prefs,
    sort: r.sort === 'recent' ? 'recent' : 'importance',
    showResolved: r.showResolved === true,
    routineExpanded: r.routineExpanded === true,
    showOnlyNew: r.showOnlyNew === true,
    createdAt: typeof r.createdAt === 'number' ? r.createdAt : Date.now(),
  };
}

export function loadSavedViews(userId?: string | null, projectId?: string | null): SavedView[] {
  try {
    const raw = localStorage.getItem(savedViewsKey(userId, projectId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    // Tolerate both wrapped {version, views} and bare arrays from any
    // earlier ad-hoc shape — pick views[] if present, else treat parsed as the array.
    const arr: unknown = Array.isArray(parsed) ? parsed : (parsed && typeof parsed === 'object' ? (parsed as { views?: unknown }).views : null);
    if (!Array.isArray(arr)) return [];
    const out: SavedView[] = [];
    for (const item of arr) {
      const v = sanitizeSavedView(item);
      if (v) out.push(v);
    }
    return out;
  } catch {
    return [];
  }
}

export function saveSavedViews(
  userId: string | undefined | null,
  projectId: string | undefined | null,
  views: SavedView[],
): void {
  try {
    const key = savedViewsKey(userId, projectId);
    if (!views || views.length === 0) {
      localStorage.removeItem(key);
      return;
    }
    const file: SavedViewsFile = { version: 1, views };
    localStorage.setItem(key, JSON.stringify(file));
  } catch { /* ignore */ }
}

export function effectiveSignal(
  msg: { message_type: string; from_type: string; context: Record<string, unknown> },
  prefs: Record<string, EventPref>,
  tempMutes?: TemporaryMutes,
): 'high' | 'routine' | 'mute' {
  const et = msg.context.event_type as string | undefined;
  if (tempMutes && isEventTypeTemporarilyMuted(et, tempMutes)) return 'mute';
  const pref = et ? prefs[et] : undefined;
  if (pref === 'mute') return 'mute';
  if (pref === 'routine') return 'routine';
  if (pref === 'surfaced') return 'high';
  return classifySignalLevel(msg);
}

export function classifySignalLevel(msg: {
  message_type: string;
  from_type: string;
  context: Record<string, unknown>;
}): 'high' | 'routine' {
  if ((msg.from_type || '').toLowerCase() === 'user') return 'high';
  if (['FEEDBACK', 'BLOCKER', 'BUG_REPORT'].includes(msg.message_type)) return 'high';
  if (msg.message_type === 'NOTE') {
    const et = msg.context.event_type as string | undefined;
    if (et && HIGH_SIGNAL_NOTE_EVENTS.has(et)) return 'high';
  }
  return 'routine';
}

export const STALE_ROUTINE_COLLAPSE_AGE_MS = 3600000; // 1 hour — stale routine notes collapse to a per-day digest

export type RoutineRow<T> =
  | { kind: 'single'; msg: T }
  | { kind: 'group'; key: string; event_type: string; label: string; msgs: T[] }
  | { kind: 'daily_digest'; day: string; count: number; breakdown: Record<string, number>; msgs: T[] };

export function groupRoutineMessages<T extends { id: string; context: Record<string, unknown>; created_at?: string | null }>(
  msgs: T[],
): RoutineRow<T>[] {
  // Import parseTimestamp inline to avoid circular dependencies
  const parseTimestamp = (v: string | null | undefined): Date => {
    if (!v) return new Date(0);
    return new Date(v.endsWith('Z') || v.includes('+') ? v : v + 'Z');
  };

  const TIME_WINDOW_MS = 3600000; // 1 hour

  // First pass: assign each message to a group based on time window and event type
  const groupAssignments: number[] = [];
  const groups: { msgs: T[]; et: string; dateStr: string; label: string; firstIdx: number }[] = [];

  for (let idx = 0; idx < msgs.length; idx++) {
    const msg = msgs[idx];
    const et = msg.context.event_type as string | undefined;

    if (!et) {
      groupAssignments.push(-1); // Single message without event type
      continue;
    }

    const dateStr = getMessageDateString(msg as { created_at: string | null });
    const msgTime = msg.created_at ? parseTimestamp(msg.created_at).getTime() : 0;

    // Find a matching group (same event_type, same date, within time window)
    let groupIdx = -1;
    for (let i = 0; i < groups.length; i++) {
      const group = groups[i];
      if (group.et === et && group.dateStr === dateStr && group.msgs.length > 0) {
        const lastMsg = group.msgs[group.msgs.length - 1];
        const lastMsgTime = lastMsg.created_at ? parseTimestamp(lastMsg.created_at).getTime() : 0;
        if (msgTime - lastMsgTime <= TIME_WINDOW_MS) {
          groupIdx = i;
          break;
        }
      }
    }

    if (groupIdx === -1) {
      // Create new group
      const label = ROUTINE_EVENT_LABELS[et] ?? et.replace(/_/g, ' ');
      groupIdx = groups.length;
      groups.push({ msgs: [msg], et, dateStr, label, firstIdx: idx });
    } else {
      // Add to existing group
      groups[groupIdx].msgs.push(msg);
    }

    groupAssignments.push(groupIdx);
  }

  // Second pass: render in order, rendering groups at their first message (if 3+)
  const rows: RoutineRow<T>[] = [];
  const renderedGroups = new Set<number>();

  for (let i = 0; i < msgs.length; i++) {
    const msg = msgs[i];
    const groupIdx = groupAssignments[i];

    if (groupIdx === -1) {
      // Single message without event type
      rows.push({ kind: 'single', msg });
    } else {
      const group = groups[groupIdx];
      if (group.msgs.length >= 3) {
        // Render as group chip at first message position only
        if (i === group.firstIdx && !renderedGroups.has(groupIdx)) {
          rows.push({
            kind: 'group',
            key: group.msgs[0].id,
            event_type: group.et,
            label: group.label,
            msgs: group.msgs,
          });
          renderedGroups.add(groupIdx);
        }
        // Skip other messages in the group (already part of the group chip)
      } else {
        // Below threshold: render as single at each original position
        rows.push({ kind: 'single', msg });
      }
    }
  }

  return rows;
}

export function outcomeBreakdown(msgs: { context: Record<string, unknown> }[]): string {
  const failed = msgs.filter(m => m.context.result === 'failed').length;
  const merged = msgs.filter(m => m.context.result === 'merged').length;
  const reviewNeeded = msgs.filter(m => m.context.result === 'review_needed').length;
  if (failed === 0 && merged === 0 && reviewNeeded === 0) return '';
  const parts: string[] = [];
  if (failed > 0) parts.push(`${failed} failed`);
  if (merged > 0) parts.push(`${merged} merged`);
  if (reviewNeeded > 0) parts.push(`${reviewNeeded} review`);
  return `· ${parts.join(', ')}`;
}

export function isFailingGroup(msgs: { context: Record<string, unknown> }[]): boolean {
  const failed = msgs.filter(m => m.context.result === 'failed').length;
  return failed >= 3 && failed / msgs.length >= 0.5;
}

export function routineSummary(msgs: { message_type: string; context: Record<string, unknown> }[]): string {
  const groups: Record<string, { label: string; group: typeof msgs }> = {};
  for (const m of msgs) {
    const et = (m.context.event_type as string | undefined) ?? '';
    const key = et || '__unknown__';
    const label = et ? (ROUTINE_EVENT_LABELS[et] ?? et.replace(/_/g, ' ')) : 'notes';
    if (!groups[key]) groups[key] = { label, group: [] };
    groups[key].group.push(m);
  }
  return Object.values(groups).slice(0, 4).map(({ label, group }) => {
    const bd = outcomeBreakdown(group);
    return `${group.length} ${label}${bd ? ' ' + bd : ''}`;
  }).join(', ');
}

export function getToastSeverity(msg: { message_type: string; from_type: string; context: Record<string, unknown> }): 'error' | 'info' {
  const et = msg.context.event_type as string | undefined;
  if (msg.message_type === 'BLOCKER' || msg.message_type === 'BUG_REPORT') return 'error';
  if (et && CRITICAL_NOTE_EVENTS.has(et)) return 'error';
  return 'info';
}

export function lastSeenKey(userId: string | null | undefined, projectId: string | null | undefined): string {
  return `messages_panel_last_seen:${userId || 'anonymous'}:${projectId || 'none'}`;
}

export function loadLastSeen(userId: string | null | undefined, projectId: string | null | undefined): number | null {
  try {
    const raw = localStorage.getItem(lastSeenKey(userId, projectId));
    if (!raw) return null;
    const val = parseInt(raw, 10);
    return !isNaN(val) && val > 0 ? val : null;
  } catch {
    return null;
  }
}

export function saveLastSeen(userId: string | null | undefined, projectId: string | null | undefined, timestamp: number): void {
  try {
    localStorage.setItem(lastSeenKey(userId, projectId), String(timestamp));
  } catch { /* ignore */ }
}

// Merges a localStorage timestamp with a backend timestamp, preferring the more recent value.
// Returns null when both are absent. Use this to implement the offline fallback: pass backend
// result (or null if the fetch failed) and let the function select the correct source.
export function resolveLastSeen(localTs: number | null, backendTs: number | null): number | null {
  if (localTs !== null && backendTs !== null) return Math.max(localTs, backendTs);
  return backendTs ?? localTs;
}

// ── Time-window mute (hides routine ops for a chosen duration) ────────────────

export type MuteWindow = { expiresAt: number };

export function muteWindowKey(userId: string | undefined, projectId: string | undefined): string {
  return `messages_panel_mute_window:${userId || 'anonymous'}:${projectId || 'default'}`;
}

export function loadMuteWindow(userId?: string | null, projectId?: string | null): MuteWindow | null {
  try {
    const key = muteWindowKey(userId || undefined, projectId || undefined);
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { expiresAt?: unknown };
    if (typeof parsed.expiresAt !== 'number' || parsed.expiresAt <= Date.now()) {
      localStorage.removeItem(key);
      return null;
    }
    return { expiresAt: parsed.expiresAt };
  } catch {
    return null;
  }
}

export function saveMuteWindow(
  userId: string | undefined | null,
  projectId: string | undefined | null,
  muteWin: MuteWindow,
): void {
  try {
    localStorage.setItem(muteWindowKey(userId || undefined, projectId || undefined), JSON.stringify(muteWin));
  } catch { /* ignore */ }
}

export function clearMuteWindow(userId: string | undefined | null, projectId: string | undefined | null): void {
  try {
    localStorage.removeItem(muteWindowKey(userId || undefined, projectId || undefined));
  } catch { /* ignore */ }
}

export function isMuteWindowActive(muteWin: MuteWindow | null): boolean {
  return muteWin !== null && muteWin.expiresAt > Date.now();
}

export function formatMuteCountdown(expiresAt: number): string {
  const msLeft = expiresAt - Date.now();
  const minsLeft = Math.floor(msLeft / 60000);
  if (minsLeft < 1) return '<1m left';
  return `${minsLeft}m left`;
}

export function formatTimestamp(timestamp: number): string {
  const now = new Date();
  const diffMs = now.getTime() - timestamp;
  const diffMins = Math.floor(diffMs / 60000);

  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  const hours = Math.floor(diffMins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days === 1) return 'yesterday';
  return `${days}d ago`;
}

export function formatSinceTimestamp(timestamp: number): string {
  const date = new Date(timestamp);
  const now = new Date();
  const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const isToday = date.toDateString() === now.toDateString();
  const isYesterday = new Date(date.getTime() + 86400000).toDateString() === now.toDateString();

  const day = dayNames[date.getDay()];
  const hour = date.getHours();
  const min = date.getMinutes().toString().padStart(2, '0');
  const ampm = hour >= 12 ? 'pm' : 'am';
  const displayHour = hour % 12 || 12;

  const time = `${displayHour}:${min}${ampm}`;
  if (isToday) return `Today ${time}`;
  if (isYesterday) return `Yesterday ${time}`;
  return `${day} ${time}`;
}

export type MessageWithDivider<T> =
  | { kind: 'divider'; label: string }
  | { kind: 'message'; msg: T };

function getMessageDateString(msg: { created_at: string | null }): string {
  if (!msg.created_at) return '';
  const date = new Date(msg.created_at);
  const today = new Date();
  const yesterday = new Date(today);
  yesterday.setDate(yesterday.getDate() - 1);

  const normalize = (d: Date) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).toISOString();
  const msgDateStr = normalize(date);
  const todayStr = normalize(today);
  const yesterdayStr = normalize(yesterday);

  if (msgDateStr === todayStr) return 'Today';
  if (msgDateStr === yesterdayStr) return 'Yesterday';

  const dayNames = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
  const monthNames = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const day = dayNames[date.getDay()];
  const month = monthNames[date.getMonth()];
  const dayNum = date.getDate();
  return `${day} ${month} ${dayNum}`;
}

export function insertTimeDividers<T extends { created_at: string | null }>(msgs: T[]): MessageWithDivider<T>[] {
  const result: MessageWithDivider<T>[] = [];
  let lastDateStr = '';

  for (const msg of msgs) {
    const dateStr = getMessageDateString(msg);
    if (dateStr && dateStr !== lastDateStr) {
      result.push({ kind: 'divider', label: dateStr });
      lastDateStr = dateStr;
    }
    result.push({ kind: 'message', msg });
  }

  return result;
}

export function isCriticalSeverity(msg: {
  message_type: string;
  context: Record<string, unknown>;
}): boolean {
  if (msg.message_type === 'BLOCKER' || msg.message_type === 'BUG_REPORT') return true;
  if (msg.message_type === 'NOTE') {
    const et = msg.context.event_type as string | undefined;
    if (et && CRITICAL_NOTE_EVENTS.has(et)) return true;
  }
  return false;
}

// ── Temporary event-type mutes (short-duration silence for noisy types) ────────

export type TemporaryMutes = Record<string, number>; // event_type -> expiresAt (ms)

export function temporaryMutesKey(userId: string | undefined, projectId: string | undefined): string {
  return `messages_panel_temp_mutes:${userId || 'anonymous'}:${projectId || 'default'}`;
}

export function loadTemporaryMutes(userId?: string | null, projectId?: string | null): TemporaryMutes {
  try {
    const key = temporaryMutesKey(userId || undefined, projectId || undefined);
    const raw = localStorage.getItem(key);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const now = Date.now();
    const mutes: TemporaryMutes = {};
    for (const [et, expiresAt] of Object.entries(parsed)) {
      if (typeof expiresAt === 'number' && expiresAt > now) {
        mutes[et] = expiresAt;
      }
    }
    return mutes;
  } catch {
    return {};
  }
}

export function saveTemporaryMutes(
  userId: string | undefined | null,
  projectId: string | undefined | null,
  mutes: TemporaryMutes,
): void {
  try {
    const key = temporaryMutesKey(userId || undefined, projectId || undefined);
    if (Object.keys(mutes).length === 0) {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, JSON.stringify(mutes));
    }
  } catch { /* ignore */ }
}

export function isEventTypeTemporarilyMuted(eventType: string | undefined, tempMutes: TemporaryMutes): boolean {
  if (!eventType) return false;
  const expiresAt = tempMutes[eventType];
  return expiresAt !== undefined && expiresAt > Date.now();
}

// Count high-signal open messages (genuine operator attention items, excluding routine ops chatter).
// High-signal includes: user messages, FEEDBACK/BLOCKER/BUG_REPORT types, and critical escalation NOTEs.
// This computation is used consistently whether the messages panel is open or closed.
export function countHighSignalMessages(messages: Array<{ message_type: string; from_type: string; status: string; context: Record<string, unknown> }>): number {
  return messages.filter(m =>
    m.status === 'open' && classifySignalLevel(m) === 'high'
  ).length;
}

// Collapses stale routine rows (older than STALE_ROUTINE_COLLAPSE_AGE_MS) into per-day digest
// entries. Failing groups and high-signal events are never collapsed.
export function collapseStaleRoutineRows<T extends { id: string; context: Record<string, unknown>; created_at?: string | null }>(
  rows: RoutineRow<T>[],
  nowMs: number = Date.now(),
): RoutineRow<T>[] {
  const parseTs = (v: string | null | undefined): number => {
    if (!v) return 0;
    return new Date(v.endsWith('Z') || v.includes('+') ? v : v + 'Z').getTime();
  };
  const cutoffMs = nowMs - STALE_ROUTINE_COLLAPSE_AGE_MS;
  const digestBuckets = new Map<string, { msgs: T[]; breakdown: Record<string, number> }>();
  const digestFirstPos = new Map<string, number>();
  const output: (RoutineRow<T> | string)[] = [];

  for (const row of rows) {
    const rowMsgs = row.kind === 'single' ? [row.msg] : row.msgs;
    const lastTs = parseTs(rowMsgs[rowMsgs.length - 1].created_at);
    const rowStale = lastTs > 0 && lastTs < cutoffMs;
    const rowFailingGrp = row.kind === 'group' && isFailingGroup(row.msgs);
    const rowHighSignal = rowMsgs.some(m => {
      const et = m.context.event_type as string | undefined;
      return et !== undefined && HIGH_SIGNAL_NOTE_EVENTS.has(et);
    });

    if (rowStale && !rowFailingGrp && !rowHighSignal) {
      const day = getMessageDateString(rowMsgs[0] as { created_at: string | null });
      if (!digestFirstPos.has(day)) {
        digestFirstPos.set(day, output.length);
        output.push(day);
        digestBuckets.set(day, { msgs: [], breakdown: {} });
      }
      // eslint-disable-next-line @typescript-eslint/no-non-null-assertion
      const bucket = digestBuckets.get(day)!;
      for (const m of rowMsgs) {
        bucket.msgs.push(m);
        const et = (m.context.event_type as string | undefined) ?? '__unknown__';
        bucket.breakdown[et] = (bucket.breakdown[et] || 0) + 1;
      }
    } else {
      output.push(row);
    }
  }

  return output.map(item => {
    if (typeof item !== 'string') return item;
    // eslint-disable-next-line @typescript-eslint/no-non-null-assertion
    const b = digestBuckets.get(item)!;
    return { kind: 'daily_digest' as const, day: item, count: b.msgs.length, breakdown: b.breakdown, msgs: b.msgs };
  });
}

export function formatDailyDigestSummary(breakdown: Record<string, number>): string {
  return Object.entries(breakdown)
    .slice(0, 3)
    .map(([et, cnt]) => `${cnt} ${ROUTINE_EVENT_LABELS[et] ?? et.replace(/_/g, ' ')}`)
    .join(', ');
}

// ── Server-side noise prefs (per-operator, per-project) ──────────────────────
// Mirrors the existing per-operator last-seen persistence pattern. localStorage
// remains the offline fallback; on successful fetch the server value wins.

function sanitizeServerEventPrefs(raw: Record<string, unknown>): Record<string, EventPref> {
  const out: Record<string, EventPref> = {};
  for (const [k, v] of Object.entries(raw)) {
    if (v === 'mute' || v === 'routine' || v === 'surfaced') out[k] = v;
  }
  return out;
}

export async function fetchServerNoisePrefs(
  projectId: string,
): Promise<Record<string, EventPref> | null> {
  try {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/noise-prefs?project_id=${encodeURIComponent(projectId)}`);
    if (!res.ok) return null;
    const data = await res.json() as { prefs?: Record<string, unknown> };
    return sanitizeServerEventPrefs(data.prefs ?? {});
  } catch {
    return null;
  }
}

export async function saveServerNoisePrefs(
  projectId: string,
  prefs: Record<string, EventPref>,
): Promise<void> {
  try {
    await apiFetch(`${API_BASE_URL}/api/admin/messages/noise-prefs`, {
      method: 'PUT',
      body: JSON.stringify({ project_id: projectId, prefs }),
    });
  } catch { /* ignore — localStorage copy remains authoritative offline */ }
}

// Merges local and server noise prefs, preferring server when available.
// Pass null serverPrefs to fall back to local (offline or fetch failed).
export function resolveNoisePrefs(
  localPrefs: Record<string, EventPref>,
  serverPrefs: Record<string, EventPref> | null,
): Record<string, EventPref> {
  return serverPrefs ?? localPrefs;
}

// ── Server-side saved views (per-operator, per-project) ──────────────────────
// Mirrors the noise-prefs server persistence pattern. localStorage remains the
// offline fallback; on successful fetch the server value wins.

export async function fetchServerSavedViews(
  projectId: string,
): Promise<SavedView[] | null> {
  try {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/saved-views?project_id=${encodeURIComponent(projectId)}`);
    if (!res.ok) return null;
    const data = await res.json() as { saved_views?: unknown[] };
    if (!Array.isArray(data.saved_views)) return [];
    const out: SavedView[] = [];
    for (const item of data.saved_views) {
      const v = sanitizeSavedView(item);
      if (v) out.push(v);
    }
    return out;
  } catch {
    return null;
  }
}

export async function saveServerSavedViews(
  projectId: string,
  views: SavedView[],
): Promise<void> {
  try {
    await apiFetch(`${API_BASE_URL}/api/admin/messages/saved-views`, {
      method: 'PUT',
      body: JSON.stringify({ project_id: projectId, saved_views: views }),
    });
  } catch { /* ignore — localStorage copy remains authoritative offline */ }
}

export function resolveServerSavedViews(
  localViews: SavedView[],
  serverViews: SavedView[] | null,
): SavedView[] {
  return serverViews ?? localViews;
}
