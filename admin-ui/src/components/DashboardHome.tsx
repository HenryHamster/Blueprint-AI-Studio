import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import {
  TrendingUp,
  TrendingDown,
  AlertTriangle,
  CheckCircle,
  ExternalLink,
  GitMerge,
  Minus,
} from 'lucide-react';
import { ResponsiveContainer, AreaChart, Area } from 'recharts';
import { apiFetch, fetchTask, type PromptFingerprintAnalyticsResponse, type VersionLifecycleResponse } from '../api';
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
import { DriftSignalsCard } from './DriftSignalsCard';
import { PoolAccountHealthPanel, type AccountHealthEntry } from './PoolAccountHealthPanel';
import { SystemMaturityCard, type SystemMaturityRow } from './SystemMaturityCard';
import { DirectionPortfolioSection, type DirectionProgress } from './DirectionPortfolioSection';
import { SrMissedRetrospective, SrReviewerDisagreementsPanel, DirectionSrBreakdownPanel, type SrMissedTask, type SrReviewerDisagreementsData, type EdgesTrend, type DirectionSrEntry } from './SrTrendBreakdown'; // EdgesTrend used in DigestData
import { SrUnhandledEdgesRetrospectivesPanel } from './SrUnhandledEdgesRetrospectivesPanel';
import { ReviewQualitySection, type SrFixIterationBucket, type SrTrendWithFixIterations } from './ReviewQualitySection';
import { TAXONOMY_COLORS, TAXONOMY_LABELS, TAXONOMY_ORDER } from './dashboardConstants';
import { ManualInterventionPanel, AutoRecoveredPanel, AnomalyTrackerPanel, PipelineRegressionsPanel } from './PipelineRegressionsPanel';
import { RecentlyResolvedRegressionsPanel } from './RecentlyResolvedRegressionsPanel';
import { ViewportDebtPanel } from './ViewportDebtPanel';
import type { Task } from '../types/task';
import type { Project } from '../types/project';

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

interface FailureGroupTask {
  task_id: string;
  title: string;
  error_snippet?: string;
  attempt_count?: number;
  taxonomy?: string;
  timeout_subtype?: string | null;
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
  memos_available?: number | null;
  memos_cited?: number | null;
}

