import { useState, useEffect, useCallback, useRef } from 'react';
import {
  TrendingUp,
  TrendingDown,
  AlertTriangle,
  CheckCircle,
  Clock,
  ExternalLink,
  GitMerge,
  Minus,
} from 'lucide-react';
import { ResponsiveContainer, AreaChart, Area } from 'recharts';
import { apiFetch } from '../api';
import { timeAgo, formatDurationSec } from '../utils/time';
import { isSafeUrl } from './TaskCard';
import type { Task } from '../types/task';
import type { ProjectDiagnostics } from '../types/diagnostics';

// ---------- local types ----------

interface DirectionProgress {
  direction_id: string;
  direction_text: string;
  priority: string;
  cycles_in_window: number;
  tasks_created: number;
  prs_merged: number;
  possibly_saturated: boolean;
  product_ratio?: number | null;
  estimated_cost_usd?: number | null;
  cost_per_merged_pr?: number | null;
}

interface FailureGroupTask {
  task_id: string;
  title: string;
  error_snippet?: string;
  attempt_count?: number;
}

interface FailureGroup {
  failure_class: string;
  label: string;
  count: number;
  tasks: FailureGroupTask[];
}

interface RecentCompletion {
  task_id: string;
  title: string;
  completed_at: string | null;
  pr_url: string | null;
  task_type: string;
  direction_id?: string | null;
  direction_text?: string | null;
  duration_seconds?: number | null;
}

interface LatestCycle {
  cycle_id: string;
  started_at: string;
  completed_at: string | null;
  outcome: string;
  directions_created: number;
  directions_retired: number;
  vision_updated: boolean;
  summary_text?: string | null;
}

interface DigestData {
  failed_count: number;
  success_rate?: number | null;
  trend?: 'improving' | 'declining' | 'stable';
  trend_history?: { period_end: string; success_rate: number | null; completed: number; failed: number; failed_by_class?: Record<string, number>; period_cost_usd?: number | null }[];
  total_estimated_cost_usd?: number | null;
  direction_progress?: DirectionProgress[];
  pending_approval_count: number;
  overall_product_ratio?: number | null;
  failure_groups?: FailureGroup[];
  recent_completions?: RecentCompletion[];
  narrative_summary?: string | null;
  latest_cycle?: LatestCycle | null;
  completed_count?: number;
}

interface SparkPoint {
  active: number;
  queued: number;
  failed: number;
}

// ---------- props ----------

export interface DashboardHomeProps {
  tasks: Task[];
  selectedProjectId: string | null;
  onViewTask: (task: Task) => void;
}

// ---------- helpers ----------

const FAILURE_CLASS_COLORS: Record<string, string> = {
  CODE_ERROR: 'bg-red-500/20 text-red-400 border-red-500/30',
  PROVISION_FAILURE: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  AGENT_TIMEOUT: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  RATE_LIMIT: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  EXTERNAL_API: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
};

const FAILURE_CLASS_STROKE: Record<string, string> = {
  CODE_ERROR: '#f87171',
  PROVISION_FAILURE: '#fb923c',
  AGENT_TIMEOUT: '#fbbf24',
  RATE_LIMIT: '#facc15',
  EXTERNAL_API: '#c084fc',
};

const TASK_TYPE_COLORS: Record<string, string> = {
  create: 'bg-emerald-500/20 text-emerald-400',
  fix: 'bg-red-500/20 text-red-400',
  review: 'bg-amber-500/20 text-amber-400',
  research: 'bg-blue-500/20 text-blue-400',
  plan: 'bg-purple-500/20 text-purple-400',
  refactor: 'bg-cyan-500/20 text-cyan-400',
};

function mergeRate(merged: number, created: number): { pct: number; color: string } {
  if (created === 0) return { pct: 0, color: 'text-neutral-500' };
  const pct = Math.round((merged / created) * 100);
  if (pct >= 60) return { pct, color: 'text-emerald-400' };
  if (pct >= 30) return { pct, color: 'text-amber-400' };
  return { pct, color: 'text-red-400' };
}

