/* eslint-disable react-hooks/set-state-in-effect */
import { useEffect, useRef, useState, useCallback, useMemo, type ReactElement } from 'react';
import { X, CheckCircle, XCircle, Microscope, Rocket, ExternalLink, Loader2, Clock, ChevronDown, ChevronRight } from 'lucide-react';
import { List, useDynamicRowHeight, type RowComponentProps } from 'react-window';
import { apiFetch, API_BASE_URL } from '../api';
import { timeAgo, formatDurationSec, parseTimestamp } from '../utils/time';
import { isSafeUrl, FAILURE_CLASS_CONFIG } from './TaskCard';
import { TAXONOMY_LABELS, TAXONOMY_COLORS } from './taxonomyConstants';
import { usePoller } from '../hooks/usePoller';

export type ActivityEventType = 'TASK_COMPLETED' | 'TASK_FAILED' | 'RESEARCH_FINDINGS' | 'DEPLOY_EVENT' | 'TASK_IN_PROGRESS';

export interface ActivityEvent {
  id: string;
  type: ActivityEventType;
  title: string;
  timestamp: string;
  task_id?: string;
  pr_url?: string;
  error_summary?: string;
  investigation_summary?: string | null;
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
  changed_files?: string[] | null;
  scope?: string | null;
  direction_id?: string | null;
  review_iterations?: number | null;
  failure_class?: string | null;
  taxonomy?: string | null;
  rollback_count?: number | null;
  rolled_back_from_sha?: string | null;
}

interface ActivityFeedProps {
  selectedProjectId: string | null;
  refreshInterval: number;
  onSelectTask: (task: { id: string }) => void;
  onClose: () => void;
  lastVisitTimestamp?: number | null;
  onEventsChange?: (events: ActivityEvent[]) => void;
  initialFilter?: string;
}

function formatFullDatetime(dateStr: string): string {
  return parseTimestamp(dateStr).toLocaleString();
}

