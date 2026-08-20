import { ResponsiveContainer, AreaChart, Area } from 'recharts';
import { TAXONOMY_COLORS, TAXONOMY_LABELS, TAXONOMY_ORDER } from './dashboardConstants';
import type { DigestData } from './DashboardHome';

const TAXONOMY_STROKE: Record<string, string> = {
  scope_related: '#22d3ee',
  review_quality: '#a855f7',
  infra_related: '#f59e0b',
  code_defect: '#ef4444',
};

export function FailureTaxonomyBreakdown({
  digest,
  taxSparkMap,
}: {
  digest: DigestData | null;
  taxSparkMap: Record<string, { v: number }[]>;
}) {
  if (!digest?.failure_taxonomy_distribution || digest.failure_taxonomy_distribution.total_failures <= 0) {
    return null;
  }
  return (
    <section>
      <div className="flex flex-wrap gap-1.5 items-center">
        <span className="text-ui-sm text-neutral-500 uppercase tracking-wide mr-1">Taxonomy</span>
        {TAXONOMY_ORDER.map(k => {
          const tax = digest.failure_taxonomy_distribution;
          if (!tax) return null;
          const cat = tax.categories[k];
          if (!cat) return null;
          const isDominant = tax.dominant_category === k;
          const base = TAXONOMY_COLORS[k] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30';
          const cls = isDominant ? `${base} ring-1 ring-current` : base;
          const stroke = TAXONOMY_STROKE[k] ?? '#a3a3a3';
          const taxSpark = taxSparkMap[k] ?? [];
          const hasSpark = taxSpark.length >= 3;
          return (
            <span key={k} className={`inline-flex items-center gap-1.5 text-ui-sm font-semibold px-2 py-0.5 rounded border ${cls}`} title={`${TAXONOMY_LABELS[k]}: ${cat.count} (${cat.percentage}%)${isDominant ? ' — dominant' : ''}`}>
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
  );
}
