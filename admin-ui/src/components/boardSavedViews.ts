import type { DateRange } from '../hooks/useSearchFilters';
import type { TaskSortBy } from '../utils/taskCsv';
import { SAVED_VIEWS_CAP } from './savedViews';

// Cap mirrors the Messages saved-views experience so both surfaces behave the same.
export const BOARD_SAVED_VIEWS_CAP = SAVED_VIEWS_CAP;

const DATE_RANGES: DateRange[] = ['all', '1h', '6h', '24h', '7d'];
const SORT_VALUES: TaskSortBy[] = ['newest', 'oldest', 'type', 'title', 'duration', 'attempts'];

// The full task-board filter combination captured by a saved view, plus the
// active sort order (persisted separately via SORT_KEY in App.tsx). Applying a
// view restores every field below through the live filter/sort setters.
export type BoardSavedView = {
  id: string;
  name: string;
  description?: string;
  query: string;
  taskTypeFilter: string[];
  statusFilter: string[];
  dateRange: DateRange;
  blocked: boolean;
  requiresApproval: boolean;
  stuck: boolean;
  stuckMinSeconds: number;
  failureClassFilter: string[];
  sortBy: TaskSortBy;
  createdAt: number;
};

export type BoardSavedViewsFile = { version: 1; views: BoardSavedView[] };

export function boardSavedViewsKey(userId?: string | null, projectId?: string | null): string {
  return `board_saved_views:${userId || 'anonymous'}:${projectId || 'default'}`;
}

// Keep only string entries from an array-valued field; non-arrays collapse to [].
function sanitizeStringArray(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  return raw.filter((v): v is string => typeof v === 'string');
}

function sanitizeBoardSavedView(raw: unknown): BoardSavedView | null {
  if (!raw || typeof raw !== 'object') return null;
  const r = raw as Record<string, unknown>;
  const id = typeof r.id === 'string' && r.id ? r.id : null;
  const name = typeof r.name === 'string' && r.name ? r.name : null;
  if (!id || !name) return null;
  const stuckMinSeconds = Number(r.stuckMinSeconds);
  return {
    id,
    name,
    description: typeof r.description === 'string' ? r.description : undefined,
    query: typeof r.query === 'string' ? r.query : '',
    taskTypeFilter: sanitizeStringArray(r.taskTypeFilter),
    statusFilter: sanitizeStringArray(r.statusFilter),
    dateRange: DATE_RANGES.includes(r.dateRange as DateRange) ? (r.dateRange as DateRange) : 'all',
    blocked: r.blocked === true,
    requiresApproval: r.requiresApproval === true,
    stuck: r.stuck === true,
    stuckMinSeconds: Number.isFinite(stuckMinSeconds) && stuckMinSeconds >= 0 ? stuckMinSeconds : 0,
    failureClassFilter: sanitizeStringArray(r.failureClassFilter),
    sortBy: SORT_VALUES.includes(r.sortBy as TaskSortBy) ? (r.sortBy as TaskSortBy) : 'newest',
    createdAt: typeof r.createdAt === 'number' ? r.createdAt : Date.now(),
  };
}

export function loadBoardSavedViews(userId?: string | null, projectId?: string | null): BoardSavedView[] {
  try {
    const raw = localStorage.getItem(boardSavedViewsKey(userId, projectId));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    const arr: unknown = Array.isArray(parsed)
      ? parsed
      : (parsed && typeof parsed === 'object' ? (parsed as { views?: unknown }).views : null);
    if (!Array.isArray(arr)) return [];
    const out: BoardSavedView[] = [];
    for (const item of arr) {
      const v = sanitizeBoardSavedView(item);
      if (v) out.push(v);
    }
    // Enforce the cap on read too, so an over-stuffed payload from another
    // device/version can never grow the in-memory list past the limit.
    return out.slice(0, BOARD_SAVED_VIEWS_CAP);
  } catch {
    return [];
  }
}

export function saveBoardSavedViews(
  userId: string | undefined | null,
  projectId: string | undefined | null,
  views: BoardSavedView[],
): void {
  try {
    const key = boardSavedViewsKey(userId, projectId);
    if (!views || views.length === 0) {
      localStorage.removeItem(key);
      return;
    }
    const file: BoardSavedViewsFile = { version: 1, views: views.slice(0, BOARD_SAVED_VIEWS_CAP) };
    localStorage.setItem(key, JSON.stringify(file));
  } catch { /* ignore */ }
}