const EVENT_ICON: Record<ActivityEventType, ReactElement> = {
  TASK_COMPLETED: <CheckCircle className="w-4 h-4 text-emerald-400 shrink-0" />,
  TASK_FAILED: <XCircle className="w-4 h-4 text-red-400 shrink-0" />,
  RESEARCH_FINDINGS: <Microscope className="w-4 h-4 text-blue-400 shrink-0" />,
  DEPLOY_EVENT: <Rocket className="w-4 h-4 text-purple-400 shrink-0" />,
  TASK_IN_PROGRESS: <Clock className="w-4 h-4 text-amber-400 shrink-0" />,
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

type FilterOption = ActivityEventType | 'ALL' | 'NEEDS_REVIEW';

const FILTER_PILLS: { label: string; value: FilterOption; activeClass: string }[] = [
  { label: 'All', value: 'ALL', activeClass: 'bg-neutral-600 text-neutral-100 border-neutral-500' },
  { label: 'In progress', value: 'TASK_IN_PROGRESS', activeClass: 'bg-amber-500/20 text-amber-400 border-amber-500/40' },
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

type FeedItem =
  | { kind: 'header'; label: string }
  | { kind: 'divider' }
  | { kind: 'row'; event: ActivityEvent };

interface FeedRowData {
  items: FeedItem[];
  onRowClick: (event: ActivityEvent) => void;
  expandedErrors: Set<string>;
  onToggleExpanded: (id: string) => void;
}

function FeedRow({
  index,
  style,
  items,
  onRowClick,
  expandedErrors,
  onToggleExpanded,
}: RowComponentProps<FeedRowData>) {
  const item = items[index];
  if (item.kind === 'header') {
    return (
      <div style={style}>
        <div className="px-6 pt-2 pb-1">
          <span className="text-ui-sm font-semibold uppercase text-neutral-600 tracking-wider">{item.label}</span>
        </div>
      </div>
    );
  }
  if (item.kind === 'divider') {
    return (
      <div style={style}>
        <div className="px-6 py-1 flex items-center gap-2">
          <div className="flex-1 h-px bg-amber-500/30" />
          <span className="text-ui-sm font-semibold text-amber-500/80 uppercase tracking-wider whitespace-nowrap">New since your last visit</span>
          <div className="flex-1 h-px bg-amber-500/30" />
        </div>
      </div>
    );
  }
  const { event } = item;
  const isExpanded = expandedErrors.has(event.id);
  return (
    <div style={style}>
      <div className="px-6 pb-2">
        <div
          onClick={() => onRowClick(event)}
          className={`bg-neutral-900 border border-neutral-800 border-l-2 ${EVENT_BORDER[event.type]} rounded-lg px-4 py-3 ${event.task_id ? 'hover:bg-neutral-800/50 cursor-pointer' : ''} transition-colors`}
        >
          <div className="flex items-start gap-3">
            {EVENT_ICON[event.type]}

            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm text-neutral-100 truncate font-medium leading-tight">
                  {event.title}
                </span>

                {event.type === 'RESEARCH_FINDINGS' && event.finding_count != null && (
                  <span className="shrink-0 bg-blue-500/20 text-blue-400 border border-blue-500/30 text-ui-sm font-semibold px-1.5 py-0.5 rounded-full leading-none">
                    {event.finding_count} finding{event.finding_count !== 1 ? 's' : ''}
                  </span>
                )}

                {event.type === 'TASK_COMPLETED' && event.task_type && (
                  <span className="shrink-0 bg-neutral-800 text-neutral-400 text-ui-sm px-1.5 py-0.5 rounded">
                    {event.task_type}
                  </span>
                )}

                {event.type === 'TASK_COMPLETED' && event.changed_files && event.changed_files.length > 0 && (
                  <span
                    className="shrink-0 bg-neutral-800 text-neutral-400 text-ui-sm px-1.5 py-0.5 rounded"
                    title={event.changed_files.join('\n')}
                  >
                    {event.changed_files.length} file{event.changed_files.length !== 1 ? 's' : ''}
                  </span>
                )}

                {event.type === 'TASK_COMPLETED' && event.scope && (
                  <span className="shrink-0 bg-indigo-500/20 text-indigo-300 border border-indigo-500/30 text-ui-sm font-semibold px-1.5 py-0.5 rounded-full leading-none">
                    {event.scope}
                  </span>
                )}

                {event.type === 'TASK_COMPLETED' && event.direction_id && (
                  <span
                    className="shrink-0 bg-neutral-800 text-neutral-500 text-ui-sm font-mono px-1.5 py-0.5 rounded"
                    title={`Direction ${event.direction_id}`}
                  >
                    dir:{event.direction_id.slice(0, 6)}
                  </span>
                )}

                {event.type === 'TASK_COMPLETED' && event.review_iterations != null && event.review_iterations > 0 && (
                  <span
                    className="shrink-0 bg-neutral-800 text-neutral-400 text-ui-sm px-1.5 py-0.5 rounded"
                    title="Review iterations"
                  >
                    {event.review_iterations} review{event.review_iterations !== 1 ? 's' : ''}
                  </span>
                )}

                {event.type === 'TASK_FAILED' && event.failure_class && FAILURE_CLASS_CONFIG[event.failure_class] && (
                  <span
                    className={`shrink-0 text-ui-xs uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded ${FAILURE_CLASS_CONFIG[event.failure_class].color}`}
                    title={FAILURE_CLASS_CONFIG[event.failure_class].hint}
                  >
                    {FAILURE_CLASS_CONFIG[event.failure_class].label}
                  </span>
                )}

                {event.type === 'TASK_FAILED' && event.taxonomy && TAXONOMY_LABELS[event.taxonomy] && (
                  <span
                    className={`shrink-0 text-ui-xs leading-none px-1 py-0.5 rounded border ${TAXONOMY_COLORS[event.taxonomy] ?? 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30'}`}
                    title={TAXONOMY_LABELS[event.taxonomy]}
                  >
                    {TAXONOMY_LABELS[event.taxonomy]}
                  </span>
                )}

                {event.type === 'DEPLOY_EVENT' && event.deploy_status && (
                  <span className={`shrink-0 border text-ui-sm font-semibold px-1.5 py-0.5 rounded-full leading-none ${DEPLOY_STATUS_BADGE[event.deploy_status] ?? 'bg-neutral-700 text-neutral-300 border-neutral-600'}`}>
                    {event.deploy_status}
                  </span>
                )}

                {event.type === 'DEPLOY_EVENT' && event.rolled_back_from_sha && (
                  <span className="shrink-0 bg-red-500/10 text-red-400 border border-red-500/30 text-ui-sm font-mono px-1.5 py-0.5 rounded" title={`Rolled back from ${event.rolled_back_from_sha}`}>
                    rolled back from {event.rolled_back_from_sha}
                  </span>
                )}
              </div>

              {event.type === 'TASK_FAILED' && event.error_summary && (
                isExpanded ? (
                  <pre className="whitespace-pre-wrap text-xs text-red-400/80 mt-1 max-h-32 overflow-y-auto font-mono bg-neutral-950 rounded p-2">
                    {event.error_summary}
                  </pre>
                ) : (
                  <p className="text-xs text-neutral-500 mt-0.5 truncate" title={event.error_summary}>
                    {event.error_summary}
                  </p>
                )
              )}

              {event.type === 'TASK_FAILED' && !event.error_summary && event.investigation_summary && (
                <p className="text-xs text-neutral-500 mt-0.5 truncate" title={event.investigation_summary}>
                  {event.investigation_summary}
                </p>
              )}
              {event.type === 'TASK_FAILED' && event.error_summary && event.investigation_summary && isExpanded && (
                <p className="text-xs text-neutral-400 mt-1 italic" title={event.investigation_summary}>
                  Investigation: {event.investigation_summary.slice(0, 200)}
                </p>
              )}

              {event.type === 'TASK_IN_PROGRESS' && event.latest_status_message && (
                <p className="text-xs text-neutral-500 mt-0.5 truncate" title={event.latest_status_message}>
                  {event.latest_status_message}
                </p>
              )}
            </div>

            <div className="flex items-center gap-2 shrink-0 ml-auto">
              {event.type === 'TASK_FAILED' && event.error_summary && (
                <button
                  onClick={e => {
                    e.stopPropagation();
                    onToggleExpanded(event.id);
                  }}
                  className="text-neutral-500 hover:text-neutral-300 transition-colors"
                  title={isExpanded ? 'Collapse error' : 'Expand error'}
                >
                  {isExpanded
                    ? <ChevronDown className="w-3.5 h-3.5" />
                    : <ChevronRight className="w-3.5 h-3.5" />}
                </button>
              )}

              {event.type === 'TASK_COMPLETED' && event.commit_hash && (
                event.repo_url && isSafeUrl(event.repo_url) ? (
                  // eslint-disable-next-line viewport/min-touch-target -- secondary inline link within an already-tappable row; enlarging would distort the dense per-event data layout
                  <a
                    href={event.repo_url}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={e => e.stopPropagation()}
                    className="font-mono text-ui-sm bg-neutral-800 text-neutral-400 hover:text-neutral-200 px-1.5 py-0.5 rounded transition-colors"
                    title={event.commit_hash}
                  >
                    {event.commit_hash.slice(0, 7)}
                  </a>
                ) : (
                  <span className="font-mono text-ui-sm bg-neutral-800 text-neutral-400 px-1.5 py-0.5 rounded" title={event.commit_hash}>
                    {event.commit_hash.slice(0, 7)}
                  </span>
                )
              )}
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

              {(event.type === 'TASK_COMPLETED' || event.type === 'TASK_FAILED') && event.duration_seconds != null && (
                <span className="text-ui-sm text-neutral-600 font-mono">ran {formatDurationSec(event.duration_seconds)}</span>
              )}
              <span
                className="text-xs text-neutral-500 font-mono whitespace-nowrap"
                title={formatFullDatetime(event.timestamp)}
              >
                {timeAgo(event.timestamp)}
              </span>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

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
  const [searchQuery, setSearchQuery] = useState('');
  // Track whether the user has explicitly selected a time range so we don't
  // override their choice when lastVisitTimestamp arrives asynchronously after auth.
  const userChangedRangeRef = useRef(false);

  useEffect(() => {
    if (lastVisitTimestamp != null && !userChangedRangeRef.current) {
      setTimeRange('last_visit');
    }
  }, [lastVisitTimestamp]);

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

  // Polling only (the leading fetch above owns the loading-spinner kickoff).
  // Coordinated on the shared visibility-aware clock; pauses when hidden.
  usePoller(fetchEvents, {
    intervalMs: refreshInterval,
    enabled: refreshInterval > 0,
    leading: false,
    deps: [fetchEvents, refreshInterval],
  });

  const filteredEvents = useMemo(() => events.filter(e => {
    const statusMatches = activeFilter === 'ALL' || e.type === activeFilter;
    if (!statusMatches) return false;

    if (!searchQuery) return true;

    const queryLower = searchQuery.toLowerCase();
    const titleMatches = e.title.toLowerCase().includes(queryLower);
    const summaryMatches = e.error_summary?.toLowerCase().includes(queryLower) ?? false;
    return titleMatches || summaryMatches;
  }), [events, activeFilter, searchQuery]);

  const handleRowClick = useCallback((event: ActivityEvent) => {
    if (!event.task_id) return;
    onSelectTask({ id: event.task_id });
  }, [onSelectTask]);

  const toggleExpanded = useCallback((id: string) => {
    setExpandedErrors(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const feedItems: FeedItem[] = useMemo(() => {
    if (filteredEvents.length === 0) return [];
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
      const d = parseTimestamp(event.timestamp);
      if (d >= startOfToday) groups[0].events.push(event);
      else if (d >= startOfYesterday) groups[1].events.push(event);
      else if (d >= startOfWeek) groups[2].events.push(event);
      else groups[3].events.push(event);
    }

    const hasNewAndOld = lastVisitTimestamp != null &&
      filteredEvents.some(e => parseTimestamp(e.timestamp).getTime() > lastVisitTimestamp) &&
      filteredEvents.some(e => parseTimestamp(e.timestamp).getTime() <= lastVisitTimestamp);
    let dividerInserted = false;

    const items: FeedItem[] = [];
    for (const { label, events: groupEvents } of groups) {
      if (groupEvents.length === 0) continue;
      items.push({ kind: 'header', label });
      for (const event of groupEvents) {
        const isOld = lastVisitTimestamp != null && parseTimestamp(event.timestamp).getTime() <= lastVisitTimestamp;
        if (hasNewAndOld && isOld && !dividerInserted) {
          items.push({ kind: 'divider' });
          dividerInserted = true;
        }
        items.push({ kind: 'row', event });
      }
    }
    return items;
  }, [filteredEvents, lastVisitTimestamp]);

  const rowHeight = useDynamicRowHeight({ defaultRowHeight: 68 });

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/40" onClick={onClose} />
      <div className="w-full max-w-[680px] bg-neutral-950 border-l border-neutral-800 flex flex-col shadow-2xl overflow-hidden">
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
          className="bg-neutral-800 hover:bg-neutral-700 text-neutral-400 rounded transition-colors border border-neutral-700 min-h-[44px] min-w-[44px] flex items-center justify-center"
          title="Close activity feed"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Filter pills */}
      <div className="px-6 pt-3 pb-1.5 border-b border-neutral-800 flex flex-col gap-2 shrink-0">
        <input
          type="text"
          placeholder="Search events by title or error..."
          value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          className="w-full bg-neutral-900 border border-neutral-700 rounded px-3 py-2 text-sm text-neutral-200 placeholder-neutral-600 focus:outline-none focus:border-neutral-500 focus:ring-1 focus:ring-neutral-500"
        />
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
              onClick={() => { userChangedRangeRef.current = true; setTimeRange('last_visit'); }}
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
              onClick={() => { userChangedRangeRef.current = true; setTimeRange(pill.value); }}
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
                <span key={label} className={`border text-ui-sm font-semibold px-2 py-0.5 rounded-full leading-none ${color}`}>
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
      {loading ? (
        <div className="flex-1 overflow-y-auto px-6 py-4 space-y-2">
          {Array.from({ length: 8 }).map((_, i) => <SkeletonRow key={i} />)}
        </div>
      ) : filteredEvents.length === 0 ? (
        <div className="flex-1 overflow-y-auto px-6 py-4 flex flex-col items-center justify-center text-neutral-500">
          <Clock className="w-8 h-8 mb-3 opacity-40" />
          <p className="text-sm">
            {searchQuery
              ? 'No events match your search'
              : activeFilter === 'ALL'
                ? 'No recent activity'
                : activeFilter === 'TASK_IN_PROGRESS'
                  ? 'No tasks in progress'
                  : 'No matching events'}
          </p>
          <p className="text-xs mt-1 opacity-70">
            {searchQuery
              ? 'Try different keywords'
              : activeFilter === 'ALL'
                ? 'Events will appear here as tasks run'
                : activeFilter === 'TASK_IN_PROGRESS'
                  ? 'Tasks in progress will appear here'
                  : 'Try a different filter'}
          </p>
        </div>
      ) : (
        <List
          rowCount={feedItems.length}
          rowComponent={FeedRow}
          rowProps={{
            items: feedItems,
            onRowClick: handleRowClick,
            expandedErrors,
            onToggleExpanded: toggleExpanded,
          }}
          rowHeight={rowHeight}
          overscanCount={6}
          className="flex-1 py-2"
        />
      )}

      {/* Loading indicator for background refresh */}
      {!loading && refreshInterval > 0 && (
        <div className="px-6 py-2 border-t border-neutral-800 flex items-center gap-1.5 text-ui-sm text-neutral-600 shrink-0">
          <Loader2 className="w-3 h-3" />
          Auto-refreshes every {Math.round(refreshInterval / 1000)}s
        </div>
      )}
      </div>
    </div>
  );
}
