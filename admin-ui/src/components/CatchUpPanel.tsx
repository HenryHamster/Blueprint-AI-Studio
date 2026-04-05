import { useState, useEffect, useCallback, useRef } from 'react';
import { Clock, ChevronDown, ChevronUp, AlertTriangle, CheckCircle, XCircle, Loader2 } from 'lucide-react';
import { apiFetch } from '../api';
import { isSafeUrl, TaskTypeBadge, TASK_TYPE_STYLES } from './TaskCard';
import type { TaskType } from './TaskCard';

interface NeedsAttentionItem {
  task_id: string;
  title: string;
  reason: 'failed' | 'approval_required';
  error_snippet: string | null;
  pr_url: string | null;
  waiting_since?: string;
  waiting_hours?: number;
}

interface RecentFailure {
  task_id: string;
  title: string;
  failed_at: string | null;
  error_snippet: string | null;
  waiting_hours?: number;
}

interface RecentCompletion {
  task_id: string;
  title: string;
  completed_at: string | null;
  pr_url: string | null;
  task_type: string;
}

interface ActiveTask {
  task_id: string;
  title: string;
  status: string;
  task_type: string;
  started_at: string | null;
}

interface DigestData {
  window_hours: number;
  generated_at: string;
  completed_count: number;
  failed_count: number;
  pending_approval_count: number;
  research_cycles: number;
  prs_opened: number;
  active_count: number;
  active_tasks: ActiveTask[];
  needs_attention: NeedsAttentionItem[];
  recent_failures: RecentFailure[];
  recent_completions: RecentCompletion[];
}

export interface CatchUpPanelProps {
  selectedProjectId: string | null;
  lastVisitTimestamp: number | null;
  refreshInterval: number;
  onSelectTask: (task: { id: string }) => void;
  onOpenActivityFeed?: () => void;
}

type TimeWindow = 'last_visit' | '6h' | '24h' | '7d';

function windowToHours(window: TimeWindow, lastVisitTimestamp: number | null): number {
  switch (window) {
    case 'last_visit': {
      if (!lastVisitTimestamp) return 24;
      const elapsed = (Date.now() - lastVisitTimestamp) / (1000 * 60 * 60);
      return Math.max(1, Math.min(168, Math.ceil(elapsed)));
    }
    case '6h': return 6;
    case '24h': return 24;
    case '7d': return 168;
  }
}

function formatRelativeTime(isoString: string | null): string {
  if (!isoString) return '';
  const date = new Date(isoString);
  const diffMs = Date.now() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  if (diffMins < 1) return 'just now';
  if (diffMins < 60) return `${diffMins}m ago`;
  const diffHrs = Math.floor(diffMins / 60);
  if (diffHrs < 24) return `${diffHrs}h ago`;
  return `${Math.floor(diffHrs / 24)}d ago`;
}

function WaitingBadge({ hours }: { hours: number | undefined }) {
  if (hours === undefined || hours === null) return null;
  const label = hours < 24 ? `${hours}h` : `${Math.floor(hours / 24)}d`;
  const color = hours > 24
    ? 'bg-red-500/15 text-red-400 border-red-500/20'
    : hours >= 6
      ? 'bg-amber-500/15 text-amber-400 border-amber-500/20'
      : 'bg-neutral-700/30 text-neutral-500 border-neutral-700/30';
  return (
    <span className={`shrink-0 text-[9px] font-mono px-1.5 py-0.5 rounded border ${color}`}>
      {label}
    </span>
  );
}

function CompletionRow({ c, onSelectTask }: { c: RecentCompletion; onSelectTask: (task: { id: string }) => void }) {
  return (
    <div
      className="flex items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
      onClick={() => onSelectTask({ id: c.task_id })}
    >
      <span className="flex-1 truncate text-neutral-400">{c.title}</span>
      <div className="shrink-0 flex items-center gap-2">
        {c.pr_url && isSafeUrl(c.pr_url) && (
          <a
            href={c.pr_url}
            target="_blank"
            rel="noopener noreferrer"
            onClick={e => e.stopPropagation()}
            className="text-[9px] text-emerald-400 hover:text-emerald-300 font-medium"
          >
            PR
          </a>
        )}
        {c.completed_at && (
          <span className="text-[10px] text-neutral-600 font-mono">{formatRelativeTime(c.completed_at)}</span>
        )}
      </div>
    </div>
  );
}

