export const HIGH_SIGNAL_NOTE_EVENTS = new Set([
  'failure_alert', 'rollback_recommended', 'fix_loop_divergence_regressed',
  'feedback_acknowledgment', 'account_model_regression_quarantined',
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

export function loadEventPrefs(): Record<string, EventPref> {
  try { return JSON.parse(localStorage.getItem(EVENT_PREFS_KEY) ?? '{}'); } catch { return {}; }
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

export function groupRoutineMessages<T extends { id: string; context: Record<string, unknown> }>(
  msgs: T[],
): RoutineRow<T>[] {
  const rows: RoutineRow<T>[] = [];
  let i = 0;
  while (i < msgs.length) {
    const et = msgs[i].context.event_type as string | undefined;
    let j = i + 1;
    while (j < msgs.length && (msgs[j].context.event_type as string | undefined) === et) j++;
    const run = msgs.slice(i, j);
    if (run.length >= 3 && et) {
      const label = ROUTINE_EVENT_LABELS[et] ?? et.replace(/_/g, ' ');
      rows.push({ kind: 'group', key: run[0].id, event_type: et, label, msgs: run });
    } else {
      run.forEach(m => rows.push({ kind: 'single', msg: m }));
    }
    i = j;
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
  const criticalNoteEvents = new Set(['failure_alert', 'rollback_recommended', 'fix_loop_divergence_regressed', 'account_model_regression_quarantined']);
  if (msg.message_type === 'BLOCKER' || msg.message_type === 'BUG_REPORT') return 'error';
  if (et && criticalNoteEvents.has(et)) return 'error';
  return 'info';
}
