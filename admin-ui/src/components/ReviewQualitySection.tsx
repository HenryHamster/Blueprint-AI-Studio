import React, { useState, useCallback, useEffect } from 'react';
import { Search, ExternalLink } from 'lucide-react';
import { apiFetch } from '../api';
import { SrTrendBreakdown, EdgesTrendBreakdown, type SrInvestigationSignal, type EdgesTrend, type SrTrendData } from './SrTrendBreakdown';
import type { Task } from '../types/task';

// ---------- local types (exported for use in DashboardHome) ----------

export interface SrFixIterationBucket {
  hit_rate?: number | null;
  miss_rate?: number | null;
  sample_count?: number | null;
}

export type SrTrendWithFixIterations = SrTrendData & {
  by_fix_iteration?: Record<string, SrFixIterationBucket> | null;
  fix_iteration_breakdown?: Record<string, SrFixIterationBucket> | null;
  iteration_breakdown?: Record<string, SrFixIterationBucket> | null;
  overall_hit_rate?: number | null;
  previous_overall_hit_rate?: number | null;
  overall_trend_verdict?: 'improving' | 'declining' | 'stable' | null;
  overall_trend_significant?: boolean | null;
};

// ---------- FixIterationBreakdown ----------

const FIX_ITERATION_BUCKETS = ['1', '2', '3+'] as const;

function FixIterationBreakdown({ trends }: { trends: SrTrendWithFixIterations | null | undefined }) {
  const breakdown = trends?.by_fix_iteration ?? trends?.fix_iteration_breakdown ?? trends?.iteration_breakdown ?? null;
  if (!breakdown) return null;
  return (
    <section>
      <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-2">SR by Fix Iteration</p>
      <div className="flex flex-wrap gap-1.5">
        {FIX_ITERATION_BUCKETS.map(bucket => {
          const stats = breakdown[bucket] ?? breakdown[`fix-${bucket}`] ?? breakdown[`fix_${bucket}`] ?? breakdown[bucket.replace('+', 'plus')] ?? null;
          const sampleCount = stats?.sample_count ?? 0;
          const enoughSignal = sampleCount >= 5 && stats?.hit_rate != null && stats?.miss_rate != null;
          return (
            <span
              key={bucket}
              className="inline-flex items-center gap-1.5 text-ui-sm font-semibold px-2 py-0.5 rounded border bg-workbench-control/10 text-neutral-300 border-neutral-700/40"
              title={enoughSignal ? `n=${sampleCount}` : 'insufficient signal (<5 qualifying tasks)'}
            >
              fix-{bucket}
              {enoughSignal ? (
                <>
                  <span className="font-bold text-emerald-400">{Math.round((stats.hit_rate ?? 0) * 100)}%</span>
                  <span className="opacity-70">miss {Math.round((stats.miss_rate ?? 0) * 100)}%</span>
                  <span className="text-neutral-500 font-normal">n={sampleCount}</span>
                </>
              ) : (
                <span className="text-neutral-500 font-normal">insufficient signal</span>
              )}
            </span>
          );
        })}
      </div>
    </section>
  );
}

// ---------- subset of DigestData fields consumed by this section ----------

