export const TIMEOUT_SUBTYPE_LABELS: Record<string, string> = {
  heartbeat_lost: 'heartbeat lost',
  oom: 'OOM',
  execution_timeout: 'exec timeout',
  cascade_cancellation: 'cascade',
  iteration_limit: 'iter limit',
  process_crash: 'crash',
  unknown: 'unknown',
};

export interface RecentCompletion {
  task_id: string;
  title: string;
  completed_at: string | null;
  pr_url: string | null;
  task_type: string;
  direction_id?: string | null;
  direction_text?: string | null;
  scope?: string | null;
  duration_seconds?: number | null;
  review_iterations?: number | null;
}

export type TimeWindow = 'last_visit' | '6h' | '24h' | '7d';

export type DismissEntry = { type: 'dismiss' } | { type: 'snooze'; until: number };
export type DismissedState = Record<string, DismissEntry>;

export function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const minutes = seconds / 60;
  if (minutes < 60) return `${Math.round(minutes)}m`;
  const hours = minutes / 60;
  return hours < 10 ? `${hours.toFixed(1)}h` : `${Math.round(hours)}h`;
}

export function loadDismissed(): DismissedState {
  try {
    const raw = localStorage.getItem('blueprint-admin:dismissedAttention');
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

export function saveDismissed(state: DismissedState): void {
  localStorage.setItem('blueprint-admin:dismissedAttention', JSON.stringify(state));
}

export function formatSnoozeRemaining(until: number): string {
  const remaining = until - Date.now();
  if (remaining <= 0) return 'expired';
  const hours = Math.floor(remaining / (3600 * 1000));
  const mins = Math.floor((remaining % (3600 * 1000)) / 60000);
  if (hours > 0) return `snoozed for ${hours}h more`;
  return `snoozed for ${mins}m more`;
}

const CATCH_UP_VIEWED_KEY_BASE = 'blueprint-admin:lastCatchUpViewed';

/** Returns the localStorage key for the last-catch-up-viewed timestamp, scoped per operator when userEmail is provided. */
export function getCatchUpViewedKey(userEmail?: string | null): string {
  return userEmail ? `${CATCH_UP_VIEWED_KEY_BASE}:${userEmail}` : CATCH_UP_VIEWED_KEY_BASE;
}

export function getLastCatchUpViewed(userEmail?: string | null): number | null {
  try {
    const stored = localStorage.getItem(getCatchUpViewedKey(userEmail));
    const ts = stored ? parseInt(stored, 10) : null;
    return ts && !isNaN(ts) ? ts : null;
  } catch { return null; }
}

export function markCatchUpViewed(userEmail?: string | null): void {
  try { localStorage.setItem(getCatchUpViewedKey(userEmail), String(Date.now())); } catch { /* */ }
}

/** Merges local and backend last-seen timestamps, preferring the backend value when it is newer (cross-device). */
export function resolveLastCatchUpViewed(localTs: number | null, backendTs: number | null): number | null {
  if (backendTs != null && (localTs == null || backendTs > localTs)) return backendTs;
  return localTs;
}

export function windowToHours(window: TimeWindow, lastViewed: number | null): number {
  switch (window) {
    case 'last_visit': {
      if (!lastViewed) return 24;
      const elapsed = (Date.now() - lastViewed) / (1000 * 60 * 60);
      return Math.max(1, Math.min(168, Math.ceil(elapsed)));
    }
    case '6h': return 6;
    case '24h': return 24;
    case '7d': return 168;
  }
}
