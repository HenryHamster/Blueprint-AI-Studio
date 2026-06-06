import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { AlertTriangle } from 'lucide-react';
import { apiFetch, fetchTask, type PromptFingerprintAnalyticsResponse, type VersionLifecycleResponse, type SrReviewerDisagreementsEnvelope } from '../api';
import { VersionLifecyclePanel } from './VersionLifecyclePanel';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { RecentErrorsWidget } from './RecentErrorsWidget';
import { DirectionAutoPauseWidget } from './DirectionAutoPauseWidget';
import { PRMonitorSection, type OpenPR } from './PRMonitorSection';
import { MediumScopeWidget, type MediumScopeMeasurement } from './MediumScopeWidget';
import { RemediationFunnelSection } from './RemediationFunnelSection';
import { FailureStageWidget } from './FailureStageWidget';
import { TaskDurationWidget, type TaskDurationStats } from './TaskDurationWidget';
import { FixLoopConvergenceWidget } from './FixLoopConvergenceWidget';
import { OuterReviewLoopConvergenceWidget } from './OuterReviewLoopConvergenceWidget';
import { ScopeConversionRatesSection } from './ScopeConversionRatesSection';
import { ScopeTrendsSection, type ScopeBaselinesResponse } from './ScopeTrendsSection';
import { ProviderCostPanel } from './ProviderCostPanel';
import { ProviderModelBreakdownPanel } from './ProviderModelBreakdownPanel';
import { LlmCostBreakdownPanel } from './LlmCostBreakdownPanel';
import { EffortEfficiencyPanel } from './EffortEfficiencyPanel';
import { DriftSignalsCard } from './DriftSignalsCard';
import { PoolAccountHealthPanel, type AccountHealthEntry } from './PoolAccountHealthPanel';
import { SystemMaturityCard, type SystemMaturityRow } from './SystemMaturityCard';
import { DirectionPortfolioSection, type DirectionProgress } from './DirectionPortfolioSection';
import { type SrMissedTask, type DirectionSrEntry } from './SrTrendBreakdown';
import { ReviewQualitySection, type SrFixIterationBucket, type SrTrendWithFixIterations } from './ReviewQualitySection';
import { SrDiagnosticsStack, type BlindSpotsData, type DirectionSrBreakdownData } from './SrDiagnosticsStack';
import {
  MetricCard,
  FailedCard,
  SuccessRateCard,
  ConversionRateCard,
  AcceptanceRateCard,
  CostCard,
  SrCatchRateCard,
} from './DashboardMetricCards';
import { RecurringPatternsSection } from './RecurringPatternsSection';
import { TAXONOMY_ORDER } from './dashboardConstants';
import { ViewportDebtPanel } from './ViewportDebtPanel';
import { SystemStatusCard } from './SystemStatusCard';
import { PipelineEfficiencyPanel } from './PipelineEfficiencyPanel';
import { WarmReuseVsCostTrend } from './WarmReuseVsCostTrend';
import { FailureClassPill } from './FailureClassPill';
import { SystemHealthCard, type HealthOverviewResponse } from './SystemHealthCard';
import { AnomalyThrottleBanner } from './AnomalyThrottleBanner';
import { FailureTaxonomyBreakdown } from './FailureTaxonomyBreakdown';
import { ReviewRejectionBreakdown } from './ReviewRejectionBreakdown';
import { CostOfFailuresWidget } from './CostOfFailuresWidget';
import { ResearchYieldFunnel } from './ResearchYieldFunnel';
import { OpenResidualCasesSection } from './OpenResidualCasesSection';
import { RejectionPatternSummary } from './RejectionPatternSummary';
import { RecentMergesTimeline } from './RecentMergesTimeline';
import { MergeGateFleetHealthCard } from './MergeGateFleetHealthCard';
import { DependencyHealthCard } from './DependencyHealthCard';
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

// SrFixIterationBucket and SrTrendWithFixIterations are defined and exported from ReviewQualitySection
export type { SrFixIterationBucket, SrTrendWithFixIterations };

// DirectionSrEntry is re-exported from SrTrendBreakdown (canonical model)
export type { DirectionSrEntry };

export interface SparkPoint {
  active: number;
  queued: number;
  failed: number;
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
  onPanelReviewPr?: (pr: OpenPR) => void;
  userRole?: 'admin' | 'user' | null;
}

// ---------- component ----------

const WINDOW_OPTIONS = [
  { label: '6h', value: 6 },
  { label: '12h', value: 12 },
  { label: '24h', value: 24 },
  { label: '48h', value: 48 },
  { label: '7d', value: 168 },
];