interface DigestData {
  failed_count: number;
  success_rate?: number | null;
  trend?: 'improving' | 'declining' | 'stable';
  trend_history?: { period_end: string; success_rate: number | null; completed: number; failed: number; failed_by_class?: Record<string, number>; failed_by_taxonomy?: Record<string, number>; period_cost_usd?: number | null }[];
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
  pipeline_acceptance_rate?: number | null;
  scope_conversion_rates?: Record<string, { created: number; merged: number; calibrated_in?: number; calibrated_out?: number }> | null;
  prev_scope_conversion_rates?: Record<string, { created: number; merged: number; calibrated_in?: number; calibrated_out?: number }> | null;
  cost_by_failure_class?: Record<string, number> | null;
  total_failed_cost_usd?: number | null;
  failure_taxonomy_distribution?: {
    categories: Record<string, { count: number; percentage: number }>;
    total_failures: number;
    dominant_category: string | null;
  } | null;
  failure_stage_distribution?: {
    stages: Record<string, number>;
    total_classified: number;
    dominant_stage: string | null;
  } | null;
  pipeline_filter_funnel?: Record<string, number> | null;
  remediation_funnel?: {
    investigations_spawned?: number;
    investigations_completed?: number;
    investigations_with_diagnosis?: number;
    remediations_spawned?: number;
    remediations_merged?: number;
    remediations_failed?: number;
    avg_time_to_remediation_hours?: number | null;
    active_investigations?: number;
    investigations_by_confidence?: Record<string, number> | null;
    failed_remediations_by_class?: Record<string, number> | null;
    merged_remediations_by_trigger_class?: Record<string, number> | null;
    investigations_spawned_by_class?: Record<string, number> | null;
    investigations_completed_by_class?: Record<string, number> | null;
    remediations_spawned_by_class?: Record<string, number> | null;
    remediation_success_by_confidence?: Record<string, { merged: number; failed: number }> | null;
    diagnosis_drop_off_by_reason?: Record<string, number> | null;
    residual_remediation_funnel?: {
      open_cases_by_error_class?: {
        error_class: string;
        count: number;
        most_recent_at: string | null;
        cases: {
          fingerprint: string;
          status: string;
          hit_count: number;
          last_seen_at: string | null;
          last_spawned_at?: string | null;
          sample_message?: string | null;
          consecutive_failure_count?: number | null;
        }[];
      }[];
    } | null;
  } | null;
  recurring_patterns?: {
    fingerprint: string;
    occurrence_count: number;
    severity?: string | null;
    example_message?: string | null;
    affected_direction_count: number;
    affected_directions?: { direction_id: string; direction_text: string }[];
    remediation_status: 'fix_verified' | 'fix_shipped' | 'investigating' | 'unaddressed';
    proactive?: boolean;
  }[] | null;
  investigation_conversion?: {
    total_completed?: number;
    parseable?: number;
    unparseable?: number;
    structural?: number;
    low_confidence?: number;
    actionable?: number;
    remediation_spawned?: number;
  } | null;
  anomaly_adjustment?: {
    active: boolean;
    adjusted_at?: string | null;
    trigger_anomalies?: string[];
    pre_adjustment_max_vms?: number | null;
    adjusted_max_vms?: number | null;
    failure_gate_threshold?: number | null;
    pre_adjustment_failure_gate?: number | null;
    forced_restoration_count?: number;
    forced_restored_at?: string | null;
  } | null;
  first_attempt_success_rate?: {
    overall?: number | null;
    trend?: string | null;
    sample_size?: number | null;
    first_attempt_by_review_category?: Record<string, number> | null;
    [key: string]: unknown;
  } | null;
  self_review_effectiveness?: {
    with_self_review_approval_rate?: number | null;
    without_self_review_approval_rate?: number | null;
    clean_verdict_approval_rate?: number | null;
    fixed_verdict_approval_rate?: number | null;
    sample_size?: number | null;
    sr_fixed_count?: number | null;
    sr_clean_count?: number | null;
  } | null;
  review_accuracy?: {
    total_reviews?: number | null;
    overrides?: number | null;
    dismissals?: number | null;
    misses?: number | null;
    total_approvals?: number | null;
    precision?: number | null;
    recall?: number | null;
  } | null;
  self_review_prediction_accuracy?: {
    prediction_hit_rate?: number | null;
    miss_rate?: number | null;
    false_alarm_rate?: number | null;
    sample_size?: number | null;
    self_flagged_shipped_unfixed_task_count?: number | null;
    self_flagged_shipped_unfixed_task_rate?: number | null;
  } | null;
  self_review_prediction_accuracy_trends?: SrTrendWithFixIterations | null;
  sr_regression_risk_test_signal_trends?: {
    computed_at?: string | null;
    bucket_mode?: string | null;
    trend?: string | null;
    miss_by_test_signal_rate_delta?: number | null;
    current?: {
      miss_by_test_signal_rate?: number | null;
      numerator?: number | null;
      denominator?: number | null;
      sample_size?: number | null;
    } | null;
    previous?: {
      miss_by_test_signal_rate?: number | null;
      numerator?: number | null;
      denominator?: number | null;
      sample_size?: number | null;
    } | null;
  } | null;
  self_review_skip_rate?: {
    overall_rate: number;
    skipped_count: number;
    sample_size: number;
    by_task_type: Record<string, { rate: number; skipped_count: number; sample_size: number }>;
  } | null;
  sr_disallowed_blind_spot_rate?: {
    overall_rate: number;
    violation_count: number;
    sample_size: number;
    by_task_type: Record<string, { rate: number; violation_count: number; sample_size: number }>;
  } | null;
  fresh_reviewer_effectiveness?: {
    fresh_reviewer_sample_size: number;
    fresh_reviewer_first_attempt_approval_rate: number | null;
    baseline_first_attempt_approval_rate: number | null;
    per_category_hit_rate: Record<string, number>;
    reviewer_finding_added_value: Record<string, number>;
    prediction_sample_size: number;
  } | null;
  edges_trend?: EdgesTrend | null;
  sr_false_attestation_trends?: {
    computed_at?: string | null;
    bucket_mode?: string | null;
    per_category?: Record<string, {
      false_attestation_rate?: number | null;
      previous_false_attestation_rate?: number | null;
      delta?: number | null;
      trend?: string | null;
      ci_confirmed?: number | null;
      false_attested?: number | null;
      ci_data_missing?: number | null;
    }> | null;
  } | null;
  median_cost_per_merged_pr?: number | null;
  prediction_calibration_factor?: number | null;
  prediction_calibration_note?: string | null;
  prediction_calibration_sample_count?: number | null;
  rejection_pattern_summary?: {
    total_rejected: number;
    top_notes: { snippet: string; count: number }[];
  } | null;
  tasks_truncated?: boolean;
  memo_pathway_exits?: number | null;
  memo_ever_cited?: { ever_cited: boolean; first_cycle_id: string | null; first_cited_at: string | null } | null;
  recent_cycle_memo_stats?: Array<{ cycle_id: string | null; started_at: string | null; memos_available: number; memos_cited: number }> | null;
}

// SrFixIterationBucket and SrTrendWithFixIterations are defined and exported from ReviewQualitySection
export type { SrFixIterationBucket, SrTrendWithFixIterations };

// Per-direction SR catch-rate breakdown (endpoint: GET /projects/{id}/direction-sr-breakdown)
// DirectionSrEntry is re-exported from SrTrendBreakdown (canonical model)
export type { DirectionSrEntry };

interface DirectionSrBreakdownData {
  directions: DirectionSrEntry[];
  min_sample: number;
}

/**
 * Type guard for the /api/admin/digest response. Checks the fields the
 * dashboard requires so any backend rename or removal is caught at fetch
 * time rather than silently rendering stale or missing data.
 */
