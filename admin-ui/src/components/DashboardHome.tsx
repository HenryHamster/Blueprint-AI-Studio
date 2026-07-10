import { useState, useCallback, useRef, useMemo } from 'react';
import { AlertTriangle } from 'lucide-react';
import { fetchTask } from '../api';
import { timeAgo } from '../utils/time';
import { RecentErrorsWidget } from './RecentErrorsWidget';
import { DirectionAutoPauseWidget } from './DirectionAutoPauseWidget';
import { PRMonitorSection, type OpenPR } from './PRMonitorSection';
import { MediumScopeWidget } from './MediumScopeWidget';
import { RemediationFunnelSection } from './RemediationFunnelSection';
import { FailureStageWidget } from './FailureStageWidget';
import { TaskDurationWidget } from './TaskDurationWidget';
import { FixLoopConvergenceWidget } from './FixLoopConvergenceWidget';
import { OuterReviewLoopConvergenceWidget } from './OuterReviewLoopConvergenceWidget';
import { ScopeConversionRatesSection } from './ScopeConversionRatesSection';
import { ScopeTrendsSection } from './ScopeTrendsSection';
import { ProviderCostPanel } from './ProviderCostPanel';
import { ProviderModelBreakdownPanel } from './ProviderModelBreakdownPanel';
import { LlmCostBreakdownPanel } from './LlmCostBreakdownPanel';
import { CapabilityTrialsPanel } from './CapabilityTrialsPanel';
import { EffortEfficiencyPanel } from './EffortEfficiencyPanel';
import { DriftSignalsCard } from './DriftSignalsCard';
import { PoolAccountHealthPanel } from './PoolAccountHealthPanel';
import { SystemMaturityCard } from './SystemMaturityCard';
import { DirectionPortfolioSection, type DirectionProgress } from './DirectionPortfolioSection';
import { type DirectionSrEntry } from './SrTrendBreakdown';
import { ReviewQualitySection } from './ReviewQualitySection';
import { SrDiagnosticsStack } from './SrDiagnosticsStack';
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
import { UILoadTimingPanel } from './UILoadTimingPanel';
import { SystemStatusCard } from './SystemStatusCard';
import { PipelineEfficiencyPanel } from './PipelineEfficiencyPanel';
import { WarmReuseVsCostTrend } from './WarmReuseVsCostTrend';
import { FailureClassPill } from './FailureClassPill';
import { SystemHealthCard } from './SystemHealthCard';
import { FleetCapacityCard } from './FleetCapacityCard';
import { AnomalyThrottleBanner } from './AnomalyThrottleBanner';
import { FailureTaxonomyBreakdown } from './FailureTaxonomyBreakdown';
import { ReviewRejectionBreakdown } from './ReviewRejectionBreakdown';
import { CostOfFailuresWidget } from './CostOfFailuresWidget';
import { ResearchYieldFunnel } from './ResearchYieldFunnel';
import { OpenResidualCasesSection } from './OpenResidualCasesSection';
import { RejectionPatternSummary } from './RejectionPatternSummary';
import { RecentMergesTimeline } from './RecentMergesTimeline';
import { MergeGateFleetHealthCard } from './MergeGateFleetHealthCard';
import { ParkWakeHealthCard } from './ParkWakeHealthCard';
import { DependencyHealthCard } from './DependencyHealthCard';
import { ImprovementLanesPanel } from './ImprovementLanesPanel';
import { AdversarialQualityPanel } from './AdversarialQualityPanel';
import type { Task } from '../types/task';
import type { Project } from '../types/project';
import { type DigestData as DigestDataBase, type FailureGroup } from '../types/digest';
import { useDashboardDigest, type SparkPoint } from '../hooks/useDashboardDigest';
import { useOpenPRs } from '../hooks/useOpenPRs';
import { useProjectMetrics } from '../hooks/useProjectMetrics';
import type { FailingTaskTypeEntry } from '../types/diagnostics';

