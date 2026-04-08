import { useEffect, useState, useCallback, type ReactElement } from 'react';
import { X, CheckCircle, XCircle, Microscope, Rocket, ExternalLink, Loader2, Clock, ChevronDown, ChevronRight } from 'lucide-react';
import { apiFetch, API_BASE_URL } from '../api';
import { isSafeUrl } from './TaskCard';
import type { Task } from './TaskCard';

export type ActivityEventType = 'TASK_COMPLETED' | 'TASK_FAILED' | 'RESEARCH_FINDINGS' | 'DEPLOY_EVENT' | 'TASK_IN_PROGRESS';

export interface ActivityEvent {
  id: string;
  type: ActivityEventType;
  title: string;
  timestamp: string;
  task_id?: string;
  pr_url?: string;
  error_summary?: string;
  finding_count?: number;
  deploy_status?: string;
  project_id?: string;
  started_at?: string;
  latest_status_message?: string;
  task_type?: string;
  commit_hash?: string;
  repo_url?: string;
  status?: string;
  duration_seconds?: number;
}

interface ActivityFeedProps {
  selectedProjectId: string | null;
  refreshInterval: number;
  onSelectTask: (task: Task) => void;
  onClose: () => void;
  lastVisitTimestamp?: number | null;
  onEventsChange?: (events: ActivityEvent[]) => void;
  initialFilter?: string;
}

function formatRelativeTime(dateStr: string): string {
  const date = new Date(dateStr);
  const diffMs = Date.now() - date.getTime();
  const diffMin = Math.floor(diffMs / 60000);
  const diffHr = Math.floor(diffMin / 60);
  const diffDays = Math.floor(diffHr / 24);

  if (diffMin < 1) return 'just now';
  if (diffMin < 60) return `${diffMin}m ago`;
  if (diffHr < 24) return `${diffHr}h ago`;
  if (diffDays < 30) return `${diffDays}d ago`;
  return date.toLocaleDateString();
}

function formatFullDatetime(dateStr: string): string {
  return new Date(dateStr).toLocaleString();
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  if (m < 60) return s > 0 ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rem = m % 60;
  return rem > 0 ? `${h}h ${rem}m` : `${h}h`;
}

const EVENT_ICON: Record<ActivityEventType, ReactElement> = {
  TASK_COMPLETED: <CheckCircle className="w-4 h-4 text-emerald-400 shrink-0" />,
  TASK_FAILED: <XCircle className="w-4 h-4 text-red-400 shrink-0" />,
  RESEARCH_FINDINGS: <Microscope className="w-4 h-4 text-blue-400 shrink-0" />,
  DEPLOY_EVENT: <Rocket className="w-4 h-4 text-purple-400 shrink-0" />,
  TASK_IN_PROGRESS: <Loader2 className="w-4 h-4 text-amber-400 shrink-0 animate-spin" />,
};

const EVENT_BORDER: Record<ActivityEventType, string> = {
  TASK_COMPLETED: 'border-l-emerald-500',
  TASK_FAILED: 'border-l-red-500',
  RESEARCH_FINDINGS: 'border-l-blue-500',
  DEPLOY_EVENT: 'border-l-purple-500',
  TASK_IN_PROGRESS: 'border-l-amber-500',
};

const DEPLOY_STATUS_BADGE: Record<string, string> = {
  STABLE: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  MONITORING: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  ROLLED_BACK: 'bg-red-500/20 text-red-400 border-red-500/30',
  DEPLOYING: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
  FAILED: 'bg-red-500/20 text-red-400 border-red-500/30',
};

function SkeletonRow() {
  return (
    <div className="bg-neutral-900 border border-neutral-800 border-l-2 border-l-neutral-700 rounded-lg px-4 py-3 animate-pulse">
      <div className="flex items-center gap-3">
        <div className="w-4 h-4 rounded-full bg-neutral-700 shrink-0" />
        <div className="flex-1 min-w-0 space-y-1.5">
          <div className="h-3 bg-neutral-700 rounded w-2/3" />
          <div className="h-2.5 bg-neutral-800 rounded w-1/3" />
        </div>
        <div className="h-2.5 bg-neutral-700 rounded w-14 shrink-0" />
      </div>
    </div>
  );
}

type FilterOption = ActivityEventType | 'ALL';

