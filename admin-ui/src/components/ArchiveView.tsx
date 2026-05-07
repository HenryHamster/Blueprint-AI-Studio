import { X, Search, ExternalLink, Eye, Loader2, Archive, ArchiveRestore, ChevronLeft, ChevronRight, RefreshCw } from 'lucide-react';
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
  all: 'border-l-blue-500',
};

const STATUS_DOT_COLORS: Record<string, string> = {
  COMPLETED: 'bg-emerald-500',
  FAILED: 'bg-red-500',
  CANCELLED: 'bg-neutral-500',
};

const DATE_RANGES: { value: string; label: string }[] = [
  { value: '24h', label: 'Last 24h' },
  { value: '7d', label: '7d' },
  { value: '30d', label: '30d' },
  { value: 'all', label: 'All Time' },
];

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
}: ArchiveViewProps) {
  const showingFrom = (archivePage - 1) * archivePageSize + 1;
  const showingTo = showingFrom + archiveTasks.length - 1;
  const totalPages = Math.max(1, Math.ceil(archiveTotalTasks / archivePageSize));
  const hasNextPage = archivePage < totalPages;

  return (
    <div className="flex-1 overflow-hidden flex flex-col bg-neutral-950 rounded-lg border border-neutral-800">
      {/* Header */}
      <div className="flex items-center justify-between px-6 py-4 border-b border-neutral-800 shrink-0">
        <div className="flex items-center gap-3">
          <Archive className="w-5 h-5 text-neutral-400" />
          <h2 className="text-lg font-semibold text-neutral-100">Archive</h2>
          {archiveLoading && (
            <Loader2 className="w-4 h-4 text-neutral-500 animate-spin" />
          )}
        </div>
        <button
          onClick={() => setShowArchive(false)}
          className="p-1.5 rounded-lg text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800 transition-colors"
        >
          <X className="w-5 h-5" />
        </button>
      </div>

      {/* Filter bar */}
      <div className="px-6 py-3 border-b border-neutral-800 flex flex-wrap items-center gap-3 shrink-0">
        {/* Status filters */}
        <div className="flex items-center gap-1.5">
          {STATUS_FILTERS.map((sf) => (
            <button
              key={sf.value}
              onClick={() => setArchiveStatusFilter(sf.value)}
              className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-all border ${
                archiveStatusFilter === sf.value
                  ? `bg-neutral-800 text-white border-l-2 ${STATUS_BORDER_COLORS[sf.value] || 'border-l-neutral-500'} border-t-neutral-700 border-r-neutral-700 border-b-neutral-700`
                  : 'bg-neutral-950 border-neutral-800 text-neutral-500 hover:border-neutral-700'
              }`}
            >
              {sf.label}
            </button>
          ))}
        </div>

        {/* Search input */}
        <div className="relative flex-1 min-w-[200px] max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-neutral-600" />
          <input
            type="text"
            value={archiveSearchQuery}
            onChange={(e) => setArchiveSearchQuery(e.target.value)}
            placeholder="Search by title or description..."
            className="w-full bg-neutral-900/50 border border-neutral-800/50 rounded-lg pl-9 pr-3 py-1.5 text-sm text-neutral-100 placeholder-neutral-600 focus:ring-1 focus:ring-neutral-700 focus:outline-none"
          />
        </div>

        {/* Date range filters */}
        <div className="flex items-center gap-1.5">
          {DATE_RANGES.map((dr) => (
            <button
              key={dr.value}
              onClick={() => setArchiveDateRange(dr.value)}
              className={`px-2.5 py-1 rounded-lg text-xs font-medium transition-all border ${
                archiveDateRange === dr.value
                  ? 'bg-neutral-800 text-white border-neutral-700'
                  : 'bg-neutral-950 border-neutral-800 text-neutral-500 hover:border-neutral-700'
              }`}
            >
              {dr.label}
            </button>
          ))}
        </div>

        {/* Sort order */}
        <select
          value={archiveSortOrder}
          onChange={(e) => setArchiveSortOrder(e.target.value as 'desc' | 'asc')}
          className="bg-neutral-900 border border-neutral-800 text-neutral-400 text-xs rounded-lg px-2.5 py-1 focus:outline-none focus:ring-1 focus:ring-neutral-700"
        >
          <option value="desc">Newest first</option>
          <option value="asc">Oldest first</option>
        </select>
      </div>

      {/* Results list */}
      <div className="flex-1 overflow-y-auto px-6 py-4 relative">
        {archiveLoading && (
          <div className="absolute inset-0 bg-neutral-950/60 flex items-center justify-center z-10 rounded-b-lg">
            <div className="flex items-center gap-2 px-4 py-2 rounded-lg bg-neutral-900 border border-neutral-800 shadow-lg">
              <Loader2 className="w-4 h-4 text-neutral-400 animate-spin" />
              <span className="text-xs text-neutral-400">Loading...</span>
            </div>
          </div>
        )}
        {archiveTasks.length === 0 && !archiveLoading ? (
          <div className="flex flex-col items-center justify-center h-full text-neutral-500">
            <Archive className="w-10 h-10 mb-3 text-neutral-700" />
            <p className="text-sm">No archived tasks match your filters.</p>
            <button
              onClick={() => {
                setArchiveStatusFilter('all');
                setArchiveSearchQuery('');
                setArchiveDateRange('all');
              }}
              className="mt-3 px-3 py-1.5 rounded-lg text-xs font-medium bg-neutral-800 text-neutral-300 hover:bg-neutral-700 transition-colors border border-neutral-700"
            >
              Clear filters
            </button>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            {archiveTasks.map((task) => {
              const typeStyle = task.task_type ? TASK_TYPE_STYLES[task.task_type] : null;
              const TypeIcon = typeStyle?.icon;
              const dotColor = STATUS_DOT_COLORS[task.status] || 'bg-neutral-600';

              return (
                <div
                  key={task.id}
                  className={`bg-neutral-900 border border-neutral-800 border-l-2 ${STATUS_BORDER_COLORS[task.status] || 'border-l-neutral-600'} rounded-lg px-4 py-3 hover:bg-neutral-800/50 transition-colors flex flex-wrap items-center gap-x-4 gap-y-2`}
                >
                  {/* Title & description */}
                  <div className="flex-1 min-w-0 basis-full sm:basis-0">
                    <p className="text-sm font-medium text-neutral-100 truncate">
                      {task.title}
                    </p>
                    {task.description && (
                      <p className="text-xs text-neutral-500 truncate mt-0.5">
                        {task.description}
                      </p>
                    )}
                  </div>

                  {/* Task type badge */}
                  {typeStyle && TypeIcon && (
                    <span
                      className={`inline-flex items-center gap-1 px-2 py-0.5 rounded text-xs font-medium border ${typeStyle.bg} ${typeStyle.text} shrink-0`}
                    >
                      <TypeIcon className="w-3 h-3" />
                      {typeStyle.label}
                    </span>
                  )}

                  {/* Status badge */}
                  <span className="inline-flex items-center gap-1.5 text-xs text-neutral-400 shrink-0">
                    <span className={`w-2 h-2 rounded-full ${dotColor}`} />
                    {task.status}
                  </span>

                  {/* Timestamp */}
                  <span className="text-xs text-neutral-600 shrink-0 w-16 text-right">
                    {timeAgo(task.created_at)}
                  </span>

                  {/* PR link */}
                  {task.pr_url && isSafeUrl(task.pr_url) && (
                    <a
                      href={task.pr_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="p-1.5 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-700/50 transition-colors shrink-0"
                      title="View PR"
                    >
                      <ExternalLink className="w-4 h-4" />
                    </a>
                  )}

                  {/* View Details */}
                  <button
                    onClick={() => handleViewLogs(task)}
                    className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium bg-neutral-800 text-neutral-300 hover:bg-neutral-700 transition-colors shrink-0"
                  >
                    <Eye className="w-3.5 h-3.5" />
                    View Details
                  </button>

                  {/* Retry (FAILED only) */}
                  {task.status === 'FAILED' && onRetryTask && (
                    <button
                      onClick={() => onRetryTask(task.id)}
                      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium bg-neutral-800 text-blue-400 hover:bg-neutral-700 transition-colors shrink-0"
                      title="Retry task"
                    >
                      <RefreshCw className="w-3 h-3" />
                      Retry
                    </button>
                  )}

                  {/* Unarchive */}
                  {onUnarchiveTask && (
                    <button
                      onClick={() => onUnarchiveTask(task.id)}
                      className="inline-flex items-center gap-1 px-2.5 py-1 rounded-md text-xs font-medium bg-neutral-800 text-neutral-300 hover:bg-neutral-700 transition-colors shrink-0"
                      title="Unarchive task"
                    >
                      <ArchiveRestore className="w-3 h-3" />
                      Unarchive
                    </button>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>

      {/* Pagination footer */}
      <div className="flex flex-wrap items-center justify-between gap-2 px-6 py-2 border-t border-neutral-800 shrink-0">
        <div className="flex items-center gap-2">
          <label className="text-xs text-neutral-500">Per page</label>
          <select
            value={archivePageSize}
            onChange={(e) => setArchivePageSize(Number(e.target.value))}
            className="bg-neutral-800 border border-neutral-700 text-neutral-300 text-xs rounded px-2 py-1"
          >
            <option value={25}>25</option>
            <option value={50}>50</option>
            <option value={100}>100</option>
          </select>
        </div>

        <span className="text-xs text-neutral-500">
          {archiveTasks.length > 0
            ? `Showing ${showingFrom}-${showingTo} of ${archiveTotalTasks}`
            : 'No results'}
        </span>

        <div className="flex items-center gap-2">
          <button
            onClick={() => setArchivePage(archivePage - 1)}
            disabled={archivePage <= 1}
            className="px-3 py-1 text-xs font-medium rounded-md bg-neutral-800 text-neutral-300 hover:bg-neutral-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors inline-flex items-center gap-1"
          >
            <ChevronLeft className="w-3.5 h-3.5" />
            Previous
          </button>
          <span className="text-xs text-neutral-500 min-w-[80px] text-center">Page {archivePage} of {totalPages}</span>
          <button
            onClick={() => setArchivePage(archivePage + 1)}
            disabled={!hasNextPage}
            className="px-3 py-1 text-xs font-medium rounded-md bg-neutral-800 text-neutral-300 hover:bg-neutral-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors inline-flex items-center gap-1"
          >
            Next
            <ChevronRight className="w-3.5 h-3.5" />
          </button>
        </div>
      </div>
    </div>
  );
}
