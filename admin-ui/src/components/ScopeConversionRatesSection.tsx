import { TrendingUp, TrendingDown, Minus } from 'lucide-react';
import { STAGE_ORDER, STAGE_COLORS } from './taxonomyConstants';

const SCOPE_ORDER = ['small', 'medium', 'large'];

type ScopeRate = {
  created: number;
  merged: number;
  merged_cross_module?: number;
  failed_scope_overflow?: number;
  calibrated_in?: number;
  calibrated_out?: number;
  avg_expansion_ratio_merged?: number;
  avg_expansion_ratio_failed?: number;
  failure_stages?: Record<string, number>;
};

export function ScopeConversionRatesSection({
  scopeRates,
  prevScopeRates,
  loading,
}: {
  scopeRates: Record<string, ScopeRate> | null | undefined;
  prevScopeRates: Record<string, ScopeRate> | null | undefined;
  loading: boolean;
}) {
  if (loading) return null;
  if (!scopeRates) return null;
  const scopes = SCOPE_ORDER.filter(s => scopeRates[s] != null);
  if (scopes.length === 0) return null;

  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Scope Conversion Rates</h2>
      <div className="flex gap-2 flex-wrap">
        {scopes.map(scope => {
          const { created, merged, merged_cross_module = 0, failed_scope_overflow = 0, calibrated_in = 0, calibrated_out = 0, avg_expansion_ratio_merged, avg_expansion_ratio_failed, failure_stages } = scopeRates[scope];
          const topStages = failure_stages
            ? Object.entries(failure_stages)
                .filter(([, c]) => (c || 0) > 0)
                .sort((a, b) => {
                  if (b[1] !== a[1]) return b[1] - a[1];
                  const ai = STAGE_ORDER.indexOf(a[0] as typeof STAGE_ORDER[number]);
                  const bi = STAGE_ORDER.indexOf(b[0] as typeof STAGE_ORDER[number]);
                  return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
                })
                .slice(0, 2)
            : [];
          const expansionWarn =
            typeof avg_expansion_ratio_merged === 'number' &&
            typeof avg_expansion_ratio_failed === 'number' &&
            avg_expansion_ratio_failed > avg_expansion_ratio_merged + 0.5;
          const recalibrated = calibrated_in + calibrated_out;
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
            <div key={scope} className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3 min-w-[110px]">
              <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">{scope}</p>
              <div className="flex items-center gap-1.5">
                <p className={`text-xl font-bold font-mono ${rateColor}`}>
                  {rate != null ? `${Math.round(rate * 100)}%` : '--'}
                </p>
                {trend && <TrendIcon className={`w-3.5 h-3.5 ${trendColor}`} />}
              </div>
              <p className="text-ui-sm text-neutral-600 mt-0.5">{merged}/{created} merged</p>
              {merged_cross_module > 0 && (
                <p className="text-ui-sm text-emerald-500/70 mt-0.5" title="Merged PRs touching 2+ top-level directories">
                  {merged_cross_module}/{merged} cross-dir
                </p>
              )}
              {failed_scope_overflow > 0 && (
                <p className="text-ui-sm text-red-400/80 mt-0.5" title="Tasks that failed due to scope overflow">
                  {failed_scope_overflow} scope overflow
                </p>
              )}
              {recalibrated > 0 && (
                <p className="text-ui-sm text-neutral-600 mt-0.5">{recalibrated} recalibrated</p>
              )}
              {topStages.length > 0 && (
                <div className="flex gap-1 flex-wrap mt-1" title="Top failure stages for this scope">
                  {topStages.map(([stage, count]) => (
                    <span
                      key={stage}
                      className={`text-ui-xs px-1.5 py-0.5 rounded border ${STAGE_COLORS[stage] || 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30'}`}
                    >
                      {stage} {count}
                    </span>
                  ))}
                </div>
              )}
              {(typeof avg_expansion_ratio_merged === 'number' || typeof avg_expansion_ratio_failed === 'number') && (
                <p
                  className={`text-ui-sm mt-0.5 ${expansionWarn ? 'text-amber-500' : 'text-neutral-600'}`}
                  title="Avg files-changed / declared related_files (merged vs failed). Failed >> merged indicates scope underestimates file count."
                >
                  exp {typeof avg_expansion_ratio_merged === 'number' ? avg_expansion_ratio_merged.toFixed(1) : '--'}x
                  {' / '}
                  <span className={expansionWarn ? 'text-amber-500 font-semibold' : ''}>
                    {typeof avg_expansion_ratio_failed === 'number' ? `${avg_expansion_ratio_failed.toFixed(1)}x` : '--'}
                  </span>
                </p>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