const FILTER_PILLS: { label: string; value: FilterOption; activeClass: string }[] = [
  { label: 'All', value: 'ALL', activeClass: 'bg-neutral-600 text-neutral-100 border-neutral-500' },
  { label: 'Completed', value: 'TASK_COMPLETED', activeClass: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/40' },
  { label: 'Failed', value: 'TASK_FAILED', activeClass: 'bg-red-500/20 text-red-400 border-red-500/40' },
  { label: 'Research', value: 'RESEARCH_FINDINGS', activeClass: 'bg-blue-500/20 text-blue-400 border-blue-500/40' },
  { label: 'Deploy', value: 'DEPLOY_EVENT', activeClass: 'bg-purple-500/20 text-purple-400 border-purple-500/40' },
];

type TimeRangeOption = '6h' | '24h' | 'week' | 'all' | 'last_visit';

const TIME_RANGE_PILLS: { label: string; value: TimeRangeOption }[] = [
  { label: 'Last 6h', value: '6h' },
  { label: 'Last 24h', value: '24h' },
  { label: 'This Week', value: 'week' },
  { label: 'All', value: 'all' },
];

function getTimeRangeSince(range: TimeRangeOption, lastVisitTimestamp?: number | null): string | null {
  if (range === 'all') return null;
  if (range === 'last_visit') {
    if (lastVisitTimestamp != null) return new Date(lastVisitTimestamp).toISOString();
    // fall back to 24h ago if no last-visit timestamp
    return new Date(Date.now() - 24 * 3600 * 1000).toISOString();
  }
  const now = Date.now();
  if (range === '6h') return new Date(now - 6 * 3600 * 1000).toISOString();
  if (range === '24h') return new Date(now - 24 * 3600 * 1000).toISOString();
  // 'week' = last 7 days
  return new Date(now - 7 * 24 * 3600 * 1000).toISOString();
}

export default function ActivityFeed({ selectedProjectId, refreshInterval, onSelectTask, onClose, lastVisitTimestamp, onEventsChange, initialFilter }: ActivityFeedProps) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedErrors, setExpandedErrors] = useState<Set<string>>(new Set());
  const [activeFilter, setActiveFilter] = useState<FilterOption>((initialFilter as FilterOption) ?? 'ALL');
  const [timeRange, setTimeRange] = useState<TimeRangeOption>(lastVisitTimestamp != null ? 'last_visit' : 'all');

  const fetchEvents = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (selectedProjectId) params.set('project_id', selectedProjectId);
      const since = getTimeRangeSince(timeRange, lastVisitTimestamp);
      if (since) params.set('since', since);
      const res = await apiFetch(`${API_BASE_URL}/api/admin/activity?${params}`);
      if (!res.ok) {
        const errorBody = await res.json().catch(() => null);
        const msg = errorBody?.detail || errorBody?.message || `HTTP ${res.status}`;
        throw new Error(msg);
      }
      const data = await res.json();
      const loaded: ActivityEvent[] = Array.isArray(data) ? data : (data.events ?? []);
      setEvents(loaded);
      onEventsChange?.(loaded);
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId, timeRange, lastVisitTimestamp, onEventsChange]);

  useEffect(() => {
    setLoading(true);
    fetchEvents();
  }, [fetchEvents]);

  useEffect(() => {
    if (refreshInterval <= 0) return;
    const interval = setInterval(fetchEvents, refreshInterval);
    return () => clearInterval(interval);
  }, [fetchEvents, refreshInterval]);

  const filteredEvents = activeFilter === 'ALL' ? events : events.filter(e => e.type === activeFilter);

  const handleRowClick = (event: ActivityEvent) => {
    if (!event.task_id) return;
    // Construct a minimal Task object to open TaskDetailDrawer
    onSelectTask({ id: event.task_id } as Task);
  };

  return (
    <div className="flex-1 overflow-hidden flex flex-col min-h-0">
      {/* Header */}
      <div className="px-6 py-4 border-b border-neutral-800 flex items-center justify-between shrink-0">
        <div className="flex items-center gap-2">
          <Clock className="w-4 h-4 text-neutral-400" />
          <h2 className="text-sm font-semibold text-neutral-100">Activity Feed</h2>
          {!loading && (
            <span className="bg-neutral-800 text-neutral-400 border border-neutral-700 text-xs font-mono px-2 py-0.5 rounded-full">
              {filteredEvents.length}
            </span>
          )}
          {!loading && (() => {
            const pendingCount = events.filter(e => e.status === 'NEEDS_REVIEW').length;
            if (pendingCount === 0) return null;
            return (
              <span className="bg-amber-500/20 text-amber-400 border border-amber-500/40 text-xs font-semibold px-2 py-0.5 rounded-full">
                {pendingCount} pending approval
              </span>
            );
          })()}
        </div>
        <button
          onClick={onClose}
          className="bg-neutral-800 hover:bg-neutral-700 text-neutral-400 p-1.5 rounded text-xs transition-colors border border-neutral-700"
          title="Close activity feed"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Filter pills */}
      <div className="px-6 pt-2 pb-1.5 border-b border-neutral-800 flex flex-col gap-1.5 shrink-0">
        <div className="flex items-center gap-1.5 flex-wrap">
          {FILTER_PILLS.map(pill => (
            <button
              key={pill.value}
              onClick={() => setActiveFilter(pill.value)}
              className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${
                activeFilter === pill.value
                  ? pill.activeClass
                  : 'bg-transparent text-neutral-500 border-neutral-700 hover:border-neutral-600 hover:text-neutral-400'
              }`}
            >
              {pill.label}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1.5 flex-wrap pb-0.5">
          {lastVisitTimestamp != null && (
            <button
              onClick={() => setTimeRange('last_visit')}
              className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${
                timeRange === 'last_visit'
                  ? 'bg-amber-500/20 text-amber-300 border-amber-500/50'
                  : 'bg-transparent text-neutral-500 border-neutral-700 hover:border-neutral-600 hover:text-neutral-400'
              }`}
            >
              Last Visit
            </button>
          )}
          {TIME_RANGE_PILLS.map(pill => (
            <button
              key={pill.value}
              onClick={() => setTimeRange(pill.value)}
              className={`text-xs px-2.5 py-1 rounded-full border transition-colors ${
                timeRange === pill.value
                  ? 'bg-neutral-700 text-neutral-200 border-neutral-500'
                  : 'bg-transparent text-neutral-500 border-neutral-700 hover:border-neutral-600 hover:text-neutral-400'
              }`}
            >
              {pill.label}
            </button>
          ))}
        </div>
      </div>

      {/* Summary bar */}
      {!loading && (() => {
        const todayUtc = new Date().toISOString().slice(0, 10);
        const counts = {
          TASK_COMPLETED: 0,
          TASK_FAILED: 0,
          RESEARCH_FINDINGS: 0,
          DEPLOY_EVENT: 0,
        };
        for (const e of events) {
          if (e.timestamp.slice(0, 10) === todayUtc && e.type in counts) {
            counts[e.type as keyof typeof counts]++;
          }
        }
        const pills: { label: string; color: string }[] = [];
        if (counts.TASK_COMPLETED > 0) pills.push({ label: `${counts.TASK_COMPLETED} completed`, color: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30' });
        if (counts.TASK_FAILED > 0)    pills.push({ label: `${counts.TASK_FAILED} failed`,    color: 'bg-red-500/20 text-red-400 border-red-500/30' });
        if (counts.RESEARCH_FINDINGS > 0) pills.push({ label: `${counts.RESEARCH_FINDINGS} research`, color: 'bg-blue-500/20 text-blue-400 border-blue-500/30' });
        if (counts.DEPLOY_EVENT > 0)   pills.push({ label: `${counts.DEPLOY_EVENT} deploy`,   color: 'bg-purple-500/20 text-purple-400 border-purple-500/30' });
        return (
          <div className="px-6 py-2 border-b border-neutral-800 flex items-center gap-2 flex-wrap shrink-0">
            {pills.length === 0 ? (
              <span className="text-xs text-neutral-600">No activity today</span>
            ) : (
              pills.map(({ label, color }) => (
                <span key={label} className={`border text-[10px] font-semibold px-2 py-0.5 rounded-full leading-none ${color}`}>
                  {label}
                </span>
              ))
            )}
          </div>
        );
      })()}

      {/* Error banner */}
      {error && (
        <div className="mx-6 mt-4 px-4 py-2 bg-red-950/50 text-red-400 text-xs rounded border border-red-900/50 shrink-0 flex items-center justify-between gap-3">
          <span>Failed to load activity: {error}</span>
          <button
            onClick={fetchEvents}
            className="text-xs text-blue-400 underline shrink-0 hover:text-blue-300 transition-colors"
          >
            Retry
          </button>
        </div>
      )}

      {/* Event list */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-2">
        {loading ? (
          Array.from({ length: 8 }).map((_, i) => <SkeletonRow key={i} />)
        ) : filteredEvents.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-neutral-500">
            <Clock className="w-8 h-8 mb-3 opacity-40" />
            <p className="text-sm">{activeFilter === 'ALL' ? 'No recent activity' : 'No matching events'}</p>
            <p className="text-xs mt-1 opacity-70">{activeFilter === 'ALL' ? 'Events will appear here as tasks run' : 'Try a different filter'}</p>
          </div>
        ) : (() => {
          const now = new Date();
          const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate());
          const startOfYesterday = new Date(startOfToday.getTime() - 86400000);
          const startOfWeek = new Date(startOfToday.getTime() - 6 * 86400000);

          const groups: { label: string; events: ActivityEvent[] }[] = [
            { label: 'Today', events: [] },
            { label: 'Yesterday', events: [] },
            { label: 'This Week', events: [] },
            { label: 'Older', events: [] },
          ];

          for (const event of filteredEvents) {
            const d = new Date(event.timestamp);
            if (d >= startOfToday) groups[0].events.push(event);
            else if (d >= startOfYesterday) groups[1].events.push(event);
            else if (d >= startOfWeek) groups[2].events.push(event);
            else groups[3].events.push(event);
          }

          // Determine if we need a last-visit divider and track whether it's been inserted
          const hasNewAndOld = lastVisitTimestamp != null &&
            filteredEvents.some(e => new Date(e.timestamp).getTime() > lastVisitTimestamp) &&
            filteredEvents.some(e => new Date(e.timestamp).getTime() <= lastVisitTimestamp);
          let dividerInserted = false;

          return groups.flatMap(({ label, events: groupEvents }) => {
            if (groupEvents.length === 0) return [];
            return [
              <div key={`header-${label}`} className="pt-2 pb-1 first:pt-0 sticky top-0 z-10 bg-neutral-950/90 backdrop-blur-sm">
                <span className="text-[10px] font-semibold uppercase text-neutral-600 tracking-wider">{label}</span>
              </div>,
              ...groupEvents.flatMap(event => {
              const isOld = lastVisitTimestamp != null && new Date(event.timestamp).getTime() <= lastVisitTimestamp;
              const showDivider = hasNewAndOld && isOld && !dividerInserted;
              if (showDivider) dividerInserted = true;
              return [
                ...(showDivider ? [
                  <div key="last-visit-divider" className="flex items-center gap-2 py-1">
                    <div className="flex-1 h-px bg-amber-500/30" />
                    <span className="text-[10px] font-semibold text-amber-500/80 uppercase tracking-wider whitespace-nowrap">New since your last visit</span>
                    <div className="flex-1 h-px bg-amber-500/30" />
                  </div>
                ] : []),
                <div
              key={event.id}
              onClick={() => handleRowClick(event)}
              className={`bg-neutral-900 border border-neutral-800 border-l-2 ${EVENT_BORDER[event.type]} rounded-lg px-4 py-3 ${event.task_id ? 'hover:bg-neutral-800/50 cursor-pointer' : ''} transition-colors`}
            >
              <div className="flex items-start gap-3">
                {EVENT_ICON[event.type]}

                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-sm text-neutral-100 truncate font-medium leading-tight">
                      {event.title}
                    </span>

                    {/* Finding count badge */}
                    {event.type === 'RESEARCH_FINDINGS' && event.finding_count != null && (
                      <span className="shrink-0 bg-blue-500/20 text-blue-400 border border-blue-500/30 text-[10px] font-semibold px-1.5 py-0.5 rounded-full leading-none">
                        {event.finding_count} finding{event.finding_count !== 1 ? 's' : ''}
                      </span>
                    )}

                    {/* Task type badge */}
                    {event.type === 'TASK_COMPLETED' && event.task_type && (
                      <span className="shrink-0 bg-neutral-800 text-neutral-400 text-[10px] px-1.5 py-0.5 rounded">
                        {event.task_type}
                      </span>
                    )}

                    {/* Deploy status badge */}
                    {event.type === 'DEPLOY_EVENT' && event.deploy_status && (
                      <span className={`shrink-0 border text-[10px] font-semibold px-1.5 py-0.5 rounded-full leading-none ${DEPLOY_STATUS_BADGE[event.deploy_status] ?? 'bg-neutral-700 text-neutral-300 border-neutral-600'}`}>
                        {event.deploy_status}
                      </span>
                    )}
                  </div>

                  {/* Error summary for TASK_FAILED */}
                  {event.type === 'TASK_FAILED' && event.error_summary && (
                    expandedErrors.has(event.id) ? (
                      <pre className="whitespace-pre-wrap text-xs text-red-400/80 mt-1 max-h-32 overflow-y-auto font-mono bg-neutral-950 rounded p-2">
                        {event.error_summary}
                      </pre>
                    ) : (
                      <p className="text-xs text-neutral-500 mt-0.5 truncate" title={event.error_summary}>
                        {event.error_summary}
                      </p>
                    )
                  )}

                  {/* Status message for TASK_IN_PROGRESS */}
                  {event.type === 'TASK_IN_PROGRESS' && event.latest_status_message && (
                    <p className="text-xs text-neutral-500 mt-0.5 truncate" title={event.latest_status_message}>
                      {event.latest_status_message}
                    </p>
                  )}
                </div>

                <div className="flex items-center gap-2 shrink-0 ml-auto">
                  {/* Expand toggle for TASK_FAILED */}
                  {event.type === 'TASK_FAILED' && event.error_summary && (
                    <button
                      onClick={e => {
                        e.stopPropagation();
                        setExpandedErrors(prev => {
                          const next = new Set(prev);
                          if (next.has(event.id)) next.delete(event.id);
                          else next.add(event.id);
                          return next;
                        });
                      }}
                      className="text-neutral-500 hover:text-neutral-300 transition-colors"
                      title={expandedErrors.has(event.id) ? 'Collapse error' : 'Expand error'}
                    >
                      {expandedErrors.has(event.id)
                        ? <ChevronDown className="w-3.5 h-3.5" />
                        : <ChevronRight className="w-3.5 h-3.5" />}
                    </button>
                  )}

                  {/* Commit hash badge */}
                  {event.type === 'TASK_COMPLETED' && event.commit_hash && (
                    event.repo_url && isSafeUrl(event.repo_url) ? (
                      <a
                        href={event.repo_url}
                        target="_blank"
                        rel="noopener noreferrer"
                        onClick={e => e.stopPropagation()}
                        className="font-mono text-[10px] bg-neutral-800 text-neutral-400 hover:text-neutral-200 px-1.5 py-0.5 rounded transition-colors"
                        title={event.commit_hash}
                      >
                        {event.commit_hash.slice(0, 7)}
                      </a>
                    ) : (
                      <span className="font-mono text-[10px] bg-neutral-800 text-neutral-400 px-1.5 py-0.5 rounded" title={event.commit_hash}>
                        {event.commit_hash.slice(0, 7)}
                      </span>
                    )
                  )}
                  {/* PR link */}
                  {event.pr_url && isSafeUrl(event.pr_url) && (
                    <a
                      href={event.pr_url}
                      target="_blank"
                      rel="noopener noreferrer"
                      onClick={e => e.stopPropagation()}
                      className="text-neutral-500 hover:text-neutral-300 transition-colors"
                      title="Open PR"
                    >
                      <ExternalLink className="w-3.5 h-3.5" />
                    </a>
                  )}

                  {/* Duration label for completed/failed tasks */}
                  {(event.type === 'TASK_COMPLETED' || event.type === 'TASK_FAILED') && event.duration_seconds != null && (
                    <span className="text-[10px] text-neutral-600 font-mono">ran {formatDuration(event.duration_seconds)}</span>
                  )}
                  {/* Relative timestamp */}
                  <span
                    className="text-xs text-neutral-500 font-mono whitespace-nowrap"
                    title={formatFullDatetime(event.timestamp)}
                  >
                    {formatRelativeTime(event.timestamp)}
                  </span>
                </div>
              </div>
            </div>
              ];
            })
            ];
          });
        })()
        }
      </div>

      {/* Loading indicator for background refresh */}
      {!loading && refreshInterval > 0 && (
        <div className="px-6 py-2 border-t border-neutral-800 flex items-center gap-1.5 text-[10px] text-neutral-600 shrink-0">
          <Loader2 className="w-3 h-3" />
          Auto-refreshes every {Math.round(refreshInterval / 1000)}s
        </div>
      )}
    </div>
  );
}
