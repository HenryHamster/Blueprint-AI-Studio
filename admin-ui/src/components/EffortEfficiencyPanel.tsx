import { Fragment, type ReactNode } from 'react';
import type {
  EffortModelEfficiency,
  EfficiencyBucket,
  EfficiencyResolvedModel,
  EffortEfficiencyRow,
  ModelEfficiencyRow,
  ProviderRoutingVerdict,
  ProviderRoutingShare,
  CostTelemetryReconciliation,
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
 * Headline merge figures for a bucket. The verified-merge count/rate is the
 * primary signal so degraded no-CI fall-open merges are not presented as
 * genuine landings; the raw landed count is shown secondarily only when it
 * diverges. Falls back to the raw landed figures when the backend predates the
 * verified-merge rollup (verified_* absent), keeping older payloads readable.
 */
function mergeFigures(row: EfficiencyBucket): {
  count: number;
  rate: number | null;
  raw: number;
  rawDiffers: boolean;
} {
  const hasVerified = row.verified_landed_count != null;
  return {
    count: row.verified_landed_count ?? row.landed_count,
    rate: hasVerified ? (row.verified_landed_rate ?? null) : row.landed_rate,
    raw: row.landed_count,
    rawDiffers: hasVerified && row.verified_landed_count !== row.landed_count,
  };
}

/**
 * Inline, expandable reveal of which ACTUAL models a requested effort tier ran
 * on (requested-vs-actual divergence). Renders nothing when the tier carries no
 * resolved-model breakdown (older backends / empty windows). Unpriced resolved
 * models are flagged, and a tier that ran on more than one model — or any
 * unpriced model — gets an amber summary so the divergence is obvious at a
 * glance without expanding.
 */
function ResolvedModelsReveal({ models }: { models: EfficiencyResolvedModel[] }) {
  if (models.length === 0) return null;
  const diverged = models.length > 1 || models.some((m) => !m.priced);
  return (
    <details className="text-ui-sm px-1 py-0.5">
      <summary
        className={`cursor-pointer select-none ${diverged ? 'text-amber-400/90' : 'text-neutral-500'} hover:text-neutral-300`}
      >
        Resolved models ({models.length})
      </summary>
      <ul className="mt-1 space-y-0.5 pl-3">
        {models.map((m) => (
          <li
            key={`${m.provider}:${m.model}`}
            className="flex justify-between gap-2 text-neutral-400"
          >
            <span className="break-all">
              {m.model} · {m.provider}
              {!m.priced && <span className="text-amber-400/80"> (unpriced)</span>}
            </span>
            <span className="tabular-nums whitespace-nowrap text-neutral-500">
              {m.landed_count}/{m.run_count} landed
            </span>
          </li>
        ))}
      </ul>
    </details>
  );
}

/**
 * A single efficiency sub-table (effort tiers OR actual models). Shares the
 * mobile-card / `sm:table` desktop idiom used by ProviderCostPanel and
 * LlmCostBreakdownPanel so the operator dashboard stays compact on both.
 */
type SubTableRow = EfficiencyBucket & {
  _key: string;
  _label: string;
  resolved_models?: EfficiencyResolvedModel[];
};

function EfficiencySubTable({
  title,
  caption,
  rows,
  label,
  renderExpansion,
}: {
  title: string;
  // Optional one-line help text shown beneath the title (e.g. clarifying what
  // the tier label actually measures).
  caption?: string;
  rows: SubTableRow[];
  label: string;
  // Optional per-row reveal rendered beneath the row (e.g. a tier's resolved
  // models). Returning null/undefined renders no expansion for that row.
  renderExpansion?: (row: SubTableRow) => ReactNode;
}) {
  if (rows.length === 0) return null;
  return (
    <div className="mb-3 last:mb-0">
      <h3 className="text-ui-sm font-semibold text-neutral-500 uppercase tracking-wider mb-1 px-1">{title}</h3>
      {caption && (
        <p className="text-ui-xs text-neutral-600 mb-1 px-1 normal-case tracking-normal">{caption}</p>
      )}
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        {/* Mobile: stacked card layout */}
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {rows.map((row) => {
            const m = mergeFigures(row);
            const expansion = renderExpansion?.(row);
            return (
              <div key={row._key} className="px-3 py-2 text-ui-sm">
                <div className="mb-1 font-medium text-neutral-200 break-words">{row._label}</div>
                <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-500">
                  <span>Runs: <span className="text-neutral-300 tabular-nums">{row.run_count}</span></span>
                  <span>Landed: <span className={`tabular-nums ${rateColor(m.rate)}`}>{m.count}/{row.run_count} ({fmtRate(m.rate)})</span>{m.rawDiffers && <span className="text-neutral-600"> · raw {m.raw}</span>}</span>
                  <span>Cost: <span className="text-neutral-300 font-mono tabular-nums">${row.total_cost_usd.toFixed(2)}</span></span>
                  <span>$/Merge: <span className={`font-mono tabular-nums ${cpmColor(row.cost_per_merge)}`}>{fmtCpm(row.cost_per_merge)}</span></span>
                  <span>Tokens: <span className="text-neutral-300 tabular-nums">{fmtTokens(row.input_tokens + row.output_tokens)}</span></span>
                </div>
                {expansion && <div className="mt-1">{expansion}</div>}
              </div>
            );
          })}
        </div>
        {/* Desktop: table layout */}
        <table className="hidden sm:table w-full text-xs">
          <thead>
            <tr className="text-ui-sm text-neutral-500 border-b border-neutral-800/50">
              <th className="text-left px-3 py-2 font-medium">{label}</th>
              <th className="text-right px-3 py-2 font-medium">Runs</th>
              <th className="text-right px-3 py-2 font-medium">Landed</th>
              <th className="text-right px-3 py-2 font-medium">Cost</th>
              <th className="text-right px-3 py-2 font-medium">$/Merge</th>
              <th className="text-right px-3 py-2 font-medium">Tokens</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {rows.map((row) => {
              const m = mergeFigures(row);
              const expansion = renderExpansion?.(row);
              return (
                <Fragment key={row._key}>
                  <tr>
                    <td className="px-3 py-2 font-medium text-neutral-200">{row._label}</td>
                    <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{row.run_count}</td>
                    <td className={`text-right px-3 py-2 tabular-nums ${rateColor(m.rate)}`}>
                      {m.count}/{row.run_count} <span className="text-ui-sm">({fmtRate(m.rate)})</span>
                      {m.rawDiffers && <span className="block text-ui-xs text-neutral-600 leading-tight">raw {m.raw}</span>}
                    </td>
                    <td className="text-right px-3 py-2 text-neutral-300 font-mono tabular-nums">${row.total_cost_usd.toFixed(2)}</td>
                    <td className={`text-right px-3 py-2 font-mono tabular-nums ${cpmColor(row.cost_per_merge)}`}>{fmtCpm(row.cost_per_merge)}</td>
                    <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{fmtTokens(row.input_tokens + row.output_tokens)}</td>
                  </tr>
                  {expansion && (
                    <tr>
                      <td colSpan={6} className="px-3 pb-2 pt-0">{expansion}</td>
                    </tr>
                  )}
                </Fragment>
              );
            })}
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
 * Compact callout for the provider-routing A/B verdict: ADOPT/REJECT/INSUFFICIENT_DATA
 * badge, cost ratio, and verified-merge-rate delta so the operator sees the routing
 * payoff without reading raw JSON.
 */
function ProviderRoutingVerdictCallout({ verdict }: { verdict: ProviderRoutingVerdict }) {
  const { state, supporting, recommended_action } = verdict;
  const badgeColor: Record<string, string> = {
    ADOPT: 'bg-emerald-500/20 text-emerald-300 border-emerald-500/40',
    REJECT: 'bg-red-500/20 text-red-300 border-red-500/40',
    INSUFFICIENT_DATA: 'bg-amber-500/20 text-amber-300 border-amber-500/40',
  };
  return (
    <div className="mb-2 rounded border border-neutral-700/50 bg-neutral-900/30 px-2.5 py-1.5 text-ui-sm">
      <div className="flex items-center gap-2 mb-1">
        <span
          className={`inline-block px-1.5 py-px rounded text-ui-xs font-semibold border ${badgeColor[state]}`}
        >
          {state}
        </span>
        <span className="text-neutral-300 font-medium">Provider Routing A/B</span>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-neutral-500">
        {supporting.cost_ratio != null && (
          <span>
            Cost: <span className="text-neutral-300 tabular-nums">{supporting.cost_ratio.toFixed(2)}x</span> of Anthropic
          </span>
        )}
        {supporting.verified_rate_delta_pct != null && (
          <span>
            Merge rate delta:{' '}
            <span
              className={
                state === 'ADOPT'
                  ? 'text-emerald-400'
                  : state === 'REJECT'
                    ? 'text-red-400'
                    : 'text-neutral-300'
              }
            >
              {supporting.verified_rate_delta_pct > 0 ? '+' : ''}
              {supporting.verified_rate_delta_pct}pp
            </span>
          </span>
        )}
      </div>
      <div className="text-ui-xs text-neutral-600 mt-0.5">{recommended_action}</div>
    </div>
  );
}

/**
 * Compact distribution block showing per-provider run count, verified landed
 * rate, and cost-per-merge from the provider_routing_share data. Renders
 * nothing when the share data is absent or empty.
 */
function ProviderRoutingShareBlock({ share }: { share: ProviderRoutingShare }) {
  const { by_provider, total_run_count, total_pr_eligible_run_count } = share;
  if (!by_provider || by_provider.length === 0) return null;

  return (
    <div className="mb-2 rounded border border-neutral-700/50 bg-neutral-900/30 px-2.5 py-1.5 text-ui-sm">
      <h3 className="text-ui-xs font-semibold text-neutral-500 uppercase tracking-wider mb-1">
        Provider Routing Distribution
      </h3>
      <table className="w-full text-ui-xs">
        <thead>
          <tr className="text-neutral-500 border-b border-neutral-800/50">
            <th className="text-left px-1 py-1 font-medium">Provider</th>
            <th className="text-right px-1 py-1 font-medium">Runs</th>
            <th className="text-right px-1 py-1 font-medium">PR-eligible</th>
            <th className="text-right px-1 py-1 font-medium">Verified Rate</th>
            <th className="text-right px-1 py-1 font-medium">$/Merge</th>
            <th className="text-right px-1 py-1 font-medium">Cost</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-neutral-800/30">
          {by_provider.map((p) => (
            <tr key={p.provider}>
              <td className="px-1 py-1 font-medium text-neutral-200 capitalize">{p.provider}</td>
              <td className="text-right px-1 py-1 text-neutral-300 tabular-nums">{p.run_count}</td>
              <td className="text-right px-1 py-1 text-neutral-300 tabular-nums">{p.pr_eligible_run_count}</td>
              <td className={`text-right px-1 py-1 tabular-nums ${rateColor(p.verified_landed_rate)}`}>
                {fmtRate(p.verified_landed_rate)}
              </td>
              <td className={`text-right px-1 py-1 font-mono tabular-nums ${cpmColor(p.cost_per_merge)}`}>
                {fmtCpm(p.cost_per_merge)}
              </td>
              <td className="text-right px-1 py-1 text-neutral-300 font-mono tabular-nums">
                ${p.total_cost_usd.toFixed(2)}
              </td>
            </tr>
          ))}
        </tbody>
        <tfoot>
          <tr className="border-t border-neutral-800/50 text-neutral-400">
            <td className="px-1 py-1 font-medium">Total</td>
            <td className="text-right px-1 py-1 tabular-nums">{total_run_count}</td>
            <td className="text-right px-1 py-1 tabular-nums">{total_pr_eligible_run_count}</td>
            <td colSpan={3} />
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

/**
 * Compact trust indicator for cost-telemetry reconciliation: shows whether the
 * primary cost-telemetry path agrees with a structurally independent direct-path
 * recomputation within declared tolerance. Green when within_tolerance is true,
 * amber with the absolute delta value when the paths diverge beyond tolerance.
 * Follows the same badge-and-detail idiom as ProviderRoutingVerdictCallout.
 */
function CostTelemetryReconciliationCallout({
  reconciliation,
}: {
  reconciliation: CostTelemetryReconciliation;
}) {
  const { within_tolerance, absolute_delta, cache_hit_rate_within_tolerance, task_count, tolerance } = reconciliation;

  if (within_tolerance && cache_hit_rate_within_tolerance) {
    return (
      <div className="mb-2 rounded border border-emerald-500/30 bg-emerald-500/10 px-2.5 py-1.5 text-ui-sm">
        <div className="flex items-center gap-2">
          <span className="inline-block px-1.5 py-px rounded text-ui-xs font-semibold border bg-emerald-500/20 text-emerald-300 border-emerald-500/40">
            ✓ Reconciled
          </span>
          <span className="text-neutral-300 font-medium">Cost Telemetry</span>
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-neutral-500 mt-1">
          <span>
            Delta: <span className="text-emerald-300 tabular-nums">${absolute_delta.toFixed(7)}</span>
          </span>
          <span>
            Tolerance: <span className="text-neutral-300 tabular-nums">${tolerance}</span>
          </span>
          <span>
            Tasks: <span className="text-neutral-300 tabular-nums">{task_count}</span>
          </span>
        </div>
        <div className="text-ui-xs text-emerald-600/70 mt-0.5">
          Cache-hit rate and cost agree within tolerance — telemetry is trustworthy.
        </div>
      </div>
    );
  }

  return (
    <div className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-ui-sm">
      <div className="flex items-center gap-2">
        <span className="inline-block px-1.5 py-px rounded text-ui-xs font-semibold border bg-amber-500/20 text-amber-300 border-amber-500/40">
          ⚠ Mismatch
        </span>
        <span className="text-neutral-300 font-medium">Cost Telemetry</span>
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-neutral-500 mt-1">
        <span>
          Delta: <span className="text-amber-300 tabular-nums">${absolute_delta.toFixed(7)}</span>
        </span>
        {!within_tolerance && (
          <span>
            Cost tolerance: <span className="text-amber-300 tabular-nums">±${tolerance}</span>
          </span>
        )}
        {!cache_hit_rate_within_tolerance && (
          <span>
            Cache-hit rate: <span className="text-amber-300 tabular-nums">diverged</span>
          </span>
        )}
        <span>
          Tasks: <span className="text-neutral-300 tabular-nums">{task_count}</span>
        </span>
      </div>
      <div className="text-ui-xs text-amber-600/70 mt-0.5">
        Cost telemetry paths diverge — investigate pricing or aggregation discrepancies.
      </div>
    </div>
  );
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
  const { by_effort, by_model, totals, unpriced_combos, fallbacks, provider_routing_verdict, provider_routing_share, cost_telemetry_reconciliation } = data;
  if ((by_effort?.length ?? 0) === 0 && (by_model?.length ?? 0) === 0) return null;
  if (totals.run_count === 0) return null;

  const coverage = totals.unpriced_coverage_fraction;
  const hasUnpricedGap = coverage != null && coverage < 1 && totals.unpriced_run_count > 0;
  const hasFallbacks = (fallbacks?.total ?? 0) > 0;

  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Effort &amp; Model Efficiency
      </h2>

      {hasFallbacks && (
        <div className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-ui-sm text-amber-300">
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
        <div className="mb-2 rounded border border-amber-500/30 bg-amber-500/10 px-2.5 py-1.5 text-ui-sm text-amber-300">
          <span className="font-semibold">Pricing coverage {fmtRate(coverage)}</span>
          {' — '}{totals.unpriced_run_count} of {totals.priced_run_count + totals.unpriced_run_count} token-bearing runs have no pricing entry, so reported cost &amp; $/merge are understated.
          {(unpriced_combos?.length ?? 0) > 0 && (
            <div className="mt-1 text-amber-400/80">
              Unpriced: <span className="font-mono">{unpriced_combos.join(', ')}</span>
            </div>
          )}
        </div>
      )}

      {provider_routing_verdict && <ProviderRoutingVerdictCallout verdict={provider_routing_verdict} />}
      {provider_routing_share && <ProviderRoutingShareBlock share={provider_routing_share} />}
      {cost_telemetry_reconciliation && <CostTelemetryReconciliationCallout reconciliation={cost_telemetry_reconciliation} />}

      <EfficiencySubTable
        title="By Effective Effort Tier"
        caption="Effective routing effort the dispatcher resolved to select the model (payload → context → task-type → category), not just effort explicitly requested in the payload — so every task type is attributed to the tier that drove its cost."
        label="Effort"
        rows={effortRows(by_effort ?? [])}
        renderExpansion={(row) => {
          const models = row.resolved_models ?? [];
          return models.length > 0 ? <ResolvedModelsReveal models={models} /> : null;
        }}
      />
      <EfficiencySubTable title="By Actual Model" label="Provider / Model" rows={modelRows(by_model ?? [])} />
    </section>
  );
}
