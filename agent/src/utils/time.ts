/**
 * Shared timestamp utilities for the agent.
 *
 * The backend stores naive UTC datetimes and serialises them as ISO-8601
 * strings *without* a trailing 'Z' (see project_timestamptz_migration in
 * the project memory).  `new Date("2026-04-12T10:30:00")` treats that as
 * **local** time, which silently shifts every parsed timestamp by the
 * agent VM's offset (typically the GCE zone's UTC offset, which can vary).
 *
 * CONVENTION: All code that turns a backend-sourced ISO string into a Date
 * MUST go through `parseBackendTimestamp` (or another helper that calls
 * it). Direct `new Date(isoString)` is forbidden when the string came from
 * an API response.
 */

/**
 * Parse an API-sourced ISO timestamp into a Date, treating naive strings
 * (no trailing 'Z' and no '+HH:MM' offset) as UTC.
 *
 * Returns `null` if the input is null/undefined/empty or cannot be parsed.
 */
export function parseBackendTimestamp(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso);
  const d = new Date(hasTz ? iso : iso + 'Z');
  return Number.isNaN(d.getTime()) ? null : d;
}
