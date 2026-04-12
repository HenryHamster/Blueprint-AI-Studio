import type { Direction } from '../components/DirectionsPanel';

export function formatRelativeTime(isoString: string): string {
  const diffMs = Date.now() - new Date(isoString.endsWith('Z') ? isoString : isoString + 'Z').getTime();
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHrs = Math.floor(diffMins / 60);
  if (diffHrs < 24) return `${diffHrs}h ${diffMins % 60}m ago`;
  return `${Math.floor(diffHrs / 24)}d ago`;
}

export function formatTimeRemaining(isoString: string, cooldownMinutes: number): string {
  const elapsedMs = Date.now() - new Date(isoString.endsWith('Z') ? isoString : isoString + 'Z').getTime();
  const remainingMs = cooldownMinutes * 60 * 1000 - elapsedMs;
  if (remainingMs <= 0) return '';
  const remainingMins = Math.ceil(remainingMs / 60000);
  if (remainingMins < 60) return `${remainingMins}m left`;
  const hrs = Math.floor(remainingMins / 60);
  const mins = remainingMins % 60;
  return mins > 0 ? `${hrs}h ${mins}m left` : `${hrs}h left`;
}

export function getCooldownStatus(d: Direction, cooldownMinutes: number): 'ready' | 'cooling' | 'never' {
  if (!d.last_researched_at) return 'never';
  const ts = d.last_researched_at;
  const elapsedMs = Date.now() - new Date(ts.endsWith('Z') ? ts : ts + 'Z').getTime();
  return elapsedMs < cooldownMinutes * 60 * 1000 ? 'cooling' : 'ready';
}

export const priorityColors: Record<string, string> = {
  high: 'bg-red-950/40 text-red-400 border-red-900/50',
  medium: 'bg-amber-950/40 text-amber-400 border-amber-900/50',
  low: 'bg-blue-950/40 text-blue-400 border-blue-900/50',
};

export const statusColors: Record<string, string> = {
  active: 'bg-emerald-950/40 text-emerald-400 border-emerald-900/50',
  completed: 'bg-neutral-800/40 text-neutral-400 border-neutral-700/50',
  paused: 'bg-amber-950/40 text-amber-400 border-amber-900/50',
  proposed: 'bg-purple-950/40 text-purple-400 border-purple-900/50',
  expired: 'bg-neutral-800/40 text-neutral-500 border-neutral-700/50',
};
