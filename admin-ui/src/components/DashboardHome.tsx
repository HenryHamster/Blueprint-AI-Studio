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
  ChevronRight,
} from 'lucide-react';
import { ResponsiveContainer, AreaChart, Area, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip } from 'recharts';
import { apiFetch } from '../api';
import { timeAgo, formatDurationSec } from '../utils/time';
import { isSafeUrl } from './TaskCard';
import { RecentErrorsWidget } from './RecentErrorsWidget';
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
  conversion_rate?: number | null;
  product_ratio?: number | null;
  estimated_cost_usd?: number | null;
  cost_per_merged_pr?: number | null;
  scope_breakdown?: Record<string, { created: number; merged: number; failed: number }> | null;
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
  daily_cost_usd?: number | null;
  daily_cost_ceiling_usd?: number | null;
  direction_progress?: DirectionProgress[];
  pending_approval_count: number;
  overall_product_ratio?: number | null;
  failure_groups?: FailureGroup[];
  recent_completions?: RecentCompletion[];
  narrative_summary?: string | null;
  latest_cycle?: LatestCycle | null;
  completed_count?: number;
  pipeline_conversion_rate?: number | null;
  scope_merge_rates?: Record<string, { created: number; merged: number }> | null;
  prev_scope_merge_rates?: Record<string, { created: number; merged: number }> | null;
}

interface OpenPR {
  pr_number: number;
  pr_url: string;
  pr_title: string;
  pr_branch: string;
  pr_created_at: string;
  task_id: string | null;
  task_status: string | null;
  task_type: string | null;
  task_title: string | null;
  is_orphaned: boolean;
}

interface SparkPoint {
  active: number;
  queued: number;
  failed: number;
}

interface WindowStats {
  merge_rate: number | null;
  created: number;
  merged: number;
  window_start: string | null;
  window_end: string | null;
}

interface WeeklySeries {
  week: string;
  created: number;
  merged: number;
  merge_rate: number | null;
  direction_merges: number;
  merge_rate_delta: number | null;
}

interface CorrelatedChange {
  title: string;
  week: string;
  merge_rate_delta: number | null;
}

interface MediumScopeMeasurement {
  medium_scope_merge_rate: number | null;
  total_created: number;
  total_merged: number;
  investment_start: string | null;
  pre_investment: WindowStats | null;
  post_investment: WindowStats | null;
  weekly_series: WeeklySeries[];
  correlated_changes: CorrelatedChange[];
}

// ---------- props ----------

