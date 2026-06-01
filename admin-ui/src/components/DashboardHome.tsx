import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import {
  AlertTriangle,
  CheckCircle,
  ExternalLink,
  GitMerge,
} from 'lucide-react';
import { ResponsiveContainer, AreaChart, Area } from 'recharts';
import { apiFetch, fetchTask, type PromptFingerprintAnalyticsResponse, type VersionLifecycleResponse, type SrReviewerDisagreementsEnvelope } from '../api';
import { VersionLifecyclePanel } from './VersionLifecyclePanel';
import { useEfficiencySnapshot, fmtEffSecs, fmtEffRate, fmtEffIter } from '../hooks/useEfficiencySnapshot';
import { timeAgo, formatDurationSec } from '../utils/time';
import { isSafeUrl } from './TaskCard';
import { RecentErrorsWidget } from './RecentErrorsWidget';
import { DirectionAutoPauseWidget } from './DirectionAutoPauseWidget';
import { PRMonitorSection, type OpenPR } from './PRMonitorSection';
import { MediumScopeWidget, type MediumScopeMeasurement } from './MediumScopeWidget';
import { RemediationFunnelSection } from './RemediationFunnelSection';
import { FailureStageWidget } from './FailureStageWidget';
import { TaskDurationWidget, type TaskDurationStats } from './TaskDurationWidget';
import { FixLoopConvergenceWidget } from './FixLoopConvergenceWidget';
import { ScopeConversionRatesSection } from './ScopeConversionRatesSection';
import { ScopeTrendsSection, type ScopeBaselinesResponse } from './ScopeTrendsSection';
import { ProviderCostPanel } from './ProviderCostPanel';
import { ProviderModelBreakdownPanel } from './ProviderModelBreakdownPanel';
import { LlmCostBreakdownPanel } from './LlmCostBreakdownPanel';
import { DriftSignalsCard } from './DriftSignalsCard';
import { PoolAccountHealthPanel, type AccountHealthEntry } from './PoolAccountHealthPanel';
import { SystemMaturityCard, type SystemMaturityRow } from './SystemMaturityCard';
import { DirectionPortfolioSection, type DirectionProgress } from './DirectionPortfolioSection';
import { type SrMissedTask, type DirectionSrEntry } from './SrTrendBreakdown';
import { ReviewQualitySection, type SrFixIterationBucket, type SrTrendWithFixIterations } from './ReviewQualitySection';
import { SrDiagnosticsStack, type BlindSpotsData, type DirectionSrBreakdownData } from './SrDiagnosticsStack';
import {
  Skeleton,
  FAILURE_CLASS_COLORS,
  MetricCard,
  FailedCard,
  SuccessRateCard,
  ConversionRateCard,
  AcceptanceRateCard,
  CostCard,
  SrCatchRateCard,
} from './DashboardMetricCards';
import { RecurringPatternsSection } from './RecurringPatternsSection';
import { TAXONOMY_COLORS, TAXONOMY_LABELS, TAXONOMY_ORDER } from './dashboardConstants';
import { ViewportDebtPanel } from './ViewportDebtPanel';
import { SystemStatusCard } from './SystemStatusCard';
import type { Task } from '../types/task';
import type { Project } from '../types/project';
import { assertDigestContract, type DigestData as DigestDataBase, type FailureGroup } from '../types/digest';

// Dashboard reads DirectionPortfolioSection's `DirectionProgress` shape off
// `direction_progress`, so it instantiates the generic canonical type with it.
// Re-exported (along with FailureGroup) for dependents — SystemStatusCard and
// DashboardMetricCards — that import these from this module.
export type DigestData = DigestDataBase<DirectionProgress>;
export type { FailureGroup };

// ---------- local types ----------

interface HealthOverviewIssue {
  severity: 'critical' | 'warning' | 'info';
  title: string;
  hint: string;
  task_ids: string[];
  category: string;
}

interface HealthOverviewResponse {
  overall_status: 'healthy' | 'degraded' | 'unhealthy';
  success_rate: { current: number | null; trend: string | null; window_hours: number };
  queue_depth: { queued: number; active: number; evaluating: number };
  actionable_issues: HealthOverviewIssue[];
}

const TIMEOUT_SUBTYPE_LABELS: Record<string, string> = {
  heartbeat_lost: 'heartbeat lost',
  oom: 'OOM',
  execution_timeout: 'exec timeout',
  cascade_cancellation: 'cascade',
  iteration_limit: 'iter limit',
  process_crash: 'crash',
  unknown: 'unknown',
};

// SrFixIterationBucket and SrTrendWithFixIterations are defined and exported from ReviewQualitySection
export type { SrFixIterationBucket, SrTrendWithFixIterations };

// DirectionSrEntry is re-exported from SrTrendBreakdown (canonical model)
export type { DirectionSrEntry };

const TAXONOMY_STROKE: Record<string, string> = {
  scope_related: '#22d3ee',
  review_quality: '#a855f7',
  infra_related: '#f59e0b',
  code_defect: '#ef4444',
};

const REVIEW_CAT_ORDER = ['lint', 'type', 'logic', 'scope', 'architecture', 'other'] as const;
const REVIEW_CAT_COLORS: Record<string, string> = {
  lint: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  type: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  logic: 'bg-red-500/20 text-red-400 border-red-500/30',
  scope: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  architecture: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  other: 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30',
};

export interface SparkPoint {
  active: number;
  queued: number;
  failed: number;
}

// ---------- Pipeline Efficiency Panel ----------

function EffDelta({ delta, lowerIsBetter }: { delta: number | null | undefined; lowerIsBetter: boolean }) {
  if (delta === null || delta === undefined || delta === 0) return null;
  const improving = lowerIsBetter ? delta < 0 : delta > 0;
  return <span className={`ml-0.5 ${improving ? 'text-emerald-400' : 'text-red-400'}`}>{delta > 0 ? '↑' : '↓'}</span>;
}

