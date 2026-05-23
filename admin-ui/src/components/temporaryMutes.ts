export type TemporaryMutes = Record<string, number>; // event_type -> expiresAt (ms)

export function temporaryMutesKey(userId: string | undefined, projectId: string | undefined): string {
  return `messages_panel_temp_mutes:${userId || 'anonymous'}:${projectId || 'default'}`;
}

export function loadTemporaryMutes(userId?: string | null, projectId?: string | null): TemporaryMutes {
  try {
    const key = temporaryMutesKey(userId || undefined, projectId || undefined);
    const raw = localStorage.getItem(key);
    if (!raw) return {};
    const parsed = JSON.parse(raw) as Record<string, unknown>;
    const now = Date.now();
    const mutes: TemporaryMutes = {};
    for (const [et, expiresAt] of Object.entries(parsed)) {
      if (typeof expiresAt === 'number' && expiresAt > now) {
        mutes[et] = expiresAt;
      }
    }
    return mutes;
  } catch {
    return {};
  }
}

export function saveTemporaryMutes(
  userId: string | undefined | null,
  projectId: string | undefined | null,
  mutes: TemporaryMutes,
): void {
  try {
    const key = temporaryMutesKey(userId || undefined, projectId || undefined);
    if (Object.keys(mutes).length === 0) {
      localStorage.removeItem(key);
    } else {
      localStorage.setItem(key, JSON.stringify(mutes));
    }
  } catch { /* ignore */ }
}

export function isEventTypeTemporarilyMuted(eventType: string | undefined, tempMutes: TemporaryMutes): boolean {
  if (!eventType) return false;
  const expiresAt = tempMutes[eventType];
  return expiresAt !== undefined && expiresAt > Date.now();
}
