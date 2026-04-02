import { useEffect, useState, useCallback, type ReactElement } from 'react';
import { X, CheckCircle, XCircle, Microscope, Rocket, ExternalLink, Loader2, Clock } from 'lucide-react';
import { apiFetch, API_BASE_URL } from '../api';
import { isSafeUrl } from './TaskCard';
import type { Task } from './TaskCard';

export type ActivityEventType = 'TASK_COMPLETED' | 'TASK_FAILED' | 'RESEARCH_FINDINGS' | 'DEPLOY_EVENT';

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
}

interface ActivityFeedProps {
  selectedProjectId: string | null;
  refreshInterval: number;
  onSelectTask: (task: Task) => void;
  onClose: () => void;
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

const EVENT_ICON: Record<ActivityEventType, ReactElement> = {
  TASK_COMPLETED: <CheckCircle className="w-4 h-4 text-emerald-400 shrink-0" />,
  TASK_FAILED: <XCircle className="w-4 h-4 text-red-400 shrink-0" />,
  RESEARCH_FINDINGS: <Microscope className="w-4 h-4 text-blue-400 shrink-0" />,
  DEPLOY_EVENT: <Rocket className="w-4 h-4 text-purple-400 shrink-0" />,
};

const EVENT_BORDER: Record<ActivityEventType, string> = {
  TASK_COMPLETED: 'border-l-emerald-500',
  TASK_FAILED: 'border-l-red-500',
  RESEARCH_FINDINGS: 'border-l-blue-500',
  DEPLOY_EVENT: 'border-l-purple-500',
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

export default function ActivityFeed({ selectedProjectId, refreshInterval, onSelectTask, onClose }: ActivityFeedProps) {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const fetchEvents = useCallback(async () => {
    try {
      const params = new URLSearchParams();
      if (selectedProjectId) params.set('project_id', selectedProjectId);
      const res = await apiFetch(`${API_BASE_URL}/api/admin/activity?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      setEvents(Array.isArray(data) ? data : (data.events ?? []));
      setError(null);
    } catch (e) {
      setError(String(e));
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId]);

  useEffect(() => {
    setLoading(true);
    fetchEvents();
  }, [fetchEvents]);

  useEffect(() => {
    if (refreshInterval <= 0) return;
    const interval = setInterval(fetchEvents, refreshInterval);
    return () => clearInterval(interval);
  }, [fetchEvents, refreshInterval]);

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
            <span className="text-xs text-neutral-500 font-mono">{events.length} event{events.length !== 1 ? 's' : ''}</span>
          )}
        </div>
        <button
          onClick={onClose}
          className="bg-neutral-800 hover:bg-neutral-700 text-neutral-400 p-1.5 rounded text-xs transition-colors border border-neutral-700"
          title="Close activity feed"
        >
          <X className="w-4 h-4" />
        </button>
      </div>

      {/* Error banner */}
      {error && (
        <div className="mx-6 mt-4 px-4 py-2 bg-red-950/50 text-red-400 text-xs rounded border border-red-900/50 shrink-0">
          Failed to load activity: {error}
        </div>
      )}

      {/* Event list */}
      <div className="flex-1 overflow-y-auto px-6 py-4 space-y-2">
        {loading ? (
          Array.from({ length: 8 }).map((_, i) => <SkeletonRow key={i} />)
        ) : events.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-16 text-neutral-500">
            <Clock className="w-8 h-8 mb-3 opacity-40" />
            <p className="text-sm">No recent activity</p>
            <p className="text-xs mt-1 opacity-70">Events will appear here as tasks run</p>
          </div>
        ) : (
          events.map(event => (
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

                    {/* Deploy status badge */}
                    {event.type === 'DEPLOY_EVENT' && event.deploy_status && (
                      <span className={`shrink-0 border text-[10px] font-semibold px-1.5 py-0.5 rounded-full leading-none ${DEPLOY_STATUS_BADGE[event.deploy_status] ?? 'bg-neutral-700 text-neutral-300 border-neutral-600'}`}>
                        {event.deploy_status}
                      </span>
                    )}
                  </div>

                  {/* Error summary for TASK_FAILED */}
                  {event.type === 'TASK_FAILED' && event.error_summary && (
                    <p className="text-xs text-neutral-500 mt-0.5 truncate" title={event.error_summary}>
                      {event.error_summary}
                    </p>
                  )}
                </div>

                <div className="flex items-center gap-2 shrink-0 ml-auto">
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
          ))
        )}
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
