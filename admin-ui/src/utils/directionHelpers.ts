import type { Direction } from '../components/DirectionsPanel';
import { timeAgo, parseTimestamp } from './time';

export { timeAgo as formatRelativeTime };

export function formatRemainingMinutes(minutes: number): string {
  if (!Number.isFinite(minutes) || minutes <= 0) return '';
  const remainingMins = Math.ceil(minutes);
  if (remainingMins < 60) return `${remainingMins}m left`;
  const hrs = Math.floor(remainingMins / 60);
  const mins = remainingMins % 60;
  return mins > 0 ? `${hrs}h ${mins}m left` : `${hrs}h left`;
}

/**
 * Days a staged direction has been waiting since it was staged. Threshold for
 * surfacing a stalling stage visually, set comfortably below the backend
 * stale-staged operator alert (~21d) so the operator sees it first.
 */
export const STAGED_STALE_DAYS = 14;

/**
 * Presentational waiting-age info for a staged direction, derived from its
 * `staged_at` timestamp. Returns null when the field is absent (e.g. older
 * payloads), so callers render nothing rather than a misleading age.
 */
export function getStagedWaitingInfo(d: Direction): { label: string; days: number; isStale: boolean } | null {
  if (!d.staged_at) return null;
  const elapsedMs = Date.now() - parseTimestamp(d.staged_at).getTime();
  const days = Math.max(0, Math.floor(elapsedMs / (24 * 60 * 60 * 1000)));
  return {
    label: `staged ${timeAgo(d.staged_at)}`,
    days,
    isStale: days >= STAGED_STALE_DAYS,
  };
}

export const priorityColors: Record<string, string> = {
  high: 'bg-red-950/40 text-red-400 border-red-900/50',
  medium: 'bg-amber-950/40 text-amber-400 border-amber-900/50',
  low: 'bg-blue-950/40 text-blue-400 border-blue-900/50',
};

export const statusColors: Record<string, string> = {
  active: 'bg-emerald-950/40 text-emerald-400 border-emerald-900/50',
  completed: 'bg-workbench-panel/40 text-neutral-400 border-neutral-700/50',
  paused: 'bg-amber-950/40 text-amber-400 border-amber-900/50',
  proposed: 'bg-purple-950/40 text-purple-400 border-purple-900/50',
  expired: 'bg-workbench-panel/40 text-neutral-500 border-neutral-700/50',
  staged: 'bg-sky-950/40 text-sky-400 border-sky-900/50',
};
