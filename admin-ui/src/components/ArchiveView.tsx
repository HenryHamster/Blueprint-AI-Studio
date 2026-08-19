import {
  Archive,
  ArchiveRestore,
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Eye,
  Loader2,
  RefreshCw,
  Search,
  Trash2,
  X,
} from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import type { Task } from './TaskCard';
import { TASK_TYPE_STYLES, isSafeUrl } from './TaskCard';
import { timeAgo } from '../utils/time';

interface ArchiveViewProps {
  archiveTasks: Task[];
  archiveLoading: boolean;
  archivePage: number;
  archivePageSize: number;
  archiveStatusFilter: string;
  archiveSearchQuery: string;
  archiveDateRange: string;
  archiveTotalTasks: number;
  setShowArchive: (show: boolean) => void;
  setArchivePage: (page: number) => void;
  setArchivePageSize: (size: number) => void;
  setArchiveStatusFilter: (status: string) => void;
  setArchiveSearchQuery: (query: string) => void;
  setArchiveDateRange: (range: string) => void;
  handleViewLogs: (task: Task) => void;
  onRetryTask?: (taskId: string) => void;
  onUnarchiveTask?: (taskId: string) => void;
  archiveSortOrder: 'desc' | 'asc';
  setArchiveSortOrder: (order: 'desc' | 'asc') => void;
  onPurgeArchive?: (olderThanDays?: number) => Promise<void>;
}

const STATUS_FILTERS: { value: string; label: string }[] = [
  { value: 'all', label: 'All' },
  { value: 'COMPLETED', label: 'Completed' },
  { value: 'CANCELLED', label: 'Cancelled' },
  { value: 'FAILED', label: 'Failed' },
];

const STATUS_BORDER_COLORS: Record<string, string> = {
  COMPLETED: 'border-l-emerald-500',
  CANCELLED: 'border-l-neutral-500',
  FAILED: 'border-l-red-500',
  all: 'border-l-cyan-500',
};

const STATUS_DOT_COLORS: Record<string, string> = {
  COMPLETED: 'bg-emerald-500',
  FAILED: 'bg-red-500',
  CANCELLED: 'bg-workbench-control',
};

