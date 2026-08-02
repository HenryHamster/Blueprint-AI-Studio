import {
  ExampleList,
  deltaColorClass,
  formatDelta,
  fpRateColorClass,
  isSampleTooSmall,
} from './primitives';
import type {
  DirectionSrEntry,
  EdgesTrend,
  SrCoFailurePair,
  SrMissExample,
  SrTrendData,
} from './types';

function edgesTrendColor(trend: string | null | undefined): string {
  if (trend === 'improving') return 'text-emerald-400';
  if (trend === 'regressing') return 'text-red-400';
  return 'text-neutral-500';
}

export function EdgesTrendBreakdown({ trend }: { trend: EdgesTrend | null | undefined }) {
  if (!trend || !trend.current) return null;
  const curr = trend.current;
  const prev = trend.previous;
  const delta = trend.tasks_with_edges_rate_delta;
  const trendVal = trend.trend;
  const history = trend.rate_history ?? [];
  const cycleLabel =
    trend.bucket_mode === 'cycle' && trend.current_cycle_id
      ? `cycle ${trend.current_cycle_id.slice(-6)} vs ${trend.previous_cycle_id ? trend.previous_cycle_id.slice(-6) : '–'}`
      : null;
  return (
    <div data-testid="edges-trend-breakdown" className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[220px]">
      <div className="flex items-baseline gap-2 mb-1">
        <p className="text-ui-xs text-neutral-500 uppercase tracking-wide">Edges considered</p>
        {cycleLabel && <span className="text-ui-xs text-neutral-600 font-mono">{cycleLabel}</span>}
        {curr.sample_size != null && (
          <span className="text-ui-xs text-neutral-600 font-mono ml-auto">n={curr.sample_size}</span>
        )}
      </div>
      <div className="flex items-center gap-2 text-ui-sm">
        <span className="text-neutral-400 flex-1">tasks with edges</span>
        <span className={`font-mono tabular-nums ${edgesTrendColor(trendVal)}`}>
          {curr.tasks_with_edges_rate != null ? `${Math.round(curr.tasks_with_edges_rate * 100)}%` : '–'}
        </span>
        {delta != null && (
          <span className={`font-mono tabular-nums text-xs ${deltaColorClass(-delta)}`}>
            {formatDelta(delta)}
          </span>
        )}
      </div>
      {curr.unhandled_edge_flagged_rate != null && (
        <div className="flex items-center gap-2 text-ui-sm" title="Share of enumerated edges the agent itself marked as not handled — its honesty about uncertainty.">
          <span className="text-neutral-400 flex-1">unhandled flagged</span>
          <span className="font-mono tabular-nums text-neutral-300">
            {Math.round(curr.unhandled_edge_flagged_rate * 100)}%
          </span>
        </div>
      )}
      {(curr.correctness_rejection_rate_with_edges != null || curr.correctness_rejection_rate_without_edges != null) && (() => {
        const withEdges = curr.correctness_rejection_rate_with_edges;
        const withoutEdges = curr.correctness_rejection_rate_without_edges;
        const gap = withEdges != null && withoutEdges != null ? withoutEdges - withEdges : null;
        return (
          <div className="flex items-center gap-2 text-ui-sm">
            <span className="text-neutral-400 flex-1" title="Human-reviewer correctness rejection rate, split by whether the task enumerated edges.">
              correctness rej w/wo
            </span>
            <span className="font-mono tabular-nums text-neutral-300" title="correctness rejection rate when edges were enumerated">
              {withEdges != null ? `${Math.round(withEdges * 100)}%` : '–'}
            </span>
            <span className="text-neutral-600 text-ui-xs">/</span>
            <span className="font-mono tabular-nums text-neutral-300" title="correctness rejection rate when edges were NOT enumerated">
              {withoutEdges != null ? `${Math.round(withoutEdges * 100)}%` : '–'}
            </span>
            {gap != null && (
              <span
                className={`font-mono tabular-nums text-ui-xs ${deltaColorClass(-gap)}`}
                title="gap = without-edges minus with-edges; positive = edges correlate with fewer correctness rejections"
              >
                {`Δ${formatDelta(gap)}`}
              </span>
            )}
          </div>
        );
      })()}
      {(curr.correctness_rejection_rate_with_unhandled_flag != null || curr.correctness_rejection_rate_with_all_handled != null) && (() => {
        const unhandled = curr.correctness_rejection_rate_with_unhandled_flag;
        const allHandled = curr.correctness_rejection_rate_with_all_handled;
        const gap = unhandled != null && allHandled != null ? unhandled - allHandled : null;
        return (
          <div className="flex items-center gap-2 text-ui-sm">
            <span
              className="text-neutral-400 flex-1"
              title="Higher rate-with-unhandled-flag than rate-with-all-handled means the agent's honesty about unhandled edges is informative — reviewers consistently catch issues the author flagged."
            >
              rej w/unhandled-flag vs all-handled
            </span>
            <span className="font-mono tabular-nums text-neutral-300" title="correctness rejection rate when ≥1 author-origin edge was flagged unhandled">
              {unhandled != null ? `${Math.round(unhandled * 100)}%` : '–'}
            </span>
            <span className="text-neutral-600 text-ui-xs">/</span>
            <span className="font-mono tabular-nums text-neutral-300" title="correctness rejection rate when all author-origin edges were marked handled">
              {allHandled != null ? `${Math.round(allHandled * 100)}%` : '–'}
            </span>
            {gap != null && (
              <span
                className={`font-mono tabular-nums text-ui-xs ${deltaColorClass(-gap)}`}
                title="gap = with-unhandled-flag minus with-all-handled; positive = author's unhandled-flag signal is informative"
              >
                {`Δ${formatDelta(gap)}`}
              </span>
            )}
          </div>
        );
      })()}
      {history.length > 0 && (
        <div className="flex items-center gap-1 mt-1">
          <span className="text-ui-xs text-neutral-600">history</span>
          <div className="flex gap-0.5">
            {history.map((rate, index) => (
              <span key={index} className="text-ui-xs font-mono text-neutral-500">{Math.round(rate * 100)}%</span>
            ))}
          </div>
        </div>
      )}
      {prev && prev.tasks_with_edges_rate != null && (
        <div className="flex items-center gap-2 text-ui-sm pt-1 border-t border-neutral-800/60">
          <span className="text-neutral-600 flex-1">prev</span>
          <span className="font-mono tabular-nums text-neutral-500">
            {Math.round(prev.tasks_with_edges_rate * 100)}%
          </span>
        </div>
      )}
    </div>
  );
}