interface ReviewQualityDigest {
  review_accuracy?: {
    total_reviews?: number | null;
    overrides?: number | null;
    dismissals?: number | null;
    misses?: number | null;
    total_approvals?: number | null;
    precision?: number | null;
    recall?: number | null;
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
  edges_trend?: EdgesTrend | null;
}

// ---------- component ----------

interface ReviewQualitySectionProps {
  digest: ReviewQualityDigest | null;
  loading: boolean;
  selectedProjectId: string | null;
  tasks: Task[];
  onViewTask: (task: Task) => void;
  /**
   * Resolve and navigate to a task by id. Falls back to fetching the task when
   * it is not in the in-memory `tasks` slice (e.g. an out-of-window SR miss).
   * Preferred over `onViewTask` for drill-throughs that may reference tasks
   * outside the dashboard's loaded list.
   */
  onViewTaskById?: (taskId: string) => void;
  srCatchRateTargets: Record<string, number> | null | undefined;
  onCategoryClick: (category: string) => void;
  onScopeClick?: (scope: string) => void;
  onConfigureTargets: () => void;
  sectionRef: React.RefObject<HTMLElement | null>;
}

export function ReviewQualitySection({
  digest,
  loading,
  selectedProjectId,
  tasks,
  onViewTask,
  onViewTaskById,
  srCatchRateTargets,
  onCategoryClick,
  onScopeClick,
  onConfigureTargets,
  sectionRef,
}: ReviewQualitySectionProps) {
  const [srInvestigationTaskIds, setSrInvestigationTaskIds] = useState<Record<string, string>>({});
  const [srTrendDrilldownOpen, setSrTrendDrilldownOpen] = useState(false);

  useEffect(() => {
    if (!selectedProjectId) return;
    void (async () => {
      try {
        const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-investigation-task-ids`);
        if (res.ok) {
          const data = (await res.json()) as { task_ids?: Record<string, string> };
          setSrInvestigationTaskIds(data.task_ids ?? {});
        }
      } catch { /* silent */ }
    })();
  }, [selectedProjectId]);
  const [srTrendDrilldownRows, setSrTrendDrilldownRows] = useState<{ task_id: string; title: string; missed_categories: string[]; sr_verdict: string | null }[]>([]);
  const [srTrendDrilldownLoading, setSrTrendDrilldownLoading] = useState(false);

  const fetchSrTrendDrilldown = useCallback(async (categories: string[]) => {
    if (!selectedProjectId || categories.length === 0) return;
    setSrTrendDrilldownLoading(true);
    try {
      const params = new URLSearchParams({ alert_type: 'trends' });
      categories.forEach(c => params.append('categories', c));
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-alert-drilldown?${params}`);
      if (res.ok) setSrTrendDrilldownRows(((await res.json()) as { items: { task_id: string; title: string; missed_categories: string[]; sr_verdict: string | null }[] }).items ?? []);
    } finally {
      setSrTrendDrilldownLoading(false);
    }
  }, [selectedProjectId]);

  const handleSpawnSrInvestigation = useCallback(async (signal: SrInvestigationSignal): Promise<string | null> => {
    if (!selectedProjectId) return null;
    try {
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-investigation`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(signal),
      });
      if (!res.ok) return null;
      const data = (await res.json()) as { task_id: string; created: boolean };
      setSrInvestigationTaskIds(prev => ({ ...prev, [signal.category]: data.task_id }));
      return data.task_id;
    } catch {
      return null;
    }
  }, [selectedProjectId]);

  if (
    loading ||
    (
      digest?.review_accuracy == null &&
      digest?.self_review_effectiveness == null &&
      digest?.self_review_prediction_accuracy == null &&
      digest?.self_review_prediction_accuracy_trends == null &&
      digest?.self_review_skip_rate == null &&
      digest?.sr_disallowed_blind_spot_rate == null &&
      digest?.sr_false_attestation_trends == null
    )
  ) {
    return null;
  }

  return (
    <section ref={sectionRef}>
      <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-2">Review Quality</p>
      <div className="flex flex-wrap gap-3">
        {digest?.review_accuracy?.precision != null && (
          <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[110px]">
            <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-0.5">Precision</p>
            <p className={`text-xl font-bold font-mono ${digest.review_accuracy.precision >= 0.8 ? 'text-emerald-400' : digest.review_accuracy.precision >= 0.6 ? 'text-amber-400' : 'text-red-400'}`}>
              {Math.round(digest.review_accuracy.precision * 100)}%
            </p>
            <p className="text-ui-xs text-neutral-600 mt-0.5">{digest.review_accuracy.total_reviews} reviews</p>
          </div>
        )}
        {digest?.review_accuracy?.recall != null && (
          <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[110px]">
            <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-0.5">Recall</p>
            <p className={`text-xl font-bold font-mono ${digest.review_accuracy.recall >= 0.8 ? 'text-emerald-400' : digest.review_accuracy.recall >= 0.6 ? 'text-amber-400' : 'text-red-400'}`}>
              {Math.round(digest.review_accuracy.recall * 100)}%
            </p>
            <p className="text-ui-xs text-neutral-600 mt-0.5">{digest.review_accuracy.total_approvals} approvals</p>
          </div>
        )}
        {digest?.self_review_effectiveness?.with_self_review_approval_rate != null && (
          <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[130px]">
            <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-0.5">SR Approval Lift</p>
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
                    <p className={`text-ui-xs mt-0.5 ${lift > 0 ? 'text-emerald-500' : lift < 0 ? 'text-red-500' : 'text-neutral-600'}`}>
                      {lift > 0 ? '+' : ''}{Math.round(lift * 100)}pp vs no SR
                    </p>
                  )}
                </>
              );
            })()}
          </div>
        )}
        {(() => {
          const sre = digest?.self_review_effectiveness;
          const sample = sre?.sample_size ?? 0;
          const fixedCount = sre?.sr_fixed_count;
          const cleanCount = sre?.sr_clean_count;
          if (
            sre == null ||
            sample < 5 ||
            fixedCount == null ||
            cleanCount == null
          ) {
            return null;
          }
          const fixedShare = fixedCount / sample;
          const cleanShare = cleanCount / sample;
          const fixedApproval = sre.fixed_verdict_approval_rate;
          const cleanApproval = sre.clean_verdict_approval_rate;
          const approvalColor = (rate: number | null | undefined) =>
            rate == null
              ? 'text-neutral-500'
              : rate >= 0.7
                ? 'text-emerald-400'
                : rate >= 0.5
                  ? 'text-amber-400'
                  : 'text-red-400';
          return (
            <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[200px]">
              <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-0.5">SR Self-Fix Path</p>
              <div className="space-y-0.5">
                <div className="flex items-baseline justify-between gap-3">
                  <span className="text-ui-sm text-neutral-400">Self-fixed</span>
                  <span className="text-xs font-mono text-neutral-200">
                    {Math.round(fixedShare * 100)}%
                    <span className="text-ui-xs text-neutral-600 ml-1">(n={fixedCount} of {sample})</span>
                    {fixedApproval != null && (
                      <span className={`text-ui-xs ml-1 ${approvalColor(fixedApproval)}`}>
                        {Math.round(fixedApproval * 100)}% approved
                      </span>
                    )}
                  </span>
                </div>
                <div className="flex items-baseline justify-between gap-3">
                  <span className="text-ui-sm text-neutral-400">Clean SR</span>
                  <span className="text-xs font-mono text-neutral-200">
                    {Math.round(cleanShare * 100)}%
                    <span className="text-ui-xs text-neutral-600 ml-1">(n={cleanCount})</span>
                    {cleanApproval != null && (
                      <span className={`text-ui-xs ml-1 ${approvalColor(cleanApproval)}`}>
                        {Math.round(cleanApproval * 100)}% approved
                      </span>
                    )}
                  </span>
                </div>
              </div>
            </div>
          );
        })()}
        {digest?.self_review_prediction_accuracy?.prediction_hit_rate != null && (
          <div
            className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[110px]"
            title="SR Prediction Hit Rate: share of issues flagged by SR that the reviewer LLM also flagged. This is inter-agent agreement against the reviewer LLM, not human validation."
          >
            <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-0.5">SR Prediction Hit</p>
            <p className={`text-xl font-bold font-mono ${digest.self_review_prediction_accuracy.prediction_hit_rate >= 0.7 ? 'text-emerald-400' : digest.self_review_prediction_accuracy.prediction_hit_rate >= 0.4 ? 'text-amber-400' : 'text-red-400'}`}>
              {Math.round(digest.self_review_prediction_accuracy.prediction_hit_rate * 100)}%
            </p>
            {digest.self_review_prediction_accuracy.false_alarm_rate != null && (
              <p className="text-ui-xs text-neutral-600 mt-0.5">{Math.round(digest.self_review_prediction_accuracy.false_alarm_rate * 100)}% false alarms</p>
            )}
          </div>
        )}
        {(() => {
          const spa = digest?.self_review_prediction_accuracy;
          const rate = spa?.self_flagged_shipped_unfixed_task_rate ?? null;
          const taskCount = spa?.self_flagged_shipped_unfixed_task_count;
          if (rate == null && taskCount == null) return null;
          const color = rate == null ? 'text-neutral-300' : rate <= 0.2 ? 'text-emerald-400' : rate <= 0.4 ? 'text-amber-400' : 'text-red-400';
          const tooltip = [
            'SR Unfixed Flags: tasks where SR already failed a category but the agent submitted without fixing it and a reviewer confirmed the same category.',
            taskCount != null ? `${taskCount} tasks with at least one self-flagged unresolved category` : null,
          ].filter(Boolean).join('\n');
          return (
            <div
              className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[130px]"
              title={tooltip}
              data-testid="sr-self-flagged-unresolved-tile"
            >
              <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-0.5">SR Unfixed Flags</p>
              <p className={`text-xl font-bold font-mono ${color}`}>
                {rate != null ? `${Math.round(rate * 100)}%` : '--'}
              </p>
              <p className="text-ui-xs text-neutral-600 mt-0.5">
                {taskCount != null ? `${taskCount} tasks` : ''}
              </p>
            </div>
          );
        })()}
        {digest?.sr_regression_risk_test_signal_trends?.current?.miss_by_test_signal_rate != null && (() => {
          const rrts = digest?.sr_regression_risk_test_signal_trends;
          if (!rrts?.current) return null;
          const missRate = rrts.current.miss_by_test_signal_rate ?? 0;
          const numer = rrts.current.numerator ?? 0;
          const denom = rrts.current.denominator ?? 0;
          const trend = rrts.trend;
          const delta = rrts.miss_by_test_signal_rate_delta;
          const color = missRate <= 0.15 ? 'text-emerald-400' : missRate <= 0.35 ? 'text-amber-400' : 'text-red-400';
          const trendIcon = trend === 'improving' ? '↓' : trend === 'regressing' ? '↑' : '→';
          const trendColor = trend === 'improving' ? 'text-emerald-500' : trend === 'regressing' ? 'text-red-500' : 'text-neutral-500';
          const tooltip = [
            'SR regression_risk catch vs test-failure signal',
            `Miss rate: SR passed but testFix loop ran (${numer}/${denom})`,
            'A miss = SR said regression_risk:passed yet the task\'s testFix loop subsequently ran ≥1 iteration.',
            delta != null ? `Δ vs previous period: ${delta >= 0 ? '+' : ''}${Math.round(delta * 100)}pp` : null,
          ].filter(Boolean).join('\n');
          return (
            <div
              className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[110px]"
              title={tooltip}
            >
              <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-0.5">RR vs Test Signal</p>
              <p className={`text-xl font-bold font-mono ${color}`}>
                {Math.round(missRate * 100)}%
              </p>
              <p className="text-ui-xs text-neutral-600 mt-0.5">
                {numer}/{denom} miss <span className={trendColor}>{trendIcon}</span>
              </p>
            </div>
          );
        })()}
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
              className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[110px]"
              title={tooltip}
            >
              <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-0.5">SR Skip Rate</p>
              <p className={`text-xl font-bold font-mono ${color}`}>
                {Math.round(rate * 100)}%
              </p>
              <p className="text-ui-xs text-neutral-600 mt-0.5">n={skip.sample_size}</p>
            </div>
          );
        })()}
        {digest?.sr_disallowed_blind_spot_rate != null && (() => {
          const dbs = digest?.sr_disallowed_blind_spot_rate;
          if (!dbs) return null;
          const rate = dbs.overall_rate;
          const color = rate <= 0.05 ? 'text-emerald-400' : rate <= 0.15 ? 'text-amber-400' : 'text-red-400';
          const byTypeEntries = Object.entries(dbs.by_task_type ?? {});
          // Cross-reference: backend/app/sr_blind_spots.py DISALLOWED_BLIND_SPOT_EXAMPLES
          const categoryLabels = 'full backend test suite, docker pytest, viewport tests';
          const tooltip = [
            `Disallowed patterns: ${categoryLabels}`,
            ...(byTypeEntries.length
              ? byTypeEntries.map(([t, b]) => `${t}: ${Math.round(b.rate * 100)}% (n=${b.sample_size})`)
              : []),
          ].join('\n');
          return (
            <div
              className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[110px]"
              title={tooltip}
              data-testid="sr-disallowed-blind-spot-tile"
            >
              <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-0.5">SR Disallowed</p>
              <p className={`text-xl font-bold font-mono ${color}`}>
                {Math.round(rate * 100)}%
              </p>
              <p className="text-ui-xs text-neutral-600 mt-0.5">n={dbs.sample_size}</p>
            </div>
          );
        })()}
        {digest?.sr_false_attestation_trends?.per_category != null && (() => {
          const cats = digest?.sr_false_attestation_trends?.per_category;
          if (!cats) return null;
          const entries = Object.entries(cats).filter(([, v]) => v.false_attestation_rate != null);
          if (entries.length === 0) return null;
          const CAT_LABELS: Record<string, string> = {
            tests_executed_cleanly: 'Tests',
            lint_executed_cleanly: 'Lint',
            typecheck_executed_cleanly: 'Typecheck',
          };
          const tooltipLines = entries.map(([cat, v]) => {
            const label = CAT_LABELS[cat] ?? cat;
            const rate = v.false_attestation_rate ?? 0;
            const n = (v.ci_confirmed ?? 0) + (v.false_attested ?? 0);
            const trendStr = v.trend === 'improving' ? '↓' : v.trend === 'regressing' ? '↑' : '→';
            return `${label}: ${Math.round(rate * 100)}% (${v.false_attested ?? 0}/${n}) ${trendStr}`;
          });
          const worstRate = Math.max(...entries.map(([, v]) => v.false_attestation_rate ?? 0));
          const color = worstRate <= 0.05 ? 'text-emerald-400' : worstRate <= 0.1 ? 'text-amber-400' : 'text-red-400';
          return (
            <div
              className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[130px]"
              title={['SR False Attestation (SR passed but CI failed):', ...tooltipLines].join('\n')}
              data-testid="sr-false-attestation-tile"
            >
              <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-0.5">SR False Attest</p>
              <p className={`text-xl font-bold font-mono ${color}`}>
                {Math.round(worstRate * 100)}%
              </p>
              <div className="flex flex-col gap-0.5 mt-0.5">
                {entries.map(([cat, v]) => {
                  const label = CAT_LABELS[cat] ?? cat;
                  const rate = v.false_attestation_rate ?? 0;
                  const trendIcon = v.trend === 'improving' ? '↓' : v.trend === 'regressing' ? '↑' : '→';
                  const trendColor = v.trend === 'improving' ? 'text-emerald-500' : v.trend === 'regressing' ? 'text-red-500' : 'text-neutral-500';
                  return (
                    <p key={cat} className="text-ui-xs text-neutral-600">
                      {label}: {Math.round(rate * 100)}% <span className={trendColor}>{trendIcon}</span>
                    </p>
                  );
                })}
              </div>
            </div>
          );
        })()}
        <SrTrendBreakdown
          trends={digest?.self_review_prediction_accuracy_trends}
          variant="card"
          onCategoryClick={onCategoryClick}
          onScopeClick={onScopeClick}
          srCatchRateTargets={srCatchRateTargets}
          onConfigureTargets={onConfigureTargets}
          onSpawnSrInvestigation={selectedProjectId ? handleSpawnSrInvestigation : undefined}
          activeInvestigationTaskIds={srInvestigationTaskIds}
        />
        <FixIterationBreakdown trends={digest?.self_review_prediction_accuracy_trends} />
        <EdgesTrendBreakdown trend={digest?.edges_trend} />
      </div>
      {selectedProjectId && (() => {
        const cats = Object.entries(
          digest?.self_review_prediction_accuracy_trends?.categories ?? {}
        ).filter(([, c]) => c.trend === 'decreasing').map(([k]) => k);
        if (cats.length === 0) return null;
        return (
          <div className="mt-2">
            <button
              onClick={() => {
                if (srTrendDrilldownOpen) { setSrTrendDrilldownOpen(false); return; }
                setSrTrendDrilldownOpen(true);
                void fetchSrTrendDrilldown(cats);
              }}
              className="text-ui-sm flex items-center gap-1 text-amber-400/70 hover:text-amber-400 transition-colors mb-1"
            >
              <Search className="w-3 h-3" />
              <span>{srTrendDrilldownOpen ? 'Hide' : 'Investigate'} regression ({cats.join(', ')})</span>
            </button>
            {srTrendDrilldownOpen && (
              <div className="bg-workbench-base/60 border border-neutral-700/40 rounded-lg px-3 py-2 text-xs text-neutral-300 flex flex-col gap-1.5">
                {srTrendDrilldownLoading && <span className="text-neutral-500">Loading…</span>}
                {!srTrendDrilldownLoading && srTrendDrilldownRows.length === 0 && <span className="text-neutral-500">No contributing tasks found.</span>}
                {!srTrendDrilldownLoading && srTrendDrilldownRows.map(row => (
                  <div key={row.task_id} className="flex items-start gap-2">
                    <span className={`shrink-0 text-ui-sm px-1.5 py-0.5 rounded border font-medium ${row.sr_verdict === 'pass' ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/30' : 'bg-red-500/10 text-red-400 border-red-500/30'}`}>
                      SR {row.sr_verdict ?? '?'}
                    </span>
                    <div className="flex-1 min-w-0">
                      <span className="text-neutral-200 truncate block">{row.title}</span>
                      <span className="text-neutral-500 text-ui-sm">missed: {row.missed_categories.join(', ')}</span>
                    </div>
                    <button onClick={() => { if (onViewTaskById) { onViewTaskById(row.task_id); } else { const t = tasks.find(t => t.id === row.task_id); if (t) onViewTask(t); } }} className="shrink-0 opacity-60 hover:opacity-100 transition-opacity" title="View task SR panel">
                      <ExternalLink className="w-3 h-3" />
                    </button>
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })()}
    </section>
  );
}