function SkeletonRow() {
  return (
    <div className="flex items-center gap-2 px-2 py-1.5 rounded bg-neutral-950 border border-neutral-800/60 animate-pulse">
      <div className="h-2.5 bg-neutral-700/50 rounded flex-1" />
      <div className="h-2.5 bg-neutral-700/50 rounded w-12" />
    </div>
  );
}

export function CatchUpPanel({ selectedProjectId, lastVisitTimestamp, refreshInterval, onSelectTask, onOpenActivityFeed }: CatchUpPanelProps) {
  const [collapsed, setCollapsed] = useState(() =>
    localStorage.getItem('blueprint-admin:catchUpPanelCollapsed') === 'true'
  );
  const [activeWindow, setActiveWindow] = useState<TimeWindow>('last_visit');
  const [digest, setDigest] = useState<DigestData | null>(null);
  const [loading, setLoading] = useState(false);
  const [activeExpanded, setActiveExpanded] = useState(false);
  const [failuresExpanded, setFailuresExpanded] = useState(false);
  const [completionsExpanded, setCompletionsExpanded] = useState(false);
  const [expandedSnippets, setExpandedSnippets] = useState<Set<string>>(new Set());
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchDigest = useCallback(async (silent = false) => {
    if (!silent) setLoading(true);
    try {
      const hours = windowToHours(activeWindow, lastVisitTimestamp);
      const params = new URLSearchParams({ hours: String(hours) });
      if (selectedProjectId) params.set('project_id', selectedProjectId);
      const res = await apiFetch(`/api/admin/digest?${params}`);
      if (res.ok) {
        const data: DigestData = await res.json();
        setDigest(data);
      }
    } catch {
      // silently fail — digest is non-critical
    } finally {
      if (!silent) setLoading(false);
    }
  }, [activeWindow, lastVisitTimestamp, selectedProjectId]);

  // Fetch on mount and when window/project changes
  useEffect(() => {
    if (collapsed) return;
    fetchDigest();
  }, [fetchDigest, collapsed]);

  // Auto-refresh
  useEffect(() => {
    if (collapsed || refreshInterval === 0) return;
    intervalRef.current = setInterval(() => fetchDigest(true), refreshInterval);
    return () => { if (intervalRef.current) clearInterval(intervalRef.current); };
  }, [fetchDigest, collapsed, refreshInterval]);

  const toggle = () => {
    const next = !collapsed;
    setCollapsed(next);
    localStorage.setItem('blueprint-admin:catchUpPanelCollapsed', String(next));
  };

  const toggleSnippet = (id: string) => {
    setExpandedSnippets(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const hasActivity = digest && (
    digest.completed_count > 0 ||
    digest.failed_count > 0 ||
    digest.pending_approval_count > 0 ||
    digest.research_cycles > 0 ||
    digest.prs_opened > 0 ||
    digest.active_count > 0
  );

  // Failures not already in needs_attention
  const attentionIds = new Set(digest?.needs_attention.map(i => i.task_id) ?? []);
  const extraFailures = digest?.recent_failures.filter(f => !attentionIds.has(f.task_id)) ?? [];

  const windowLabels: { key: TimeWindow; label: string }[] = [
    { key: 'last_visit', label: lastVisitTimestamp ? 'Since last visit' : 'Since last visit (24h)' },
    { key: '6h', label: 'Last 6h' },
    { key: '24h', label: 'Last 24h' },
    { key: '7d', label: 'Last 7d' },
  ];

  return (
    <div className="mb-4 shrink-0">
      <div className="flex items-center gap-2">
        <button
          onClick={toggle}
          className="flex items-center gap-2 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors"
        >
          {collapsed ? <ChevronDown className="w-3 h-3" /> : <ChevronUp className="w-3 h-3" />}
          <Clock className="w-3 h-3" />
          Catch-up
        </button>
        {!collapsed && (
          <div className="flex items-center gap-1 ml-2">
            {windowLabels.map(({ key, label }) => (
              <button
                key={key}
                onClick={() => setActiveWindow(key)}
                className={`px-2 py-0.5 rounded-full text-[10px] font-medium transition-colors ${
                  activeWindow === key
                    ? 'bg-amber-500/20 text-amber-400 border border-amber-500/30'
                    : 'text-neutral-500 hover:text-neutral-300 border border-transparent'
                }`}
              >
                {label}
              </button>
            ))}
          </div>
        )}
      </div>

      {!collapsed && (
        <div className="mt-2 bg-neutral-900/50 border border-neutral-800/50 rounded-xl p-4">
          {loading ? (
            <div className="flex flex-col gap-2">
              <SkeletonRow />
              <SkeletonRow />
              <SkeletonRow />
            </div>
          ) : !hasActivity ? (
            <div className="flex items-center gap-2 text-xs text-neutral-600 py-1">
              <CheckCircle className="w-3.5 h-3.5" />
              All clear — nothing to catch up on
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              {/* Stats row */}
              <div className="flex flex-wrap gap-3 text-[11px] text-neutral-400">
                {digest!.completed_count > 0 && (
                  <span className="flex items-center gap-1">
                    <CheckCircle className="w-3 h-3 text-emerald-500" />
                    {digest!.completed_count} completed
                  </span>
                )}
                {digest!.failed_count > 0 && (
                  <span className="flex items-center gap-1">
                    <XCircle className="w-3 h-3 text-red-500" />
                    {digest!.failed_count} failed
                  </span>
                )}
                {digest!.research_cycles > 0 && (
                  <span>{digest!.research_cycles} research cycle{digest!.research_cycles !== 1 ? 's' : ''}</span>
                )}
                {digest!.prs_opened > 0 && (
                  <span>{digest!.prs_opened} PR{digest!.prs_opened !== 1 ? 's' : ''} opened</span>
                )}
                {digest!.active_count > 0 && (
                  <span className="flex items-center gap-1">
                    <Loader2 className="w-3 h-3 text-neutral-500 animate-spin" />
                    {digest!.active_count} running
                  </span>
                )}
              </div>

              {/* Needs attention */}
              {digest!.needs_attention.length > 0 && (
                <div className="border border-amber-500/30 rounded-lg p-3 flex flex-col gap-2">
                  <div className="flex items-center gap-1.5 text-[11px] font-bold text-amber-400 uppercase tracking-widest">
                    <AlertTriangle className="w-3 h-3" />
                    Needs attention ({digest!.needs_attention.length})
                  </div>
                  <div className="flex flex-col gap-1.5">
                    {digest!.needs_attention.map(item => (
                      <div key={item.task_id} className="flex flex-col gap-1">
                        <div
                          className="flex items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
                          onClick={() => onSelectTask({ id: item.task_id })}
                        >
                          <span className="flex-1 truncate text-neutral-300">{item.title}</span>
                          <span className={`shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded ${
                            item.reason === 'failed'
                              ? 'bg-red-500/10 text-red-400'
                              : 'bg-amber-500/10 text-amber-400'
                          }`}>
                            {item.reason === 'failed' ? 'Failed' : 'Approval required'}
                          </span>
                          <WaitingBadge hours={item.waiting_hours} />
                        </div>
                        {item.error_snippet && (
                          <div className="px-2">
                            <button
                              onClick={() => toggleSnippet(item.task_id)}
                              className="text-[10px] text-neutral-600 hover:text-neutral-400 transition-colors"
                            >
                              {expandedSnippets.has(item.task_id) ? 'hide error ▲' : 'show error ▼'}
                            </button>
                            {expandedSnippets.has(item.task_id) && (
                              <pre className="mt-1 text-[10px] text-red-400/80 bg-red-950/20 border border-red-900/30 rounded p-2 overflow-x-auto whitespace-pre-wrap break-all">
                                {item.error_snippet}
                              </pre>
                            )}
                          </div>
                        )}
                      </div>
                    ))}
                  </div>
                </div>
              )}

              {/* In progress */}
              {digest!.active_count > 0 && (
                <div>
                  <button
                    onClick={() => setActiveExpanded(v => !v)}
                    className="flex items-center gap-1.5 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors mb-1.5"
                  >
                    {activeExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                    <Loader2 className="w-3 h-3 text-neutral-500" />
                    In progress ({digest!.active_count})
                  </button>
                  {activeExpanded && (
                    <div className="flex flex-col gap-1.5">
                      {digest!.active_tasks.map(t => (
                        <div
                          key={t.task_id}
                          className="flex items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
                          onClick={() => onSelectTask({ id: t.task_id })}
                        >
                          <span className="flex-1 truncate text-neutral-400">{t.title}</span>
                          <span className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-500">
                            {t.status}
                          </span>
                          {t.started_at && (
                            <span className="shrink-0 text-[10px] text-neutral-600 font-mono">{formatRelativeTime(t.started_at)}</span>
                          )}
                        </div>
                      ))}
                      {digest!.active_count > digest!.active_tasks.length && (
                        <div className="text-[10px] text-neutral-600 px-2">
                          +{digest!.active_count - digest!.active_tasks.length} more
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              {/* Recent failures (not in needs_attention) */}
              {extraFailures.length > 0 && (
                <div>
                  <button
                    onClick={() => setFailuresExpanded(v => !v)}
                    className="flex items-center gap-1.5 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors mb-1.5"
                  >
                    {failuresExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                    <XCircle className="w-3 h-3 text-red-500" />
                    Recent failures ({extraFailures.length})
                  </button>
                  {failuresExpanded && (
                    <div className="flex flex-col gap-1.5">
                      {extraFailures.map(f => (
                        <div
                          key={f.task_id}
                          className="flex items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50"
                          onClick={() => onSelectTask({ id: f.task_id })}
                        >
                          <span className="flex-1 truncate text-neutral-400">{f.title}</span>
                          {f.failed_at && (
                            <span className="shrink-0 text-[10px] text-neutral-600 font-mono">{formatRelativeTime(f.failed_at)}</span>
                          )}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              )}

              {/* Recent completions */}
              {digest!.recent_completions.length > 0 && (() => {
                const completions = digest!.recent_completions;
                // Group by task_type, preserving insertion order
                const groups = new Map<string, RecentCompletion[]>();
                for (const c of completions) {
                  const key = c.task_type || 'other';
                  if (!groups.has(key)) groups.set(key, []);
                  groups.get(key)!.push(c);
                }
                const groupEntries = Array.from(groups.entries());
                const allSameType = groupEntries.length === 1;

                return (
                  <div>
                    <button
                      onClick={() => setCompletionsExpanded(v => !v)}
                      className="flex items-center gap-1.5 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors mb-1.5"
                    >
                      {completionsExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                      <CheckCircle className="w-3 h-3 text-emerald-500" />
                      Recent completions ({completions.length})
                    </button>
                    {completionsExpanded && (
                      <div className="flex flex-col gap-2">
                        {/* Type summary line */}
                        <div className="flex flex-wrap items-center gap-1.5">
                          {groupEntries.map(([type, items], idx) => {
                            const style = TASK_TYPE_STYLES[type];
                            return (
                              <span key={type} className="flex items-center gap-1 text-[10px]">
                                {idx > 0 && <span className="text-neutral-700">·</span>}
                                <span className={`${style?.bg ?? 'bg-neutral-500/10 border-neutral-500/20'} ${style?.text ?? 'text-neutral-400'} border px-1.5 py-0.5 rounded font-semibold uppercase tracking-wider`}>
                                  {items.length} {style?.label ?? type}
                                </span>
                              </span>
                            );
                          })}
                        </div>
                        {/* Grouped completion rows */}
                        {allSameType ? (
                          <div className="flex flex-col gap-1.5">
                            {completions.map(c => (
                              <CompletionRow key={c.task_id} c={c} onSelectTask={onSelectTask} />
                            ))}
                          </div>
                        ) : (
                          groupEntries.map(([type, items]) => (
                            <div key={type} className="flex flex-col gap-1.5">
                              <div className="flex items-center gap-1.5">
                                <TaskTypeBadge taskType={type as TaskType} />
                              </div>
                              {items.map(c => (
                                <CompletionRow key={c.task_id} c={c} onSelectTask={onSelectTask} />
                              ))}
                            </div>
                          ))
                        )}
                      </div>
                    )}
                  </div>
                );
              })()}
              {onOpenActivityFeed && (
                <button
                  onClick={onOpenActivityFeed}
                  className="text-[10px] text-neutral-500 hover:text-neutral-300 transition-colors mt-1 self-end"
                >
                  View full activity ↗
                </button>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
