import { ROUTINE_EVENT_LABELS, HIGH_SIGNAL_NOTE_EVENTS } from './eventPrefs';

export const STALE_ROUTINE_COLLAPSE_AGE_MS = 3600000; // 1 hour — stale routine notes collapse to a per-day digest

export type RoutineRow<T> =
  | { kind: 'single'; msg: T }
  | { kind: 'group'; key: string; event_type: string; label: string; msgs: T[] }
  | { kind: 'daily_digest'; day: string; count: number; breakdown: Record<string, number>; msgs: T[] };

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

export function groupRoutineMessages<T extends { id: string; context: Record<string, unknown>; created_at?: string | null }>(
  msgs: T[],
): RoutineRow<T>[] {
  const parseTimestamp = (v: string | null | undefined): Date => {
    if (!v) return new Date(0);
    return new Date(v.endsWith('Z') || v.includes('+') ? v : v + 'Z');
  };

  const TIME_WINDOW_MS = 3600000; // 1 hour

  const groupAssignments: number[] = [];
  const groups: { msgs: T[]; et: string; dateStr: string; label: string; firstIdx: number }[] = [];

  for (let idx = 0; idx < msgs.length; idx++) {
    const msg = msgs[idx];
    const et = msg.context.event_type as string | undefined;

    if (!et) {
      groupAssignments.push(-1);
      continue;
    }

    const dateStr = getMessageDateString(msg as { created_at: string | null });
    const msgTime = msg.created_at ? parseTimestamp(msg.created_at).getTime() : 0;

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
      const label = ROUTINE_EVENT_LABELS[et] ?? et.replace(/_/g, ' ');
      groupIdx = groups.length;
      groups.push({ msgs: [msg], et, dateStr, label, firstIdx: idx });
    } else {
      groups[groupIdx].msgs.push(msg);
    }

    groupAssignments.push(groupIdx);
  }

  const rows: RoutineRow<T>[] = [];
  const renderedGroups = new Set<number>();

  for (let i = 0; i < msgs.length; i++) {
    const msg = msgs[i];
    const groupIdx = groupAssignments[i];

    if (groupIdx === -1) {
      rows.push({ kind: 'single', msg });
    } else {
      const group = groups[groupIdx];
      if (group.msgs.length >= 3) {
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
      } else {
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
