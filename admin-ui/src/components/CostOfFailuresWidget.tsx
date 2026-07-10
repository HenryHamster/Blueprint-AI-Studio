import { FAILURE_CLASS_COLORS } from './DashboardMetricCardsPrimitives';
import type { DigestData } from './DashboardHome';

export function CostOfFailuresWidget({ digest }: { digest: DigestData | null }) {
  if ((digest?.total_failed_cost_usd ?? 0) <= 0) return null;
  return (
    <section>
      <div className="flex flex-wrap gap-1.5 items-center">
        <span className="text-ui-sm text-neutral-500 uppercase tracking-wide mr-1">Cost of Failures</span>
        <span className="text-sm font-bold text-red-400">${(digest?.total_failed_cost_usd ?? 0).toFixed(2)}</span>
        {Object.entries(digest?.cost_by_failure_class ?? {})
          .sort((a, b) => b[1] - a[1])
          .map(([cls, cost]) => {
            const color = FAILURE_CLASS_COLORS[cls] ?? 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30';
            return (
              <span key={cls} className={`inline-flex items-center gap-1.5 text-ui-sm font-semibold px-2 py-0.5 rounded border ${color}`}>
                {cls}
                <span className="font-bold">${cost.toFixed(2)}</span>
              </span>
            );
          })}
      </div>
    </section>
  );
}
