// Canonical set of escalation event types emitted by backend/app/notification_dispatch.py.
// One source of truth: high-signal classification, error toast severity, and ContextBadges
// all read from this set, so a new escalation type added here automatically gets surfaced
// across all three behaviors. Keep in sync with notification_dispatch.py escalation emitters.
export const ESCALATION_EVENT_TYPES: ReadonlySet<string> = new Set([
  'auto_fix_escalated',
  'residual_fix_escalated',
  'residual_fix_audit_deferred',
  'remediation_escalated',
]);

// Per-event-type label and Tailwind class for the ContextBadges escalation badge.
// Adding a new entry to ESCALATION_EVENT_TYPES without a corresponding entry here
// is caught by the invariant test in MessagesPanel.utils.test.ts.
export const ESCALATION_BADGE_LABELS: Readonly<Record<string, string>> = {
  auto_fix_escalated: 'Auto-fix Escalated',
  residual_fix_escalated: 'Residual Fix Escalated',
  residual_fix_audit_deferred: 'Audit Deferred',
  remediation_escalated: 'Remediation Escalated',
};

export const HIGH_SIGNAL_NOTE_EVENTS: ReadonlySet<string> = new Set([
  'failure_alert', 'rollback_recommended', 'fix_loop_divergence_regressed',
  'feedback_acknowledgment', 'account_model_regression_quarantined',
  ...ESCALATION_EVENT_TYPES,
]);

export const CRITICAL_NOTE_EVENTS: ReadonlySet<string> = new Set([
  'failure_alert', 'rollback_recommended', 'fix_loop_divergence_regressed',
  'account_model_regression_quarantined',
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

export function effectiveSignal(
  msg: { message_type: string; from_type: string; context: Record<string, unknown> },
  prefs: Record<string, EventPref>,
): 'high' | 'routine' | 'mute' {
  const et = msg.context.event_type as string | undefined;
  const pref = et ? prefs[et] : undefined;
  if (pref === 'mute') return 'mute';
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

export type RoutineRow<T> =
  | { kind: 'single'; msg: T }
  | { kind: 'group'; key: string; event_type: string; label: string; msgs: T[] };

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