export function DirectionSrBreakdownPanel({
  entries,
  onDirectionClick,
  trends,
}: {
  entries: DirectionSrEntry[];
  onDirectionClick?: (directionId: string) => void;
  /**
   * Snapshot-level freshness signals from the parent SR trend snapshot. The
   * freshness applies to the whole snapshot (not per-direction), so it is
   * passed through from DashboardHome. When absent, no badge renders — keeping
   * existing two-argument call sites and tests unchanged.
   */
  trends?: Pick<SrTrendData, 'is_stale' | 'data_sufficient' | 'last_sufficient_computed_at' | 'computed_at'>;
}) {
  const sorted = [...entries].sort((a, b) => (a.overall_catch_rate ?? 1) - (b.overall_catch_rate ?? 1));
  if (sorted.length === 0) return null;
  return (
    <div data-testid="direction-sr-breakdown-panel" className="flex flex-col gap-1.5">
      {(trends?.is_stale || trends?.data_sufficient === false) && (
        <div className="flex flex-wrap items-center gap-2">
          {trends?.is_stale && (
            <span
              data-testid="sr-trend-stale-badge"
              className="text-ui-xs text-amber-400 font-mono"
              title={`Trend data is stale — last computed ${trends.computed_at ? new Date(trends.computed_at).toLocaleDateString() : 'unknown'}. Numbers may not reflect current production.`}
            >
              stale · as of {trends.computed_at ? new Date(trends.computed_at).toLocaleDateString() : '?'}
            </span>
          )}
          {trends?.data_sufficient === false && (() => {
            const lastRealAt = trends.last_sufficient_computed_at ?? trends.computed_at;
            const lastRealDate = lastRealAt ? new Date(lastRealAt).toLocaleDateString() : '?';
            return (
              <span
                data-testid="sr-trend-insufficient-badge"
                className="text-ui-xs text-amber-500 font-mono"
                title="Most recent measurement windows had insufficient reviewer-finding data — displayed numbers are frozen and may not reflect current production."
              >
                frozen · last real data {lastRealDate}
              </span>
            );
          })()}
        </div>
      )}
      {sorted.map(direction => {
        const rate = direction.overall_catch_rate;
        const rateColor = rate == null
          ? 'text-neutral-500'
          : rate >= 0.7
            ? 'text-emerald-400'
            : rate >= 0.4
              ? 'text-amber-400'
              : 'text-red-400';
        const categories = Object.entries(direction.categories).sort(([, a], [, b]) => (b.miss_rate ?? 0) - (a.miss_rate ?? 0));
        return (
          <div key={direction.direction_id} className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2">
            <div className="flex items-baseline gap-2 mb-0.5">
              <span className={`font-mono font-bold tabular-nums ${rateColor}`} title="overall SR catch rate for this direction">
                {rate != null ? `${Math.round(rate * 100)}%` : '–'}
              </span>
              <span
                className={`text-ui-sm text-neutral-300 truncate flex-1${onDirectionClick ? ' cursor-pointer hover:underline' : ''}`}
                title={direction.direction_text}
                onClick={() => onDirectionClick?.(direction.direction_id)}
                role={onDirectionClick ? 'button' : undefined}
              >
                {direction.direction_text || direction.direction_id}
              </span>
              {direction.mixed_prompt_versions && (
                <span className="text-ui-xs text-amber-400 font-mono shrink-0" title="Tasks from multiple SR prompt versions — attribution unreliable">mixed v</span>
              )}
              {!direction.mixed_prompt_versions && direction.prompt_version && (
                <span className="text-ui-xs text-amber-600 font-mono shrink-0" title="SR prompt version for this window">{`v=${direction.prompt_version.slice(0, 7)}`}</span>
              )}
              <span className="text-ui-xs text-neutral-600 shrink-0">n={direction.sample_size}</span>
            </div>
            {categories.length > 0 && (
              <div className="flex flex-col gap-0.5">
                {categories.map(([cat, stat]) => {
                  const tooSmall = isSampleTooSmall(stat.sample_count);
                  return (
                    <div key={cat} className="text-ui-sm">
                      <div className="flex items-center gap-1.5">
                        <span className="text-neutral-400">{cat}</span>
                        {stat.miss_rate != null && (
                          tooSmall ? (
                            <span className="font-mono text-neutral-500">{Math.round(stat.miss_rate * 100)}/{stat.sample_count} — too small</span>
                          ) : (
                            <span className="font-mono text-orange-400">{Math.round(stat.miss_rate * 100)}% miss</span>
                          )
                        )}
                        {stat.all_iterations_miss_rate != null && (
                          <span className="font-mono text-sky-600" title="all-iterations miss rate">{`all:${Math.round(stat.all_iterations_miss_rate * 100)}%`}</span>
                        )}
                        {stat.false_positive_rate != null && (
                          <span className={`font-mono ${fpRateColorClass(stat.false_positive_rate)}`} title="false-positive rate">{`fp:${Math.round(stat.false_positive_rate * 100)}%`}</span>
                        )}
                        <span className="text-neutral-600 ml-auto">n={stat.sample_count}</span>
                      </div>
                      {(stat.examples ?? []).length > 0 && (
                        <ExampleList
                          examples={(stat.examples ?? []).map(example => ({
                            severity: example.severity as SrMissExample['severity'],
                            desc: example.desc,
                          }))}
                          indent
                        />
                      )}
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

const CO_FAILURE_LIFT_THRESHOLD = 1.5;
const CO_FAILURE_TOP_N = 5;

export function CoFailurePairsSection({ pairs }: { pairs: SrCoFailurePair[] }) {
  const top = pairs
    .filter(pair => pair.lift >= CO_FAILURE_LIFT_THRESHOLD)
    .sort((a, b) => b.lift - a.lift)
    .slice(0, CO_FAILURE_TOP_N);
  if (top.length === 0) return null;
  return (
    <div className="mt-1.5 pt-1.5 border-t border-neutral-800/60" data-testid="sr-co-failure-pairs">
      <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-1">Co-failures</p>
      <div className="flex flex-col gap-0.5">
        {top.map((pair, index) => {
          const liftColor = pair.lift >= 2.5 ? 'text-red-400' : pair.lift >= 1.8 ? 'text-amber-400' : 'text-neutral-400';
          return (
            <div key={index} className="flex items-center gap-1.5 text-ui-sm min-w-0">
              <span className="text-neutral-300 truncate flex-1" title={`${pair.cat_a} + ${pair.cat_b} co-fail`}>
                <span className="font-medium">{pair.cat_a}</span>
                <span className="text-neutral-600 mx-0.5">+</span>
                <span className="font-medium">{pair.cat_b}</span>
              </span>
              <span
                className={`font-mono tabular-nums shrink-0 ${liftColor}`}
                title={`lift=${pair.lift.toFixed(2)}: P(${pair.cat_b}|${pair.cat_a})=${Math.round(pair.p_b_given_a * 100)}% vs base ${Math.round(pair.base_rate_b * 100)}%`}
              >
                {`×${pair.lift.toFixed(1)}`}
              </span>
              <span className="font-mono tabular-nums text-neutral-600 shrink-0 text-ui-xs">n={pair.co_fail_count}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