function assertDigestContract(data: unknown): data is DigestData {
  if (typeof data !== 'object' || data === null) return false;
  const d = data as Record<string, unknown>;
  return (
    typeof d.failed_count === 'number' &&
    typeof d.pending_approval_count === 'number' &&
    Array.isArray(d.direction_progress) &&
    Array.isArray(d.recent_completions) &&
    Array.isArray(d.failure_groups)
  );
}

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

interface BlindSpotTheme {
  theme: string;
  count: number;
  layer_distribution?: Record<string, number>;
}

interface BlindSpotsData {
  themes: BlindSpotTheme[];
  total_distinct_themes: number;
  window_days: number;
}

interface SparkPoint {
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
  onConfigureTargets?: () => void;
  onSpawnOrphanFix?: (pr: OpenPR) => void;
}

// ---------- helpers ----------

const FAILURE_CLASS_COLORS: Record<string, string> = {
  CODE_ERROR: 'bg-red-500/20 text-red-400 border-red-500/30',
  PROVISION_FAILURE: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  AGENT_TIMEOUT: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  RATE_LIMIT: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  EXTERNAL_API: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  SCOPE_OVERFLOW: 'bg-teal-500/20 text-teal-400 border-teal-500/30',
};

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

export function DashboardHome({ tasks, selectedProjectId, projects, onViewTask, onDirectionClick, onCategoryClick, onConfigureTargets, onSpawnOrphanFix }: DashboardHomeProps) {
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
  const [srRecentMisses, setSrRecentMisses] = useState<SrMissedTask[] | null>(null);
  const [srRecentMissesTotal, setSrRecentMissesTotal] = useState<number>(0);
  const [srReviewerDisagreements, setSrReviewerDisagreements] = useState<SrReviewerDisagreementsData | null>(null);
  const [directionSrBreakdown, setDirectionSrBreakdown] = useState<DirectionSrBreakdownData | null>(null);
  const [fingerprintAnalytics, setFingerprintAnalytics] = useState<PromptFingerprintAnalyticsResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [healthOverview, setHealthOverview] = useState<HealthOverviewResponse | null>(null);
  const [fixLoopTrendDays, setFixLoopTrendDays] = useState<7 | 14 | 30>(7);
  const [openPRsRefreshing, setOpenPRsRefreshing] = useState(false);
  const [lastFetchError, setLastFetchError] = useState(false);
  const [windowHours, setWindowHours] = useState(24);
  const [selectedSrCategory, setSelectedSrCategory] = useState<string>('');
  const sparkRef = useRef<SparkPoint[]>([]);
  const [sparkData, setSparkData] = useState<SparkPoint[]>([]);
  const reviewQualitySectionRef = useRef<HTMLElement>(null);

  const selectedProject = projects?.find(p => p.id === selectedProjectId);
  const srCatchRateTargets = selectedProject?.sr_catch_rate_targets;

  // Fetch open PRs only — used by manual refresh button
  const fetchOpenPRs = useCallback(async () => {
    if (!selectedProjectId) return;
    setOpenPRsRefreshing(true);
    try {
      const res = await apiFetch(`/api/admin/open-prs?project_id=${encodeURIComponent(selectedProjectId)}`);
      if (res.ok) {
        const data = await res.json();
        setOpenPRs(data.open_prs ?? []);
        setOpenPRsIntegrationBranch(data.project_integration_branch ?? null);
        setOpenPRsPromotionTarget(data.project_promotion_target ?? null);
      }
    } catch { /* silent */ }
    finally {
      setOpenPRsRefreshing(false);
    }
  }, [selectedProjectId]);

  const handleCategoryClick = useCallback((category: string) => {
    setSelectedSrCategory(category);
    onCategoryClick?.(category);
  }, [onCategoryClick]);

  const handleScrollToReviewQuality = useCallback(() => {
    reviewQualitySectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  const handleConfigureTargets = useCallback(() => {
    onConfigureTargets?.();
  }, [onConfigureTargets]);

  const handleViewTaskById = useCallback((taskId: string) => {
    const task = tasks.find(t => t.id === taskId);
    if (task) {
      onViewTask(task);
    } else {
      fetchTask(taskId).then(onViewTask).catch(() => {
        // Silently fail; user can retry or navigate manually
      });
    }
  }, [tasks, onViewTask]);

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
          if (res.ok) {
            const data = await res.json();
            if (assertDigestContract(data)) {
              setDigest(data);
              digestOk = true;
            } else {
              setDigest(null);
            }
          } else {
            setDigest(null);
          }
        } catch {
          setDigest(null);
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
            if (res.ok) { diagnosticsOk = true; }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/open-prs?project_id=${encodeURIComponent(selectedProjectId)}`);
            if (res.ok) {
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
            if (res.ok) setMeasurementData(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/scope-baselines?project_id=${encodeURIComponent(selectedProjectId)}&lookback_days=30`);
            if (res.ok) setScopeBaselines(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/max-plan-accounts/health`);
            if (res.ok) {
              const data = await res.json();
              setAccountHealth(data.accounts ?? []);
            }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/system-maturity`);
            if (res.ok) {
              const data = await res.json();
              setSystemMaturity(Array.isArray(data) ? data : []);
            }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-blind-spots`);
            if (res.ok) setBlindSpots(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-reviewer-disagreements`);
            if (res.ok) setSrReviewerDisagreements(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const params = new URLSearchParams();
            if (selectedSrCategory) params.set('category', selectedSrCategory);
            const query = params.toString();
            const url = `/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-recent-misses${query ? `?${query}` : ''}`;
            const res = await apiFetch(url);
            if (res.ok) {
              const data = await res.json();
              setSrRecentMisses(data.misses ?? []);
              setSrRecentMissesTotal(data.total ?? (data.misses ?? []).length);
            }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/direction-sr-breakdown`);
            if (res.ok) setDirectionSrBreakdown(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/prompt-fingerprint-analytics`);
            if (res.ok) setFingerprintAnalytics(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/version-lifecycle`);
            if (res.ok) setVersionLifecycle(await res.json());
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
          if (res.ok) setTaskDurationStats(await res.json());
        } catch { /* silent */ }
      })(),
      (async () => {
        try {
          const url = selectedProjectId
            ? `/api/admin/health-overview?project_id=${encodeURIComponent(selectedProjectId)}&lookback_hours=${windowHours}`
            : `/api/admin/health-overview?lookback_hours=${windowHours}`;
          const res = await apiFetch(url);
          if (res.ok) setHealthOverview(await res.json());
          else setHealthOverview(null);
        } catch { setHealthOverview(null); }
      })(),
    );

    await Promise.allSettled(fetches);
    setLoading(false);

    // Show error banner only when all primary fetches fail (backend unreachable)
    const allFailed = selectedProjectId
      ? !digestOk && !diagnosticsOk && !openPRsOk
      : !digestOk;
    setLastFetchError(allFailed);
  }, [selectedProjectId, windowHours, selectedSrCategory]);

  const refreshVersionLifecycle = useCallback(async () => {
    if (!selectedProjectId) return;
    try {
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/version-lifecycle`);
      if (res.ok) setVersionLifecycle(await res.json());
    } catch { /* silent */ }
  }, [selectedProjectId]);

  // Initial + polling
  useEffect(() => {
    fetchData();
    const id = setInterval(fetchData, 30_000);
    return () => clearInterval(id);
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
      <AnomalyThrottleBanner digest={digest} />

      {/* System Health — actionable issues with remediation hints */}
      <SystemHealthCard data={healthOverview} loading={loading} tasks={tasks} onViewTask={onViewTask} />

      {/* Pipeline Regressions */}
      {selectedProjectId && <PipelineRegressionsPanel projectId={selectedProjectId} />}

      {/* Manual Intervention Cases */}
      {selectedProjectId && <ManualInterventionPanel projectId={selectedProjectId} />}

      {/* Auto-Recovered Regression Cases */}
      {selectedProjectId && <AutoRecoveredPanel projectId={selectedProjectId} />}

      {/* Persistent Anomaly Tracker */}
      {selectedProjectId && <AnomalyTrackerPanel projectId={selectedProjectId} />}

      {/* Recently Resolved Regressions */}
      {selectedProjectId && <RecentlyResolvedRegressionsPanel projectId={selectedProjectId} />}

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
                onViewTask={(taskId: string) => {
                  const task = tasks.find(t => t.id === taskId);
                  if (task) onViewTask(task);
                }}
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
            srCatchRateTargets={srCatchRateTargets}
            onCategoryClick={handleCategoryClick}
            onConfigureTargets={handleConfigureTargets}
            sectionRef={reviewQualitySectionRef}
          />

                    {/* SR Recent Misses */}
          {!loading && srRecentMisses && srRecentMisses.length > 0 && (
            <section>
              <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-2">
                Recent SR Misses
                <span className="ml-1 text-neutral-600 font-normal normal-case tracking-normal">· {srRecentMissesTotal} task{srRecentMissesTotal !== 1 ? 's' : ''}</span>
              </p>
              <SrMissedRetrospective
                tasks={srRecentMisses}
                selectedCategory={selectedSrCategory}
                onTaskClick={(taskId) => {
                  const task = tasks.find(t => t.id === taskId);
                  if (task) onViewTask(task);
                }}
              />
            </section>
          )}

          {/* SR Unhandled Edges */}
          <SrUnhandledEdgesRetrospectivesPanel
            selectedProjectId={selectedProjectId}
            onSelectTask={(taskId) => {
              const task = tasks.find(t => t.id === taskId);
              if (task) onViewTask(task);
            }}
          />

          {/* SR Blind Spots aggregation */}
          {!loading && blindSpots && blindSpots.themes.length > 0 && (
            <section>
              <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-2">
                SR Blind Spots
                <span className="ml-1 text-neutral-600 font-normal normal-case tracking-normal">· {blindSpots.total_distinct_themes} themes · {blindSpots.window_days}d</span>
              </p>
              <div className="flex flex-wrap gap-2">
                {blindSpots.themes.slice(0, 8).map((t, i) => (
                  <div key={i} className="flex flex-col gap-0.5 min-w-0">
                    <span
                      className="inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-0.5 rounded border bg-violet-500/10 text-violet-400 border-violet-500/20"
                      title={t.theme}
                    >
                      <span className="truncate max-w-[160px]">{t.theme}</span>
                      <span className="font-bold shrink-0">×{t.count}</span>
                    </span>
                    {t.layer_distribution && Object.keys(t.layer_distribution).length > 0 && (
                      <div className="flex flex-wrap gap-1">
                        {Object.entries(t.layer_distribution)
                          .sort((a, b) => b[1] - a[1])
                          .map(([layer, cnt]) => (
                            <span
                              key={layer}
                              className="inline-flex items-center gap-0.5 text-[9px] px-1.5 py-0.5 rounded border bg-neutral-500/10 text-neutral-400 border-neutral-500/20 max-w-[140px]"
                            >
                              <span className="truncate">{layer}</span>
                              <span className="shrink-0">×{cnt}</span>
                            </span>
                          ))}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </section>
          )}

          {/* SR Reviewer Disagreements */}
          {!loading && srReviewerDisagreements && srReviewerDisagreements.disagreements.length > 0 && (
            <section>
              <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-2">
                SR ↔ Reviewer Disagreements
                <span className="ml-1 text-neutral-600 font-normal normal-case tracking-normal">· {srReviewerDisagreements.window_days}d · {srReviewerDisagreements.total_entries} categor{srReviewerDisagreements.total_entries !== 1 ? 'ies' : 'y'}</span>
              </p>
              <SrReviewerDisagreementsPanel data={srReviewerDisagreements} />
            </section>
          )}

          {/* Per-Direction SR Catch-Rate */}
          {!loading && directionSrBreakdown && directionSrBreakdown.directions.length > 0 && (
            <section data-testid="direction-sr-breakdown">
              <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-2">
                SR Catch-Rate by Direction
                <span className="ml-1 text-neutral-600 font-normal normal-case tracking-normal">
                  · {directionSrBreakdown.directions.length} direction{directionSrBreakdown.directions.length !== 1 ? 's' : ''} · worst-first
                </span>
              </p>
              <DirectionSrBreakdownPanel
                entries={directionSrBreakdown.directions}
                onDirectionClick={onDirectionClick}
              />
            </section>
          )}

          {/* Prompt Fingerprint SR Quality */}
          {!loading && fingerprintAnalytics && fingerprintAnalytics.fingerprints.length > 0 && (
            <section data-testid="prompt-fingerprint-sr-quality">
              <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-2">
                Prompt Fingerprint SR Quality
                <span className="ml-1 text-neutral-600 font-normal normal-case tracking-normal">
                  · {fingerprintAnalytics.fingerprints.length} regime{fingerprintAnalytics.fingerprints.length !== 1 ? 's' : ''} · {fingerprintAnalytics.window_days}d
                </span>
              </p>
              <div className="flex flex-col gap-1">
                {fingerprintAnalytics.fingerprints.map((fp) => {
                  const catchRate = fp.sr_catch_rate;
                  const catchColor = catchRate == null ? 'text-neutral-500' : catchRate >= 0.7 ? 'text-emerald-400' : catchRate >= 0.4 ? 'text-amber-400' : 'text-red-400';
                  const delta = fp.sr_catch_rate_delta_vs_project_mean;
                  const deltaColor = delta == null ? '' : delta >= 0 ? 'text-emerald-400' : 'text-red-400';
                  return (
                    <div key={fp.fingerprint} className="bg-neutral-900/50 border border-neutral-800/50 rounded px-2 py-1.5 text-[11px]">
                      <div className="flex items-center gap-2 mb-0.5">
                        <span className="font-mono text-neutral-400 text-[10px]">{fp.fingerprint.slice(0, 12)}…</span>
                        <span className="text-neutral-600 text-[10px]">n={fp.sample_size}</span>
                      </div>
                      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[10px] text-neutral-500">
                        <span>catch <span className={`font-semibold ${catchColor}`}>{catchRate != null ? `${Math.round(catchRate * 100)}%` : '--'}</span>{' '}<span className="text-neutral-600">n={fp.sr_catch_sample_size}</span>{delta != null && <span className={`ml-1 ${deltaColor}`}>({delta >= 0 ? '+' : ''}{Math.round(delta * 100)}pp)</span>}</span>
                        <span>rescue <span className="font-semibold text-neutral-300">{fp.reviewer_rescue_rate != null ? `${Math.round(fp.reviewer_rescue_rate * 100)}%` : '--'}</span>{' '}<span className="text-neutral-600">n={fp.reviewer_rescue_sample_size}</span></span>
                        {fp.sr_false_positive_rate != null && <span>FP <span className="font-semibold text-neutral-300">{Math.round(fp.sr_false_positive_rate * 100)}%</span>{' '}<span className="text-neutral-600">n={fp.sr_false_positive_sample_size}</span></span>}
                      </div>
                      {fp.sr_reviewer_disagreement_top_categories.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-1">
                          {fp.sr_reviewer_disagreement_top_categories.slice(0, 3).map((d) => (
                            <span key={d.category} className="text-[9px] px-1 py-0.5 rounded bg-neutral-800/60 text-neutral-400 border border-neutral-700/40">
                              {d.category}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
          )}

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
          {(digest?.recurring_patterns ?? []).length > 0 && (() => {
            const recurringPatterns = digest?.recurring_patterns ?? [];
            return (
            <section>
              <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
                Recurring Patterns <span className="text-neutral-600 font-normal normal-case tracking-normal">· {recurringPatterns.length}</span>
              </h2>
              <div className="flex flex-col gap-1">
                {recurringPatterns.slice(0, 5).map(pattern => {
                  const statusStyles: Record<string, string> = {
                    fix_verified: 'bg-green-500/10 text-green-400 border-green-500/20',
                    fix_shipped: 'bg-blue-500/10 text-blue-400 border-blue-500/20',
                    investigating: 'bg-yellow-500/10 text-yellow-400 border-yellow-500/20',
                    unaddressed: 'bg-red-500/10 text-red-400 border-red-500/20',
                  };
                  const statusClass = statusStyles[pattern.remediation_status] ?? 'bg-neutral-700/30 text-neutral-400 border-neutral-700/30';
                  const snippet = (pattern.example_message ?? '').length > 120
                    ? (pattern.example_message ?? '').slice(0, 120) + '…'
                    : (pattern.example_message ?? '');
                  const dirs = pattern.affected_directions ?? [];
                  return (
                    <div
                      key={pattern.fingerprint}
                      className="flex flex-col gap-0.5 text-xs bg-neutral-900/50 border border-neutral-800/50 px-2 py-1.5 rounded"
                    >
                      <div className="flex items-center gap-2">
                        <span className={`shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded border uppercase tracking-wider ${statusClass}`}>
                          {pattern.remediation_status.replace('_', ' ')}
                        </span>
                        <span className="flex-1 truncate text-neutral-400 font-mono text-[10px]">{snippet}</span>
                        <span className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-neutral-800/50 text-neutral-300 border border-neutral-700/30">
                          ×{pattern.occurrence_count}
                        </span>
                      </div>
                      {dirs.length > 0 && (
                        <div className="flex flex-wrap items-center gap-1 text-[10px] text-neutral-500">
                          {dirs.slice(0, 4).map(d => (
                            // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
                            <button
                              key={d.direction_id}
                              type="button"
                              onClick={() => onDirectionClick?.(d.direction_id)}
                              className="text-[10px] px-1 py-0.5 rounded bg-neutral-800/50 text-neutral-300 border border-neutral-700/30 hover:border-neutral-600 truncate max-w-[200px]"
                              title={d.direction_text || d.direction_id}
                            >
                              {(d.direction_text || d.direction_id).slice(0, 40)}
                            </button>
                          ))}
                          {dirs.length > 4 && <span>+{dirs.length - 4} more</span>}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            </section>
            );
          })()}

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

function AnomalyThrottleBanner({ digest }: { digest: DigestData | null }) {
  const adj = digest?.anomaly_adjustment;
  if (!adj || !adj.active) return null;
  const triggers = adj.trigger_anomalies ?? [];
  const pre = adj.pre_adjustment_max_vms;
  const post = adj.adjusted_max_vms;
  const gatePre = adj.pre_adjustment_failure_gate;
  const gateNow = adj.failure_gate_threshold;
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
        </div>
      </div>
    </section>
  );
}

// ---------- system status ----------

function SystemStatusCard({ digest, loading }: { digest: DigestData | null; loading: boolean }) {
  const cycle = digest?.latest_cycle ?? null;
  const memoExits = digest?.memo_pathway_exits ?? 0;
  const memoEverCited = digest?.memo_ever_cited ?? null;
  const recentCycleMemoStats = digest?.recent_cycle_memo_stats ?? [];
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
                  {(cycle.memos_available != null || cycle.memos_cited != null) && (
                    <span className="text-violet-400" title="memos available / cited in last cycle">
                      {cycle.memos_available ?? 0} memo{(cycle.memos_available ?? 0) !== 1 ? 's' : ''} avail, {cycle.memos_cited ?? 0} cited
                    </span>
                  )}
                </div>
                {cycle.summary_text && (
                  <p className="text-[10px] text-neutral-500 mt-1">{cycle.summary_text}</p>
                )}
              </>
            )}
            {memoExits > 0 && (
              <div className="mt-2 pt-2 border-t border-neutral-800/50">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-[10px] text-neutral-500 uppercase tracking-wide">Memo pathway</span>
                  <span className="text-[10px] font-semibold text-violet-400 px-1.5 py-0.5 rounded border border-violet-500/30 bg-violet-500/10">
                    {memoExits} exit{memoExits !== 1 ? 's' : ''}
                  </span>
                  {memoEverCited?.ever_cited ? (
                    <span
                      className="text-[10px] font-semibold text-emerald-400 px-1.5 py-0.5 rounded border border-emerald-500/30 bg-emerald-500/10"
                      title={memoEverCited.first_cited_at ? `first cited ${timeAgo(memoEverCited.first_cited_at)}` : undefined}
                    >
                      ever cited ✓
                    </span>
                  ) : memoEverCited != null ? (
                    <span className="text-[10px] text-neutral-500 px-1.5 py-0.5 rounded border border-neutral-700/50">
                      not yet cited
                    </span>
                  ) : null}
                  {recentCycleMemoStats.length > 0 && (
                    <div className="flex flex-wrap gap-1">
                      {recentCycleMemoStats.slice(0, 5).map((s, i) => (
                        <span
                          key={s.cycle_id ?? i}
                          className="text-[10px] text-neutral-400 px-1.5 py-0.5 rounded border border-neutral-700/50 bg-neutral-800/50"
                          title={s.started_at ? `cycle ${timeAgo(s.started_at)}` : undefined}
                        >
                          {s.memos_cited}/{s.memos_available}
                        </span>
                      ))}
                    </div>
                  )}
                </div>
              </div>
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
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4">
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
  SCOPE_OVERFLOW: 'scope',
};

function FailedCard({ value, groups, spark, loading }: {
  value: number; groups?: FailureGroup[]; spark: SparkPoint[]; loading: boolean;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const fill = SPARK_COLORS['red'];
  const topGroups = (groups ?? []).slice(0, 3);
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4">
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
  const sparkPoints = useMemo(() => (trendHistory ?? []).map(p => ({ v: p.success_rate ?? 0 })), [trendHistory]);
  if (loading) return <Skeleton className="h-20" />;
  const TrendIcon = trend === 'improving' ? TrendingUp : trend === 'declining' ? TrendingDown : Minus;
  const trendColor = trend === 'improving' ? 'text-emerald-400' : trend === 'declining' ? 'text-red-400' : 'text-neutral-500';
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4">
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
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4">
      <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">Conversion Rate</p>
      <p className={`text-2xl font-bold font-mono ${color}`}>{pct != null ? `${pct}%` : '--'}</p>
      <p className="text-[10px] text-neutral-600 mt-1">PRs merged / tasks created</p>
    </div>
  );
}

function AcceptanceRateCard({ rate, loading }: {
  rate?: number | null;
  loading: boolean;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const pct = rate != null ? Math.round(rate * 100) : null;
  const color = pct == null ? 'text-neutral-500' : pct >= 60 ? 'text-emerald-400' : pct >= 30 ? 'text-amber-400' : 'text-red-400';
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4">
      <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">Acceptance Rate</p>
      <p className={`text-2xl font-bold font-mono ${color}`}>{pct != null ? `${pct}%` : '--'}</p>
      <p className="text-[10px] text-neutral-600 mt-1">PRs merged / (merged + rejected)</p>
    </div>
  );
}

function CostCard({ totalCost, trendHistory, loading }: {
  totalCost?: number | null;
  trendHistory?: { period_cost_usd?: number | null }[];
  loading: boolean;
}) {
  const sparkPoints = useMemo(() => (trendHistory ?? []).map(p => ({ v: p.period_cost_usd ?? 0 })), [trendHistory]);
  if (loading) return <Skeleton className="h-20" />;
  const fill = SPARK_COLORS['blue'];
  const display = totalCost == null ? '--' : totalCost < 10 ? `$${totalCost.toFixed(2)}` : `$${Math.round(totalCost)}`;
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4">
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

const MIN_SR_CATCH_FINDINGS = 5;

function deriveReviewerFindingCount(trends: SrTrendWithFixIterations | null | undefined): number {
  const categories = trends?.categories;
  if (!categories) return 0;
  return Object.values(categories).reduce((sum, cat) => sum + (cat.current_human_count ?? 0), 0);
}

function SrCatchRateCard({ rate, trends, loading, onScrollToReviewQuality }: {
  rate?: number | null;
  trends?: SrTrendWithFixIterations | null;
  loading: boolean;
  onScrollToReviewQuality?: () => void;
}) {
  if (loading) return <Skeleton className="h-20" />;
  const reviewerFindingCount = deriveReviewerFindingCount(trends);
  if (rate == null || reviewerFindingCount < MIN_SR_CATCH_FINDINGS) {
    return (
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4 cursor-pointer hover:border-neutral-700/80 transition-colors" onClick={onScrollToReviewQuality}>
        <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">SR Catch Rate</p>
        <p className="text-2xl font-bold font-mono text-neutral-500">too small</p>
        <p className="text-[10px] text-neutral-600 mt-1">n={reviewerFindingCount} findings</p>
      </div>
    );
  }

  const overallPrevRate = trends?.previous_overall_hit_rate ?? null;
  const delta = overallPrevRate != null ? rate - overallPrevRate : null;
  const verdict = trends?.overall_trend_verdict ?? null;
  const significant = trends?.overall_trend_significant ?? null;

  const srAloneRate = trends?.overall_sr_alone_hit_rate ?? null;
  const prevSrAloneRate = trends?.previous_overall_sr_alone_hit_rate ?? null;
  const srAloneDelta = srAloneRate != null && prevSrAloneRate != null ? srAloneRate - prevSrAloneRate : null;
  const srAloneVerdict = trends?.overall_sr_alone_trend_verdict ?? null;
  const srAloneSignificant = trends?.overall_sr_alone_trend_significant ?? null;
  const reviewerLift = srAloneRate != null ? rate - srAloneRate : null;

  const pct = Math.round(rate * 100);
  const color = rate >= 0.7 ? 'text-emerald-400' : rate >= 0.4 ? 'text-amber-400' : 'text-red-400';
  const deltaPp = delta != null ? Math.round(delta * 100) : 0;
  // Use backend significance-gated verdict when available; fall back to neutral when significance unknown
  const improving = verdict === 'improving' || (verdict == null && significant !== false && delta != null && delta > 0.01);
  const declining = verdict === 'declining' || (verdict == null && significant !== false && delta != null && delta < -0.01);
  const deltaColor = improving ? 'text-emerald-500' : declining ? 'text-red-500' : 'text-neutral-500';
  const deltaIcon = improving ? '↑' : declining ? '↓' : '→';
  // De-emphasize delta when backend reports non-significant change
  const deltaOpacity = (verdict === 'stable' || significant === false) ? 'opacity-50' : '';

  const srAlonePct = srAloneRate != null ? Math.round(srAloneRate * 100) : null;
  const reviewerLiftPp = reviewerLift != null ? Math.round(reviewerLift * 100) : null;
  const srAloneDeltaPp = srAloneDelta != null ? Math.round(srAloneDelta * 100) : null;
  const srAloneImproving = srAloneVerdict === 'improving' || (srAloneVerdict == null && srAloneSignificant !== false && srAloneDelta != null && srAloneDelta > 0.01);
  const srAloneDeclining = srAloneVerdict === 'declining' || (srAloneVerdict == null && srAloneSignificant !== false && srAloneDelta != null && srAloneDelta < -0.01);
  const srAloneDeltaColor = srAloneImproving ? 'text-emerald-500' : srAloneDeclining ? 'text-red-500' : 'text-neutral-500';
  const srAloneDeltaOpacity = (srAloneVerdict === 'stable' || srAloneSignificant === false) ? 'opacity-50' : '';

  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 sm:p-4 cursor-pointer hover:border-neutral-700/80 transition-colors" onClick={onScrollToReviewQuality}>
      <div className="flex items-center justify-between mb-1">
        <p className="text-[10px] text-neutral-500 uppercase tracking-wide">SR Catch Rate</p>
        {trends?.is_stale && (
          <span className="text-[9px] text-amber-400 font-mono" title={`Trend data is stale — last computed ${trends.computed_at ? new Date(trends.computed_at).toLocaleDateString() : 'unknown'}`}>stale</span>
        )}
        {trends?.data_sufficient === false && !trends?.is_stale && (
          <span className="text-[9px] text-amber-500 font-mono" title="Insufficient reviewer-finding data — displayed numbers are frozen">frozen</span>
        )}
      </div>
      <div className="flex items-center gap-2">
        <p className={`text-2xl font-bold font-mono ${color}`}>{pct}%</p>
        {delta != null && (
          <span className={`text-sm font-semibold ${deltaColor} ${deltaOpacity}`}>
            {deltaIcon} {deltaPp >= 0 ? '+' : ''}{deltaPp}pp
          </span>
        )}
      </div>
      {srAlonePct != null ? (
        <p className="text-[10px] font-mono mt-0.5" title="author = SR-alone catch rate (excludes reviewer saves); +reviewer = additional coverage from fresh-reviewer pass">
          <span className={srAloneDeclining ? 'text-red-400' : srAloneImproving ? 'text-emerald-400' : 'text-neutral-400'}>
            author {srAlonePct}%
          </span>
          {srAloneDeltaPp != null && (
            <span className={`${srAloneDeltaColor} ${srAloneDeltaOpacity} ml-0.5`}>
              {srAloneDeltaPp >= 0 ? '+' : ''}{srAloneDeltaPp}pp
            </span>
          )}
          {reviewerLiftPp != null && reviewerLiftPp > 0 && (
            <span className="text-neutral-500"> / +reviewer {reviewerLiftPp}pp</span>
          )}
        </p>
      ) : (
        <p className="text-[10px] text-neutral-600 mt-1">n={reviewerFindingCount} findings</p>
      )}
      {srAlonePct != null && (
        <p className="text-[10px] text-neutral-600">n={reviewerFindingCount} findings</p>
      )}
    </div>
  );
}