// Dashboard reads DirectionPortfolioSection's `DirectionProgress` shape off
// `direction_progress`, so it instantiates the generic canonical type with it.
// Re-exported (along with FailureGroup) for dependents — SystemStatusCard and
// DashboardMetricCards — that import these from this module.
export type DigestData = DigestDataBase<DirectionProgress>;
export type { FailureGroup };

// ---------- local types ----------

// DirectionSrEntry is re-exported from SrTrendBreakdown (canonical model)
export type { DirectionSrEntry };

// SparkPoint is defined in useDashboardDigest and re-exported here for dependents
export type { SparkPoint };

// ---------- props ----------

export interface DashboardHomeProps {
  tasks: Task[];
  selectedProjectId: string | null;
  projects?: Project[];
  // Membership-scoped aggregate task counts from the tasks hook (/api/admin/tasks).
  // These cover the whole accessible fleet (global*) or the selected project
  // (scoped*), NOT the current paginated task page, so the Active/Queued cards
  // can render true counts instead of a slice. All optional/additive: when
  // undefined the cards fall back to deriving from the `tasks` page.
  globalActiveCount?: number;
  globalQueuedCount?: number;
  scopedActiveCount?: number | null;
  scopedQueuedCount?: number | null;
  onViewTask: (task: Task) => void;
  onDirectionClick?: (directionId: string) => void;
  onCategoryClick?: (category: string) => void;
  onScopeClick?: (scope: string) => void;
  onConfigureTargets?: () => void;
  onSpawnOrphanFix?: (pr: OpenPR) => void;
  onPanelReviewPr?: (pr: OpenPR) => void;
  gatedTaskTypes?: FailingTaskTypeEntry[];
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

export function DashboardHome({ tasks, selectedProjectId, projects, globalActiveCount, globalQueuedCount, scopedActiveCount, scopedQueuedCount, onViewTask, onDirectionClick, onCategoryClick, onScopeClick, onConfigureTargets, onSpawnOrphanFix, onPanelReviewPr, gatedTaskTypes }: DashboardHomeProps) {
  const [fixLoopTrendDays, setFixLoopTrendDays] = useState<7 | 14 | 30>(7);
  const [windowHours, setWindowHours] = useState(24);
  const [selectedSrCategory, setSelectedSrCategory] = useState<string>('');
  const [srDrillThroughError, setSrDrillThroughError] = useState<{ taskId: string; message: string } | null>(null);
  const [expandedBlindSpotThemes, setExpandedBlindSpotThemes] = useState<Set<string>>(new Set());
  const [scopeTrendsOpen, setScopeTrendsOpen] = useState(false);
  const reviewQualitySectionRef = useRef<HTMLElement>(null);

  // Reset project-scoped UI state synchronously during render on project switch
  // (React "adjust state during rendering" pattern).
  const [lastProjectIdForUi, setLastProjectIdForUi] = useState(selectedProjectId);
  if (lastProjectIdForUi !== selectedProjectId) {
    setLastProjectIdForUi(selectedProjectId);
    setExpandedBlindSpotThemes(new Set());
  }

  // Compute task counts from props before hook calls — used by useDashboardDigest for sparkline
  const pageActiveCount = tasks.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING').length;
  const pageQueuedCount = tasks.filter(t => t.status === 'QUEUED').length;
  const activeCount = selectedProjectId && scopedActiveCount != null
    ? scopedActiveCount
    : (globalActiveCount ?? pageActiveCount);
  const queuedCount = selectedProjectId && scopedQueuedCount != null
    ? scopedQueuedCount
    : (globalQueuedCount ?? pageQueuedCount);

  const { digest, digestStale, loading, lastFetchError, sparkData, refetchDigest } = useDashboardDigest(selectedProjectId, windowHours, activeCount, queuedCount);
  const { openPRs, openPRsStale, openPRsDegraded, openPRsIntegrationBranch, openPRsPromotionTarget, openPRsRefreshing, fetchOpenPRs } = useOpenPRs(selectedProjectId);
  const {
    measurementData,
    scopeBaselines,
    accountHealth,
    accountHealthServedStale,
    accountHealthGeneratedAt,
    systemMaturity,
    blindSpots,
    srRecentMisses,
    srRecentMissesTotal,
    srReviewerDisagreements,
    directionSrBreakdown,
    fingerprintAnalytics,
    taskDurationStats,
    healthOverview,
    fleetUtilization,
    agentServingShas,
  } = useProjectMetrics(selectedProjectId, windowHours, selectedSrCategory);

  const selectedProject = projects?.find(p => p.id === selectedProjectId);
  const srCatchRateTargets = selectedProject?.sr_catch_rate_targets;

  const handleCategoryClick = useCallback((category: string) => {
    setSelectedSrCategory(category);
    onCategoryClick?.(category);
  }, [onCategoryClick]);

  const handleScrollToReviewQuality = useCallback(() => {
    reviewQualitySectionRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }, []);

  const handleOpenDirections = selectedProjectId && onDirectionClick
    ? () => onDirectionClick('')
    : undefined;

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

  const digestIsStale = digestStale || Boolean(digest?.served_stale);
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
      {/* Digest staleness banner — shown when the backend served a stale
          fallback (served_stale) OR when the client-side fetch failed but
          prior digest data is preserved (digestStale). Distinct from the
          "Backend unreachable" banner, which only fires when no data exists. */}
      {(digestStale || digest?.served_stale) && !loading && (
        <div
          data-testid="digest-stale-banner"
          className="flex items-center gap-2 px-3 py-2 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs"
        >
          <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
          <span>
            Digest data may be stale
            {digest?.generated_at ? ` — last successful computation ${timeAgo(digest.generated_at)}` : ''}
          </span>
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
      <AnomalyThrottleBanner digest={digest} onRestored={refetchDigest} gatedTaskTypes={gatedTaskTypes} />

      {/* System Health — actionable issues with remediation hints */}
      <SystemHealthCard
        data={healthOverview}
        loading={loading}
        tasks={tasks}
        onViewTask={onViewTask}
        onOpenDirections={handleOpenDirections}
      />

      {/* Fleet Capacity — global slot utilization and saturation */}
      <FleetCapacityCard data={fleetUtilization} agentServingShas={agentServingShas} loading={loading} />

      {/* Viewport Debt — only relevant for the self-repo; backend always scans Blueprint's own admin-ui */}
      {selectedProjectId && selectedProject?.is_self_repo && <ViewportDebtPanel projectId={selectedProjectId} />}

      {/* Pipeline Efficiency */}
      <PipelineEfficiencyPanel projectId={selectedProjectId} />

      {/* Warm-reuse vs cost-per-merged-PR trend (baseline reference line) */}
      <WarmReuseVsCostTrend projectId={selectedProjectId} />

      {/* UI Load Timing — p50/p90 per surface */}
      <UILoadTimingPanel />

      {/* Challenge Gate Health — adversarial quality panel */}
      <AdversarialQualityPanel projectId={selectedProjectId} />

      {/* System Status */}
      <SystemStatusCard digest={digest} loading={loading} />

      {/* Orchestrator Errors widget: high-visibility observability for prod errors */}
      <RecentErrorsWidget />

      {/* Main grid: 2-col on large screens */}
      <div className="lg:grid lg:grid-cols-3 lg:gap-4 space-y-4 lg:space-y-0">
        <div className="lg:col-span-2 space-y-4">
          {/* Section 1: Task Status Overview */}
          <section>
            <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Task Status</h2>
            {!loading && digest?.tasks_truncated && (
              <div
                data-testid="tasks-truncated-banner"
                className="flex items-center gap-2 px-3 py-2 rounded-lg bg-amber-500/10 border border-amber-500/20 text-amber-400 text-xs mb-2"
              >
                <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
                <span>Metrics reflect a partial view — task window exceeded 5 000 rows</span>
              </div>
            )}
            <div className="grid grid-cols-2 sm:grid-cols-5 gap-2">
              <MetricCard label="Active" value={activeCount} color="emerald" spark={sparkData} dataKey="active" loading={loading} />
              <MetricCard label="Queued" value={queuedCount} color="blue" spark={sparkData} dataKey="queued" loading={loading} />
              <MetricCard label="Completed" value={digest?.completed_count ?? 0} color="violet" spark={[]} dataKey="active" loading={loading} />
              <FailedCard value={failedCount} groups={digest?.failure_groups} spark={sparkData} loading={loading} windowHours={windowHours} />
              <SuccessRateCard rate={successRate} trend={digest?.trend} trendHistory={digest?.trend_history} loading={loading} />
              <ConversionRateCard rate={digest?.pipeline_conversion_rate} loading={loading} />
              <AcceptanceRateCard rate={digest?.pipeline_acceptance_rate} loading={loading} />
              <CostCard totalCost={digest?.total_estimated_cost_usd} trendHistory={digest?.trend_history} loading={loading} windowHours={windowHours} />
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
                    className={`text-ui-sm px-3 py-2 rounded transition-colors min-h-11 min-w-11 ${fixLoopTrendDays === d ? 'bg-neutral-700 text-neutral-200' : 'text-neutral-500 hover:text-neutral-300'}`}
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
            onScopeClick={onScopeClick}
            onConfigureTargets={onConfigureTargets ?? (() => {})}
            sectionRef={reviewQualitySectionRef}
          />

          {/* SR diagnostics stack */}
          {srDrillThroughError && (
            <div
              role="alert"
              data-testid="sr-drill-through-error"
              className="flex items-start gap-2 rounded border border-red-500/40 bg-red-500/10 px-2.5 py-1.5 text-ui-md text-red-300"
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

          <div id="cost-section" className="space-y-4 scroll-mt-16">
            {/* Section 2d: Provider Account Health */}
            {!!selectedProjectId && (
              <PoolAccountHealthPanel
                accounts={accountHealth}
                loading={loading}
                servedStale={accountHealthServedStale}
                generatedAt={accountHealthGeneratedAt}
              />
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
          </div>

          {/* Section 2g2: Capability Trials */}
          {!loading && <CapabilityTrialsPanel digest={digest} projectId={selectedProjectId ?? undefined} onRefreshDigest={refetchDigest} />}

          {/* Section 2h: Effort-tier & actual-model efficiency (cost vs landed) */}
          {!loading && <EffortEfficiencyPanel data={digest?.effort_model_efficiency} />}

          {/* Drift Signals */}
          {!!selectedProjectId && <DriftSignalsCard projectId={selectedProjectId} />}

        </div>

        {/* Section 4: Recent Merge Timeline */}
        <div className="lg:col-span-1">
          <RecentMergesTimeline mergeEvents={mergeEvents} loading={loading} />

          {/* Fleet-wide no-CI merge-gate verification health (read-only) */}
          {(digest?.merge_gate_fleet_health || digestIsStale) && (
            <div className="mt-4">
              <MergeGateFleetHealthCard data={digest?.merge_gate_fleet_health} stale={digestIsStale} />
            </div>
          )}

          {/* Park/wake delivery health (read-only) */}
          {(digest?.park_wake_health || digestIsStale) && (
            <div className="mt-4">
              <ParkWakeHealthCard data={digest?.park_wake_health} stale={digestIsStale} />
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

          {/* Improvement lane control-plane observability */}
          <div className="mt-4">
            <ImprovementLanesPanel projectId={selectedProjectId} />
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
            stale={openPRsStale}
            degraded={openPRsDegraded}
          />
        </div>
      </div>
    </div>
  );
}
