import { useEffect, useMemo, useRef, useState } from 'react';
import { Save, MoreVertical, Pencil, Trash2 } from 'lucide-react';
import type { DateRange } from '../hooks/useSearchFilters';
import type { TaskSortBy } from '../utils/taskCsv';
import { FILTER_TASK_TYPE_IDS } from '../taskCapabilities';
import { STATUS_OPTIONS } from '../constants';
import { SaveViewDialog } from './SaveViewDialog';
import {
  type BoardSavedView,
  loadBoardSavedViews,
  saveBoardSavedViews,
  BOARD_SAVED_VIEWS_CAP,
} from './boardSavedViews';

const DEFAULT_VIEW_ID = '__default__';

// The slice of board state a saved view captures. Mirrors the BoardSavedView
// payload minus its identity fields (id/name/description/createdAt).
export interface BoardFilterSnapshot {
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
}

interface BoardSavedViewsBarProps {
  userId?: string | null;
  projectId?: string | null;
  current: BoardFilterSnapshot;
  onApply: (snapshot: BoardFilterSnapshot) => void;
}

const DEFAULT_SNAPSHOT: BoardFilterSnapshot = {
  query: '',
  taskTypeFilter: [...FILTER_TASK_TYPE_IDS],
  statusFilter: STATUS_OPTIONS.map(o => o.value),
  dateRange: 'all',
  blocked: false,
  requiresApproval: false,
  stuck: false,
  stuckMinSeconds: 0,
  failureClassFilter: [],
  sortBy: 'newest',
};

function sameSet(a: string[], b: string[]): boolean {
  if (a.length !== b.length) return false;
  const setB = new Set(b);
  return a.every(v => setB.has(v));
}

function snapshotMatches(a: BoardFilterSnapshot, b: BoardFilterSnapshot): boolean {
  return (
    a.query === b.query &&
    a.dateRange === b.dateRange &&
    a.blocked === b.blocked &&
    a.requiresApproval === b.requiresApproval &&
    a.stuck === b.stuck &&
    a.stuckMinSeconds === b.stuckMinSeconds &&
    a.sortBy === b.sortBy &&
    sameSet(a.taskTypeFilter, b.taskTypeFilter) &&
    sameSet(a.statusFilter, b.statusFilter) &&
    sameSet(a.failureClassFilter, b.failureClassFilter)
  );
}

function viewToSnapshot(view: BoardSavedView): BoardFilterSnapshot {
  return {
    query: view.query,
    taskTypeFilter: view.taskTypeFilter,
    statusFilter: view.statusFilter,
    dateRange: view.dateRange,
    blocked: view.blocked,
    requiresApproval: view.requiresApproval,
    stuck: view.stuck,
    stuckMinSeconds: view.stuckMinSeconds,
    failureClassFilter: view.failureClassFilter,
    sortBy: view.sortBy,
  };
}

function snapshotSummary(s: BoardFilterSnapshot): string {
  const parts: string[] = [];
  parts.push(s.query.trim() ? `Search: ${s.query.trim().slice(0, 40)}` : 'No search');
  if (s.statusFilter.length < STATUS_OPTIONS.length) parts.push(`${s.statusFilter.length} statuses`);
  if (s.taskTypeFilter.length < FILTER_TASK_TYPE_IDS.length) parts.push(`${s.taskTypeFilter.length} types`);
  if (s.dateRange !== 'all') parts.push(s.dateRange);
  if (s.blocked) parts.push('blocked');
  if (s.requiresApproval) parts.push('needs review');
  if (s.stuck) parts.push(s.stuckMinSeconds > 0 ? `stuck >${Math.round(s.stuckMinSeconds / 60)}m` : 'stuck');
  if (s.failureClassFilter.length > 0) parts.push(`${s.failureClassFilter.length} failure class`);
  parts.push(`sort: ${s.sortBy}`);
  return parts.join(' · ');
}

