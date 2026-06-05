import type {
  EffortModelEfficiency,
  EfficiencyBucket,
  EffortEfficiencyRow,
  ModelEfficiencyRow,
} from '../types/digest';

function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(n);
}

function fmtRate(rate: number | null): string {
  return rate != null ? `${Math.round(rate * 100)}%` : '—';
}

function rateColor(rate: number | null): string {
  if (rate == null) return 'text-neutral-500';
  if (rate >= 0.8) return 'text-emerald-400';
  if (rate >= 0.5) return 'text-amber-400';
  return 'text-red-400';
}

function cpmColor(cpm: number | null): string {
  if (cpm == null) return 'text-neutral-500';
  if (cpm <= 5) return 'text-emerald-400';
  if (cpm <= 15) return 'text-amber-400';
  return 'text-red-400';
}

function fmtCpm(cpm: number | null): string {
  return cpm != null ? `$${cpm.toFixed(2)}` : '—';
}

/**
 * A single efficiency sub-table (effort tiers OR actual models). Shares the
 * mobile-card / `sm:table` desktop idiom used by ProviderCostPanel and
 * LlmCostBreakdownPanel so the operator dashboard stays compact on both.
 */
function EfficiencySubTable({
  title,
  rows,
  label,
}: {
  title: string;
  rows: (EfficiencyBucket & { _key: string; _label: string })[];
  label: string;
}) {
  if (rows.length === 0) return null;
  return (
    <div className="mb-3 last:mb-0">
      <h3 className="text-[10px] font-semibold text-neutral-500 uppercase tracking-wider mb-1 px-1">{title}</h3>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        {/* Mobile: stacked card layout */}
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {rows.map((row) => (
            <div key={row._key} className="px-3 py-2 text-[10px]">
              <div className="mb-1 font-medium text-neutral-200 break-words">{row._label}</div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-500">
                <span>Runs: <span className="text-neutral-300 tabular-nums">{row.run_count}</span></span>
                <span>Landed: <span className={`tabular-nums ${rateColor(row.landed_rate)}`}>{row.landed_count}/{row.run_count} ({fmtRate(row.landed_rate)})</span></span>
                <span>Cost: <span className="text-neutral-300 font-mono tabular-nums">${row.total_cost_usd.toFixed(2)}</span></span>
                <span>$/Merge: <span className={`font-mono tabular-nums ${cpmColor(row.cost_per_merge)}`}>{fmtCpm(row.cost_per_merge)}</span></span>
                <span>Tokens: <span className="text-neutral-300 tabular-nums">{fmtTokens(row.input_tokens + row.output_tokens)}</span></span>
              </div>
            </div>
          ))}
        </div>
        {/* Desktop: table layout */}
        <table className="hidden sm:table w-full text-xs">
          <thead>
            <tr className="text-[10px] text-neutral-500 border-b border-neutral-800/50">
              <th className="text-left px-3 py-2 font-medium">{label}</th>
              <th className="text-right px-3 py-2 font-medium">Runs</th>
              <th className="text-right px-3 py-2 font-medium">Landed</th>
              <th className="text-right px-3 py-2 font-medium">Cost</th>
              <th className="text-right px-3 py-2 font-medium">$/Merge</th>
              <th className="text-right px-3 py-2 font-medium">Tokens</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {rows.map((row) => (
              <tr key={row._key}>
                <td className="px-3 py-2 font-medium text-neutral-200">{row._label}</td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{row.run_count}</td>
                <td className={`text-right px-3 py-2 tabular-nums ${rateColor(row.landed_rate)}`}>
                  {row.landed_count}/{row.run_count} <span className="text-[10px]">({fmtRate(row.landed_rate)})</span>
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 font-mono tabular-nums">${row.total_cost_usd.toFixed(2)}</td>
                <td className={`text-right px-3 py-2 font-mono tabular-nums ${cpmColor(row.cost_per_merge)}`}>{fmtCpm(row.cost_per_merge)}</td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{fmtTokens(row.input_tokens + row.output_tokens)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function effortRows(rows: EffortEfficiencyRow[]) {
  return rows.map((r) => ({ ...r, _key: r.effort, _label: r.effort }));
}

function modelRows(rows: ModelEfficiencyRow[]) {
  return rows.map((r) => ({
    ...r,
    _key: `${r.provider}:${r.model}`,
    _label: r.priced ? `${r.model} · ${r.provider}` : `${r.model} · ${r.provider} (unpriced)`,
  }));
}

/**
 * Operator view of cost & landed/merge-rate attributable by EFFORT tier and by
 * ACTUAL resolved model. Purely presentational — the rollup rides the digest
 * payload already fetched by DashboardHome (no extra fetch). Renders nothing
 * when the rollup is absent or carries no runs, so older backends and empty
 * windows degrade gracefully.
 *
 * Caveats are surfaced explicitly: model-fallback/downgrade runs and unpriced
 * pricing coverage gaps are shown in an amber banner so understated cost is
 * visible instead of hidden.
 */
export function EffortEfficiencyPanel({ data }: { data: EffortModelEfficiency | null | undefined }) {
  if (!data) return null;
  const { by_effort, by_model, totals, unpriced_combos, fallbacks } = data;
  if ((by_effort?.length ?? 0) === 0 && (by_model?.length ?? 0) === 0) return null;
  if (totals.run_count === 0) return null;

  const coverage = totals.unpriced_coverage_fraction;
  const hasUnpricedGap = coverage != null && coverage < 1 && totals.unpriced_run_count > 0;
  const hasFallbacks = (fallbacks?.total ?? 0) > 0;

  return (
    <section>
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Effort &amp; Model Efficiency
      </h2>

      {hasFallbacks && (
        <div className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[10px] text-amber-300">
          <span className="font-semibold">{fallbacks.total} run{fallbacks.total === 1 ? '' : 's'} fell back / downgraded</span>
          {' — '}the requested model differed from the model that actually ran, so per-tier cost reflects the cheaper resolved model.
          {fallbacks.combos.length > 0 && (
            <div className="mt-1 text-amber-400/80">
              {fallbacks.combos.map((c, i) => (
                <span key={i} className="font-mono mr-2">
                  {c.requested}&rarr;{c.effective}{c.reason ? ` (${c.reason})` : ''}&times;{c.count}
                </span>
              ))}
            </div>
          )}
        </div>
      )}

      {hasUnpricedGap && (
        <div className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-[10px] text-amber-300">
          <span className="font-semibold">Pricing coverage {fmtRate(coverage)}</span>
          {' — '}{totals.unpriced_run_count} of {totals.priced_run_count + totals.unpriced_run_count} token-bearing runs have no pricing entry, so reported cost &amp; $/merge are understated.
          {(unpriced_combos?.length ?? 0) > 0 && (
            <div className="mt-1 text-amber-400/80">
              Unpriced: <span className="font-mono">{unpriced_combos.join(', ')}</span>
            </div>
          )}
        </div>
      )}

      <EfficiencySubTable title="By Effort Tier" label="Effort" rows={effortRows(by_effort ?? [])} />
      <EfficiencySubTable title="By Actual Model" label="Provider / Model" rows={modelRows(by_model ?? [])} />
    </section>
  );
}
