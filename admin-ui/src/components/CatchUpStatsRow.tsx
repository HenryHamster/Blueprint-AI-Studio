import { useMemo } from 'react';
import { AlertTriangle, CheckCircle, XCircle, Loader2, DollarSign } from 'lucide-react';
import { parseTimestamp } from '../utils/time';
import { TASK_TYPE_STYLES } from './TaskCard';
import type { DigestData } from '../types/digest';
import type { DirectionProgress } from './DirectionProgressSection';

export interface CatchUpStatsRowProps {
  digest: DigestData<DirectionProgress>;
}

/**
 * The CatchUp headline "stats row": success rate (with sparkline), first-attempt
 * success, pipeline acceptance/conversion, product focus, type distribution,
 * pending/completed/failed counts, cost metrics, provisioning overhead, daily
 * spend, and research/PR/running/queued counts.
 *
 * Extracted verbatim from CatchUpPanel's inline render. `costs` and
 * `directionCostTotals` are derived here (memoised) instead of being threaded
 * through props, keeping this block self-contained.
 */
export function CatchUpStatsRow({ digest }: CatchUpStatsRowProps) {
  const costs = useMemo(
    () => (digest.trend_history ?? []).filter(p => p.period_cost_usd != null).map(p => p.period_cost_usd as number),
    [digest.trend_history]
  );

  const directionCostTotals = useMemo(() => {
    const totals: Record<string, number> = {};
    for (const dp of digest.direction_progress ?? []) {
      if (!dp.cost_by_scope) continue;
      for (const [scope, usd] of Object.entries(dp.cost_by_scope)) {
        if (typeof usd === 'number' && usd > 0) {
          totals[scope] = (totals[scope] ?? 0) + usd;
        }
      }
    }
    return totals;
  }, [digest.direction_progress]);

  return (
    <div className="flex flex-wrap gap-x-3 gap-y-2 text-[11px] text-neutral-400">
      {digest.success_rate != null && (
        <span
          className={`flex items-center gap-1 ${digest.success_rate >= 0.8 ? 'text-emerald-400' : digest.success_rate >= 0.5 ? 'text-amber-400' : 'text-red-400'}`}
          aria-label={`Success rate: ${Math.round(digest.success_rate * 100)}% (${digest.success_rate >= 0.8 ? 'healthy' : digest.success_rate >= 0.5 ? 'warning' : 'critical'})`}
        >
          {Math.round(digest.success_rate * 100)}% success
          {digest.success_rate_delta != null && (
            <span
              className={digest.success_rate_delta > 0 ? 'text-emerald-400' : digest.success_rate_delta < 0 ? 'text-red-400' : 'text-neutral-500'}
              title={digest.prev_success_rate != null ? `was ${Math.round(digest.prev_success_rate * 100)}% last period` : undefined}
            >
              {digest.success_rate_delta > 0 ? '+' : ''}{Math.round(digest.success_rate_delta * 100)}%
            </span>
          )}
          {digest.trend_history && digest.trend_history.length >= 3 && (
            <div className="flex items-end gap-px h-3 ml-1">
              {digest.trend_history.map((p, i) => {
                const rate = p.success_rate ?? 0;
                const color = rate >= 0.8 ? 'bg-emerald-400' : rate >= 0.5 ? 'bg-amber-400' : 'bg-red-400';
                const dateLabel = (() => {
                  const d = parseTimestamp(p.period_end);
                  return isNaN(d.getTime()) ? p.period_end : d.toLocaleDateString(undefined, {month: 'short', day: 'numeric'});
                })();
                const ratePart = p.success_rate == null ? 'no data' : `${Math.round(p.success_rate * 100)}%`;
                const titleParts = [`Success: ${ratePart}`];
                if (p.first_attempt_success_rate != null) {
                  titleParts.push(`1st attempt: ${Math.round(p.first_attempt_success_rate * 100)}%`);
                }
                if (p.period_cost_usd != null) {
                  titleParts.push(`Cost: $${p.period_cost_usd.toFixed(2)}`);
                }
                const title = `${dateLabel}: ${titleParts.join(' | ')} (${p.completed} done, ${p.failed} failed)`;
                return <div key={i} title={title} className={`w-1 rounded-sm ${p.success_rate == null ? 'bg-neutral-700' : color}`} style={{height: `${Math.max(rate * 100, 8)}%`}} />;
              })}
            </div>
          )}
        </span>
      )}
      {digest.first_attempt_success_rate && digest.first_attempt_success_rate.sample_size >= 5 && (() => {
        const fas = digest.first_attempt_success_rate;
        if (!fas) return null;
        const tone = (r: number | null) => r == null ? 'text-neutral-500' : r >= 0.8 ? 'text-emerald-400' : r >= 0.5 ? 'text-amber-400' : 'text-red-400';
        const compareTone = (w: number | null, wo: number | null) =>
          w == null || wo == null ? 'text-neutral-500' : w > wo ? 'text-emerald-400' : w < wo ? 'text-red-400' : 'text-amber-400';
        const fmt = (r: number | null) => r == null ? '—' : `${Math.round(r * 100)}%`;
        return (
          <>
            {fas.overall != null && (
              <span
                className={`flex items-center gap-1 ${tone(fas.overall)}`}
                title={`First-attempt success rate (n=${fas.sample_size})${fas.trend ? ` — trend: ${fas.trend}` : ''}`}
              >
                {Math.round(fas.overall * 100)}% 1st-attempt
                {fas.trend === 'improving' && <span className="text-emerald-400">↑</span>}
                {fas.trend === 'declining' && <span className="text-red-400">↓</span>}
                {fas.trend === 'stable' && <span className="text-neutral-500">→</span>}
              </span>
            )}
            {(fas.with_playbook != null || fas.without_playbook != null) && (
              <span
                className={compareTone(fas.with_playbook, fas.without_playbook)}
                title="First-attempt success with vs without playbook"
              >
                Playbook: {fmt(fas.with_playbook)} vs {fmt(fas.without_playbook)} without
              </span>
            )}
            {(fas.with_arch_context != null || fas.without_arch_context != null) && (
              <span
                className={compareTone(fas.with_arch_context, fas.without_arch_context)}
                title="First-attempt success with vs without arch context"
              >
                Arch context: {fmt(fas.with_arch_context)} vs {fmt(fas.without_arch_context)} without
              </span>
            )}
            {(fas.with_measurements != null || fas.without_measurements != null) && (
              <span
                className={compareTone(fas.with_measurements ?? null, fas.without_measurements ?? null)}
                title="First-attempt success with vs without measurements"
              >
                Measurements: {fmt(fas.with_measurements ?? null)} vs {fmt(fas.without_measurements ?? null)} without
              </span>
            )}
            {fas.by_scope && Object.keys(fas.by_scope).length > 0 && (() => {
              const order = ['small', 'medium', 'large'];
              const byScope = fas.by_scope;
              if (!byScope) return null;
              const entries = Object.entries(byScope)
                .filter(([, v]) => typeof v === 'number')
                .sort(([a], [b]) => {
                  const ai = order.indexOf(a); const bi = order.indexOf(b);
                  return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
                });
              if (entries.length === 0) return null;
              return (
                <span
                  className="flex items-center gap-1 text-neutral-400"
                  title="First-attempt success rate by task scope"
                >
                  <span>Scope:</span>
                  {entries.map(([scope, rate], i) => (
                    <span key={scope} className={tone(rate)}>
                      {scope}: {Math.round(rate * 100)}%{i < entries.length - 1 ? ' |' : ''}
                    </span>
                  ))}
                </span>
              );
            })()}
            {fas.by_type && Object.keys(fas.by_type).length > 0 && (() => {
              const byType = fas.by_type;
              if (!byType) return null;
              const entries = Object.entries(byType)
                .filter(([, v]) => typeof v === 'number')
                .sort((a, b) => a[0].localeCompare(b[0]));
              if (entries.length === 0) return null;
              return (
                <span
                  className="flex items-center gap-1 text-neutral-400"
                  title="First-attempt success rate by task type"
                >
                  <span>By type:</span>
                  {entries.map(([type, rate], i) => (
                    <span key={type} className={tone(rate)}>
                      {type}: {Math.round(rate * 100)}%{i < entries.length - 1 ? ' |' : ''}
                    </span>
                  ))}
                </span>
              );
            })()}
            {fas.first_attempt_by_review_category && Object.keys(fas.first_attempt_by_review_category).length > 0 && (() => {
              const fbrc = fas.first_attempt_by_review_category;
              if (!fbrc) return null;
              const entries = Object.entries(fbrc)
                .filter(([, v]) => typeof v === 'number' && v > 0)
                .sort((a, b) => b[1] - a[1])
                .slice(0, 3);
              if (entries.length === 0) return null;
              return (
                <span
                  className="flex items-center gap-1 text-neutral-400"
                  title="Top review categories responsible for first-attempt rejections"
                >
                  <span>Rejections:</span>
                  {entries.map(([cat, frac]) => (
                    <span
                      key={cat}
                      className="px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-300"
                    >
                      {cat} {Math.round(frac * 100)}%
                    </span>
                  ))}
                </span>
              );
            })()}
          </>
        );
      })()}
      {digest.pipeline_acceptance_rate != null && (() => {
        const par = digest.pipeline_acceptance_rate;
        if (par == null) return null;
        const pct = Math.round(par * 100);
        const tone = pct >= 60 ? 'text-emerald-400' : pct >= 30 ? 'text-amber-400' : 'text-red-400';
        return (
          <span className={tone} title="PRs merged / PRs decided (merged + rejected)">
            {pct}% accepted
          </span>
        );
      })()}
      {digest.pipeline_conversion_rate != null && (() => {
        const pcr = digest.pipeline_conversion_rate;
        if (pcr == null) return null;
        const pct = Math.round(pcr * 100);
        const tone = pct >= 60 ? 'text-emerald-400' : pct >= 30 ? 'text-amber-400' : 'text-red-400';
        return (
          <span className={tone} title="PRs merged / PR-eligible tasks created (excludes research/audit/memo/investigate)">
            {pct}% conv
          </span>
        );
      })()}
      {digest.overall_product_ratio != null && (() => {
        const opr = digest.overall_product_ratio;
        if (opr == null) return null;
        const pct = Math.round(opr * 100);
        const tone = opr >= 0.6 ? 'text-emerald-400' : opr >= 0.4 ? 'text-amber-400' : 'text-slate-400';
        return (
          <span className={tone} title="Fraction of tasks shipping user-facing product value vs structural/overhead work">
            {pct}% product focus
          </span>
        );
      })()}
      {digest.overall_type_distribution && Object.keys(digest.overall_type_distribution).length > 0 && (
        <span className="flex items-center gap-1" title="Task type distribution across all directions">
          {Object.entries(digest.overall_type_distribution)
            .sort((a, b) => b[1] - a[1])
            .map(([type, count]) => {
              const style = TASK_TYPE_STYLES[type];
              return (
                <span
                  key={type}
                  className={`${style?.bg ?? 'bg-neutral-500/10 border-neutral-500/20'} ${style?.text ?? 'text-neutral-400'} border px-1.5 py-0.5 rounded font-semibold uppercase tracking-wider text-[10px]`}
                >
                  {style?.label ?? type} {count}
                </span>
              );
            })}
        </span>
      )}
      {digest.pending_approval_count > 0 && (
        <span className="flex items-center gap-1">
          <AlertTriangle className="w-3 h-3 text-amber-400" />
          {digest.pending_approval_count} pending approval
        </span>
      )}
      {digest.completed_count > 0 && (
        <span className="flex items-center gap-1">
          <CheckCircle className="w-3 h-3 text-emerald-500" />
          {digest.completed_count} completed
          {digest.completed_delta != null && digest.completed_delta !== 0 && (
            <span
              className={digest.completed_delta > 0 ? 'text-emerald-400' : 'text-red-400'}
              title={digest.prev_completed_count != null ? `was ${digest.prev_completed_count} last period` : undefined}
            >
              {digest.completed_delta > 0 ? '↑' : '↓'}
            </span>
          )}
        </span>
      )}
      {digest.failed_count > 0 && (
        <span className="flex items-center gap-1">
          <XCircle className="w-3 h-3 text-red-500" />
          {digest.failed_count} failed
          {digest.failed_delta != null && digest.failed_delta !== 0 && (
            <span
              className={digest.failed_delta > 0 ? 'text-red-400' : 'text-emerald-400'}
              title={digest.prev_failed_count != null ? `was ${digest.prev_failed_count} last period` : undefined}
            >
              {digest.failed_delta > 0 ? '↑' : '↓'}
            </span>
          )}
          {digest.total_failed_cost_usd != null && digest.total_failed_cost_usd > 0 && (
            <span className="inline-flex items-center gap-0.5 text-neutral-500 font-mono">
              ·<DollarSign className="w-2.5 h-2.5" />{digest.total_failed_cost_usd.toFixed(2)} wasted
            </span>
          )}
        </span>
      )}
      {digest.total_estimated_cost_usd != null && digest.total_estimated_cost_usd > 0 && (
        <span className="inline-flex items-center gap-0.5 text-neutral-500 font-mono" title="Total estimated spend">
          <DollarSign className="w-2.5 h-2.5" />{digest.total_estimated_cost_usd.toFixed(2)} total
          {(() => {
            if (costs.length < 2) return null;
            const curr = costs[costs.length - 1];
            const prev = costs[costs.length - 2];
            const delta = curr - prev;
            if (Math.abs(delta) < 0.01) return null;
            const up = delta > 0;
            return (
              <span
                className={up ? "text-amber-600" : "text-emerald-600"}
                title={`Period cost change: $${prev.toFixed(2)} → $${curr.toFixed(2)}`}
              >
                {up ? "↑" : "↓"}${Math.abs(delta).toFixed(2)}
              </span>
            );
          })()}
        </span>
      )}
      {(digest.spot_cost_usd != null || digest.on_demand_cost_usd != null) && (
        (digest.spot_cost_usd ?? 0) + (digest.on_demand_cost_usd ?? 0) > 0
      ) && (
        <span
          className="inline-flex items-center gap-0.5 text-neutral-500 font-mono"
          title="Spot vs on-demand compute cost split"
        >
          Spot: ${(digest.spot_cost_usd ?? 0).toFixed(2)} / On-demand: ${(digest.on_demand_cost_usd ?? 0).toFixed(2)}
        </span>
      )}
      {(() => {
        if (!digest.direction_progress || digest.direction_progress.length === 0) return null;
        const order = ['small', 'medium', 'large'];
        const entries = order.filter(s => directionCostTotals[s] != null);
        if (entries.length === 0) return null;
        const title = `Cost by scope: ${entries.map(s => `${s} $${directionCostTotals[s].toFixed(2)}`).join(' · ')}`;
        return (
          <span className="inline-flex items-center gap-1 font-mono text-neutral-500" title={title}>
            {entries.map(s => (
              <span key={s} className="inline-flex items-center gap-0.5 rounded bg-neutral-100 px-1 py-0.5 text-[10px]">
                {s} ${directionCostTotals[s].toFixed(2)}
              </span>
            ))}
          </span>
        );
      })()}
      {digest.provisioning_overhead_pct != null && digest.provisioning_overhead_pct > 0 && (() => {
        const pop = digest.provisioning_overhead_pct;
        if (pop == null) return null;
        const pctDisplay = pop * 100;
        const tone = pctDisplay < 10
          ? 'text-emerald-500'
          : pctDisplay <= 25
            ? 'text-amber-500'
            : 'text-red-500';
        const overheadUsd = digest.provisioning_overhead_usd ?? 0;
        return (
          <span
            className={`inline-flex items-center gap-0.5 font-mono ${tone}`}
            title={`Provisioning overhead: $${overheadUsd.toFixed(2)} (${pctDisplay.toFixed(1)}%)`}
          >
            Provisioning overhead: {pctDisplay.toFixed(1)}%
          </span>
        );
      })()}
      {digest.daily_cost_usd != null && digest.daily_cost_usd > 0 && (() => {
        const spent = digest.daily_cost_usd;
        if (spent == null) return null;
        const ceiling = digest.daily_cost_ceiling_usd ?? 0;
        const ratio = ceiling > 0 ? spent / ceiling : 0;
        const tone = ceiling <= 0
          ? 'text-neutral-500'
          : ratio >= 0.9
            ? 'text-red-500'
            : ratio >= 0.7
              ? 'text-amber-500'
              : 'text-emerald-500';
        const title = ceiling > 0
          ? `Today: $${spent.toFixed(2)} of $${ceiling.toFixed(2)} daily ceiling (${Math.round(ratio * 100)}%)`
          : `Today: $${spent.toFixed(2)} (no daily ceiling set)`;
        return (
          <span className={`inline-flex items-center gap-0.5 font-mono ${tone}`} title={title}>
            <DollarSign className="w-2.5 h-2.5" />{spent.toFixed(2)} today
            {ceiling > 0 && <span className="text-neutral-500">/{ceiling.toFixed(0)}</span>}
          </span>
        );
      })()}
      {digest.research_cycles > 0 && (
        <span>{digest.research_cycles} research cycle{digest.research_cycles !== 1 ? 's' : ''}</span>
      )}
      {digest.prs_opened > 0 && (
        <span>{digest.prs_opened} PR{digest.prs_opened !== 1 ? 's' : ''} opened</span>
      )}
      {digest.running_count > 0 && (
        <span className="flex items-center gap-1">
          <Loader2 className="w-3 h-3 text-neutral-500 animate-spin" />
          {digest.running_count} running
        </span>
      )}
      {digest.queued_count > 0 && (
        <span>{digest.queued_count} queued</span>
      )}
    </div>
  );
}