export interface DashboardHomeProps {
  tasks: Task[];
  selectedProjectId: string | null;
  onViewTask: (task: Task) => void;
  onDirectionClick?: (directionId: string) => void;
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
            <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
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

export function DashboardHome({ tasks, selectedProjectId, onViewTask, onDirectionClick }: DashboardHomeProps) {
  const [digest, setDigest] = useState<DigestData | null>(null);
  const [diagnostics, setDiagnostics] = useState<ProjectDiagnostics | null>(null);
  const [openPRs, setOpenPRs] = useState<OpenPR[]>([]);
  const [measurementData, setMeasurementData] = useState<MediumScopeMeasurement | null>(null);
  const [loading, setLoading] = useState(true);
  const [lastFetchError, setLastFetchError] = useState(false);
  const [showAllIssues, setShowAllIssues] = useState(false);
  const [windowHours, setWindowHours] = useState(24);
  const sparkRef = useRef<SparkPoint[]>([]);
  const [sparkData, setSparkData] = useState<SparkPoint[]>([]);

  // Fetch data
  const fetchData = useCallback(async () => {
    let digestOk = false;
    const fetches: Promise<void>[] = [];

    // Digest
    fetches.push(
      (async () => {
        try {
          const url = selectedProjectId
            ? `/api/admin/digest?hours=${windowHours}&project_id=${encodeURIComponent(selectedProjectId)}`
            : `/api/admin/digest?hours=${windowHours}`;
          const res = await apiFetch(url);
          if (res.ok) { setDigest(await res.json()); digestOk = true; }
        } catch { /* silent */ }
      })(),
    );

    // Diagnostics + open PRs — only when a project is selected
    let diagnosticsOk = false;
    let openPRsOk = false;
    if (selectedProjectId) {
      fetches.push(
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/diagnostics`);
            if (res.ok) { setDiagnostics(await res.json()); diagnosticsOk = true; }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/open-prs?project_id=${encodeURIComponent(selectedProjectId)}`);
            if (res.ok) {
              const data = await res.json();
              setOpenPRs(data.open_prs ?? []);
              openPRsOk = true;
            }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/medium-scope-measurement?project_id=${encodeURIComponent(selectedProjectId)}&lookback_days=90`);
            if (res.ok) setMeasurementData(await res.json());
          } catch { /* silent */ }
        })(),
      );
    }

    await Promise.allSettled(fetches);
    setLoading(false);

    // Show error banner only when all primary fetches fail (backend unreachable)
    const allFailed = selectedProjectId
      ? !digestOk && !diagnosticsOk && !openPRsOk
      : !digestOk;
    setLastFetchError(allFailed);
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

  // Direction progress sorted by priority (high → medium → low)
  const priorityOrder: Record<string, number> = { high: 0, medium: 1, low: 2 };
  const directions = [...(digest?.direction_progress ?? [])].sort((a, b) => (priorityOrder[a.priority] ?? 9) - (priorityOrder[b.priority] ?? 9));
  const totalDirPRs = directions.reduce((s, d) => s + d.prs_merged, 0);
  const productPct = digest?.overall_product_ratio != null ? Math.round(digest.overall_product_ratio * 100) : null;

  return (
    <div className="flex-1 overflow-auto space-y-4">
      {/* Backend unreachable banner */}
      {lastFetchError && !loading && (
        <div className="flex items-center gap-2 px-3 py-2 rounded-lg bg-red-500/10 border border-red-500/20 text-red-400 text-xs">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
          <span>Backend unreachable — showing stale data</span>
        </div>
      )}
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

      {/* Orchestrator Errors widget: high-visibility observability for prod errors */}
      <RecentErrorsWidget />

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
              <ConversionRateCard rate={digest?.pipeline_conversion_rate} loading={loading} />
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
                <div className="flex gap-2 overflow-x-auto pb-1 scrollbar-hide" style={{ scrollbarWidth: 'none' }}>
                  {directions.map(d => {
                    const cvr = d.conversion_rate != null ? Math.round(d.conversion_rate * 100) : null;
                    const cvrColor = cvr == null ? 'text-neutral-500' : cvr >= 60 ? 'text-emerald-400' : cvr >= 30 ? 'text-amber-400' : 'text-red-400';
                    return (
                      <div
                        key={d.direction_id}
                        className="min-w-[200px] max-w-[220px] bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 shrink-0 cursor-pointer hover:border-neutral-700 transition-colors"
                        onClick={() => onDirectionClick?.(d.direction_id)}
                      >
                        <div className="flex items-center gap-1.5 mb-1.5">
                          <span className={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded border ${priorityBadge(d.priority)}`}>{d.priority}</span>
                          {d.possibly_saturated && <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 border border-amber-500/30">Saturated</span>}
                        </div>
                        <p className="text-xs text-neutral-200 line-clamp-2 mb-2">{d.direction_text}</p>
                        <div className="flex items-center gap-2 text-[10px] text-neutral-500">
                          {cvr != null && <span className={cvrColor}>{cvr}% merge</span>}
                          <span>{d.prs_merged} PR{d.prs_merged !== 1 ? 's' : ''}</span>
                          {d.product_ratio != null && (
                            <span className={d.product_ratio >= 0.6 ? 'text-emerald-400' : d.product_ratio >= 0.3 ? 'text-amber-400' : 'text-red-400'}>{Math.round(d.product_ratio * 100)}% prod</span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </section>

          {/* Section 2b: Scope Merge Rates */}
          {!!selectedProjectId && (
            <ScopeMergeRatesSection
              scopeRates={digest?.scope_merge_rates}
              prevScopeRates={digest?.prev_scope_merge_rates}
              loading={loading}
            />
          )}

          {/* Section 2c: Medium Scope Measurement */}
          {!!selectedProjectId && (
            <MediumScopeWidget data={measurementData} loading={loading} />
          )}

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

          {/* Section 5: PR Monitor */}
          <PRMonitorSection openPRs={openPRs} loading={loading} hasProject={!!selectedProjectId} />
        </div>
      </div>
    </div>
  );
}

// ---------- scope merge rates ----------

const SCOPE_ORDER = ['small', 'medium', 'large'];

function ScopeMergeRatesSection({
  scopeRates,
  prevScopeRates,
  loading,
}: {
  scopeRates: Record<string, { created: number; merged: number }> | null | undefined;
  prevScopeRates: Record<string, { created: number; merged: number }> | null | undefined;
  loading: boolean;
}) {
  if (loading) return null;
  if (!scopeRates) return null;
  const scopes = SCOPE_ORDER.filter(s => scopeRates[s] != null);
  if (scopes.length === 0) return null;

  return (
    <section>
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Scope Merge Rates</h2>
      <div className="flex gap-2 flex-wrap">
        {scopes.map(scope => {
          const { created, merged } = scopeRates[scope];
          const rate = created > 0 ? merged / created : null;
          const prev = prevScopeRates?.[scope];
          const prevRate = prev && prev.created > 0 ? prev.merged / prev.created : null;

          let trend: 'improving' | 'declining' | 'stable' | null = null;
          if (rate != null && prevRate != null) {
            const delta = rate - prevRate;
            if (delta > 0.05) trend = 'improving';
            else if (delta < -0.05) trend = 'declining';
            else trend = 'stable';
          }

          const TrendIcon = trend === 'improving' ? TrendingUp : trend === 'declining' ? TrendingDown : Minus;
          const trendColor = trend === 'improving' ? 'text-emerald-400' : trend === 'declining' ? 'text-red-400' : 'text-neutral-500';
          const rateColor = rate == null ? 'text-neutral-500' : rate >= 0.6 ? 'text-emerald-400' : rate >= 0.3 ? 'text-amber-400' : 'text-red-400';

          return (
            <div key={scope} className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 min-w-[110px]">
              <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">{scope}</p>
              <div className="flex items-center gap-1.5">
                <p className={`text-xl font-bold font-mono ${rateColor}`}>
                  {rate != null ? `${Math.round(rate * 100)}%` : '--'}
                </p>
                {trend && <TrendIcon className={`w-3.5 h-3.5 ${trendColor}`} />}
              </div>
              <p className="text-[10px] text-neutral-600 mt-0.5">{merged}/{created} merged</p>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ---------- PR monitor ----------

const PR_STATUS_COLORS: Record<string, string> = {
  QUEUED: 'bg-blue-500/20 text-blue-400',
  PROVISIONING: 'bg-blue-500/20 text-blue-400',
  IN_PROGRESS: 'bg-emerald-500/20 text-emerald-400',
  EVALUATING: 'bg-amber-500/20 text-amber-400',
  COMPLETED: 'bg-neutral-500/20 text-neutral-400',
  FAILED: 'bg-red-500/20 text-red-400',
  CANCELLED: 'bg-neutral-500/20 text-neutral-500',
};

function PRMonitorSection({ openPRs, loading, hasProject }: { openPRs: OpenPR[]; loading: boolean; hasProject: boolean }) {
  const [expanded, setExpanded] = useState(true);

  return (
    <section className="mt-4">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-1.5 mb-2 group"
      >
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest">Open PRs</h2>
        <span className="text-[10px] text-neutral-500 font-mono">{openPRs.length}</span>
        <ChevronRight className={`w-3 h-3 text-neutral-500 transition-transform ${expanded ? 'rotate-90' : ''}`} />
      </button>
      {expanded && (
        !hasProject ? (
          <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 text-neutral-500 text-xs">Select a project</div>
        ) : loading ? (
          <div className="space-y-2"><Skeleton className="h-10 w-full" /><Skeleton className="h-10 w-full" /></div>
        ) : openPRs.length === 0 ? (
          <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 text-neutral-500 text-xs">No open PRs</div>
        ) : (
          <div className="space-y-1.5 max-h-[400px] overflow-y-auto">
            {openPRs.map(pr => {
              const typeCls = TASK_TYPE_COLORS[pr.task_type ?? ''] ?? 'bg-neutral-500/20 text-neutral-400';
              const statusCls = PR_STATUS_COLORS[pr.task_status ?? ''] ?? 'bg-neutral-500/20 text-neutral-500';
              return (
                <div key={pr.pr_number} className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-2.5">
                  <div className="flex items-start gap-2">
                    <div className={`w-1.5 h-1.5 rounded-full mt-1.5 shrink-0 ${pr.is_orphaned ? 'bg-red-400' : 'bg-emerald-400'}`} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-1.5">
                        <span className="text-xs text-neutral-300 font-mono">#{pr.pr_number}</span>
                        <p className="text-xs text-neutral-200 truncate">{pr.pr_title}</p>
                      </div>
                      <div className="flex items-center gap-1.5 mt-1 flex-wrap">
                        {pr.task_type && (
                          <span className={`px-1.5 py-0.5 rounded text-[9px] font-medium ${typeCls}`}>{pr.task_type}</span>
                        )}
                        {pr.task_status && (
                          <span className={`px-1.5 py-0.5 rounded text-[9px] font-medium ${statusCls}`}>{pr.task_status}</span>
                        )}
                        {pr.is_orphaned && (
                          <span className="text-[9px] text-red-400 font-medium">No active task</span>
                        )}
                        <span className="text-[10px] text-neutral-500">{timeAgo(pr.pr_created_at)}</span>
                      </div>
                      <p className="text-[9px] text-neutral-600 font-mono truncate mt-0.5">{pr.pr_branch}</p>
                    </div>
                    {pr.pr_url && isSafeUrl(pr.pr_url) && (
                      <a href={pr.pr_url} target="_blank" rel="noopener noreferrer" className="text-neutral-500 hover:text-neutral-300 shrink-0">
                        <ExternalLink className="w-3.5 h-3.5" />
                      </a>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )
      )}
    </section>
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
          <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
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
          <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
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
          <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
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

function ConversionRateCard({ rate, loading }: {
  rate?: number | null;
  loading: boolean;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const pct = rate != null ? Math.round(rate * 100) : null;
  const color = pct == null ? 'text-neutral-500' : pct >= 60 ? 'text-emerald-400' : pct >= 30 ? 'text-amber-400' : 'text-red-400';
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4">
      <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">Conversion Rate</p>
      <p className={`text-2xl font-bold font-mono ${color}`}>{pct != null ? `${pct}%` : '--'}</p>
      <p className="text-[10px] text-neutral-600 mt-1">PRs merged / tasks created</p>
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
          <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
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

// ---------- medium scope widget ----------

const TOOLTIP_STYLE = {
  contentStyle: { background: '#171717', border: '1px solid #404040', borderRadius: 4 },
  itemStyle: { color: '#f5f5f5' },
  labelStyle: { color: '#a3a3a3' },
};

function MediumScopeWidget({ data, loading }: { data: MediumScopeMeasurement | null; loading: boolean }) {
  const rate = data?.medium_scope_merge_rate;
  const rateColor = rate == null ? 'text-neutral-500' : rate >= 0.6 ? 'text-emerald-400' : rate >= 0.3 ? 'text-amber-400' : 'text-red-400';

  return (
    <section>
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Medium Scope Merge Rate</h2>
      {loading ? (
        <Skeleton className="h-40 w-full" />
      ) : data == null || rate == null ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 text-neutral-500 text-xs">
          No medium-scope task data yet
        </div>
      ) : (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 space-y-4">
          {/* Headline rate */}
          <div className="flex items-center gap-4">
            <div>
              <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-0.5">Overall Rate</p>
              <p className={`text-3xl font-bold font-mono ${rateColor}`}>{Math.round(rate * 100)}%</p>
              <p className="text-[10px] text-neutral-500 mt-0.5">{data.total_merged} merged / {data.total_created} created</p>
            </div>
            {/* Pre/Post comparison */}
            {(data.pre_investment || data.post_investment) && (() => {
              const preRate = data.pre_investment?.merge_rate;
              const postRate = data.post_investment?.merge_rate;
              const deltaPp = preRate != null && postRate != null ? Math.round((postRate - preRate) * 100) : null;
              const deltaColor = deltaPp == null
                ? 'text-neutral-400'
                : Math.abs(deltaPp) <= 3 ? 'text-neutral-400' : deltaPp > 0 ? 'text-emerald-400' : 'text-red-400';
              return (
                <div className="flex gap-4 ml-4 items-center">
                  {data.pre_investment && (
                    <div className="text-center">
                      <p className="text-[10px] text-neutral-500 mb-0.5">Pre-investment</p>
                      <p className="text-lg font-bold font-mono text-neutral-300">
                        {preRate != null ? `${Math.round(preRate * 100)}%` : '--'}
                      </p>
                      <p className="text-[9px] text-neutral-600">{data.pre_investment.merged}/{data.pre_investment.created}</p>
                    </div>
                  )}
                  {data.pre_investment && data.post_investment && (
                    <div className="flex items-center text-neutral-600 text-xs">→</div>
                  )}
                  {data.post_investment && (
                    <div className="text-center">
                      <p className="text-[10px] text-neutral-500 mb-0.5">Post-investment</p>
                      <p className={`text-lg font-bold font-mono ${
                        postRate != null && preRate != null
                          ? postRate > preRate ? 'text-emerald-400' : 'text-red-400'
                          : 'text-neutral-300'
                      }`}>
                        {postRate != null ? `${Math.round(postRate * 100)}%` : '--'}
                      </p>
                      <p className="text-[9px] text-neutral-600">{data.post_investment.merged}/{data.post_investment.created}</p>
                    </div>
                  )}
                  {deltaPp != null && (
                    <div className="text-center">
                      <p className="text-[10px] text-neutral-500 mb-0.5">Delta</p>
                      <p className={`text-lg font-bold font-mono ${deltaColor}`}>
                        {deltaPp > 0 ? '+' : ''}{deltaPp}pp
                      </p>
                    </div>
                  )}
                </div>
              );
            })()}
          </div>
          {data.pre_investment?.merge_rate != null && data.post_investment?.merge_rate != null && (() => {
            const deltaPp = Math.round((data.post_investment.merge_rate - data.pre_investment.merge_rate) * 100);
            const within = Math.abs(deltaPp) <= 3;
            const color = within ? 'text-neutral-400' : deltaPp > 0 ? 'text-emerald-400' : 'text-red-400';
            const text = within
              ? `No significant change detected (within 3pp, ${deltaPp > 0 ? '+' : ''}${deltaPp}pp)`
              : deltaPp > 0
                ? `Medium-scope merge rate improved by ${deltaPp} percentage points since investment began`
                : `Medium-scope merge rate declined by ${Math.abs(deltaPp)} percentage points since investment began`;
            return <p className={`text-[11px] ${color}`}>{text}</p>;
          })()}

          {/* Weekly trend chart */}
          {data.weekly_series.length >= 2 && (
            <div className="h-32">
              <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
                <LineChart data={data.weekly_series} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#262626" />
                  <XAxis
                    dataKey="week"
                    tick={{ fill: '#737373', fontSize: 9 }}
                    tickFormatter={(v: string) => v.slice(5)}
                  />
                  <YAxis
                    tick={{ fill: '#737373', fontSize: 9 }}
                    tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
                    domain={[0, 1]}
                  />
                  <Tooltip
                    {...TOOLTIP_STYLE}
                    formatter={(v: unknown) => [typeof v === 'number' ? `${Math.round(v * 100)}%` : '--', 'Merge rate']}
                    labelFormatter={(label: unknown) => `Week of ${label}`}
                  />
                  <Line
                    type="monotone"
                    dataKey="merge_rate"
                    stroke="#34d399"
                    strokeWidth={1.5}
                    dot={false}
                    isAnimationActive={false}
                    connectNulls
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Correlated changes */}
          {data.correlated_changes.length > 0 && (
            <div>
              <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1.5">Correlated Changes</p>
              <div className="space-y-1">
                {data.correlated_changes.slice(0, 5).map((c, i) => {
                  const delta = c.merge_rate_delta;
                  const deltaColor = delta == null ? 'text-neutral-500' : delta > 0 ? 'text-emerald-400' : 'text-red-400';
                  return (
                    <div key={i} className="flex items-center gap-2 text-[10px]">
                      <span className="text-neutral-500 font-mono shrink-0">{c.week.slice(5)}</span>
                      <span className="text-neutral-300 truncate flex-1">{c.title}</span>
                      {delta != null && (
                        <span className={`font-mono shrink-0 ${deltaColor}`}>
                          {delta > 0 ? '+' : ''}{Math.round(delta * 100)}pp
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
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

  // Cost ceiling
  if (digest && (digest.daily_cost_ceiling_usd ?? 0) > 0) {
    const ceiling = digest.daily_cost_ceiling_usd!;
    const daily = digest.daily_cost_usd ?? 0;
    if (daily >= ceiling) {
      items.push({
        severity: 'warning',
        icon: AlertTriangle,
        title: `Daily cost ceiling exceeded ($${daily.toFixed(2)} / $${ceiling.toFixed(2)})`,
      });
    } else if (daily >= ceiling * 0.8) {
      items.push({
        severity: 'info',
        icon: AlertTriangle,
        title: `Daily cost approaching ceiling ($${daily.toFixed(2)} / $${ceiling.toFixed(2)})`,
      });
    }
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