const DATE_RANGES: { value: string; label: string }[] = [
  { value: '24h', label: 'Last 24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: 'all', label: 'All time' },
];

const PURGE_AGE_OPTIONS: { label: string; days?: number }[] = [
  { label: 'Older than 30 days', days: 30 },
  { label: 'Older than 90 days', days: 90 },
  { label: 'All archived tasks', days: undefined },
];

function statusLabel(status: string): string {
  return status.replace(/_/g, ' ');
}

export default function ArchiveView({
  archiveTasks,
  archiveLoading,
  archivePage,
  archivePageSize,
  archiveStatusFilter,
  archiveSearchQuery,
  archiveDateRange,
  archiveTotalTasks,
  setShowArchive,
  setArchivePage,
  setArchivePageSize,
  setArchiveStatusFilter,
  setArchiveSearchQuery,
  setArchiveDateRange,
  handleViewLogs,
  onRetryTask,
  onUnarchiveTask,
  archiveSortOrder,
  setArchiveSortOrder,
  onPurgeArchive,
}: ArchiveViewProps) {
  const [purgeConfirm, setPurgeConfirm] = useState<{ days?: number } | null>(null);
  const [purgeLoading, setPurgeLoading] = useState(false);
  const [purgeMenuOpen, setPurgeMenuOpen] = useState(false);
  const [pendingActionKey, setPendingActionKey] = useState<string | null>(null);
  const purgeMenuRef = useRef<HTMLDivElement | null>(null);
  const purgeTriggerRef = useRef<HTMLButtonElement | null>(null);
  const purgeDialogRef = useRef<HTMLDivElement | null>(null);
  const cancelPurgeButtonRef = useRef<HTMLButtonElement | null>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const purgeLoadingRef = useRef(false);

  useEffect(() => {
    purgeLoadingRef.current = purgeLoading;
  }, [purgeLoading]);

  useEffect(() => {
    if (!purgeMenuOpen) return undefined;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPurgeMenuOpen(false);
    };
    const onPointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && !purgeMenuRef.current?.contains(target)) {
        setPurgeMenuOpen(false);
      }
    };
    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('pointerdown', onPointerDown, true);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('pointerdown', onPointerDown, true);
    };
  }, [purgeMenuOpen]);

  useEffect(() => {
    if (purgeConfirm === null) return undefined;
    cancelPurgeButtonRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        if (!purgeLoadingRef.current) {
          setPurgeConfirm(null);
        }
        return;
      }
      if (event.key !== 'Tab' || !purgeDialogRef.current) return;
      const focusable = Array.from(
        purgeDialogRef.current.querySelectorAll<HTMLElement>(
          'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
        ),
      );
      if (focusable.length === 0) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('keydown', onKeyDown);
      const focusTarget = previousFocusRef.current;
      if (focusTarget && document.contains(focusTarget)) {
        focusTarget.focus();
      } else {
        purgeTriggerRef.current?.focus();
      }
      previousFocusRef.current = null;
    };
  }, [purgeConfirm]);

  const handlePurgeClick = (days?: number) => {
    previousFocusRef.current = purgeTriggerRef.current;
    setPurgeMenuOpen(false);
    setPurgeConfirm({ days });
  };

  const confirmPurge = async () => {
    if (!onPurgeArchive || purgeConfirm === null || purgeLoading) return;
    setPurgeLoading(true);
    try {
      await onPurgeArchive(purgeConfirm.days);
    } finally {
      setPurgeLoading(false);
      setPurgeConfirm(null);
    }
  };

  const clearFilters = () => {
    setArchiveStatusFilter('all');
    setArchiveSearchQuery('');
    setArchiveDateRange('all');
  };

  const runRowAction = async (key: string, action: () => void | Promise<void>) => {
    if (pendingActionKey === key) return;
    setPendingActionKey(key);
    try {
      await action();
    } finally {
      setPendingActionKey(null);
    }
  };

  const showingFrom = archiveTasks.length > 0 ? (archivePage - 1) * archivePageSize + 1 : 0;
  const showingTo = archiveTasks.length > 0 ? showingFrom + archiveTasks.length - 1 : 0;
  const totalPages = Math.max(1, Math.ceil(archiveTotalTasks / archivePageSize));
  const hasNextPage = archivePage < totalPages;

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden rounded-lg border border-neutral-800 bg-workbench-shell">
      <header className="flex shrink-0 flex-col gap-3 border-b border-neutral-800 px-3 py-3 md:flex-row md:items-center md:justify-between md:px-5">
        <div className="flex min-w-0 items-center gap-3">
          <Archive className="h-5 w-5 shrink-0 text-neutral-400" aria-hidden="true" />
          <div className="min-w-0">
            <h2 className="truncate text-lg font-semibold text-neutral-100">Archive</h2>
            <p className="mt-0.5 text-xs text-neutral-500">
              {archiveTotalTasks > 0 ? `${archiveTotalTasks} archived` : 'No archived tasks'}
            </p>
          </div>
          {archiveLoading && <Loader2 className="h-4 w-4 shrink-0 animate-spin text-neutral-500" aria-hidden="true" />}
        </div>
        <div className="flex min-w-0 items-center justify-between gap-2 md:justify-end">
          {onPurgeArchive && (
            <div
              ref={purgeMenuRef}
              className="relative min-w-0"
              onBlur={(event) => {
                const nextFocus = event.relatedTarget;
                if (!(nextFocus instanceof Node) || !event.currentTarget.contains(nextFocus)) {
                  setPurgeMenuOpen(false);
                }
              }}
            >
              <button
                ref={purgeTriggerRef}
                type="button"
                onClick={() => setPurgeMenuOpen(open => !open)}
                aria-haspopup="menu"
                aria-expanded={purgeMenuOpen}
                aria-controls="archive-purge-menu"
                className="inline-flex min-h-11 items-center gap-2 rounded-md border border-neutral-800 bg-workbench-base px-3 text-sm font-semibold text-red-300 transition-colors hover:border-red-900 hover:bg-red-950/35 hover:text-red-200 md:min-h-9 md:text-xs"
                title="Permanently delete archived tasks"
              >
                <Trash2 className="h-4 w-4 shrink-0" aria-hidden="true" />
                Clear archive
              </button>
              {purgeMenuOpen && (
                <div
                  id="archive-purge-menu"
                  role="menu"
                  className="absolute right-0 top-full z-20 mt-1 w-56 overflow-hidden rounded-md border border-neutral-700 bg-workbench-base shadow-xl"
                >
                  {PURGE_AGE_OPTIONS.map(option => (
                    <button
                      key={option.label}
                      type="button"
                      role="menuitem"
                      onClick={() => handlePurgeClick(option.days)}
                      className="flex min-h-11 w-full items-center px-3 text-left text-sm text-neutral-300 transition-colors hover:bg-workbench-hover hover:text-red-300 md:min-h-9 md:text-xs"
                    >
                      {option.label}
                    </button>
                  ))}
                </div>
              )}
            </div>
          )}
          <button
            type="button"
            onClick={() => setShowArchive(false)}
            className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-md text-neutral-500 transition-colors hover:bg-workbench-hover hover:text-neutral-100 md:min-h-9 md:min-w-9"
            title="Close archive"
            aria-label="Close archive"
          >
            <X className="h-5 w-5" aria-hidden="true" />
          </button>
        </div>
      </header>

      {purgeConfirm !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 px-4">
          <div
            ref={purgeDialogRef}
            role="dialog"
            aria-modal="true"
            aria-labelledby="archive-purge-title"
            aria-busy={purgeLoading}
            className="w-full max-w-sm rounded-lg border border-neutral-700 bg-workbench-base p-5 shadow-2xl"
          >
            <div className="flex items-center gap-3">
              <Trash2 className="h-5 w-5 shrink-0 text-red-400" aria-hidden="true" />
              <h3 id="archive-purge-title" className="text-base font-semibold text-neutral-100">
                Permanently delete?
              </h3>
            </div>
            <p className="mt-3 text-sm leading-5 text-neutral-400">
              {purgeConfirm.days
                ? `Archived tasks older than ${purgeConfirm.days} days will be permanently deleted. This cannot be undone.`
                : 'All archived tasks will be permanently deleted. This cannot be undone.'}
            </p>
            <div className="mt-5 flex justify-end gap-2">
              <button
                ref={cancelPurgeButtonRef}
                type="button"
                onClick={() => setPurgeConfirm(null)}
                disabled={purgeLoading}
                className="min-h-11 rounded-md border border-neutral-700 bg-workbench-panel px-4 text-sm font-semibold text-neutral-300 transition-colors hover:bg-workbench-hover disabled:cursor-not-allowed disabled:opacity-50 md:min-h-9"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={confirmPurge}
                aria-disabled={purgeLoading}
                className={`inline-flex min-h-11 items-center gap-2 rounded-md bg-red-700 px-4 text-sm font-semibold text-white transition-colors md:min-h-9 ${
                  purgeLoading ? 'cursor-not-allowed opacity-50' : 'hover:bg-red-600'
                }`}
              >
                {purgeLoading && <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />}
                Delete permanently
              </button>
            </div>
          </div>
        </div>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-1 overflow-hidden lg:grid-cols-[minmax(0,1fr)_18rem]">
        <aside
          data-testid="archive-actions-panel"
          className="order-first min-h-0 border-b border-neutral-800 bg-[#0b1118] p-3 lg:col-start-2 lg:row-start-1 lg:border-b-0 lg:border-l"
        >
          <div className="grid gap-3">
            <section className="rounded-md border border-neutral-800 bg-workbench-shell/70 p-3">
              <div className="flex items-center justify-between gap-3">
                <h3 className="text-sm font-semibold text-neutral-100">Archive actions</h3>
                <span className="font-mono text-xs text-neutral-500">{archiveTasks.length}</span>
              </div>
              <dl className="mt-3 grid gap-2 text-xs">
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-neutral-500">Visible</dt>
                  <dd className="font-mono text-neutral-300">{archiveTasks.length}</dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-neutral-500">Total</dt>
                  <dd className="font-mono text-neutral-300">{archiveTotalTasks}</dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-neutral-500">Page</dt>
                  <dd className="font-mono text-neutral-300">{archivePage}/{totalPages}</dd>
                </div>
              </dl>
            </section>

            <section className="rounded-md border border-neutral-800 bg-workbench-shell/70 p-3">
              <h3 className="text-sm font-semibold text-neutral-100">Filters</h3>
              <dl className="mt-3 grid gap-2 text-xs">
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-neutral-500">Status</dt>
                  <dd className="font-mono text-neutral-300">{archiveStatusFilter}</dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-neutral-500">Range</dt>
                  <dd className="font-mono text-neutral-300">{archiveDateRange}</dd>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <dt className="text-neutral-500">Sort</dt>
                  <dd className="font-mono text-neutral-300">{archiveSortOrder}</dd>
                </div>
              </dl>
              <button
                type="button"
                onClick={clearFilters}
                className="mt-3 inline-flex min-h-11 w-full items-center justify-center rounded-md border border-neutral-800 bg-workbench-base px-3 text-sm font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-workbench-hover"
              >
                Clear filters
              </button>
            </section>
          </div>
        </aside>

        <main data-testid="archive-task-list" className="min-h-0 min-w-0 overflow-auto px-3 py-3 custom-scrollbar md:px-5 lg:col-start-1 lg:row-start-1">
          <div className="grid gap-3">
            <section className="grid gap-3 rounded-md border border-neutral-800 bg-[#0b1118] p-3">
              <div className="grid gap-2 md:grid-cols-[minmax(0,1fr)_auto_auto] md:items-center">
                <div className="relative min-w-0">
                  <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-neutral-600" aria-hidden="true" />
                  <input
                    type="text"
                    value={archiveSearchQuery}
                    onChange={(event) => setArchiveSearchQuery(event.target.value)}
                    placeholder="Search archived tasks"
                    className="min-h-11 w-full rounded-md border border-neutral-800 bg-workbench-shell py-2 pl-9 pr-3 text-sm text-neutral-100 outline-none transition-colors placeholder:text-neutral-600 workbench-focus"
                    aria-label="Search archived tasks"
                  />
                </div>
                <select
                  value={archiveSortOrder}
                  onChange={(event) => setArchiveSortOrder(event.target.value as 'desc' | 'asc')}
                  className="min-h-11 rounded-md border border-neutral-800 bg-workbench-shell px-3 text-sm text-neutral-300 outline-none workbench-focus"
                  aria-label="Archive sort order"
                >
                  <option value="desc">Newest first</option>
                  <option value="asc">Oldest first</option>
                </select>
                <select
                  value={archivePageSize}
                  onChange={(event) => setArchivePageSize(Number(event.target.value))}
                  className="min-h-11 rounded-md border border-neutral-800 bg-workbench-shell px-3 text-sm text-neutral-300 outline-none workbench-focus"
                  aria-label="Archived tasks per page"
                >
                  <option value={25}>25 per page</option>
                  <option value={50}>50 per page</option>
                  <option value={100}>100 per page</option>
                </select>
              </div>

              <div className="flex min-w-0 flex-wrap gap-2">
                {STATUS_FILTERS.map(filter => (
                  <button
                    key={filter.value}
                    type="button"
                    onClick={() => setArchiveStatusFilter(filter.value)}
                    aria-pressed={archiveStatusFilter === filter.value}
                    className={`min-h-11 min-w-11 rounded-md border px-3 text-sm font-semibold transition-colors ${
                      archiveStatusFilter === filter.value
                        ? `border-neutral-700 bg-workbench-panel text-neutral-100 border-l-2 ${STATUS_BORDER_COLORS[filter.value] || 'border-l-neutral-500'}`
                        : 'border-neutral-800 bg-workbench-shell text-neutral-400 hover:border-neutral-700 hover:text-neutral-200'
                    }`}
                  >
                    {filter.label}
                  </button>
                ))}
              </div>

              <div className="flex min-w-0 flex-wrap gap-2">
                {DATE_RANGES.map(range => (
                  <button
                    key={range.value}
                    type="button"
                    onClick={() => setArchiveDateRange(range.value)}
                    aria-pressed={archiveDateRange === range.value}
                    className={`min-h-11 min-w-11 rounded-md border px-3 text-sm font-semibold transition-colors ${
                      archiveDateRange === range.value
                        ? 'border-neutral-700 bg-workbench-panel text-neutral-100'
                        : 'border-neutral-800 bg-workbench-shell text-neutral-400 hover:border-neutral-700 hover:text-neutral-200'
                    }`}
                  >
                    {range.label}
                  </button>
                ))}
              </div>
            </section>

            <section className="relative min-h-[18rem]">
              {archiveLoading && (
                <div className="absolute inset-0 z-10 flex items-center justify-center rounded-md bg-workbench-shell/70">
                  <div className="flex items-center gap-2 rounded-md border border-neutral-800 bg-workbench-base px-4 py-3 shadow-lg">
                    <Loader2 className="h-4 w-4 animate-spin text-neutral-400" aria-hidden="true" />
                    <span className="text-sm text-neutral-400">Loading</span>
                  </div>
                </div>
              )}

              {archiveTasks.length === 0 && !archiveLoading ? (
                <div data-testid="archive-empty-state" className="flex min-h-[18rem] flex-col items-center justify-center rounded-md border border-neutral-800 bg-[#0b1118] px-4 text-center text-neutral-500">
                  <Archive className="mb-3 h-10 w-10 text-neutral-700" aria-hidden="true" />
                  <p className="text-sm">No archived tasks match your filters.</p>
                  <button
                    type="button"
                    onClick={clearFilters}
                    className="mt-3 min-h-11 rounded-md border border-neutral-800 bg-workbench-base px-4 text-sm font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-workbench-hover"
                  >
                    Clear filters
                  </button>
                </div>
              ) : (
                <div className="grid gap-2">
                  {archiveTasks.map(task => {
                    const typeStyle = task.task_type ? TASK_TYPE_STYLES[task.task_type] : null;
                    const TypeIcon = typeStyle?.icon;
                    const dotColor = STATUS_DOT_COLORS[task.status] || 'bg-workbench-card';

                    return (
                      <article
                        key={task.id}
                        className={`grid min-w-0 gap-3 rounded-md border border-l-2 border-neutral-800 ${STATUS_BORDER_COLORS[task.status] || 'border-l-neutral-600'} bg-workbench-base/70 p-3 transition-colors hover:bg-workbench-hover md:grid-cols-[minmax(0,1fr)_auto] md:items-center`}
                      >
                        <div className="min-w-0">
                          <p className="truncate text-sm font-semibold text-neutral-100" data-viewport-allow-truncation>
                            {task.title}
                          </p>
                          {task.description && (
                            <p className="mt-1 truncate text-xs text-neutral-500" data-viewport-allow-truncation>
                              {task.description}
                            </p>
                          )}
                          <div className="mt-2 flex min-w-0 flex-wrap items-center gap-2">
                            {typeStyle && TypeIcon && (
                              <span className={`inline-flex min-h-7 items-center gap-1 rounded border px-2 text-xs font-semibold ${typeStyle.bg} ${typeStyle.text}`}>
                                <TypeIcon className="h-3.5 w-3.5" aria-hidden="true" />
                                {typeStyle.label}
                              </span>
                            )}
                            <span className="inline-flex min-h-7 items-center gap-1.5 rounded border border-neutral-800 bg-workbench-shell/60 px-2 text-xs font-semibold text-neutral-300">
                              <span className={`h-2 w-2 rounded-full ${dotColor}`} aria-hidden="true" />
                              {statusLabel(task.status)}
                            </span>
                            <span className="inline-flex min-h-7 items-center rounded border border-neutral-800 bg-workbench-shell/60 px-2 font-mono text-xs text-neutral-500">
                              {timeAgo(task.created_at)}
                            </span>
                          </div>
                        </div>

                        <div className="flex min-w-0 flex-wrap gap-2 md:justify-end">
                          {task.pr_url && isSafeUrl(task.pr_url) && (
                            <a
                              href={task.pr_url}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex min-h-11 min-w-11 items-center justify-center rounded-md border border-neutral-800 bg-workbench-shell text-neutral-400 transition-colors hover:border-neutral-700 hover:bg-workbench-hover hover:text-neutral-100"
                              title="View pull request"
                              aria-label={`View pull request for ${task.title}`}
                            >
                              <ExternalLink className="h-4 w-4" aria-hidden="true" />
                            </a>
                          )}
                          <button
                            type="button"
                            onClick={() => handleViewLogs(task)}
                            className="inline-flex min-h-11 items-center gap-2 rounded-md border border-neutral-800 bg-workbench-shell px-3 text-sm font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-workbench-hover"
                          >
                            <Eye className="h-4 w-4" aria-hidden="true" />
                            Details
                          </button>
                          {task.status === 'FAILED' && onRetryTask && (
                            <button
                              type="button"
                              onClick={() => { void runRowAction(`retry:${task.id}`, () => onRetryTask(task.id)); }}
                              disabled={pendingActionKey === `retry:${task.id}`}
                              className="inline-flex min-h-11 items-center gap-2 rounded-md border border-neutral-800 bg-workbench-shell px-3 text-sm font-semibold text-blue-300 transition-colors hover:border-neutral-700 hover:bg-workbench-hover"
                              title="Retry task"
                            >
                              {pendingActionKey === `retry:${task.id}`
                                ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                                : <RefreshCw className="h-4 w-4" aria-hidden="true" />}
                              Retry
                            </button>
                          )}
                          {onUnarchiveTask && (
                            <button
                              type="button"
                              onClick={() => { void runRowAction(`unarchive:${task.id}`, () => onUnarchiveTask(task.id)); }}
                              disabled={pendingActionKey === `unarchive:${task.id}`}
                              className="inline-flex min-h-11 items-center gap-2 rounded-md border border-neutral-800 bg-workbench-shell px-3 text-sm font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-workbench-hover"
                              title="Unarchive task"
                            >
                              {pendingActionKey === `unarchive:${task.id}`
                                ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden="true" />
                                : <ArchiveRestore className="h-4 w-4" aria-hidden="true" />}
                              Unarchive
                            </button>
                          )}
                        </div>
                      </article>
                    );
                  })}
                </div>
              )}
            </section>
          </div>
        </main>
      </div>

      <footer className="flex shrink-0 flex-col gap-3 border-t border-neutral-800 px-3 py-3 md:flex-row md:items-center md:justify-between md:px-5">
        <span className="text-sm text-neutral-500">
          {archiveTasks.length > 0
            ? `Showing ${showingFrom}-${showingTo} of ${archiveTotalTasks}`
            : 'No results'}
        </span>
        <div className="flex min-w-0 items-center gap-2">
          <button
            type="button"
            onClick={() => setArchivePage(archivePage - 1)}
            disabled={archivePage <= 1}
            className="inline-flex min-h-11 items-center gap-2 rounded-md border border-neutral-800 bg-workbench-base px-3 text-sm font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-workbench-hover disabled:cursor-not-allowed disabled:opacity-50"
          >
            <ChevronLeft className="h-4 w-4" aria-hidden="true" />
            Previous
          </button>
          <span className="min-w-16 text-center font-mono text-sm text-neutral-500">
            {archivePage}/{totalPages}
          </span>
          <button
            type="button"
            onClick={() => setArchivePage(archivePage + 1)}
            disabled={!hasNextPage}
            className="inline-flex min-h-11 items-center gap-2 rounded-md border border-neutral-800 bg-workbench-base px-3 text-sm font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-workbench-hover disabled:cursor-not-allowed disabled:opacity-50"
          >
            Next
            <ChevronRight className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
      </footer>
    </div>
  );
}
