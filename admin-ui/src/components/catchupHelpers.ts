export const TIMEOUT_SUBTYPE_LABELS: Record<string, string> = {
  heartbeat_lost: 'heartbeat lost',
  oom: 'OOM',
  execution_timeout: 'exec timeout',
  cascade_cancellation: 'cascade',
  iteration_limit: 'iter limit',
  process_crash: 'crash',
  unknown: 'unknown',
};

/** Friendly labels for EXTERNAL_API failure sub-types persisted as `external_api_subtype`
 *  (see backend/app/failure_classification.py EXTERNAL_API_SUBTYPE_* constants). */
export const EXTERNAL_API_SUBTYPE_LABELS: Record<string, string> = {
  provider_auth: 'provider auth',
  provider_quota: 'provider quota',
  provider_model_config: 'model config',
  github_pr_create: 'GitHub PR',
  db_connectivity: 'DB connectivity',
  network_transient: 'network blip',
  other: 'other',
};

/** Friendly labels for RATE_LIMIT kinds persisted as `rate_limit_kind`
 *  (see backend/app/failure_classification.py rate-limit signal persistence). */
export const RATE_LIMIT_KIND_LABELS: Record<string, string> = {
  rate_limit: 'rate limit',
  quota_exhausted: 'quota exhausted',
  unknown: 'unknown',
};

/** Friendly labels for park_wake_timeout failure_detail values
 *  (see backend/app/task_lifecycle.py expire_wake). */
export const PARK_WAKE_FAILURE_DETAIL_LABELS: Record<string, string> = {
  compute_job_terminal_wake_lost_after_job_completed: 'wake lost after job completed',
  compute_job_terminal_job_not_terminal_by_deadline: 'job did not finish in time',
};

// RecentCompletion is the canonical digest type, hoisted to ../types/digest.
// Re-exported here so existing importers (catchupRows, CatchUpPanel, tests)
// keep resolving it from this module — single source of truth, no divergence.
export type { RecentCompletion } from '../types/digest';

export type TimeWindow = 'last_visit' | '6h' | '24h' | '7d';

type DismissEntry = { type: 'dismiss' } | { type: 'snooze'; until: number };
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