export function DashboardHome({ tasks, selectedProjectId, projects, onViewTask, onDirectionClick, onCategoryClick, onScopeClick, onConfigureTargets, onSpawnOrphanFix, onPanelReviewPr, userRole }: DashboardHomeProps) {
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

  // Reset project-scoped state synchronously on a project/window/category
  // switch, BEFORE the poller below fires its leading fetch. Declared ahead of
  // usePoller and sharing the same trigger (`fetchData` changes iff those
  // inputs change) so React runs this reset first, then the leading poll.
  useEffect(() => {
    openPRsRequestIdRef.current += 1;
    versionLifecycleRequestIdRef.current += 1;
    resetProjectScopedState();
    // fetchData is the canonical "inputs changed" signal; resetProjectScopedState
    // is stable. Both are listed so the reset runs before the poller's leading
    // fetch on any project/window/category switch.
  }, [fetchData, resetProjectScopedState]);

  // Coordinated polling on the shared 30s clock. Pauses when the tab is hidden
  // and catches up on return; ticks align with every other 30s poller so
  // apiFetch's in-flight de-dup can collapse overlapping identical GETs.
  //
  // Each tick starts a fresh "latest" request by reading the CURRENT id from
  // the ref (via increment) rather than a stale id captured at setup time.
  // Capturing the id in the closure froze polling after any manual refetch
  // (which bumps the ref): fetchData's `requestId === ref.current` guards then
  // permanently failed and the dashboard silently stopped applying responses.
  usePoller(
    () => fetchData(++dashboardRequestIdRef.current),
    { intervalMs: DEFAULT_POLL_MS, deps: [fetchData] },
  );

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
    const selfSufficientCount = digest?.direction_portfolio?.self_sufficient_count ?? dirs.filter(d => {
      const history = d.research_history ?? [];
      const histHasAttrib = history.some(e => e.autonomous_prs_merged != null);
      const lifetimeAutonomous = histHasAttrib
        ? history.reduce((s, e) => s + (e.autonomous_prs_merged ?? 0), 0) + ((d.history_aggregate?.total_autonomous_prs_merged as number | null) ?? 0)
        : null;
      const operatorActions = (d.history_aggregate?.total_operator_actions as number | null) ?? null;
      return (lifetimeAutonomous ?? 0) >= 10 && operatorActions === 0;
    }).length;
    return { directions: dirs, totalDirPRs, hasAttribution, totalAutonomousPRs, totalAssistedPRs, selfSufficientCount };
  }, [digest?.direction_progress, digest?.direction_portfolio]);
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
      {selectedProjectId && <VersionLifecyclePanel data={versionLifecycle} loading={loading} projectId={selectedProjectId} onRefresh={refreshVersionLifecycle} userRole={userRole} />}

      {/* Pipeline Efficiency */}
      <PipelineEfficiencyPanel projectId={selectedProjectId} />

      {/* Warm-reuse vs cost-per-merged-PR trend (baseline reference line) */}
      <WarmReuseVsCostTrend projectId={selectedProjectId} />

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
          {!loading && <FailureTaxonomyBreakdown digest={digest} taxSparkMap={taxSparkMap} />}

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
              <div className="mt-3">
                <OuterReviewLoopConvergenceWidget
                  trendDays={fixLoopTrendDays}
                  projectId={selectedProjectId ?? undefined}
                  windowHours={windowHours}
                />
              </div>
            </div>
          )}

          {/* Section 1d2: Review Rejection Reason Breakdown */}
          {!loading && <ReviewRejectionBreakdown digest={digest} />}

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
          {!loading && <CostOfFailuresWidget digest={digest} />}

          {/* Section 1e: Pipeline Filter Funnel */}
          {!loading && <ResearchYieldFunnel digest={digest} />}

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
          {!loading && <OpenResidualCasesSection digest={digest} />}

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
          <RejectionPatternSummary digest={digest} />

          {/* Section 2: Direction Portfolio Health */}
          <DirectionPortfolioSection
            directions={directions}
            loading={loading}
            directionPortfolio={digest?.direction_portfolio}
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

          {/* Section 2h: Effort-tier & actual-model efficiency (cost vs landed) */}
          {!loading && <EffortEfficiencyPanel data={digest?.effort_model_efficiency} />}

          {/* Drift Signals */}
          {!!selectedProjectId && <DriftSignalsCard projectId={selectedProjectId} />}

        </div>

        {/* Section 4: Recent Merge Timeline */}
        <div className="lg:col-span-1">
          <RecentMergesTimeline mergeEvents={mergeEvents} loading={loading} />

          {/* Fleet-wide no-CI merge-gate verification health (read-only) */}
          {digest?.merge_gate_fleet_health && digest.merge_gate_fleet_health.project_count > 0 && (
            <div className="mt-4">
              <MergeGateFleetHealthCard data={digest.merge_gate_fleet_health} />
            </div>
          )}

          {/* Dependency/foundation-resolution health (read-only) */}
          {digest?.dependency_health_signals && (
            <div className="mt-4">
              <DependencyHealthCard data={digest.dependency_health_signals} />
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
            onPanelReviewPr={onPanelReviewPr}
          />
        </div>
      </div>
    </div>
  );
}
