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
import { timeAgo, formatDurationSec, parseTimestamp } from '../utils/time';
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
import { SrTrendBreakdown, SrMissedRetrospective, SrReviewerDisagreementsPanel, EdgesTrendBreakdown, type SrTrendData, type SrMissedTask, type SrReviewerDisagreementsData, type EdgesTrend } from './SrTrendBreakdown'; // EdgesTrend used in DigestData
import { TAXONOMY_COLORS, TAXONOMY_LABELS, TAXONOMY_ORDER } from './dashboardConstants';
import { ManualInterventionPanel } from './PipelineRegressionsPanel';
import { RecentlyResolvedRegressionsPanel } from './RecentlyResolvedRegressionsPanel';
import type { Task } from '../types/task';
import type { ProjectDiagnostics } from '../types/diagnostics';

// ---------- local types ----------

interface IssueItem {
  severity: 'critical' | 'warning' | 'info';
  icon: typeof AlertTriangle;
  title: string;
  detail?: string;
  onClick?: () => void;
}

export interface PipelineRegression {
  signal_name: string;
  detected_at: string;
  current_rate?: number | null;
  prior_rate?: number | null;
  severity: 'critical' | 'warning' | 'info';
  resolved: boolean;
  resolved_at?: string | null;
  spawned_task_id?: string | null;
  spawned_task_title?: string | null;
  spawned_task_status?: string | null;
  regression_class?: string | null;
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
  } | null;
  self_review_prediction_accuracy_trends?: SrTrendData | null;
  self_review_skip_rate?: {
    overall_rate: number;
    skipped_count: number;
    sample_size: number;
    by_task_type: Record<string, { rate: number; skipped_count: number; sample_size: number }>;
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
  median_cost_per_merged_pr?: number | null;
  prediction_calibration_factor?: number | null;
  prediction_calibration_note?: string | null;
  prediction_calibration_sample_count?: number | null;
  rejection_pattern_summary?: {
    total_rejected: number;
    top_notes: { snippet: string; count: number }[];
  } | null;
  tasks_truncated?: boolean;
}

// Per-direction SR catch-rate breakdown types (endpoint: GET /projects/{id}/direction-sr-breakdown)
interface DirectionSrCategoryStats {
  human_count: number;
  hit_count: number;
  miss_rate: number | null;
  miss_examples?: string[];
}

export interface DirectionSrEntry {
  direction_id: string;
  direction_text: string;
  sample_size: number;
  overall_catch_rate: number | null;
  categories: Record<string, DirectionSrCategoryStats>;
  worst_category: string | null;
}

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

// ---------- props ----------