function PipelineEfficiencyPanel({ projectId }: { projectId: string | null }) {
  const data = useEfficiencySnapshot(projectId);
  if (!data) return null;

  const w24 = data.window_24h;
  const w7 = data.window_7d;

  return (
    <section>
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Pipeline Efficiency</h2>
      <div className="bg-neutral-900 border border-neutral-800 rounded-lg p-3 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-neutral-400">
        <span>
          <span className="text-neutral-500">cold-start p50</span>{': '}
          <span className="font-mono text-neutral-300">{fmtEffSecs(w24?.setup_p50_secs)}</span>
          <EffDelta delta={w24?.vs_prior_setup_p50_delta} lowerIsBetter />
        </span>
        <span>
          <span className="text-neutral-500">reuse rate</span>{': '}
          <span className="font-mono text-neutral-300">{fmtEffRate(w24?.warm_reuse_rate)}</span>
          <EffDelta delta={w24?.vs_prior_warm_reuse_rate_delta} lowerIsBetter={false} />
        </span>
        <span>
          <span className="text-neutral-500">review cycles p50</span>{': '}
          <span className="font-mono text-neutral-300">{fmtEffIter(w7?.review_iter_median)}</span>
          <EffDelta delta={w7?.vs_prior_review_iter_median_delta} lowerIsBetter />
        </span>
      </div>
    </section>
  );
}

// ---------- props ----------

export interface DashboardHomeProps {
  tasks: Task[];
  selectedProjectId: string | null;
  projects?: Project[];
  onViewTask: (task: Task) => void;
  onDirectionClick?: (directionId: string) => void;
  onCategoryClick?: (category: string) => void;
  onScopeClick?: (scope: string) => void;
  onConfigureTargets?: () => void;
  onSpawnOrphanFix?: (pr: OpenPR) => void;
}

// ---------- helpers ----------

const FAILURE_CLASS_STROKE: Record<string, string> = {
  CODE_ERROR: '#f87171',
  PROVISION_FAILURE: '#fb923c',
  AGENT_TIMEOUT: '#fbbf24',
  RATE_LIMIT: '#facc15',
  EXTERNAL_API: '#c084fc',
  SCOPE_OVERFLOW: '#2dd4bf',
};

const TASK_TYPE_COLORS: Record<string, string> = {
  create: 'bg-emerald-500/20 text-emerald-400',
  fix: 'bg-red-500/20 text-red-400',
  review: 'bg-amber-500/20 text-amber-400',
  research: 'bg-blue-500/20 text-blue-400',
  plan: 'bg-purple-500/20 text-purple-400',
  refactor: 'bg-cyan-500/20 text-cyan-400',
};


// ---------- failure class pill ----------