export function BoardSavedViewsBar({ userId, projectId, current, onApply }: BoardSavedViewsBarProps) {
  const [savedViews, setSavedViews] = useState<BoardSavedView[]>(() => loadBoardSavedViews(userId, projectId));
  const [activeViewId, setActiveViewId] = useState<string>(DEFAULT_VIEW_ID);
  const [openKebabId, setOpenKebabId] = useState<string | null>(null);
  const kebabRef = useRef<HTMLDivElement>(null);

  // Save/rename dialog state — mirrors MessagesPanel's single-dialog approach.
  const [showSaveDialog, setShowSaveDialog] = useState(false);
  const [saveDialogMode, setSaveDialogMode] = useState<'create' | 'rename'>('create');
  const [saveDialogTargetId, setSaveDialogTargetId] = useState<string | null>(null);
  const [saveDialogName, setSaveDialogName] = useState('');
  const [saveDialogDescription, setSaveDialogDescription] = useState('');
  const [saveDialogError, setSaveDialogError] = useState<string | null>(null);

  // NOTE: views are loaded once from the useState initializer above. Resetting on
  // user/project change is handled by App remounting this component via a scoped
  // `key`, so views never bleed across users or projects without a cascading-render
  // effect here.

  // Close the kebab menu on outside click — same pattern as the Messages tab strip.
  useEffect(() => {
    if (!openKebabId) return;
    function handleClickOutside(e: MouseEvent) {
      if (kebabRef.current && !kebabRef.current.contains(e.target as Node)) setOpenKebabId(null);
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [openKebabId]);

  const activeView = useMemo(
    () => savedViews.find(v => v.id === activeViewId) ?? null,
    [savedViews, activeViewId],
  );

  // The active tab is "modified" when live filters diverge from its snapshot.
  // For Default the baseline is the cleared/initial filter state.
  const activeTabModified = useMemo(() => {
    if (activeViewId === DEFAULT_VIEW_ID) return !snapshotMatches(DEFAULT_SNAPSHOT, current);
    if (!activeView) return false;
    return !snapshotMatches(viewToSnapshot(activeView), current);
  }, [activeView, activeViewId, current]);

  function applyView(viewId: string) {
    setOpenKebabId(null);
    if (viewId === DEFAULT_VIEW_ID) {
      setActiveViewId(DEFAULT_VIEW_ID);
      onApply(DEFAULT_SNAPSHOT);
      return;
    }
    const view = savedViews.find(v => v.id === viewId);
    if (!view) return;
    setActiveViewId(view.id);
    onApply(viewToSnapshot(view));
  }

  function openSaveDialog() {
    setSaveDialogMode('create');
    setSaveDialogTargetId(null);
    setSaveDialogName('');
    setSaveDialogDescription('');
    setSaveDialogError(null);
    setShowSaveDialog(true);
  }

  function openRenameDialog(view: BoardSavedView) {
    setSaveDialogMode('rename');
    setSaveDialogTargetId(view.id);
    setSaveDialogName(view.name);
    setSaveDialogDescription(view.description ?? '');
    setSaveDialogError(null);
    setShowSaveDialog(true);
    setOpenKebabId(null);
  }

  function closeSaveDialog() {
    setShowSaveDialog(false);
    setSaveDialogError(null);
  }

  function persist(next: BoardSavedView[]) {
    setSavedViews(next);
    saveBoardSavedViews(userId, projectId, next);
  }

  function commitSaveDialog() {
    const name = saveDialogName.trim();
    if (!name) {
      setSaveDialogError('Name is required');
      return;
    }
    if (saveDialogMode === 'create') {
      if (savedViews.length >= BOARD_SAVED_VIEWS_CAP) {
        setSaveDialogError(`Cap reached (${BOARD_SAVED_VIEWS_CAP}). Delete a view to add another.`);
        return;
      }
      const view: BoardSavedView = {
        id: `view_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
        name,
        description: saveDialogDescription.trim() || undefined,
        query: current.query,
        taskTypeFilter: current.taskTypeFilter,
        statusFilter: current.statusFilter,
        dateRange: current.dateRange,
        blocked: current.blocked,
        requiresApproval: current.requiresApproval,
        stuck: current.stuck,
        stuckMinSeconds: current.stuckMinSeconds,
        failureClassFilter: current.failureClassFilter,
        sortBy: current.sortBy,
        createdAt: Date.now(),
      };
      persist([...savedViews, view]);
      setActiveViewId(view.id);
    } else if (saveDialogTargetId) {
      persist(savedViews.map(v =>
        v.id === saveDialogTargetId
          ? { ...v, name, description: saveDialogDescription.trim() || undefined }
          : v,
      ));
    }
    closeSaveDialog();
  }

  function deleteView(viewId: string) {
    persist(savedViews.filter(v => v.id !== viewId));
    setOpenKebabId(null);
    if (activeViewId === viewId) setActiveViewId(DEFAULT_VIEW_ID);
  }

  const tabs: { id: string; name: string; isDefault: boolean; view: BoardSavedView | null }[] = [
    { id: DEFAULT_VIEW_ID, name: 'Default', isDefault: true, view: null },
    ...savedViews.map(v => ({ id: v.id, name: v.name, isDefault: false, view: v })),
  ];

  return (
    <div className="flex items-center gap-1 mb-2">
      {/* Saved views tab strip — compact tabs allow small touch targets for the switcher */}
      <div
        className="flex items-center gap-1 overflow-x-auto -mx-1 px-1 flex-1 min-w-0"
        data-testid="board-saved-views-tab-strip"
        data-viewport-allow-small-target
      >
        {tabs.map(tab => {
          const isActive = activeViewId === tab.id;
          const showModified = isActive && activeTabModified;
          return (
            <div key={tab.id} className="relative shrink-0 flex items-center">
              <button
                onClick={() => applyView(tab.id)}
                title={tab.view?.description || tab.name}
                className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs transition-colors border min-h-[44px] md:min-h-[32px] ${
                  isActive
                    ? 'bg-blue-500/15 text-blue-300 border-blue-500/40'
                    : 'text-neutral-500 hover:text-neutral-300 border-transparent hover:border-neutral-800/60'
                }`}
              >
                <span>{tab.name}</span>
                {showModified && (
                  <span title="Filters changed" className="w-1.5 h-1.5 rounded-full bg-amber-400" data-testid="board-view-modified-dot" />
                )}
              </button>
              {!tab.isDefault && tab.view && (() => {
                const view = tab.view;
                return (
                <div ref={openKebabId === tab.id ? kebabRef : undefined} className="relative">
                  <button
                    onClick={() => setOpenKebabId(prev => (prev === tab.id ? null : tab.id))}
                    title="Manage view"
                    aria-label={`Manage view ${tab.name}`}
                    className="text-neutral-600 hover:text-neutral-300 p-1 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                  >
                    <MoreVertical className="w-3.5 h-3.5" />
                  </button>
                  {openKebabId === tab.id && (
                    <div className="absolute right-0 top-full mt-1 z-50 min-w-[120px] bg-neutral-900 border border-neutral-700 rounded-lg shadow-xl overflow-hidden">
                      <button
                        onClick={() => openRenameDialog(view)}
                        className="w-full flex items-center gap-2 px-3 py-2 text-xs text-neutral-300 hover:bg-neutral-800 transition-colors min-h-[44px]"
                      >
                        <Pencil className="w-3 h-3" />
                        Rename
                      </button>
                      <button
                        onClick={() => deleteView(tab.id)}
                        className="w-full flex items-center gap-2 px-3 py-2 text-xs text-red-400 hover:bg-red-500/10 transition-colors min-h-[44px]"
                      >
                        <Trash2 className="w-3 h-3" />
                        Delete
                      </button>
                    </div>
                  )}
                </div>
                );
              })()}
            </div>
          );
        })}
      </div>
      <button
        onClick={openSaveDialog}
        title="Save current filters & sort as a view"
        className="shrink-0 flex items-center justify-center gap-1 px-2.5 py-1 rounded-md text-xs text-neutral-400 hover:text-neutral-200 border border-neutral-800/60 hover:border-neutral-700 transition-colors min-h-[44px] min-w-[44px] md:min-h-[32px] md:min-w-auto"
      >
        <Save className="w-3.5 h-3.5" />
        <span className="hidden md:inline">Save view</span>
      </button>

      {showSaveDialog && (
        <SaveViewDialog
          open={showSaveDialog}
          mode={saveDialogMode === 'rename' ? 'rename' : 'create'}
          name={saveDialogName}
          description={saveDialogDescription}
          error={saveDialogError}
          summaryText={snapshotSummary(current)}
          onNameChange={value => { setSaveDialogName(value); setSaveDialogError(null); }}
          onDescriptionChange={setSaveDialogDescription}
          onCommit={commitSaveDialog}
          onClose={closeSaveDialog}
        />
      )}
    </div>
  );
}
