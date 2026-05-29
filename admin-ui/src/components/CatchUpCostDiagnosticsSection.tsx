import { useMemo, useCallback } from 'react';
import { DollarSign } from 'lucide-react';
import { TAXONOMY_ORDER, TAXONOMY_LABELS, TAXONOMY_COLORS, STAGE_ORDER, STAGE_COLORS } from './taxonomyConstants';
import { trendFor } from './catchupAnalytics';
import type { TrendHistory } from './catchupAnalytics';

interface FailureGroupTask {
  taxonomy?: string | null;
}

interface FailureGroup {
  failure_class: string;
  tasks: FailureGroupTask[];
}

interface FailureTaxonomyDistribution {
  categories: Record<string, { count: number; percentage: number }>;
  total_failures: number;
  dominant_category: string | null;
}

interface FailureStageDistribution {
  stages: Record<string, number>;
  total_classified: number;
  dominant_stage: string | null;
}

interface ScopeConversionRate {
  created: number;
  merged: number;
  calibrated_in?: number;
  calibrated_out?: number;
}

interface ScopeMismatchSummary {
  total_calibrated: number;
  calibrated_success_rate: number | null;
  by_original_scope: Record<string, { total: number; completed: number; failed: number }>;
  top_directions: {
    direction_id: string;
    direction_text: string;
    calibrated_count: number;
    total_tasks: number;
    calibration_rate: number;
  }[];
}

interface RejectionPatternSummary {
  total_rejected: number;
  top_notes: { snippet: string; count: number }[];
}

interface CatchUpCostDiagnosticsSectionProps {
  failureTaxonomyDistribution?: FailureTaxonomyDistribution | null;
  costByFailureClass?: Record<string, number> | null;
  failureGroups?: FailureGroup[];
  failureStageDistribution?: FailureStageDistribution | null;
  scopeConversionRates?: Record<string, ScopeConversionRate> | null;
  prevScopeConversionRates?: Record<string, ScopeConversionRate> | null;
  scopeMismatchSummary?: ScopeMismatchSummary | null;
  rejectionPatternSummary?: RejectionPatternSummary | null;
  trendHistory?: TrendHistory[];
  activeTaxonomyFilter: string | null;
  activeStageFilter: string | null;
  onTaxonomyFilterChange: (filter: string | null) => void;
  onStageFilterChange: (filter: string | null) => void;
  onExpandFailures: () => void;
}