function priorityBadge(p: string) {
  switch (p) {
    case 'high': return 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30';
    case 'medium': return 'bg-amber-500/20 text-amber-400 border-amber-500/30';
    default: return 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30';
  }
}

// ---------- failure class pill ----------

function FailureClassPill({ group, sparkData }: { group: FailureGroup; sparkData?: { v: number }[] }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const colorClass = FAILURE_CLASS_COLORS[group.failure_class] ?? 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30';
  const stroke = FAILURE_CLASS_STROKE[group.failure_class] ?? '#a3a3a3';
  const topTasks = group.tasks.slice(0, 3);
  const hasSpark = sparkData && sparkData.length >= 3;

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <button
        className={`inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-0.5 rounded border ${colorClass} cursor-pointer`}
        onClick={() => setOpen(v => !v)}
        title={`${group.label}: ${group.count} failure${group.count !== 1 ? 's' : ''}`}
      >
        {group.label}
        <span className="font-bold">{group.count}</span>
        {hasSpark && (
          <span className="w-16 h-3 inline-block">
            <ResponsiveContainer width="100%" height="100%">
              <AreaChart data={sparkData}>
                <defs>
                  <linearGradient id={`fsp-${group.failure_class}`} x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stopColor={stroke} stopOpacity={0.4} />
                    <stop offset="100%" stopColor={stroke} stopOpacity={0} />
                  </linearGradient>
                </defs>
                <Area type="monotone" dataKey="v" stroke={stroke} fill={`url(#fsp-${group.failure_class})`} strokeWidth={1} dot={false} isAnimationActive={false} />
              </AreaChart>
            </ResponsiveContainer>
          </span>
        )}
      </button>
      {open && topTasks.length > 0 && (
        <div className="absolute z-10 top-full mt-1 left-0 w-64 bg-neutral-900 border border-neutral-700 rounded-lg p-2 shadow-lg">
          <div className="space-y-1">
            {topTasks.map(t => (
              <div key={t.task_id} className="text-[10px] text-neutral-300 truncate">{t.title}</div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------- skeleton ----------

function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`animate-pulse bg-neutral-800/60 rounded ${className}`} />;
}

// ---------- component ----------

const WINDOW_OPTIONS = [
  { label: '6h', value: 6 },
  { label: '12h', value: 12 },
  { label: '24h', value: 24 },
  { label: '48h', value: 48 },
  { label: '7d', value: 168 },
];

export function DashboardHome({ tasks, selectedProjectId, onViewTask }: DashboardHomeProps) {
  const [digest, setDigest] = useState<DigestData | null>(null);
  const [diagnostics, setDiagnostics] = useState<ProjectDiagnostics | null>(null);
  const [loading, setLoading] = useState(true);
  const [showAllIssues, setShowAllIssues] = useState(false);
  const [windowHours, setWindowHours] = useState(24);
  const sparkRef = useRef<SparkPoint[]>([]);
  const [sparkData, setSparkData] = useState<SparkPoint[]>([]);

  // Fetch data
  const fetchData = useCallback(async () => {
    const fetches: Promise<void>[] = [];

    // Digest
    fetches.push(
      (async () => {
        try {
          const url = selectedProjectId
            ? `/api/admin/digest?hours=${windowHours}&project_id=${encodeURIComponent(selectedProjectId)}`
            : `/api/admin/digest?hours=${windowHours}`;
          const res = await apiFetch(url);
          if (res.ok) setDigest(await res.json());
        } catch { /* silent */ }
      })(),
    );

    // Diagnostics — only when a project is selected
    if (selectedProjectId) {
      fetches.push(
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/diagnostics`);
            if (res.ok) setDiagnostics(await res.json());
          } catch { /* silent */ }
        })(),
      );
    }

    await Promise.allSettled(fetches);
    setLoading(false);
  }, [selectedProjectId, windowHours]);

  // Initial + polling
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch sets state after await, intentional
    fetchData();
    const id = setInterval(fetchData, 30_000);
    return () => clearInterval(id);
  }, [fetchData]);

  // Sparkline sampling
  useEffect(() => {
    const active = tasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING').length;
    const queued = tasks.filter(t => t.status === 'QUEUED').length;
    const failed = digest?.failed_count ?? 0;
    const next = [...sparkRef.current, { active, queued, failed }].slice(-60);
    sparkRef.current = next;
    setSparkData(next);
  }, [tasks, digest?.failed_count]);

  // Derived counts
  const activeCount = tasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING').length;
  const queuedCount = tasks.filter(t => t.status === 'QUEUED').length;
  const failedCount = digest?.failed_count ?? 0;
  const successRate = digest?.success_rate;

  // Build issues list
  const issues = buildIssues(diagnostics, digest, tasks, onViewTask);
  const visibleIssues = showAllIssues ? issues : issues.slice(0, 3);

  // Recent completions with pr_url (from digest)
  const mergeEvents = (digest?.recent_completions ?? []).filter(e => e.pr_url);

  // Direction progress sorted by cycles
  const directions = [...(digest?.direction_progress ?? [])].sort((a, b) => b.cycles_in_window - a.cycles_in_window);
  const totalDirPRs = directions.reduce((s, d) => s + d.prs_merged, 0);
  const productPct = digest?.overall_product_ratio != null ? Math.round(digest.overall_product_ratio * 100) : null;

  return (
    <div className="flex-1 overflow-auto space-y-4">
      {/* Header row with time window selector */}
      <div className="flex items-center justify-end px-1">
        <select
          value={windowHours}
          onChange={e => setWindowHours(Number(e.target.value))}
          className="text-xs bg-neutral-800 text-neutral-300 border border-neutral-700 rounded px-2 py-1 focus:outline-none focus:border-neutral-500"
        >
          {WINDOW_OPTIONS.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </div>

      {/* System Status */}
      <SystemStatusCard digest={digest} loading={loading} />

      {/* Main grid: 2-col on large screens */}
      <div className="lg:grid lg:grid-cols-3 lg:gap-4 space-y-4 lg:space-y-0">
        <div className="lg:col-span-2 space-y-4">
          {/* Section 1: Task Status Overview */}
          <section>
            <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Task Status</h2>
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
              <MetricCard label="Active" value={activeCount} color="emerald" spark={sparkData} dataKey="active" loading={loading} />
              <MetricCard label="Queued" value={queuedCount} color="blue" spark={sparkData} dataKey="queued" loading={loading} />
              <MetricCard label="Completed" value={digest?.completed_count ?? 0} color="violet" spark={[]} dataKey="active" loading={loading} />
              <FailedCard value={failedCount} groups={digest?.failure_groups} spark={sparkData} loading={loading} />
              <SuccessRateCard rate={successRate} trend={digest?.trend} trendHistory={digest?.trend_history} loading={loading} />
              <CostCard totalCost={digest?.total_estimated_cost_usd} trendHistory={digest?.trend_history} loading={loading} />
            </div>
          </section>

          {/* Section 1b: Failure Breakdown by Class */}
          {!loading && digest?.failure_groups && digest.failure_groups.length > 0 && (
            <section>
              <div className="flex flex-wrap gap-1.5">
                {digest.failure_groups.map(g => {
                  const classSpark = (digest.trend_history ?? []).map(p => ({ v: p.failed_by_class?.[g.failure_class] ?? 0 }));
                  return <FailureClassPill key={g.failure_class} group={g} sparkData={classSpark} />;
                })}
              </div>
            </section>
          )}

          {/* Section 2: Direction Portfolio Health */}
          <section>
            <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Direction Portfolio</h2>
            {loading ? (
              <Skeleton className="h-28 w-full" />
            ) : directions.length === 0 ? (
              <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 text-neutral-500 text-xs">No active directions</div>
            ) : (
              <>
                {/* Summary row */}
                <div className="flex items-center gap-3 text-xs text-neutral-400 mb-2">
                  <span>{directions.length} direction{directions.length !== 1 ? 's' : ''}</span>
                  {productPct != null && (
                    <>
                      <span className="text-neutral-600">&middot;</span>
                      <span className="flex items-center gap-1">
                        {productPct}% product
                        <div className="w-16 h-1.5 rounded-full bg-neutral-800 overflow-hidden">
                          <div className="h-full rounded-full bg-emerald-500" style={{ width: `${productPct}%` }} />
                        </div>
                      </span>
                    </>
                  )}
                  <span className="text-neutral-600">&middot;</span>
                  <span>{totalDirPRs} PR{totalDirPRs !== 1 ? 's' : ''} merged</span>
                </div>

                {/* Scrollable direction cards */}
                <div className="flex gap-2 overflow-x-auto pb-1">
                  {directions.map(d => {
                    const mr = mergeRate(d.prs_merged, d.tasks_created);
                    return (
                      <div key={d.direction_id} className="min-w-[200px] max-w-[220px] bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 shrink-0">
                        <div className="flex items-center gap-1.5 mb-1.5">
                          <span className={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded border ${priorityBadge(d.priority)}`}>{d.priority}</span>
                          {d.possibly_saturated && <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 border border-amber-500/30">Saturated</span>}
                        </div>
                        <p className="text-xs text-neutral-200 line-clamp-2 mb-2">{d.direction_text}</p>
                        <div className="flex items-center gap-2 text-[10px] text-neutral-500">
                          <span className={mr.color}>{mr.pct}% merge</span>
                          <span>{d.cycles_in_window} cycle{d.cycles_in_window !== 1 ? 's' : ''}</span>
                          <span>{d.prs_merged} PR{d.prs_merged !== 1 ? 's' : ''}</span>
                          {d.product_ratio != null && (
                            <span className={d.product_ratio >= 0.6 ? 'text-emerald-400' : d.product_ratio >= 0.3 ? 'text-amber-400' : 'text-red-400'}>{Math.round(d.product_ratio * 100)}% prod</span>
                          )}
                          {d.estimated_cost_usd != null && (
                            <span>${d.estimated_cost_usd < 10 ? d.estimated_cost_usd.toFixed(2) : Math.round(d.estimated_cost_usd).toString()}</span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </section>

          {/* Section 3: Top Actionable Issues */}
          <section>
            <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Actionable Issues</h2>
            {loading ? (
              <div className="space-y-2"><Skeleton className="h-14 w-full" /><Skeleton className="h-14 w-full" /></div>
            ) : issues.length === 0 ? (
              <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 flex items-center gap-2 text-emerald-400 text-xs">
                <CheckCircle className="w-4 h-4" />
                All systems healthy
              </div>
            ) : (
              <div className="space-y-1.5">
                {visibleIssues.map((issue, i) => (
                  <IssueRow key={i} {...issue} />
                ))}
                {issues.length > 3 && (
                  <button
                    onClick={() => setShowAllIssues(v => !v)}
                    className="text-[10px] text-neutral-500 hover:text-neutral-300 transition-colors"
                  >
                    {showAllIssues ? 'Show less' : `Show ${issues.length - 3} more`}
                  </button>
                )}
              </div>
            )}
          </section>
        </div>

        {/* Section 4: Recent Merge Timeline */}
        <div className="lg:col-span-1">
          <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Recent Merges</h2>
          {loading ? (
            <div className="space-y-3"><Skeleton className="h-12 w-full" /><Skeleton className="h-12 w-full" /><Skeleton className="h-12 w-full" /></div>
          ) : mergeEvents.length === 0 ? (
            <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 text-neutral-500 text-xs">No recent merges</div>
          ) : (
            <div className="relative pl-4 space-y-0">
              {/* vertical line */}
              <div className="absolute left-1.5 top-1 bottom-1 w-px bg-neutral-800" />
              {mergeEvents.map(ev => {
                const dotColor = (ev.task_type === 'fix' || ev.task_type === 'review') ? 'bg-amber-500' : 'bg-emerald-500';
                const typeCls = TASK_TYPE_COLORS[ev.task_type ?? ''] ?? 'bg-neutral-500/20 text-neutral-400';
                const dirLabel = ev.direction_text ? ev.direction_text.slice(0, 30) + (ev.direction_text.length > 30 ? '…' : '') : null;
                return (
                  <div key={ev.task_id} className="relative pb-3">
                    <div className={`absolute -left-2.5 top-1.5 w-2 h-2 rounded-full ${dotColor} ring-2 ring-neutral-950`} />
                    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-2.5">
                      <div className="flex items-start gap-2">
                        <GitMerge className="w-3.5 h-3.5 text-neutral-500 mt-0.5 shrink-0" />
                        <div className="flex-1 min-w-0">
                          <p className="text-xs text-neutral-200 truncate">{ev.title}</p>
                          {dirLabel && <p className="text-[9px] text-neutral-500 truncate mt-0.5">{dirLabel}</p>}
                          <div className="flex items-center gap-2 mt-1 text-[10px] text-neutral-500">
                            {ev.task_type && <span className={`px-1.5 py-0.5 rounded text-[9px] font-medium ${typeCls}`}>{ev.task_type}</span>}
                            <span>{timeAgo(ev.completed_at ?? '')}</span>
                            {ev.duration_seconds != null && <span>{formatDurationSec(ev.duration_seconds)}</span>}
                          </div>
                        </div>
                        {ev.pr_url && isSafeUrl(ev.pr_url) && (
                          <a href={ev.pr_url} target="_blank" rel="noopener noreferrer" className="text-neutral-500 hover:text-neutral-300 shrink-0">
                            <ExternalLink className="w-3.5 h-3.5" />
                          </a>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// ---------- system status ----------

function SystemStatusCard({ digest, loading }: { digest: DigestData | null; loading: boolean }) {
  const cycle = digest?.latest_cycle ?? null;
  const hasContent = digest?.narrative_summary || cycle;

  return (
    <section>
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">System Status</h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4">
        {loading ? (
          <Skeleton className="h-16 w-full" />
        ) : !hasContent ? (
          <p className="text-neutral-500 text-xs">No recent activity</p>
        ) : (
          <>
            {digest?.narrative_summary && (
              <p className="text-sm text-neutral-300 leading-relaxed">{digest.narrative_summary}</p>
            )}
            {cycle && (
              <>
                <div className="text-[10px] text-neutral-500 mt-2 flex items-center gap-2">
                  {cycle.outcome === 'completed' ? (
                    <CheckCircle className="w-3 h-3 text-emerald-400 shrink-0" />
                  ) : (
                    <AlertTriangle className="w-3 h-3 text-red-400 shrink-0" />
                  )}
                  <span>
                    Last cycle: {timeAgo(cycle.completed_at ?? cycle.started_at)} &mdash;{' '}
                    {cycle.directions_created} created, {cycle.directions_retired} retired
                  </span>
                </div>
                {cycle.summary_text && (
                  <p className="text-[10px] text-neutral-500 mt-1">{cycle.summary_text}</p>
                )}
              </>
            )}
          </>
        )}
      </div>
    </section>
  );
}

// ---------- sub-components ----------

const SPARK_COLORS: Record<string, string> = { emerald: '#34d399', blue: '#60a5fa', red: '#f87171' };

function MetricCard({ label, value, color, spark, dataKey, loading }: {
  label: string; value: number; color: string; spark: SparkPoint[]; dataKey: keyof SparkPoint; loading: boolean;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const fill = SPARK_COLORS[color] ?? '#a3a3a3';
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4">
      <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">{label}</p>
      <p className={`text-2xl font-bold font-mono text-${color}-400`}>{value}</p>
      {spark.length >= 3 && (
        <div className="h-8 mt-1">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={spark}>
              <defs>
                <linearGradient id={`grad-${color}`} x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={fill} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={fill} stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area type="monotone" dataKey={dataKey} stroke={fill} fill={`url(#grad-${color})`} strokeWidth={1.5} dot={false} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

const FAILURE_CLASS_SHORT: Record<string, string> = {
  CODE_ERROR: 'code',
  PROVISION_FAILURE: 'provision',
  AGENT_TIMEOUT: 'timeout',
  RATE_LIMIT: 'rate',
  EXTERNAL_API: 'ext api',
};

function FailedCard({ value, groups, spark, loading }: {
  value: number; groups?: FailureGroup[]; spark: SparkPoint[]; loading: boolean;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const fill = SPARK_COLORS['red'];
  const topGroups = (groups ?? []).slice(0, 3);
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4">
      <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">Failed (24h)</p>
      <p className="text-2xl font-bold font-mono text-red-400">{value}</p>
      {spark.length >= 3 && (
        <div className="h-8 mt-1">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={spark}>
              <defs>
                <linearGradient id="grad-red" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={fill} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={fill} stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area type="monotone" dataKey="failed" stroke={fill} fill="url(#grad-red)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
      {value > 0 && topGroups.length > 0 && (
        <div className="flex flex-wrap gap-1 mt-1.5">
          {topGroups.map(g => {
            const cls = FAILURE_CLASS_COLORS[g.failure_class] ?? 'bg-neutral-500/20 text-neutral-400';
            const short = FAILURE_CLASS_SHORT[g.failure_class] ?? g.failure_class.toLowerCase();
            return (
              <span key={g.failure_class} className={`text-[9px] px-1.5 py-0.5 rounded font-medium ${cls}`}>
                {g.count} {short}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

function SuccessRateCard({ rate, trend, trendHistory, loading }: {
  rate?: number | null; trend?: string;
  trendHistory?: { success_rate: number | null; completed: number; failed: number }[];
  loading: boolean;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const TrendIcon = trend === 'improving' ? TrendingUp : trend === 'declining' ? TrendingDown : Minus;
  const trendColor = trend === 'improving' ? 'text-emerald-400' : trend === 'declining' ? 'text-red-400' : 'text-neutral-500';
  const sparkPoints = (trendHistory ?? []).map(p => ({ v: p.success_rate ?? 0 }));
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4">
      <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">Success Rate</p>
      <div className="flex items-center gap-2">
        <p className="text-2xl font-bold font-mono text-neutral-100">{rate != null ? `${Math.round(rate * 100)}%` : '--'}</p>
        <TrendIcon className={`w-4 h-4 ${trendColor}`} />
      </div>
      {sparkPoints.length >= 3 && (
        <div className="h-8 mt-1">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={sparkPoints}>
              <defs>
                <linearGradient id="grad-success" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor="#e5e5e5" stopOpacity={0.3} />
                  <stop offset="100%" stopColor="#e5e5e5" stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area type="monotone" dataKey="v" stroke="#e5e5e5" fill="url(#grad-success)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

function CostCard({ totalCost, trendHistory, loading }: {
  totalCost?: number | null;
  trendHistory?: { period_cost_usd?: number | null }[];
  loading: boolean;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const fill = SPARK_COLORS['blue'];
  const sparkPoints = (trendHistory ?? []).map(p => ({ v: p.period_cost_usd ?? 0 }));
  const display = totalCost == null ? '--' : totalCost < 10 ? `$${totalCost.toFixed(2)}` : `$${Math.round(totalCost)}`;
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4">
      <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">Cost (24h)</p>
      <p className="text-2xl font-bold font-mono text-blue-400">{display}</p>
      {sparkPoints.length >= 3 && (
        <div className="h-8 mt-1">
          <ResponsiveContainer width="100%" height="100%">
            <AreaChart data={sparkPoints}>
              <defs>
                <linearGradient id="grad-cost" x1="0" y1="0" x2="0" y2="1">
                  <stop offset="0%" stopColor={fill} stopOpacity={0.3} />
                  <stop offset="100%" stopColor={fill} stopOpacity={0} />
                </linearGradient>
              </defs>
              <Area type="monotone" dataKey="v" stroke={fill} fill="url(#grad-cost)" strokeWidth={1.5} dot={false} isAnimationActive={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

// ---------- issues ----------

interface IssueItem {
  severity: 'critical' | 'warning' | 'info';
  icon: typeof AlertTriangle;
  title: string;
  detail?: string;
  onClick?: () => void;
}

const SEVERITY_BORDER: Record<string, string> = { critical: 'border-l-red-500', warning: 'border-l-amber-500', info: 'border-l-blue-500' };
const SEVERITY_ICON_COLOR: Record<string, string> = { critical: 'text-red-400', warning: 'text-amber-400', info: 'text-blue-400' };

function IssueRow({ severity, icon: Icon, title, detail, onClick }: IssueItem) {
  const Tag = onClick ? 'button' : 'div';
  return (
    <Tag
      onClick={onClick}
      className={`w-full text-left bg-neutral-900/50 border border-neutral-800/50 border-l-2 ${SEVERITY_BORDER[severity]} rounded-lg p-3 flex items-start gap-2 ${onClick ? 'cursor-pointer hover:bg-neutral-800/50 transition-colors' : ''}`}
    >
      <Icon className={`w-4 h-4 mt-0.5 shrink-0 ${SEVERITY_ICON_COLOR[severity]}`} />
      <div className="min-w-0">
        <p className="text-xs text-neutral-200">{title}</p>
        {detail && <p className="text-[10px] text-neutral-500 mt-0.5 truncate">{detail}</p>}
      </div>
    </Tag>
  );
}

function buildIssues(
  diagnostics: ProjectDiagnostics | null,
  digest: DigestData | null,
  tasks: Task[],
  onViewTask: (task: Task) => void,
): IssueItem[] {
  const items: IssueItem[] = [];

  // Circuit breaker
  if (diagnostics?.circuit_breaker && diagnostics.circuit_breaker.state !== 'CLOSED') {
    items.push({
      severity: 'critical',
      icon: AlertTriangle,
      title: `Circuit breaker ${diagnostics.circuit_breaker.state} (${diagnostics.circuit_breaker.failure_count} failures)`,
    });
  }

  // Stuck tasks (top 3)
  const stuckSorted = [...(diagnostics?.stuck_tasks ?? [])].sort((a, b) => b.stuck_for_seconds - a.stuck_for_seconds);
  for (const st of stuckSorted.slice(0, 3)) {
    const matchedTask = tasks.find(t => t.id === st.task_id);
    items.push({
      severity: 'warning',
      icon: AlertTriangle,
      title: `Stuck: ${st.title}`,
      detail: st.remediation_hint ?? st.stuck_reason,
      onClick: matchedTask ? () => onViewTask(matchedTask) : undefined,
    });
  }

  // Pending approvals
  if (digest && digest.pending_approval_count > 0) {
    items.push({
      severity: 'info',
      icon: Clock,
      title: `${digest.pending_approval_count} task${digest.pending_approval_count !== 1 ? 's' : ''} awaiting approval`,
    });
  }

  // Zone exhaustion
  if (diagnostics?.zone_health?.exhausted && diagnostics.zone_health.exhausted.length > 0) {
    const cooldownRecovery = diagnostics.zone_health.cooldown_recovery ?? {};
    const now = Date.now();
    const zoneDetails = diagnostics.zone_health.exhausted.map(zone => {
      const expiresAt = cooldownRecovery[zone];
      if (expiresAt) {
        const minsLeft = Math.ceil((new Date(expiresAt).getTime() - now) / 60000);
        return minsLeft > 0 ? `${zone} — recovering in ${minsLeft}min` : zone;
      }
      return zone;
    });
    items.push({
      severity: 'warning',
      icon: AlertTriangle,
      title: `Zone${diagnostics.zone_health.exhausted.length !== 1 ? 's' : ''} exhausted: ${zoneDetails.join(', ')}`,
    });
  }

  return items;
}
