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
