import { type EventPref } from './eventPrefs';

export const SAVED_VIEWS_CAP = 10;

export type SavedView = {
  id: string;
  name: string;
  description?: string;
  query: string;
  prefs: Record<string, EventPref>;
  sort: 'importance' | 'recent';
  showResolved: boolean;
  routineExpanded: boolean;
  showOnlyNew: boolean;
  createdAt: number;
};

export type SavedViewsFile = { version: 1; views: SavedView[] };

export function savedViewsKey(userId?: string | null, projectId?: string | null): string {
  return `messages_saved_views:${userId || 'anonymous'}:${projectId || 'default'}`;
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

export function loadSavedViews(userId?: string | null, projectId?: string | null): SavedView[] {
  try {
    const raw = localStorage.getItem(savedViewsKey(userId, projectId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    const arr: unknown = Array.isArray(parsed) ? parsed : (parsed && typeof parsed === 'object' ? (parsed as { views?: unknown }).views : null);
    if (!Array.isArray(arr)) return [];
    const out: SavedView[] = [];
    for (const item of arr) {
      const v = sanitizeSavedView(item);
      if (v) out.push(v);
    }
    return out;
  } catch {
    return [];
  }
}

export function saveSavedViews(
  userId: string | undefined | null,
  projectId: string | undefined | null,
  views: SavedView[],
): void {
  try {
    const key = savedViewsKey(userId, projectId);
    if (!views || views.length === 0) {
      localStorage.removeItem(key);
      return;
    }
    const file: SavedViewsFile = { version: 1, views };
    localStorage.setItem(key, JSON.stringify(file));
  } catch { /* ignore */ }
}
