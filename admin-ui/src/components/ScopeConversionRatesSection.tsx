import { TrendingUp, TrendingDown, Minus } from 'lucide-react';

const SCOPE_ORDER = ['small', 'medium', 'large'];

type ScopeRate = { created: number; merged: number; calibrated_in?: number; calibrated_out?: number };

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
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Scope Conversion Rates</h2>
      <div className="flex gap-2 flex-wrap">
        {scopes.map(scope => {
          const { created, merged, calibrated_in = 0, calibrated_out = 0 } = scopeRates[scope];
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
            <div key={scope} className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 min-w-[110px]">
              <p className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1">{scope}</p>
              <div className="flex items-center gap-1.5">
                <p className={`text-xl font-bold font-mono ${rateColor}`}>
                  {rate != null ? `${Math.round(rate * 100)}%` : '--'}
                </p>
                {trend && <TrendIcon className={`w-3.5 h-3.5 ${trendColor}`} />}
              </div>
              <p className="text-[10px] text-neutral-600 mt-0.5">{merged}/{created} merged</p>
              {recalibrated > 0 && (
                <p className="text-[10px] text-neutral-600 mt-0.5">{recalibrated} recalibrated</p>
              )}
            </div>
          );
        })}
      </div>
    </section>
  );
}