export function CatchUpCostDiagnosticsSection({
  failureTaxonomyDistribution,
  costByFailureClass,
  failureGroups,
  failureStageDistribution,
  scopeConversionRates,
  prevScopeConversionRates,
  scopeMismatchSummary,
  rejectionPatternSummary,
  trendHistory,
  activeTaxonomyFilter,
  activeStageFilter,
  onTaxonomyFilterChange,
  onStageFilterChange,
  onExpandFailures,
}: CatchUpCostDiagnosticsSectionProps) {
  const history = useMemo(() => trendHistory ?? [], [trendHistory]);

  const trendForCallback = useCallback((cat: string): { arrow: string; color: string } => {
    return trendFor(history, cat);
  }, [history]);

  return (
    <>
      {/* Failure taxonomy root-cause pills */}
      {failureTaxonomyDistribution && failureTaxonomyDistribution.total_failures > 0 && (() => {
        const ftd = failureTaxonomyDistribution;
        if (!ftd) return null;
        const costByTaxonomy: Record<string, number> = {};
        const costByClass = costByFailureClass ?? {};
        for (const g of failureGroups ?? []) {
          const groupCost = costByClass[g.failure_class] ?? 0;
          if (!groupCost || g.tasks.length === 0) continue;
          const perTask = groupCost / g.tasks.length;
          for (const t of g.tasks) {
            if (!t.taxonomy) continue;
            costByTaxonomy[t.taxonomy] = (costByTaxonomy[t.taxonomy] ?? 0) + perTask;
          }
        }
        return (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Root cause</span>
            {TAXONOMY_ORDER.map(cat => {
              const entry = ftd.categories[cat];
              if (!entry || entry.count === 0) return null;
              const cost = costByTaxonomy[cat];
              const trend = trendForCallback(cat);
              const isActive = activeTaxonomyFilter === cat;
              const isDominant = ftd.dominant_category === cat;
              return (
                <button
                  key={cat}
                  type="button"
                  onClick={() => {
                    onTaxonomyFilterChange(isActive ? null : cat);
                    onStageFilterChange(null);
                    onExpandFailures();
                  }}
                  aria-pressed={isActive}
                  className={`inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 min-h-[44px] rounded border transition-all ${TAXONOMY_COLORS[cat]} ${isActive ? 'ring-2 ring-white/60' : isDominant ? 'ring-1 ring-amber-300/70 shadow-[0_0_0_1px_rgba(252,211,77,0.35)]' : 'hover:opacity-80'}`}
                  title={`${TAXONOMY_LABELS[cat]}: ${entry.count} (${Math.round(entry.percentage)}%)${cost ? ` · $${cost.toFixed(2)}` : ''}${isDominant ? ' — dominant category' : ''} — click to ${isActive ? 'clear' : 'filter'}`}
                >
                  {isDominant && <span aria-label="dominant" className="text-amber-300">★</span>}
                  {TAXONOMY_LABELS[cat]}
                  <span className="font-bold">{entry.count}</span>
                  <span className={`font-bold ${trend.color}`}>{trend.arrow}</span>
                  {cost != null && cost > 0 && (
                    <span className="inline-flex items-center font-mono font-normal opacity-80">
                      ·<DollarSign className="w-2.5 h-2.5" />{cost.toFixed(2)}
                    </span>
                  )}
                </button>
              );
            })}
            {activeTaxonomyFilter && (
              <button
                type="button"
                onClick={() => onTaxonomyFilterChange(null)}
                className="text-[10px] text-neutral-400 hover:text-neutral-200 underline underline-offset-2"
              >
                clear
              </button>
            )}
          </div>
        );
      })()}

      {/* Failure stage distribution pills */}
      {failureStageDistribution && failureStageDistribution.total_classified > 0 && (() => {
        const fsd = failureStageDistribution;
        if (!fsd) return null;
        return (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Pipeline stage</span>
            {STAGE_ORDER.map(stage => {
              const count = fsd.stages[stage];
              if (!count) return null;
              const isActive = activeStageFilter === stage;
              const isDominant = fsd.dominant_stage === stage;
              return (
                <button
                  key={stage}
                  type="button"
                  onClick={() => {
                    onStageFilterChange(isActive ? null : stage);
                    onTaxonomyFilterChange(null);
                    onExpandFailures();
                  }}
                  aria-pressed={isActive}
                  className={`inline-flex items-center gap-1 text-[10px] font-semibold px-2 py-0.5 min-h-[44px] rounded border transition-all ${STAGE_COLORS[stage] ?? 'bg-neutral-700/20 text-neutral-400 border-neutral-700/30'} ${isActive ? 'ring-2 ring-white/60' : isDominant ? 'ring-1 ring-amber-300/70 shadow-[0_0_0_1px_rgba(252,211,77,0.35)]' : 'hover:opacity-80'}`}
                  title={`${stage}: ${count}${isDominant ? ' — dominant stage' : ''} — click to ${isActive ? 'clear' : 'filter'}`}
                >
                  {isDominant && <span aria-label="dominant" className="text-amber-300">★</span>}
                  {stage}
                  <span className="font-bold">{count}</span>
                </button>
              );
            })}
            {activeStageFilter && (
              <button
                type="button"
                onClick={() => onStageFilterChange(null)}
                className="text-[10px] text-neutral-400 hover:text-neutral-200 underline underline-offset-2"
              >
                clear
              </button>
            )}
          </div>
        );
      })()}

      {/* Scope conversion rate pills */}
      {scopeConversionRates && Object.keys(scopeConversionRates).length > 0 && (() => {
        const scr = scopeConversionRates;
        if (!scr) return null;
        return (
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Scope conversion</span>
            {['small', 'medium', 'large']
              .filter(s => scr[s] != null)
              .map(scope => {
                const { created, merged, calibrated_in = 0, calibrated_out = 0 } = scr[scope];
                const recalCount = (calibrated_in || 0) + (calibrated_out || 0);
                const rate = created > 0 ? merged / created : null;
                const prev = prevScopeConversionRates?.[scope];
                const prevRate = prev && prev.created > 0 ? prev.merged / prev.created : null;
                let trend: 'improving' | 'declining' | 'stable' | null = null;
                if (rate != null && prevRate != null) {
                  const delta = rate - prevRate;
                  trend = delta > 0.05 ? 'improving' : delta < -0.05 ? 'declining' : 'stable';
                }
                const arrow = trend === 'improving' ? '↑' : trend === 'declining' ? '↓' : trend === 'stable' ? '→' : '';
                const arrowColor = trend === 'improving' ? 'text-emerald-400' : trend === 'declining' ? 'text-red-400' : 'text-neutral-500';
                const color = rate == null
                  ? 'bg-neutral-500/10 text-neutral-400 border-neutral-500/20'
                  : rate >= 0.6 ? 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20'
                  : rate >= 0.3 ? 'bg-amber-500/10 text-amber-400 border-amber-500/20'
                  : 'bg-red-500/10 text-red-400 border-red-500/20';
                return (
                  <span
                    key={scope}
                    className={`text-[10px] border px-1.5 py-0.5 rounded inline-flex items-center gap-1 ${color}`}
                    title={recalCount > 0 ? `Recalibrations: ${calibrated_in} in, ${calibrated_out} out` : undefined}
                  >
                    {scope}: {rate != null ? `${Math.round(rate * 100)}%` : '--'} ({merged}/{created})
                    {arrow && <span className={arrowColor}>{arrow}</span>}
                    {recalCount > 0 && <span className="text-neutral-400">· {recalCount} recal</span>}
                  </span>
                );
              })}
          </div>
        );
      })()}

      {/* Scope mismatch diagnostics */}
      {scopeMismatchSummary && scopeMismatchSummary.total_calibrated > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Scope mismatch</span>
            <span className="text-[10px] text-neutral-400">
              {scopeMismatchSummary.total_calibrated} calibrated
            </span>
            {scopeMismatchSummary.calibrated_success_rate != null && (
              <span className="text-[10px] text-neutral-500">
                · {Math.round(scopeMismatchSummary.calibrated_success_rate * 100)}% success
              </span>
            )}
            {Object.entries(scopeMismatchSummary.by_original_scope).map(([orig, b]) => (
              <span key={orig} className="text-[10px] bg-cyan-500/10 text-cyan-400 border border-cyan-500/20 px-1.5 py-0.5 rounded">
                {orig}: {b.total}
              </span>
            ))}
          </div>
          {scopeMismatchSummary.top_directions.length > 0 && (
            <div className="flex flex-col gap-0.5 ml-1 max-w-full">
              {scopeMismatchSummary.top_directions.map(d => (
                <div key={d.direction_id} className="flex items-center gap-1 text-[10px] text-neutral-500">
                  <span className="truncate min-w-0">· {(d.direction_text || d.direction_id).slice(0, 60)}</span>
                  <span className="text-cyan-400 shrink-0">{d.calibrated_count}/{d.total_tasks} ({Math.round(d.calibration_rate * 100)}%)</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Rejection pattern summary */}
      {rejectionPatternSummary && rejectionPatternSummary.total_rejected > 0 && (
        <div className="flex flex-col gap-1">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest">Rejection patterns</span>
            <button
              type="button"
              onClick={onExpandFailures}
              className="text-[10px] text-neutral-400 hover:text-purple-300 underline decoration-dotted underline-offset-2"
              title="Expand recent failures to find rejected tasks"
            >
              {rejectionPatternSummary.total_rejected} rejected
            </button>
          </div>
          {rejectionPatternSummary.top_notes.length > 0 && (
            <div className="flex flex-col gap-0.5 ml-1 max-w-full">
              {rejectionPatternSummary.top_notes.map((n) => (
                <div key={n.snippet} className="flex items-center gap-1 text-[10px] text-neutral-500">
                  <span className="truncate min-w-0">· {n.snippet.slice(0, 100)}{n.snippet.length > 100 ? '…' : ''}</span>
                  <span className="text-purple-400 shrink-0">×{n.count}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </>
  );
}
