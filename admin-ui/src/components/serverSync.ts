import { apiFetch, API_BASE_URL } from '../api';
import { type EventPref } from './eventPrefs';
import { type SavedView } from './savedViews';
import { type TemporaryMutes } from './temporaryMutes';
import { type MuteWindow } from './muteWindow';

let currentProjectSyncId: string | null = null;

function markCurrentProjectSync(projectId: string): void {
  currentProjectSyncId = projectId;
}

function isCurrentProjectSync(projectId: string): boolean {
  return currentProjectSyncId === projectId;
}

function sanitizeServerEventPrefs(raw: Record<string, unknown>): Record<string, EventPref> {
  const out: Record<string, EventPref> = {};
  for (const [k, v] of Object.entries(raw)) {
    if (v === 'mute' || v === 'routine' || v === 'surfaced') out[k] = v;
  }
  return out;
}

export async function fetchServerNoisePrefs(
  projectId: string,
): Promise<Record<string, EventPref> | null> {
  markCurrentProjectSync(projectId);
  try {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/noise-prefs?project_id=${encodeURIComponent(projectId)}`);
    if (!res.ok) return null;
    const data = await res.json() as { prefs?: Record<string, unknown> };
    if (!isCurrentProjectSync(projectId)) return null;
    return sanitizeServerEventPrefs(data.prefs ?? {});
  } catch {
    return null;
  }
}

export async function saveServerNoisePrefs(
  projectId: string,
  prefs: Record<string, EventPref>,
): Promise<void> {
  try {
    await apiFetch(`${API_BASE_URL}/api/admin/messages/noise-prefs`, {
      method: 'PUT',
      body: JSON.stringify({ project_id: projectId, prefs }),
    });
  } catch { /* ignore — localStorage copy remains authoritative offline */ }
}

export function resolveNoisePrefs(
  localPrefs: Record<string, EventPref>,
  serverPrefs: Record<string, EventPref> | null,
): Record<string, EventPref> {
  return serverPrefs ?? localPrefs;
}

function sanitizeSavedView(raw: unknown): SavedView | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const id = typeof r.id === 'string' && r.id ? r.id : null;
  const name = typeof r.name === 'string' && r.name ? r.name : null;
  if (!id || !name) return null;
  const prefsRaw = r.prefs && typeof r.prefs === 'object' ? r.prefs as Record<string, unknown> : {};
  const prefs: Record<string, EventPref> = {};
  for (const [k, v] of Object.entries(prefsRaw)) {
    if (v === 'mute' || v === 'routine' || v === 'surfaced') prefs[k] = v;
  }
  return {
    id,
    name,
    description: typeof r.description === 'string' ? r.description : undefined,
    query: typeof r.query === 'string' ? r.query : '',
    prefs,
    sort: r.sort === 'recent' ? 'recent' : 'importance',
    showResolved: r.showResolved === true,
    routineExpanded: r.routineExpanded === true,
    showOnlyNew: r.showOnlyNew === true,
    createdAt: typeof r.createdAt === 'number' ? r.createdAt : Date.now(),
  };
}

export async function fetchServerSavedViews(
  projectId: string,
): Promise<SavedView[] | null> {
  markCurrentProjectSync(projectId);
  try {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/saved-views?project_id=${encodeURIComponent(projectId)}`);
    if (!res.ok) return null;
    const data = await res.json() as { saved_views?: unknown[] };
    if (!isCurrentProjectSync(projectId)) return null;
    if (!Array.isArray(data.saved_views)) return [];
    const out: SavedView[] = [];
    for (const item of data.saved_views) {
      const v = sanitizeSavedView(item);
      if (v) out.push(v);
    }
    return out;
  } catch {
    return null;
  }
}

export async function saveServerSavedViews(
  projectId: string,
  views: SavedView[],
): Promise<void> {
  try {
    await apiFetch(`${API_BASE_URL}/api/admin/messages/saved-views`, {
      method: 'PUT',
      body: JSON.stringify({ project_id: projectId, saved_views: views }),
    });
  } catch { /* ignore — localStorage copy remains authoritative offline */ }
}

export function resolveServerSavedViews(
  localViews: SavedView[],
  serverViews: SavedView[] | null,
): SavedView[] {
  return serverViews ?? localViews;
}

export async function fetchServerTempMutes(
  projectId: string,
): Promise<TemporaryMutes | null> {
  markCurrentProjectSync(projectId);
  try {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/temp-mutes?project_id=${encodeURIComponent(projectId)}`);
    if (!res.ok) return null;
    const data = await res.json() as { temp_mutes?: Record<string, unknown> };
    if (!isCurrentProjectSync(projectId)) return null;
    const raw = data.temp_mutes ?? {};
    const now = Date.now();
    const mutes: TemporaryMutes = {};
    for (const [et, exp] of Object.entries(raw)) {
      if (typeof exp === 'number' && exp > now) mutes[et] = exp;
    }
    return mutes;
  } catch {
    return null;
  }
}

export async function saveServerTempMutes(
  projectId: string,
  mutes: TemporaryMutes,
): Promise<void> {
  try {
    await apiFetch(`${API_BASE_URL}/api/admin/messages/temp-mutes`, {
      method: 'PUT',
      body: JSON.stringify({ project_id: projectId, temp_mutes: mutes }),
    });
  } catch { /* ignore — localStorage copy remains authoritative offline */ }
}

export function resolveServerTempMutes(
  localMutes: TemporaryMutes,
  serverMutes: TemporaryMutes | null,
): TemporaryMutes {
  return serverMutes ?? localMutes;
}

// Three-valued return: MuteWindow = active, null = server says cleared, undefined = fetch failed.
// resolveServerMuteWindow uses undefined to distinguish offline from an intentional clear.
export async function fetchServerMuteWindow(
  projectId: string,
): Promise<MuteWindow | null | undefined> {
  markCurrentProjectSync(projectId);
  try {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/mute-window?project_id=${encodeURIComponent(projectId)}`);
    if (!res.ok) return undefined;
    const data = await res.json() as { mute_window?: { expires_at?: unknown } | null };
    if (!isCurrentProjectSync(projectId)) return undefined;
    const raw = data.mute_window;
    if (!raw || typeof raw.expires_at !== 'number' || raw.expires_at <= Date.now()) return null;
    return { expiresAt: raw.expires_at };
  } catch {
    return undefined;
  }
}

export async function saveServerMuteWindow(
  projectId: string,
  muteWin: MuteWindow | null,
): Promise<void> {
  try {
    await apiFetch(`${API_BASE_URL}/api/admin/messages/mute-window`, {
      method: 'PUT',
      body: JSON.stringify({ project_id: projectId, expires_at: muteWin ? muteWin.expiresAt : null }),
    });
  } catch { /* ignore — localStorage copy remains authoritative offline */ }
}

export function resolveServerMuteWindow(
  localWin: MuteWindow | null,
  serverWin: MuteWindow | null | undefined,
): MuteWindow | null {
  if (serverWin === undefined) return localWin;
  return serverWin;
}

export async function fetchBackendLastSeen(projectId: string): Promise<number | null> {
  markCurrentProjectSync(projectId);
  try {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/last-seen?project_id=${encodeURIComponent(projectId)}`);
    if (!res.ok) return null;
    const data = await res.json() as { last_seen_at?: number | null };
    if (!isCurrentProjectSync(projectId)) return null;
    const val = data.last_seen_at;
    return typeof val === 'number' && val > 0 ? val : null;
  } catch {
    return null;
  }
}