export interface DashboardHomeProps {
  tasks: Task[];
  selectedProjectId: string | null;
  onViewTask: (task: Task) => void;
  onDirectionClick?: (directionId: string) => void;
  onCategoryClick?: (category: string) => void;
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
        className={`inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-0.5 rounded border ${colorClass} cursor-pointer`}
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

// ---------- issue row ----------

const _SEVERITY_BORDER: Record<string, string> = { critical: 'border-l-red-500', warning: 'border-l-amber-500', info: 'border-l-blue-500' };
const _SEVERITY_ICON_COLOR: Record<string, string> = { critical: 'text-red-400', warning: 'text-amber-400', info: 'text-blue-400' };

function IssueRow({ severity, icon: Icon, title, detail, onClick }: IssueItem) {
  const Tag = onClick ? 'button' : 'div';
  return (
    <Tag
      onClick={onClick}
      className={`w-full text-left bg-neutral-900/50 border border-neutral-800/50 border-l-2 ${_SEVERITY_BORDER[severity]} rounded-lg p-3 flex items-start gap-2 ${onClick ? 'cursor-pointer hover:bg-neutral-800/50 transition-colors' : ''}`}
    >
      <Icon className={`w-4 h-4 mt-0.5 shrink-0 ${_SEVERITY_ICON_COLOR[severity]}`} />
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

  if (diagnostics?.circuit_breaker && diagnostics.circuit_breaker.state !== 'CLOSED') {
    items.push({
      severity: 'critical',
      icon: AlertTriangle,
      title: `Circuit breaker ${diagnostics.circuit_breaker.state} (${diagnostics.circuit_breaker.failure_count} failures)`,
    });
  }

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

  if (digest && digest.pending_approval_count > 0) {
    items.push({
      severity: 'info',
      icon: Clock,
      title: `${digest.pending_approval_count} task${digest.pending_approval_count !== 1 ? 's' : ''} awaiting approval`,
    });
  }

  if (digest && (digest.daily_cost_ceiling_usd ?? 0) > 0) {
    const ceiling = digest.daily_cost_ceiling_usd ?? 0;
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

  if (diagnostics?.zone_health?.exhausted && diagnostics.zone_health.exhausted.length > 0) {
    const cooldownRecovery = diagnostics.zone_health.cooldown_recovery ?? {};
    const now = Date.now();
    const zoneDetails = diagnostics.zone_health.exhausted.map(zone => {
      const expiresAt = cooldownRecovery[zone];
      if (expiresAt) {
        const minsLeft = Math.ceil((parseTimestamp(expiresAt).getTime() - now) / 60000);
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

  const tax = digest?.failure_taxonomy_distribution;
  if (tax && tax.total_failures >= 3 && tax.dominant_category) {
    const cat = tax.categories[tax.dominant_category];
    if (cat && cat.percentage > 50) {
      const remediation: Record<string, string> = {
        scope_related: 'Review task decomposition and scope budgets to keep work within size limits.',
        review_quality: 'Strengthen self-review and lint configuration to catch defects earlier.',
        infra_related: 'Check infrastructure health (zones, provisioning, external APIs).',
        code_defect: 'Review agent code-quality patterns and prompt guidance.',
      };
      const label = TAXONOMY_LABELS[tax.dominant_category] ?? tax.dominant_category;
      items.push({
        severity: 'info',
        icon: AlertTriangle,
        title: `${label} failures dominate (${Math.round(cat.percentage)}% of ${tax.total_failures})`,
        detail: remediation[tax.dominant_category],
      });
    }
  }

  return items;
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

export function DashboardHome({ tasks, selectedProjectId, onViewTask, onDirectionClick, onCategoryClick }: DashboardHomeProps) {
  const [digest, setDigest] = useState<DigestData | null>(null);
  const [diagnostics, setDiagnostics] = useState<ProjectDiagnostics | null>(null);
  const [openPRs, setOpenPRs] = useState<OpenPR[]>([]);
  const [measurementData, setMeasurementData] = useState<MediumScopeMeasurement | null>(null);
  const [scopeBaselines, setScopeBaselines] = useState<ScopeBaselinesResponse | null>(null);
  const [scopeTrendsOpen, setScopeTrendsOpen] = useState(false);
  const [accountHealth, setAccountHealth] = useState<AccountHealthEntry[] | null>(null);
  const [systemMaturity, setSystemMaturity] = useState<SystemMaturityRow[] | null>(null);
  const [taskDurationStats, setTaskDurationStats] = useState<TaskDurationStats | null>(null);
  const [blindSpots, setBlindSpots] = useState<BlindSpotsData | null>(null);
  const [srRecentMisses, setSrRecentMisses] = useState<SrMissedTask[] | null>(null);
  const [srReviewerDisagreements, setSrReviewerDisagreements] = useState<SrReviewerDisagreementsData | null>(null);
  const [directionSrBreakdown, setDirectionSrBreakdown] = useState<DirectionSrBreakdownData | null>(null);
  const [loading, setLoading] = useState(true);
  const [fixLoopTrendDays, setFixLoopTrendDays] = useState<7 | 14 | 30>(7);
  const [openPRsRefreshing, setOpenPRsRefreshing] = useState(false);
  const [lastFetchError, setLastFetchError] = useState(false);
  const [showAllIssues, setShowAllIssues] = useState(false);
  const [windowHours, setWindowHours] = useState(24);
  const sparkRef = useRef<SparkPoint[]>([]);
  const [sparkData, setSparkData] = useState<SparkPoint[]>([]);

  // Fetch open PRs only — used by manual refresh button
  const fetchOpenPRs = useCallback(async () => {
    if (!selectedProjectId) return;
    setOpenPRsRefreshing(true);
    try {
      const res = await apiFetch(`/api/admin/open-prs?project_id=${encodeURIComponent(selectedProjectId)}`);
      if (res.ok) {
        const data = await res.json();
        setOpenPRs(data.open_prs ?? []);
      }
    } catch { /* silent */ }
    finally {
      setOpenPRsRefreshing(false);
    }
  }, [selectedProjectId]);

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
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-recent-misses`);
            if (res.ok) {
              const data = await res.json();
              setSrRecentMisses(data.misses ?? []);
            }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/direction-sr-breakdown`);
            if (res.ok) setDirectionSrBreakdown(await res.json());
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
    );

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
  const hasAttribution = directions.some(d => d.autonomous_prs_merged != null || d.assisted_prs_merged != null);
  const totalAutonomousPRs = hasAttribution ? directions.reduce((s, d) => s + (d.autonomous_prs_merged ?? 0), 0) : null;
  const totalAssistedPRs = hasAttribution ? directions.reduce((s, d) => s + (d.assisted_prs_merged ?? 0), 0) : null;
  const selfSufficientCount = directions.filter(d => {
    const history = d.research_history ?? [];
    const histHasAttrib = history.some(e => e.autonomous_prs_merged != null);
    const lifetimeAutonomous = histHasAttrib
      ? history.reduce((s, e) => s + (e.autonomous_prs_merged ?? 0), 0) + ((d.history_aggregate?.total_autonomous_prs_merged as number | null) ?? 0)
      : null;
    const operatorActions = (d.history_aggregate?.total_operator_actions as number | null) ?? null;
    return (lifetimeAutonomous ?? 0) >= 10 && operatorActions === 0;
  }).length;
  const productPct = digest?.overall_product_ratio != null ? Math.round(digest.overall_product_ratio * 100) : null;

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

      {/* Pipeline Regressions */}
      {selectedProjectId && <PipelineRegressionsPanel projectId={selectedProjectId} />}

      {/* Manual Intervention Cases */}
      {selectedProjectId && <ManualInterventionPanel projectId={selectedProjectId} />}

      {/* Recently Resolved Regressions */}
      {selectedProjectId && <RecentlyResolvedRegressionsPanel projectId={selectedProjectId} />}

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
            </div>
          </section>

          {/* Section 1b: Failure Breakdown by Class */}
          {!loading && digest?.failure_groups && digest.failure_groups.length > 0 && (
            <section>
              <div className="flex flex-wrap gap-1.5">
                {digest.failure_groups.map(g => {
                  const classSpark = (digest.trend_history ?? []).map(p => ({ v: p.failed_by_class?.[g.failure_class] ?? 0 }));
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
                  const taxSpark = (digest.trend_history ?? []).map(p => ({ v: p.failed_by_taxonomy?.[k] ?? 0 }));
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
              <div className="flex justify-end gap-0.5 mb-1">
                {([7, 14, 30] as const).map(d => (
                  <button
                    key={d}
                    onClick={() => setFixLoopTrendDays(d)}
                    className={`text-[10px] px-2 py-0.5 rounded transition-colors ${fixLoopTrendDays === d ? 'bg-neutral-700 text-neutral-200' : 'text-neutral-500 hover:text-neutral-300'}`}
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
          {!loading && (digest?.review_accuracy != null || digest?.self_review_effectiveness != null || digest?.self_review_prediction_accuracy != null || digest?.self_review_prediction_accuracy_trends != null || digest?.self_review_skip_rate != null || digest?.fresh_reviewer_effectiveness != null) && (
            <section>
              <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-2">Review Quality</p>
              <div className="flex flex-wrap gap-3">
                {digest?.review_accuracy?.precision != null && (
                  <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[110px]">
                    <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-0.5">Precision</p>
                    <p className={`text-xl font-bold font-mono ${digest.review_accuracy.precision >= 0.8 ? 'text-emerald-400' : digest.review_accuracy.precision >= 0.6 ? 'text-amber-400' : 'text-red-400'}`}>
                      {Math.round(digest.review_accuracy.precision * 100)}%
                    </p>
                    <p className="text-[9px] text-neutral-600 mt-0.5">{digest.review_accuracy.total_reviews} reviews</p>
                  </div>
                )}
                {digest?.review_accuracy?.recall != null && (
                  <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[110px]">
                    <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-0.5">Recall</p>
                    <p className={`text-xl font-bold font-mono ${digest.review_accuracy.recall >= 0.8 ? 'text-emerald-400' : digest.review_accuracy.recall >= 0.6 ? 'text-amber-400' : 'text-red-400'}`}>
                      {Math.round(digest.review_accuracy.recall * 100)}%
                    </p>
                    <p className="text-[9px] text-neutral-600 mt-0.5">{digest.review_accuracy.total_approvals} approvals</p>
                  </div>
                )}
                {digest?.self_review_effectiveness?.with_self_review_approval_rate != null && (
                  <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[130px]">
                    <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-0.5">SR Approval Lift</p>
                    {(() => {
                      const sre = digest.self_review_effectiveness ?? {};
                      const withSR = sre.with_self_review_approval_rate ?? 0;
                      const withoutSR = sre.without_self_review_approval_rate;
                      const lift = withoutSR != null ? withSR - withoutSR : null;
                      return (
                        <>
                          <p className={`text-xl font-bold font-mono ${withSR >= 0.7 ? 'text-emerald-400' : withSR >= 0.5 ? 'text-amber-400' : 'text-red-400'}`}>
                            {Math.round(withSR * 100)}%
                          </p>
                          {lift != null && (
                            <p className={`text-[9px] mt-0.5 ${lift > 0 ? 'text-emerald-500' : lift < 0 ? 'text-red-500' : 'text-neutral-600'}`}>
                              {lift > 0 ? '+' : ''}{Math.round(lift * 100)}pp vs no SR
                            </p>
                          )}
                        </>
                      );
                    })()}
                  </div>
                )}
                {digest?.self_review_prediction_accuracy?.prediction_hit_rate != null && (
                  <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[110px]">
                    <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-0.5">SR Prediction Hit</p>
                    <p className={`text-xl font-bold font-mono ${digest.self_review_prediction_accuracy.prediction_hit_rate >= 0.7 ? 'text-emerald-400' : digest.self_review_prediction_accuracy.prediction_hit_rate >= 0.4 ? 'text-amber-400' : 'text-red-400'}`}>
                      {Math.round(digest.self_review_prediction_accuracy.prediction_hit_rate * 100)}%
                    </p>
                    {digest.self_review_prediction_accuracy.false_alarm_rate != null && (
                      <p className="text-[9px] text-neutral-600 mt-0.5">{Math.round(digest.self_review_prediction_accuracy.false_alarm_rate * 100)}% false alarms</p>
                    )}
                  </div>
                )}
                {digest?.self_review_skip_rate != null && (() => {
                  const skip = digest.self_review_skip_rate;
                  const rate = skip.overall_rate;
                  const color = rate <= 0.05 ? 'text-emerald-400' : rate <= 0.15 ? 'text-amber-400' : 'text-red-400';
                  const byTypeEntries = Object.entries(skip.by_task_type ?? {});
                  const tooltip = byTypeEntries.length
                    ? byTypeEntries.map(([t, b]) => `${t}: ${Math.round(b.rate * 100)}% (n=${b.sample_size})`).join('\n')
                    : undefined;
                  return (
                    <div
                      className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[110px]"
                      title={tooltip}
                    >
                      <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-0.5">SR Skip Rate</p>
                      <p className={`text-xl font-bold font-mono ${color}`}>
                        {Math.round(rate * 100)}%
                      </p>
                      <p className="text-[9px] text-neutral-600 mt-0.5">n={skip.sample_size}</p>
                    </div>
                  );
                })()}
                <SrTrendBreakdown
                  trends={digest?.self_review_prediction_accuracy_trends}
                  variant="card"
                  onCategoryClick={onCategoryClick}
                />
                <EdgesTrendBreakdown trend={digest?.edges_trend} />
                {digest?.fresh_reviewer_effectiveness != null && (() => {
                  const fre = digest.fresh_reviewer_effectiveness!;
                  const n = fre.fresh_reviewer_sample_size;
                  const freRate = fre.fresh_reviewer_first_attempt_approval_rate;
                  const baseRate = fre.baseline_first_attempt_approval_rate;
                  const lift = freRate != null && baseRate != null ? freRate - baseRate : null;
                  const freColor = freRate != null ? (freRate >= 0.8 ? 'text-emerald-400' : freRate >= 0.5 ? 'text-amber-400' : 'text-red-400') : 'text-neutral-300';
                  const liftColor = lift == null ? 'text-neutral-400' : lift >= 0.05 ? 'text-emerald-400' : lift >= 0 ? 'text-amber-400' : 'text-red-400';
                  return (
                    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[130px]">
                      <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-0.5">FR Approval Lift</p>
                      {n < 5 ? (
                        <p className="text-xs text-neutral-500">n={n} — too small</p>
                      ) : (
                        <>
                          <p className={`text-xl font-bold font-mono ${freColor}`}>{freRate != null ? `${Math.round(freRate * 100)}%` : '--'}</p>
                          {lift != null && (
                            <p className={`text-[9px] mt-0.5 ${liftColor}`}>{lift >= 0 ? '+' : ''}{Math.round(lift * 100)}pp vs baseline</p>
                          )}
                          <p className="text-[9px] text-neutral-600 mt-0.5">n={n}</p>
                        </>
                      )}
                    </div>
                  );
                })()}
              </div>
            </section>
          )}

          {/* SR Recent Misses */}
          {!loading && srRecentMisses && srRecentMisses.length > 0 && (
            <section>
              <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-2">
                Recent SR Misses
                <span className="ml-1 text-neutral-600 font-normal normal-case tracking-normal">· {srRecentMisses.length} task{srRecentMisses.length !== 1 ? 's' : ''}</span>
              </p>
              <SrMissedRetrospective
                tasks={srRecentMisses}
                onTaskClick={(taskId) => {
                  const task = tasks.find(t => t.id === taskId);
                  if (task) onViewTask(task);
                }}
              />
            </section>
          )}

          {/* SR Blind Spots aggregation */}
          {!loading && blindSpots && blindSpots.themes.length > 0 && (
            <section>
              <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-2">
                SR Blind Spots
                <span className="ml-1 text-neutral-600 font-normal normal-case tracking-normal">· {blindSpots.total_distinct_themes} themes · {blindSpots.window_days}d</span>
              </p>
              <div className="flex flex-wrap gap-1.5">
                {blindSpots.themes.slice(0, 8).map((t, i) => (
                  <span
                    key={i}
                    className="inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-0.5 rounded border bg-violet-500/10 text-violet-400 border-violet-500/20"
                    title={t.theme}
                  >
                    <span className="truncate max-w-[160px]">{t.theme}</span>
                    <span className="font-bold shrink-0">×{t.count}</span>
                  </span>
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
              <div className="flex flex-col gap-1.5">
                {directionSrBreakdown.directions.map(d => {
                  const rate = d.overall_catch_rate;
                  const rateColor = rate == null ? 'text-neutral-500' : rate >= 0.7 ? 'text-emerald-400' : rate >= 0.4 ? 'text-amber-400' : 'text-red-400';
                  return (
                    <div
                      key={d.direction_id}
                      className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2"
                    >
                      <div className="flex items-baseline gap-2 mb-1">
                        <span
                          className={`font-mono font-bold tabular-nums text-sm ${rateColor}`}
                          title="overall SR catch rate for this direction"
                        >
                          {rate != null ? `${Math.round(rate * 100)}%` : '–'}
                        </span>
                        <span
                          className="text-[10px] text-neutral-300 truncate flex-1"
                          title={d.direction_text}
                          onClick={() => onDirectionClick?.(d.direction_id)}
                          role={onDirectionClick ? 'button' : undefined}
                          style={onDirectionClick ? { cursor: 'pointer' } : undefined}
                        >
                          {d.direction_text || d.direction_id}
                        </span>
                        <span className="text-[9px] text-neutral-600 shrink-0">n={d.sample_size}</span>
                      </div>
                      {d.worst_category && d.categories[d.worst_category] && (() => {
                        const worst = d.categories[d.worst_category!]!;
                        return (
                          <p className="text-[9px] text-neutral-500">
                            worst: <span className="text-orange-400 font-mono">{d.worst_category}</span>
                            {worst.miss_rate != null && (
                              <span className="ml-1 font-mono text-orange-400">{Math.round(worst.miss_rate * 100)}% miss</span>
                            )}
                          </p>
                        );
                      })()}
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
          />
        </div>
      </div>
    </div>
  );
}

// ---------- pipeline regressions panel ----------

const _REGRESSION_SEVERITY_BORDER: Record<string, string> = {
  critical: 'border-l-red-500',
  warning: 'border-l-amber-500',
  info: 'border-l-blue-500',
};
const _REGRESSION_STATUS_PILL: Record<string, string> = {
  IN_PROGRESS: 'bg-blue-500/15 text-blue-400 border-blue-500/30',
  PROVISIONING: 'bg-blue-500/15 text-blue-400 border-blue-500/30',
  EVALUATING: 'bg-amber-500/15 text-amber-400 border-amber-500/30',
  QUEUED: 'bg-neutral-700/30 text-neutral-400 border-neutral-700/30',
  COMPLETED: 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30',
  FAILED: 'bg-red-500/15 text-red-400 border-red-500/30',
};

export function PipelineRegressionsPanel({ projectId }: { projectId: string }) {
  const [regressions, setRegressions] = useState<PipelineRegression[]>([]);
  const [loaded, setLoaded] = useState(false);

  const fetchRegressions = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/admin/pipeline-regressions?project_id=${encodeURIComponent(projectId)}`);
      if (res.ok) {
        const data = await res.json();
        setRegressions(data.regressions ?? []);
        setLoaded(true);
      } else if (res.status !== 200) {
        setLoaded(true);
      }
    } catch { /* silent */ }
  }, [projectId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch sets state after await, intentional
    fetchRegressions();
    const id = setInterval(fetchRegressions, 60_000);
    return () => clearInterval(id);
  }, [fetchRegressions]);

  if (!loaded) return null;

  const active = regressions.filter(r => !r.resolved);
  const recentResolved = regressions.filter(r => r.resolved).slice(0, 2);
  const all = [...active, ...recentResolved];

  return (
    <section data-testid="pipeline-regressions-panel">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Pipeline Regressions
        {active.length > 0 && (
          <span className="ml-2 text-red-400 font-normal normal-case tracking-normal">· {active.length} active</span>
        )}
      </h2>
      {all.length === 0 ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 flex items-center gap-2 text-emerald-400 text-xs">
          <CheckCircle className="w-4 h-4 shrink-0" />
          No active pipeline regressions — system healthy
        </div>
      ) : (
        <div className="space-y-1.5">
          {all.map((r, i) => {
            const borderCls = r.resolved ? 'border-l-neutral-600' : (_REGRESSION_SEVERITY_BORDER[r.severity] ?? 'border-l-neutral-600');
            const statusCls = r.spawned_task_status ? (_REGRESSION_STATUS_PILL[r.spawned_task_status] ?? 'bg-neutral-700/30 text-neutral-400 border-neutral-700/30') : null;
            return (
              <div key={i} className={`bg-neutral-900/50 border border-neutral-800/50 border-l-2 ${borderCls} rounded-lg px-3 py-2 ${r.resolved ? 'opacity-60' : ''}`}>
                <div className="flex items-start justify-between gap-2">
                  <div className="min-w-0 flex-1">
                    <p className="text-xs text-neutral-200 font-medium">{r.signal_name}</p>
                    <div className="flex items-center gap-2 mt-0.5 flex-wrap">
                      <span className="text-[10px] text-neutral-500">{timeAgo(r.detected_at)}</span>
                      {r.current_rate != null && r.prior_rate != null && (
                        <span className="text-[10px] font-mono text-neutral-400">
                          {Math.round(r.prior_rate * 100)}% → {Math.round(r.current_rate * 100)}%
                        </span>
                      )}
                      {r.resolved && r.resolved_at && (
                        <span className="text-[10px] text-emerald-500">resolved {timeAgo(r.resolved_at)}</span>
                      )}
                    </div>
                    {r.spawned_task_title && (
                      <p className="text-[10px] text-neutral-500 truncate mt-0.5">{r.spawned_task_title}</p>
                    )}
                  </div>
                  <div className="flex flex-col items-end gap-1 shrink-0">
                    {statusCls && r.spawned_task_id ? (
                      <span className={`text-[9px] font-semibold px-1.5 py-0.5 rounded border ${statusCls}`} title={`Task ${r.spawned_task_id}`}>
                        {r.spawned_task_status}
                      </span>
                    ) : !r.spawned_task_id ? (
                      <span className="text-[9px] font-semibold px-1.5 py-0.5 rounded border bg-neutral-800/50 text-neutral-500 border-neutral-700/30">
                        no action
                      </span>
                    ) : null}
                    {!r.resolved && (
                      <span className={`text-[9px] px-1.5 py-0.5 rounded ${r.severity === 'critical' ? 'bg-red-500/10 text-red-400' : r.severity === 'warning' ? 'bg-amber-500/10 text-amber-400' : 'bg-blue-500/10 text-blue-400'}`}>
                        {r.severity}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            );
          })}
        </div>
      )}
    </section>
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
  if (loading) return <Skeleton className="h-20" />;
  const TrendIcon = trend === 'improving' ? TrendingUp : trend === 'declining' ? TrendingDown : Minus;
  const trendColor = trend === 'improving' ? 'text-emerald-400' : trend === 'declining' ? 'text-red-400' : 'text-neutral-500';
  const sparkPoints = (trendHistory ?? []).map(p => ({ v: p.success_rate ?? 0 }));
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
  if (loading) return <Skeleton className="h-20" />;
  const fill = SPARK_COLORS['blue'];
  const sparkPoints = (trendHistory ?? []).map(p => ({ v: p.period_cost_usd ?? 0 }));
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
