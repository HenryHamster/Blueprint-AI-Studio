/**
 * Shared timestamp utilities.
 *
 * The backend stores naive UTC datetimes and serialises them as ISO-8601
 * strings *without* a trailing 'Z'.  `new Date("2026-04-12T10:30:00")`
 * treats that as **local** time, which silently shifts every displayed
 * timestamp.  `parseTimestamp` normalises the input so all downstream
 * formatting is correct.
 *
 * CONVENTION: All code that turns a backend-sourced ISO string into a Date
 * MUST go through `parseTimestamp` (or a helper from this file that calls
 * it). Direct `new Date(isoString)` is forbidden when the string came from
 * an API response — it produces silent timezone-shifted Dates that look
 * "almost right" and cause invisible bugs (countdowns stuck at 0, "isNew"
 * flags stuck on, age strings off by hours).
 *
 * Allowed without parseTimestamp: `new Date()` (current time),
 * `new Date(epochMs)` (numeric arg), `new Date(year, month, day, ...)`
 * (multi-arg), `new Date(otherDate.getTime() + delta)`. Forbidden:
 * `new Date(apiResponseField)` of any kind.
 */

/** Parse an API timestamp (ISO string, epoch-ms, or Date) into a Date. */
export function parseTimestamp(v: string | number | Date | null | undefined): Date {
  if (!v) return new Date(0);
  if (v instanceof Date) return v;
  if (typeof v === 'number') return new Date(v);
  // Append 'Z' to naive ISO strings so they're parsed as UTC.
  return new Date(v.endsWith('Z') || v.includes('+') ? v : v + 'Z');
}

/** "5m ago" / "2h 30m ago" / "3d ago" relative time. */
export function timeAgo(v: string | number | Date): string {
  const ms = Date.now() - parseTimestamp(v).getTime();
  const sec = Math.floor(ms / 1000);
  if (sec < 5) return 'just now';
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${hr}h ${min % 60}m ago`;
  const days = Math.floor(hr / 24);
  if (days < 30) return `${days}d ago`;
  return `${Math.floor(days / 30)}mo ago`;
}

/** Format a duration given in **milliseconds**: "< 1m" / "5m" / "2h 10m". */
export function formatDurationMs(ms: number): string {
  const totalMin = Math.floor(ms / 60000);
  if (totalMin < 1) return '<1m';
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  if (h === 0) return `${m}m`;
  return m > 0 ? `${h}h ${m}m` : `${h}h`;
}

/** Format a duration given in **seconds**: "30s" / "5m 10s" / "2h 10m". */
export function formatDurationSec(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm > 0 ? `${h}h ${rm}m` : `${h}h`;
}

/** "Apr 12, 10:30:05 AM" — absolute local-time display. */
export function formatAbsolute(v: string | number): string {
  return parseTimestamp(v).toLocaleString(undefined, {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/** "10:30:05 AM" — time-only display. */
export function formatTimeOnly(v: string | number): string {
  return parseTimestamp(v).toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/**
 * Freshness affordance for a precomputed snapshot served from a cache.
 *
 * Returns a relative "as of" label (via {@link timeAgo}, or null when no
 * timestamp is supplied) and whether the snapshot is stale — its age in
 * seconds exceeds `staleAfterSeconds`. Callers degrade the surface (dim /
 * desaturate / mark) when `stale` so a cached reading is never presented as a
 * live, authoritative one.
 */
export function snapshotFreshness(
  computedAt: string | null | undefined,
  ageSeconds: number | null | undefined,
  staleAfterSeconds: number,
): { asOfLabel: string | null; stale: boolean } {
  return {
    asOfLabel: computedAt ? timeAgo(computedAt) : null,
    stale: ageSeconds != null && ageSeconds > staleAfterSeconds,
  };
}

/** Seconds remaining until a future timestamp (0 if past). */
export function secondsUntil(v: string | number): number {
  return Math.max(0, Math.floor((parseTimestamp(v).getTime() - Date.now()) / 1000));
}

/**
 * Render-time state of a bounded confirmation/deadline window.
 *
 * Computes remaining-vs-elapsed against `now` at call time so the surface is
 * honest at view time, not at fetch time. `overdue` is true once now is at or
 * past the deadline; `text` is a human label ("5m left in window" /
 * "window elapsed 3m ago"). Wrapping `Date.now()` here (rather than in the
 * component body) keeps the impure clock read out of render.
 */
export function deployWindowStatus(deadline: string | number): { overdue: boolean; text: string } {
  const remainingMs = parseTimestamp(deadline).getTime() - Date.now();
  const overdue = remainingMs <= 0;
  return {
    overdue,
    text: overdue
      ? `window elapsed ${formatDurationMs(-remainingMs)} ago`
      : `${formatDurationMs(remainingMs)} left in window`,
  };
}
