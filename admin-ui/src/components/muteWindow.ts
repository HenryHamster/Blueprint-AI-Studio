export type MuteWindow = { expiresAt: number };

export function muteWindowKey(userId: string | undefined, projectId: string | undefined): string {
  return `messages_panel_mute_window:${userId || 'anonymous'}:${projectId || 'default'}`;
}

export function loadMuteWindow(userId?: string | null, projectId?: string | null): MuteWindow | null {
  try {
    const key = muteWindowKey(userId || undefined, projectId || undefined);
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { expiresAt?: unknown };
    if (typeof parsed.expiresAt !== 'number' || parsed.expiresAt <= Date.now()) {
      localStorage.removeItem(key);
      return null;
    }
    return { expiresAt: parsed.expiresAt };
  } catch {
    return null;
  }
}

export function saveMuteWindow(
  userId: string | undefined | null,
  projectId: string | undefined | null,
  muteWin: MuteWindow,
): void {
  try {
    localStorage.setItem(muteWindowKey(userId || undefined, projectId || undefined), JSON.stringify(muteWin));
  } catch { /* ignore */ }
}

export function clearMuteWindow(userId: string | undefined | null, projectId: string | undefined | null): void {
  try {
    localStorage.removeItem(muteWindowKey(userId || undefined, projectId || undefined));
  } catch { /* ignore */ }
}

export function isMuteWindowActive(muteWin: MuteWindow | null): boolean {
  return muteWin !== null && muteWin.expiresAt > Date.now();
}

export function formatMuteCountdown(expiresAt: number): string {
  const msLeft = expiresAt - Date.now();
  const minsLeft = Math.floor(msLeft / 60000);
  if (minsLeft < 1) return '<1m left';
  return `${minsLeft}m left`;
}