function FailureClassPill({ group, sparkData, cost }: { group: FailureGroup; sparkData?: { v: number }[]; cost?: number }) {
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
        className={`inline-flex items-center justify-center gap-1.5 text-[10px] font-semibold px-3 py-2 rounded border min-h-11 ${colorClass} cursor-pointer`}
        onClick={() => setOpen(v => !v)}
        title={`${group.label}: ${group.count} failure${group.count !== 1 ? 's' : ''}`}
      >
        {group.label}
        <span className="font-bold">{group.count}</span>
        {cost != null && cost > 0 && (
          <span className="font-mono text-[9px] opacity-80">${cost.toFixed(2)}</span>
        )}
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
              <div key={t.task_id} className="text-[10px] text-neutral-300 flex items-center gap-1 min-w-0">
                {t.taxonomy && TAXONOMY_LABELS[t.taxonomy] && (
                  <span className={`shrink-0 text-[9px] leading-none px-1 py-0.5 rounded border ${TAXONOMY_COLORS[t.taxonomy] ?? 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30'}`} title={TAXONOMY_LABELS[t.taxonomy]}>
                    {TAXONOMY_LABELS[t.taxonomy]}
                  </span>
                )}
                <span className="truncate">{t.title}</span>
                {group.failure_class === 'AGENT_TIMEOUT' && t.timeout_subtype && (
                  <span
                    className="shrink-0 text-[9px] font-mono px-1 py-0.5 rounded bg-amber-500/10 text-amber-400/80 border border-amber-500/20"
                    title={`Timeout subtype: ${t.timeout_subtype}`}
                  >
                    {TIMEOUT_SUBTYPE_LABELS[t.timeout_subtype] ?? t.timeout_subtype}
                  </span>
                )}
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------- cost breakdown pill ----------

// ---------- system health card ----------

const _HEALTH_SEV_BORDER: Record<string, string> = { critical: 'border-l-red-500', warning: 'border-l-amber-500', info: 'border-l-blue-500' };
const _HEALTH_SEV_ICON: Record<string, string> = { critical: 'text-red-400', warning: 'text-amber-400', info: 'text-blue-400' };
const _HEALTH_STATUS_STYLE: Record<string, { badge: string; label: string }> = {
  healthy: { badge: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400', label: 'Healthy' },
  degraded: { badge: 'bg-amber-500/10 border-amber-500/30 text-amber-400', label: 'Degraded' },
  unhealthy: { badge: 'bg-red-500/10 border-red-500/30 text-red-400', label: 'Unhealthy' },
};

function SystemHealthCard({
  data,
  loading,
  tasks,
  onViewTask,
}: {
  data: HealthOverviewResponse | null;
  loading: boolean;
  tasks: Task[];
  onViewTask: (task: Task) => void;
}) {
  const status = data?.overall_status ?? 'healthy';
  const statusStyle = _HEALTH_STATUS_STYLE[status] ?? _HEALTH_STATUS_STYLE.healthy;
  const sr = data?.success_rate;
  const qd = data?.queue_depth;
  const issues = data?.actionable_issues ?? [];
  const trendColor = sr?.trend === 'improving' ? 'text-emerald-400' : sr?.trend === 'declining' ? 'text-red-400' : 'text-neutral-400';
  const trendIcon = sr?.trend === 'improving' ? '↑' : sr?.trend === 'declining' ? '↓' : '→';

  return (
    <section>
      <div className="flex items-center gap-2 mb-2">
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest">System Health</h2>
        {!loading && data && (
          <span className={`text-[10px] font-semibold px-2 py-0.5 rounded border ${statusStyle.badge}`}>
            {statusStyle.label}
          </span>
        )}
      </div>
      {loading ? (
        <div className="space-y-2"><Skeleton className="h-10 w-full" /><Skeleton className="h-14 w-full" /></div>
      ) : (
        <>
          {data && (sr?.current != null || qd) && (
            <div className="flex flex-wrap gap-2 mb-2">
              {sr?.current != null && (
                <div className="bg-neutral-900/50 border border-neutral-800/50 rounded px-2.5 py-1.5 text-xs flex items-center gap-1.5">
                  <span className="text-neutral-500">Success</span>
                  <span className="font-mono font-bold text-neutral-200">{Math.round(sr.current * 100)}%</span>
                  {sr.trend && <span className={`text-[10px] ${trendColor}`}>{trendIcon}</span>}
                </div>
              )}
              {qd && (
                <div className="bg-neutral-900/50 border border-neutral-800/50 rounded px-2.5 py-1.5 text-xs flex items-center gap-2">
                  <span className="text-neutral-500">Queue</span>
                  <span className="font-mono text-neutral-300" title="queued">{qd.queued}q</span>
                  <span className="font-mono text-neutral-300" title="active">{qd.active}a</span>
                  <span className="font-mono text-neutral-300" title="evaluating">{qd.evaluating}e</span>
                </div>
              )}
            </div>
          )}
          {issues.length === 0 ? (
            <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 flex items-center gap-2 text-emerald-400 text-xs">
              <CheckCircle className="w-4 h-4" />
              All systems healthy
            </div>
          ) : (
            <div className="space-y-1.5">
              {issues.map((issue, i) => {
                const borderCls = _HEALTH_SEV_BORDER[issue.severity] ?? 'border-l-neutral-500';
                const iconCls = _HEALTH_SEV_ICON[issue.severity] ?? 'text-neutral-400';
                const linkedTasks = issue.task_ids.slice(0, 3)
                  .map(id => tasks.find(t => t.id === id))
                  .filter((t): t is Task => t != null);
                return (
                  <div key={i} className={`bg-neutral-900/50 border border-neutral-800/50 border-l-2 ${borderCls} rounded-lg p-3`}>
                    <div className="flex items-start gap-2">
                      <AlertTriangle className={`w-4 h-4 mt-0.5 shrink-0 ${iconCls}`} />
                      <div className="min-w-0 flex-1">
                        <p className="text-xs text-neutral-200">{issue.title}</p>
                        {issue.hint && <p className="text-[10px] text-neutral-500 mt-0.5">{issue.hint}</p>}
                        {linkedTasks.length > 0 && (
                          <div className="flex flex-wrap gap-1 mt-1.5">
                            {linkedTasks.map(t => (
                              <button
                                key={t.id}
                                onClick={() => onViewTask(t)}
                                className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-800/60 text-neutral-300 hover:text-neutral-100 hover:bg-neutral-700/60 transition-colors truncate max-w-[200px] min-h-[44px] flex items-center"
                              >
                                {(t.title ?? t.id).slice(0, 40)}
                              </button>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </>
      )}
    </section>
  );
}

// ---------- skeleton ----------

// ---------- component ----------

const WINDOW_OPTIONS = [
  { label: '6h', value: 6 },
  { label: '12h', value: 12 },
  { label: '24h', value: 24 },
  { label: '48h', value: 48 },
  { label: '7d', value: 168 },
];

export function DashboardHome({ tasks, selectedProjectId, projects, onViewTask, onDirectionClick, onCategoryClick, onScopeClick, onConfigureTargets, onSpawnOrphanFix }: DashboardHomeProps) {
  const [digest, setDigest] = useState<DigestData | null>(null);
  const [openPRs, setOpenPRs] = useState<OpenPR[]>([]);
  const [openPRsIntegrationBranch, setOpenPRsIntegrationBranch] = useState<string | null>(null);
  const [openPRsPromotionTarget, setOpenPRsPromotionTarget] = useState<string | null>(null);
  const [measurementData, setMeasurementData] = useState<MediumScopeMeasurement | null>(null);
  const [scopeBaselines, setScopeBaselines] = useState<ScopeBaselinesResponse | null>(null);
  const [scopeTrendsOpen, setScopeTrendsOpen] = useState(false);
  const [accountHealth, setAccountHealth] = useState<AccountHealthEntry[] | null>(null);
  const [systemMaturity, setSystemMaturity] = useState<SystemMaturityRow[] | null>(null);
  const [taskDurationStats, setTaskDurationStats] = useState<TaskDurationStats | null>(null);
  const [versionLifecycle, setVersionLifecycle] = useState<VersionLifecycleResponse | null>(null);
  const [blindSpots, setBlindSpots] = useState<BlindSpotsData | null>(null);
  const [expandedBlindSpotThemes, setExpandedBlindSpotThemes] = useState<Set<string>>(new Set());
  const [srRecentMisses, setSrRecentMisses] = useState<SrMissedTask[] | null>(null);
  const [srRecentMissesTotal, setSrRecentMissesTotal] = useState<number>(0);
  const [srReviewerDisagreements, setSrReviewerDisagreements] = useState<SrReviewerDisagreementsEnvelope | null>(null);
  const [directionSrBreakdown, setDirectionSrBreakdown] = useState<DirectionSrBreakdownData | null>(null);
  const [fingerprintAnalytics, setFingerprintAnalytics] = useState<PromptFingerprintAnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [healthOverview, setHealthOverview] = useState<HealthOverviewResponse | null>(null);
  const [fixLoopTrendDays, setFixLoopTrendDays] = useState<7 | 14 | 30>(7);
  const [openPRsRefreshing, setOpenPRsRefreshing] = useState(false);
  const [lastFetchError, setLastFetchError] = useState(false);
  const [windowHours, setWindowHours] = useState(24);
  const [selectedSrCategory, setSelectedSrCategory] = useState<string>('');
  const [srDrillThroughError, setSrDrillThroughError] = useState<{ taskId: string; message: string } | null>(null);
  const sparkRef = useRef<SparkPoint[]>([]);
  const [sparkData, setSparkData] = useState<SparkPoint[]>([]);
  const reviewQualitySectionRef = useRef<HTMLElement>(null);
  const dashboardRequestIdRef = useRef(0);
  const openPRsRequestIdRef = useRef(0);
  const versionLifecycleRequestIdRef = useRef(0);

  const selectedProject = projects?.find(p => p.id === selectedProjectId);
  const srCatchRateTargets = selectedProject?.sr_catch_rate_targets;

  const resetProjectScopedState = useCallback(() => {
    setDigest(null);
    setOpenPRs([]);
    setOpenPRsIntegrationBranch(null);
    setOpenPRsPromotionTarget(null);
    setMeasurementData(null);
    setScopeBaselines(null);
    setAccountHealth(null);
    setSystemMaturity(null);
    setTaskDurationStats(null);
    setVersionLifecycle(null);
    setBlindSpots(null);
    setExpandedBlindSpotThemes(new Set());
    setSrRecentMisses(null);
    setSrRecentMissesTotal(0);
    setSrReviewerDisagreements(null);
    setDirectionSrBreakdown(null);
    setFingerprintAnalytics(null);
    setHealthOverview(null);
    setSparkData([]);
    sparkRef.current = [];
    setOpenPRsRefreshing(false);
    setLoading(true);
    setLastFetchError(false);
  }, []);

  // Fetch open PRs only — used by manual refresh button
  const fetchOpenPRs = useCallback(async () => {
    if (!selectedProjectId) return;
    const requestId = ++openPRsRequestIdRef.current;
    setOpenPRsRefreshing(true);
    try {
      const res = await apiFetch(`/api/admin/open-prs?project_id=${encodeURIComponent(selectedProjectId)}`);
      if (res.ok && requestId === openPRsRequestIdRef.current) {
        const data = await res.json();
        setOpenPRs(data.open_prs ?? []);
        setOpenPRsIntegrationBranch(data.project_integration_branch ?? null);
        setOpenPRsPromotionTarget(data.project_promotion_target ?? null);
      }
    } catch { /* silent */ }
    finally {
      if (requestId === openPRsRequestIdRef.current) {
        setOpenPRsRefreshing(false);
      }
    }
  }, [selectedProjectId]);

  const handleCategoryClick = useCallback((category: string) => {
    setSelectedSrCategory(category);
    onCategoryClick?.(category);
  }, [onCategoryClick]);

  const handleScopeClick = useCallback((scope: string) => {
    onScopeClick?.(scope);
  }, [onScopeClick]);

  const handleScrollToReviewQuality = useCallback(() => {
    reviewQualitySectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  const handleConfigureTargets = useCallback(() => {
    onConfigureTargets?.();
  }, [onConfigureTargets]);

  const handleToggleBlindSpotTheme = useCallback((theme: string) => {
    setExpandedBlindSpotThemes(prev => {
      const newSet = new Set(prev);
      if (newSet.has(theme)) newSet.delete(theme);
      else newSet.add(theme);
      return newSet;
    });
  }, []);

  const handleViewTaskById = useCallback((taskId: string) => {
    const task = tasks.find(t => t.id === taskId);
    if (task) {
      setSrDrillThroughError(null);
      onViewTask(task);
      return;
    }
    fetchTask(taskId).then(t => {
      setSrDrillThroughError(null);
      onViewTask(t);
    }).catch(() => {
      setSrDrillThroughError({ taskId, message: `Could not load task ${taskId}. It may be outside the current window.` });
    });
  }, [tasks, onViewTask]);

  // Fetch data
  const fetchData = useCallback(async (requestId: number) => {
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
          if (res.ok && requestId === dashboardRequestIdRef.current) {
            const data = await res.json();
            if (requestId === dashboardRequestIdRef.current && assertDigestContract<DirectionProgress>(data)) {
              setDigest(data);
              digestOk = true;
            } else if (requestId === dashboardRequestIdRef.current) {
              setDigest(null);
            }
          } else if (requestId === dashboardRequestIdRef.current) {
            setDigest(null);
          }
        } catch {
          if (requestId === dashboardRequestIdRef.current) {
            setDigest(null);
          }
          /* silent */
        }
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
            if (res.ok && requestId === dashboardRequestIdRef.current) { diagnosticsOk = true; }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/open-prs?project_id=${encodeURIComponent(selectedProjectId)}`);
            if (res.ok && requestId === dashboardRequestIdRef.current) {
              const data = await res.json();
              setOpenPRs(data.open_prs ?? []);
              setOpenPRsIntegrationBranch(data.project_integration_branch ?? null);
              setOpenPRsPromotionTarget(data.project_promotion_target ?? null);
              openPRsOk = true;
            }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/medium-scope-measurement?project_id=${encodeURIComponent(selectedProjectId)}&lookback_days=90`);
            if (res.ok && requestId === dashboardRequestIdRef.current) setMeasurementData(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/scope-baselines?project_id=${encodeURIComponent(selectedProjectId)}&lookback_days=30`);
            if (res.ok && requestId === dashboardRequestIdRef.current) setScopeBaselines(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/max-plan-accounts/health`);
            if (res.ok && requestId === dashboardRequestIdRef.current) {
              const data = await res.json();
              setAccountHealth(data.accounts ?? []);
            }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/system-maturity`);
            if (res.ok && requestId === dashboardRequestIdRef.current) {
              const data = await res.json();
              setSystemMaturity(Array.isArray(data) ? data : []);
            }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-blind-spots`);
            if (res.ok && requestId === dashboardRequestIdRef.current) setBlindSpots(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-reviewer-disagreements`);
            if (res.ok && requestId === dashboardRequestIdRef.current) setSrReviewerDisagreements(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const params = new URLSearchParams();
            if (selectedSrCategory) params.set('category', selectedSrCategory);
            const query = params.toString();
            const url = `/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-recent-misses${query ? `?${query}` : ''}`;
            const res = await apiFetch(url);
            if (res.ok && requestId === dashboardRequestIdRef.current) {
              const data = await res.json();
              setSrRecentMisses(data.misses ?? []);
              setSrRecentMissesTotal(data.total ?? (data.misses ?? []).length);
            }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/direction-sr-breakdown`);
            if (res.ok && requestId === dashboardRequestIdRef.current) setDirectionSrBreakdown(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/prompt-fingerprint-analytics`);
            if (res.ok && requestId === dashboardRequestIdRef.current) setFingerprintAnalytics(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/version-lifecycle`);
            if (res.ok && requestId === dashboardRequestIdRef.current) setVersionLifecycle(await res.json());
          } catch { /* silent */ }
        })(),
      );
    }

    fetches.push(
      (async () => {
        try {
          const url = selectedProjectId
            ? `/api/admin/task-duration-stats?project_id=${encodeURIComponent(selectedProjectId)}`
            : `/api/admin/task-duration-stats`;
          const res = await apiFetch(url);
          if (res.ok && requestId === dashboardRequestIdRef.current) setTaskDurationStats(await res.json());
        } catch { /* silent */ }
      })(),
      (async () => {
        try {
          const url = selectedProjectId
            ? `/api/admin/health-overview?project_id=${encodeURIComponent(selectedProjectId)}&lookback_hours=${windowHours}`
            : `/api/admin/health-overview?lookback_hours=${windowHours}`;
          const res = await apiFetch(url);
          if (requestId === dashboardRequestIdRef.current) {
            if (res.ok) setHealthOverview(await res.json());
            else setHealthOverview(null);
          }
        } catch {
          if (requestId === dashboardRequestIdRef.current) setHealthOverview(null);
        }
      })(),
    );

    await Promise.allSettled(fetches);
    if (requestId !== dashboardRequestIdRef.current) return;
    setLoading(false);

    // Show error banner only when all primary fetches fail (backend unreachable)
    const allFailed = selectedProjectId
      ? !digestOk && !diagnosticsOk && !openPRsOk
      : !digestOk;
    setLastFetchError(allFailed);
  }, [selectedProjectId, windowHours, selectedSrCategory]);

  const refreshVersionLifecycle = useCallback(async () => {
    if (!selectedProjectId) return;
    const requestId = ++versionLifecycleRequestIdRef.current;
    try {
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/version-lifecycle`);
      if (res.ok && requestId === versionLifecycleRequestIdRef.current) setVersionLifecycle(await res.json());
    } catch { /* silent */ }
  }, [selectedProjectId]);

  // Initial + polling
  useEffect(() => {
    openPRsRequestIdRef.current += 1;
    versionLifecycleRequestIdRef.current += 1;
    resetProjectScopedState();
    void fetchData(++dashboardRequestIdRef.current);
    // Each tick starts a fresh "latest" request by reading the CURRENT id from
    // the ref (via increment) rather than a stale id captured at effect-setup
    // time. Capturing the id in the closure froze polling after any manual
    // refetch (which bumps the ref): fetchData's `requestId === ref.current`
    // guards then permanently failed and the dashboard silently stopped
    // applying poll responses.
    const id = setInterval(() => void fetchData(++dashboardRequestIdRef.current), 30_000);
    return () => clearInterval(id);
  }, [fetchData, resetProjectScopedState]);

  const refetchDashboard = useCallback(() => {
    void fetchData(++dashboardRequestIdRef.current);
  }, [fetchData]);

  const { activeCount, queuedCount } = useMemo(() => ({
    activeCount: tasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING').length,
    queuedCount: tasks.filter(t => t.status === 'QUEUED').length,
  }), [tasks]);

  // Sparkline sampling
  useEffect(() => {
    const failed = digest?.failed_count ?? 0;
    const next = [...sparkRef.current, { active: activeCount, queued: queuedCount, failed }].slice(-60);
    sparkRef.current = next;
    setSparkData(next);
  }, [activeCount, queuedCount, digest?.failed_count]);
  const failedCount = digest?.failed_count ?? 0;
  const successRate = digest?.success_rate;

  // Recent completions with pr_url (from digest)
  const mergeEvents = (digest?.recent_completions ?? []).filter(e => e.pr_url);

  // Direction progress sorted by priority (high → medium → low)
  const { directions, totalDirPRs, hasAttribution, totalAutonomousPRs, totalAssistedPRs, selfSufficientCount } = useMemo(() => {
    const priorityOrder: Record<string, number> = { high: 0, medium: 1, low: 2 };
    const dirs = [...(digest?.direction_progress ?? [])].sort((a, b) => (priorityOrder[a.priority] ?? 9) - (priorityOrder[b.priority] ?? 9));
    const totalDirPRs = dirs.reduce((s, d) => s + d.prs_merged, 0);
    const hasAttribution = dirs.some(d => d.autonomous_prs_merged != null || d.assisted_prs_merged != null);
    const totalAutonomousPRs = hasAttribution ? dirs.reduce((s, d) => s + (d.autonomous_prs_merged ?? 0), 0) : null;
    const totalAssistedPRs = hasAttribution ? dirs.reduce((s, d) => s + (d.assisted_prs_merged ?? 0), 0) : null;
    const selfSufficientCount = dirs.filter(d => {
      const history = d.research_history ?? [];
      const histHasAttrib = history.some(e => e.autonomous_prs_merged != null);
      const lifetimeAutonomous = histHasAttrib
        ? history.reduce((s, e) => s + (e.autonomous_prs_merged ?? 0), 0) + ((d.history_aggregate?.total_autonomous_prs_merged as number | null) ?? 0)
        : null;
      const operatorActions = (d.history_aggregate?.total_operator_actions as number | null) ?? null;
      return (lifetimeAutonomous ?? 0) >= 10 && operatorActions === 0;
    }).length;
    return { directions: dirs, totalDirPRs, hasAttribution, totalAutonomousPRs, totalAssistedPRs, selfSufficientCount };
  }, [digest?.direction_progress]);
  const productPct = digest?.overall_product_ratio != null ? Math.round(digest.overall_product_ratio * 100) : null;

  const classSparkMap = useMemo(() => {
    const trendHistory = digest?.trend_history ?? [];
    return Object.fromEntries(
      (digest?.failure_groups ?? []).map(g => [
        g.failure_class,
        trendHistory.map(p => ({ v: p.failed_by_class?.[g.failure_class] ?? 0 })),
      ])
    );
  }, [digest?.trend_history, digest?.failure_groups]);

  const taxSparkMap = useMemo(() => {
    const trendHistory = digest?.trend_history ?? [];
    return Object.fromEntries(
      TAXONOMY_ORDER.map(k => [k, trendHistory.map(p => ({ v: p.failed_by_taxonomy?.[k] ?? 0 }))])
    );
  }, [digest?.trend_history]);

  return (
    <div className="flex-1 overflow-auto custom-scrollbar space-y-4">
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

      {/* Anomaly Throttle Banner */}
      <AnomalyThrottleBanner digest={digest} onRestored={refetchDashboard} />

      {/* System Health — actionable issues with remediation hints */}
      <SystemHealthCard data={healthOverview} loading={loading} tasks={tasks} onViewTask={onViewTask} />

      {/* Viewport Debt */}
      {selectedProjectId && <ViewportDebtPanel projectId={selectedProjectId} />}

      {/* Version Lifecycle */}
      {selectedProjectId && <VersionLifecyclePanel data={versionLifecycle} loading={loading} projectId={selectedProjectId} onRefresh={refreshVersionLifecycle} />}

      {/* Pipeline Efficiency */}
      <PipelineEfficiencyPanel projectId={selectedProjectId} />

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
              <AcceptanceRateCard rate={digest?.pipeline_acceptance_rate} loading={loading} />
              <CostCard totalCost={digest?.total_estimated_cost_usd} trendHistory={digest?.trend_history} loading={loading} />
              <SrCatchRateCard rate={digest?.self_review_prediction_accuracy?.prediction_hit_rate} trends={digest?.self_review_prediction_accuracy_trends} loading={loading} onScrollToReviewQuality={handleScrollToReviewQuality} />
            </div>
          </section>

          {/* Section 1b: Failure Breakdown by Class */}
          {!loading && digest?.failure_groups && digest.failure_groups.length > 0 && (
            <section>
              <div className="flex flex-wrap gap-1.5">
                {digest.failure_groups.map(g => {
                  const classSpark = classSparkMap[g.failure_class] ?? [];
                  const classCost = digest.cost_by_failure_class?.[g.failure_class];
                  return <FailureClassPill key={g.failure_class} group={g} sparkData={classSpark} cost={classCost ?? undefined} />;
                })}
              </div>
            </section>
          )}

          {/* Section 1c: Failure Taxonomy Breakdown */}
          {!loading && digest?.failure_taxonomy_distribution && digest.failure_taxonomy_distribution.total_failures > 0 && (
            <section>
              <div className="flex flex-wrap gap-1.5 items-center">
                <span className="text-[10px] text-neutral-500 uppercase tracking-wide mr-1">Taxonomy</span>
                {TAXONOMY_ORDER.map(k => {
                  const tax = digest.failure_taxonomy_distribution;
                  if (!tax) return null;
                  const cat = tax.categories[k];
                  if (!cat) return null;
                  const isDominant = tax.dominant_category === k;
                  const base = TAXONOMY_COLORS[k] ?? 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30';
                  const cls = isDominant ? `${base} ring-1 ring-current` : base;
                  const stroke = TAXONOMY_STROKE[k] ?? '#a3a3a3';
                  const taxSpark = taxSparkMap[k] ?? [];
                  const hasSpark = taxSpark.length >= 3;
                  return (
                    <span key={k} className={`inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-0.5 rounded border ${cls}`} title={`${TAXONOMY_LABELS[k]}: ${cat.count} (${cat.percentage}%)${isDominant ? ' — dominant' : ''}`}>
                      {TAXONOMY_LABELS[k]}
                      <span className="font-bold">{cat.count}</span>
                      <span className="opacity-70">{cat.percentage}%</span>
                      {hasSpark && (
                        <span className="w-16 h-3 inline-block">
                          <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
                            <AreaChart data={taxSpark}>
                              <defs>
                                <linearGradient id={`tsp-${k}`} x1="0" y1="0" x2="0" y2="1">
                                  <stop offset="0%" stopColor={stroke} stopOpacity={0.4} />
                                  <stop offset="100%" stopColor={stroke} stopOpacity={0} />
                                </linearGradient>
                              </defs>
                              <Area type="monotone" dataKey="v" stroke={stroke} fill={`url(#tsp-${k})`} strokeWidth={1} dot={false} isAnimationActive={false} />
                            </AreaChart>
                          </ResponsiveContainer>
                        </span>
                      )}
                    </span>
                  );
                })}
              </div>
            </section>
          )}

          {/* Section 1d: Failure Stage Distribution */}
          {!loading && <FailureStageWidget distribution={digest?.failure_stage_distribution} />}

          {/* Section 1d3: Task Duration Percentiles */}
          {!loading && <TaskDurationWidget data={taskDurationStats} />}

          {/* Section 1d4: Fix-Loop Convergence */}
          {!loading && (
            <div>
              <div className="flex justify-end gap-1 mb-1">
                {([7, 14, 30] as const).map(d => (
                  <button
                    key={d}
                    onClick={() => setFixLoopTrendDays(d)}
                    className={`text-[10px] px-3 py-2 rounded transition-colors min-h-11 min-w-11 ${fixLoopTrendDays === d ? 'bg-neutral-700 text-neutral-200' : 'text-neutral-500 hover:text-neutral-300'}`}
                  >
                    {d}d
                  </button>
                ))}
              </div>
              <FixLoopConvergenceWidget
                trendDays={fixLoopTrendDays}
                projectId={selectedProjectId ?? undefined}
                onViewTask={handleViewTaskById}
              />
            </div>
          )}

          {/* Section 1d2: Review Rejection Reason Breakdown */}
          {!loading && (() => {
            const cats = digest?.first_attempt_success_rate?.first_attempt_by_review_category;
            if (!cats || Object.keys(cats).length === 0) return null;
            const ordered = REVIEW_CAT_ORDER.filter(k => cats[k] != null);
            const extras = Object.keys(cats).filter(k => !REVIEW_CAT_ORDER.includes(k as typeof REVIEW_CAT_ORDER[number]));
            const allKeys = [...ordered, ...extras];
            return (
              <section>
                <div className="flex flex-wrap gap-1.5 items-center">
                  <span className="text-[10px] text-neutral-500 uppercase tracking-wide mr-1">Review Rejection</span>
                  {allKeys.map(k => {
                    const pct = cats[k];
                    if (pct == null) return null;
                    const color = REVIEW_CAT_COLORS[k] ?? 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30';
                    return (
                      <span key={k} className={`inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 rounded border ${color}`} title={`${k}: ${Math.round(pct * 100)}% of first-attempt rejections`}>
                        {k}
                        <span className="font-bold">{Math.round(pct * 100)}%</span>
                      </span>
                    );
                  })}
                </div>
              </section>
            );
          })()}

          {/* Section 1d3: Review Quality */}
          <ReviewQualitySection
            digest={digest}
            loading={loading}
            selectedProjectId={selectedProjectId}
            tasks={tasks}
            onViewTask={onViewTask}
            onViewTaskById={handleViewTaskById}
            srCatchRateTargets={srCatchRateTargets}
            onCategoryClick={handleCategoryClick}
            onScopeClick={handleScopeClick}
            onConfigureTargets={handleConfigureTargets}
            sectionRef={reviewQualitySectionRef}
          />

          {/* SR diagnostics stack */}
          {srDrillThroughError && (
            <div
              role="alert"
              data-testid="sr-drill-through-error"
              className="flex items-start gap-2 rounded border border-red-500/40 bg-red-500/10 px-2.5 py-1.5 text-[11px] text-red-300"
            >
              <span className="flex-1">{srDrillThroughError.message}</span>
              <button
                type="button"
                onClick={() => setSrDrillThroughError(null)}
                aria-label="Dismiss task load error"
                className="shrink-0 text-red-400 hover:text-red-200 font-bold leading-none"
              >
                ×
              </button>
            </div>
          )}
          <SrDiagnosticsStack
            loading={loading}
            selectedProjectId={selectedProjectId}
            onViewTaskById={handleViewTaskById}
            onDirectionClick={onDirectionClick}
            srRecentMisses={srRecentMisses}
            srRecentMissesTotal={srRecentMissesTotal}
            selectedSrCategory={selectedSrCategory}
            blindSpots={blindSpots}
            expandedBlindSpotThemes={expandedBlindSpotThemes}
            onToggleBlindSpotTheme={handleToggleBlindSpotTheme}
            srReviewerDisagreements={srReviewerDisagreements}
            directionSrBreakdown={directionSrBreakdown}
            srTrends={digest?.self_review_prediction_accuracy_trends}
            fingerprintAnalytics={fingerprintAnalytics}
          />

          {/* Section 1d4: Cost of Failures */}
          {!loading && (digest?.total_failed_cost_usd ?? 0) > 0 && (
            <section>
              <div className="flex flex-wrap gap-1.5 items-center">
                <span className="text-[10px] text-neutral-500 uppercase tracking-wide mr-1">Cost of Failures</span>
                <span className="text-sm font-bold text-red-400">${(digest?.total_failed_cost_usd ?? 0).toFixed(2)}</span>
                {Object.entries(digest?.cost_by_failure_class ?? {})
                  .sort((a, b) => b[1] - a[1])
                  .map(([cls, cost]) => {
                    const color = FAILURE_CLASS_COLORS[cls] ?? 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30';
                    return (
                      <span key={cls} className={`inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-0.5 rounded border ${color}`}>
                        {cls}
                        <span className="font-bold">${cost.toFixed(2)}</span>
                      </span>
                    );
                  })}
              </div>
            </section>
          )}

          {/* Section 1e: Pipeline Filter Funnel */}
          {!loading && digest?.pipeline_filter_funnel && (digest.pipeline_filter_funnel.findings_produced ?? 0) > 0 && (() => {
            const ff = digest.pipeline_filter_funnel;
            const produced = ff.findings_produced ?? 0;
            const created = ff.tasks_created ?? 0;
            const pct = produced > 0 ? Math.round((created / produced) * 100) : 0;
            const FILTER_KEYS: { key: string; label: string }[] = [
              { key: 'hash_dedup', label: 'hash dedup' },
              { key: 'word_overlap', label: 'word overlap' },
              { key: 'file_overlap', label: 'file overlap' },
              { key: 'type_filtered', label: 'type filter' },
              { key: 'empty_prompt', label: 'empty prompt' },
              { key: 'batch_cap', label: 'batch cap' },
            ];
            const drops = FILTER_KEYS
              .map(f => ({ ...f, v: ff[f.key] ?? 0 }))
              .filter(f => f.v > 0)
              .sort((a, b) => b.v - a.v);
            const topDrop = drops[0];
            return (
              <section>
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-neutral-400">
                  <span className="text-[10px] text-neutral-500 uppercase tracking-wide">Research yield</span>
                  <span>
                    <span className="font-semibold text-neutral-200">{created}</span>
                    <span className="text-neutral-500"> / {produced} findings &rarr; tasks </span>
                    <span className="font-semibold text-neutral-200">({pct}%)</span>
                  </span>
                  {topDrop && (
                    <span className="text-neutral-500">
                      top drop: <span className="text-neutral-300">{topDrop.label}</span> <span className="font-mono text-neutral-400">-{topDrop.v}</span>
                    </span>
                  )}
                  {drops.length > 1 && (
                    <span className="flex flex-wrap gap-1">
                      {drops.slice(1).map(d => (
                        <span key={d.key} className="text-[10px] text-neutral-500" title={d.label}>
                          {d.label} <span className="font-mono text-neutral-400">-{d.v}</span>
                        </span>
                      ))}
                    </span>
                  )}
                </div>
              </section>
            );
          })()}

          {/* Section 1f: Remediation Funnel */}
          {selectedProjectId && (
            <RemediationFunnelSection
              funnel={digest?.remediation_funnel ?? null}
              conversion={digest?.investigation_conversion ?? null}
              loading={loading}
              projectId={selectedProjectId}
              onViewTask={handleViewTaskById}
            />
          )}
          {!loading && (() => {
            const groups = digest?.remediation_funnel?.residual_remediation_funnel?.open_cases_by_error_class ?? [];
            if (!groups.length) return null;
            return (
              <section>
                <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Open Residual Cases</h2>
                <div className="space-y-2">
                  {groups.map((group) => (
                    <div key={group.error_class} className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-2">
                      <div className="flex items-center justify-between mb-1 text-xs">
                        <span className="font-semibold text-neutral-300">{group.error_class} · {group.count}</span>
                        <span className="text-neutral-500">{group.most_recent_at ? `latest ${timeAgo(group.most_recent_at)}` : 'no timestamp'}</span>
                      </div>
                      <div className="space-y-1">
                        {group.cases.slice(0, 8).map((c) => (
                          <div key={c.fingerprint} className="text-[11px] text-neutral-400 border border-neutral-800/60 rounded px-2 py-1">
                            <div className="flex items-center justify-between gap-2">
                              <span className="font-mono truncate min-w-0">{c.fingerprint.slice(0, 18)}…</span>
                              <span className="text-neutral-500 shrink-0">{c.status} · {c.hit_count} hits · {c.last_seen_at ? timeAgo(c.last_seen_at) : 'unknown age'}</span>
                            </div>
                            {c.sample_message && <div className="truncate text-neutral-500">{c.sample_message}</div>}
                          </div>
                        ))}
                      </div>
                    </div>
                  ))}
                </div>
              </section>
            );
          })()}

          {/* Section 1f2: System Maturity */}
          {selectedProjectId && (
            <SystemMaturityCard rows={systemMaturity} loading={loading} />
          )}

          {/* Section 1g: Recurring Patterns */}
          <RecurringPatternsSection
            recurringPatterns={digest?.recurring_patterns ?? []}
            onDirectionClick={onDirectionClick}
          />

          {/* Section 1h: Rejection Pattern Summary */}
          {(() => {
            const rps = digest?.rejection_pattern_summary;
            if (!rps || rps.total_rejected === 0 || rps.top_notes.length === 0) return null;
            return (
              <section>
                <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
                  Human Rejection Patterns <span className="text-neutral-600 font-normal normal-case tracking-normal">· {rps.total_rejected} rejected</span>
                </h2>
                <div className="flex flex-col gap-1">
                  {rps.top_notes.map((note, i) => (
                    <div
                      key={i}
                      className="flex items-start gap-2 text-xs bg-neutral-900/50 border border-neutral-800/50 px-2 py-1.5 rounded"
                    >
                      <span className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-rose-500/10 text-rose-400 border border-rose-500/20 tabular-nums">
                        ×{note.count}
                      </span>
                      <span className="flex-1 text-neutral-400 font-mono text-[10px] break-words" title={note.snippet}>
                        {note.snippet.length > 160 ? note.snippet.slice(0, 160) + '…' : note.snippet}
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            );
          })()}

          {/* Section 2: Direction Portfolio Health */}
          <DirectionPortfolioSection
            directions={directions}
            loading={loading}
            productPct={productPct}
            totalDirPRs={totalDirPRs}
            hasAttribution={hasAttribution}
            totalAutonomousPRs={totalAutonomousPRs}
            totalAssistedPRs={totalAssistedPRs}
            selfSufficientCount={selfSufficientCount}
            predictionCalibrationFactor={digest?.prediction_calibration_factor}
            predictionCalibrationNote={digest?.prediction_calibration_note}
            predictionCalibrationSampleCount={digest?.prediction_calibration_sample_count}
            medianCostPerMergedPr={digest?.median_cost_per_merged_pr}
            onDirectionClick={onDirectionClick}
            projectId={selectedProjectId}
          />

          {/* Section 2b: Scope Conversion Rates */}
          {!!selectedProjectId && (
            <ScopeConversionRatesSection
              scopeRates={digest?.scope_conversion_rates}
              prevScopeRates={digest?.prev_scope_conversion_rates}
              loading={loading}
            />
          )}

          {/* Section 2b2: Scope Trends */}
          {!!selectedProjectId && (
            <ScopeTrendsSection
              data={scopeBaselines}
              loading={loading}
              open={scopeTrendsOpen}
              onToggle={() => setScopeTrendsOpen(v => !v)}
            />
          )}

          {/* Section 2c: Medium Scope Measurement */}
          {!!selectedProjectId && (
            <MediumScopeWidget data={measurementData} loading={loading} />
          )}

          {/* Section 2d: Provider Account Health */}
          {!!selectedProjectId && (
            <PoolAccountHealthPanel accounts={accountHealth} loading={loading} />
          )}

          {/* Section 2e: Provider / Model Breakdown */}
          <ProviderModelBreakdownPanel
            selectedProjectId={selectedProjectId}
            accounts={accountHealth}
          />

          {/* Section 2f: Provider Cost & Success */}
          <ProviderCostPanel selectedProjectId={selectedProjectId} windowHours={windowHours} />

          {/* Section 2g: LLM Cost by Model */}
          <LlmCostBreakdownPanel selectedProjectId={selectedProjectId} />

          {/* Drift Signals */}
          {!!selectedProjectId && <DriftSignalsCard projectId={selectedProjectId} />}

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
                          <a href={ev.pr_url} target="_blank" rel="noopener noreferrer" className="text-neutral-500 hover:text-neutral-300 shrink-0" data-viewport-allow-small-target title="View PR on GitHub">
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

          {/* Direction auto-pause/recovery observability */}
          <div className="mt-4">
            <DirectionAutoPauseWidget selectedProjectId={selectedProjectId} />
          </div>

          {/* Section 5: PR Monitor */}
          <PRMonitorSection
            openPRs={openPRs}
            loading={loading}
            refreshing={openPRsRefreshing}
            onRefresh={fetchOpenPRs}
            hasProject={!!selectedProjectId}
            tasks={tasks}
            onViewTask={onViewTask}
            integrationBranch={openPRsIntegrationBranch}
            promotionTarget={openPRsPromotionTarget}
            onSpawnFixTask={onSpawnOrphanFix}
          />
        </div>
      </div>
    </div>
  );
}


// ---------- anomaly throttle banner ----------

function AnomalyThrottleBanner({ digest, onRestored }: { digest: DigestData | null; onRestored?: () => void }) {
  const [restoring, setRestoring] = useState(false);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const adj = digest?.anomaly_adjustment;
  if (!adj || !adj.active) return null;
  const triggers = adj.trigger_anomalies ?? [];
  const pre = adj.pre_adjustment_max_vms;
  const post = adj.adjusted_max_vms;
  const gatePre = adj.pre_adjustment_failure_gate;
  const gateNow = adj.failure_gate_threshold;
  const handleRestore = async () => {
    setRestoring(true);
    setRestoreError(null);
    try {
      const res = await apiFetch('/api/admin/anomaly-throttle/restore', { method: 'POST' });
      if (!res.ok) {
        setRestoreError(`Restore failed (${res.status})`);
        return;
      }
      onRestored?.();
    } catch {
      setRestoreError('Restore failed');
    } finally {
      setRestoring(false);
    }
  };
  return (
    <section>
      <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-3 flex items-start gap-2">
        <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <div className="text-xs font-semibold text-amber-300">
            Pipeline auto-throttled
            {adj.adjusted_at && (
              <span className="ml-2 text-[10px] font-normal text-amber-400/70">
                {timeAgo(adj.adjusted_at)}
              </span>
            )}
          </div>
          <div className="text-[11px] text-amber-200/80 mt-1 space-y-0.5">
            {triggers.length > 0 && (
              <div>
                <span className="text-amber-400/60">Triggers: </span>
                <span className="font-mono">{triggers.join(', ')}</span>
              </div>
            )}
            {pre != null && post != null && (
              <div>
                <span className="text-amber-400/60">max_vms: </span>
                <span className="font-mono">{pre} → {post}</span>
              </div>
            )}
            {gatePre != null && gateNow != null && gatePre !== gateNow && (
              <div>
                <span className="text-amber-400/60">failure_gate: </span>
                <span className="font-mono">{gatePre} → {gateNow}</span>
              </div>
            )}
            {(adj.forced_restoration_count ?? 0) > 0 && (
              <div className="text-amber-400/60">
                Forced restorations: {adj.forced_restoration_count}
                {adj.forced_restored_at && ` (last ${timeAgo(adj.forced_restored_at)})`}
              </div>
            )}
          </div>
          <div className="mt-2 flex items-center gap-2 flex-wrap">
            {/* eslint-disable-next-line viewport/min-touch-target -- dense banner control, mirrors Reset Circuit Breaker affordance */}
            <button
              type="button"
              onClick={handleRestore}
              disabled={restoring}
              className="text-[11px] font-medium px-2 py-1 rounded border border-amber-500/40 text-amber-200 hover:bg-amber-500/20 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {restoring ? 'Restoring…' : 'Clear throttle / Restore now'}
            </button>
            {restoreError && (
              <span className="text-[11px] text-red-400">{restoreError}</span>
            )}
          </div>
          <div className="text-[10px] text-amber-400/60 mt-1">
            Otherwise auto-restores once the triggering anomalies clear — this is an early restore, not the only path.
          </div>
        </div>
      </div>
    </section>
  );
}

