import { useState, useEffect } from 'react';
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
  FreshReviewerTrend,
  SrCoFailurePair,
  SrDisagreementExample,
  SrMissExample,
  SrReviewerDisagreement,
  SrReviewerDisagreementsData,
  SrReviewerDisagreementTask,
  SrReviewerDisagreementTasksResult,
  SrScopeDriftVsExpansion,
  SrTrendData,
} from './types';

function frTrendColor(trend: string | null | undefined): string {
  if (trend === 'improving') return 'text-emerald-400';
  if (trend === 'regressing') return 'text-red-400';
  return 'text-neutral-500';
}

export function FreshReviewerTrendBreakdown({ trend }: { trend: FreshReviewerTrend | null | undefined }) {
  if (!trend) return null;
  const cats = trend.per_category_hit_rate;
  const lift = trend.approval_rate_lift;
  const hasCats = !!cats && Object.keys(cats).length > 0;
  if (!hasCats && lift == null) return null;
  const cycleLabel =
    trend.bucket_mode === 'cycle' && trend.current_cycle_id
      ? `cycle ${trend.current_cycle_id.slice(-6)} vs ${trend.previous_cycle_id ? trend.previous_cycle_id.slice(-6) : '–'}`
      : null;
  return (
    <div data-testid="fr-trend-breakdown" className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[220px]">
      <div className="flex items-baseline gap-2 mb-1">
        <p className="text-[9px] text-neutral-500 uppercase tracking-wide">Fresh-Reviewer Trend</p>
        {cycleLabel && <span className="text-[9px] text-neutral-600 font-mono">{cycleLabel}</span>}
        {trend.current_sample_size != null && (
          <span className="text-[9px] text-neutral-600 font-mono ml-auto">n={trend.current_sample_size}</span>
        )}
      </div>
      {cats && Object.keys(cats).length > 0 && (
        <div className="flex flex-col gap-0.5 mb-1">
          <p className="text-[9px] text-neutral-600">Hit rate by category</p>
          {Object.entries(cats).map(([cat, metric]) => (
            <div key={cat} className="flex items-center gap-2 text-[10px] min-w-0">
              <span className="text-neutral-300 flex-1 truncate">{cat}</span>
              <span className={`font-mono tabular-nums ${frTrendColor(metric.trend)}`}>
                {metric.current != null ? `${Math.round(metric.current * 100)}%` : '–'}
              </span>
              {metric.delta != null && (
                <span className={`font-mono tabular-nums text-xs ${deltaColorClass(metric.delta)}`}>
                  {formatDelta(metric.delta)}
                </span>
              )}
            </div>
          ))}
        </div>
      )}
      {lift != null && (
        <div className="flex items-center gap-2 text-[10px] pt-1 border-t border-neutral-800/60">
          <span className="text-neutral-400 flex-1">Approval rate lift</span>
          <span className={`font-mono tabular-nums ${frTrendColor(lift.trend)}`}>
            {lift.current != null ? `${lift.current >= 0 ? '+' : ''}${Math.round(lift.current * 100)}pp` : '–'}
          </span>
          {lift.delta != null && (
            <span className={`font-mono tabular-nums text-xs ${deltaColorClass(lift.delta)}`}>
              {formatDelta(lift.delta)}
            </span>
          )}
        </div>
      )}
    </div>
  );
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
        <p className="text-[9px] text-neutral-500 uppercase tracking-wide">Edges considered</p>
        {cycleLabel && <span className="text-[9px] text-neutral-600 font-mono">{cycleLabel}</span>}
        {curr.sample_size != null && (
          <span className="text-[9px] text-neutral-600 font-mono ml-auto">n={curr.sample_size}</span>
        )}
      </div>
      <div className="flex items-center gap-2 text-[10px]">
        <span className="text-neutral-400 flex-1">tasks with edges</span>
        <span className={`font-mono tabular-nums ${frTrendColor(trendVal)}`}>
          {curr.tasks_with_edges_rate != null ? `${Math.round(curr.tasks_with_edges_rate * 100)}%` : '–'}
        </span>
        {delta != null && (
          <span className={`font-mono tabular-nums text-xs ${deltaColorClass(-delta)}`}>
            {formatDelta(delta)}
          </span>
        )}
      </div>
      {curr.unhandled_edge_flagged_rate != null && (
        <div className="flex items-center gap-2 text-[10px]" title="Share of enumerated edges the agent itself marked as not handled — its honesty about uncertainty.">
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
          <div className="flex items-center gap-2 text-[10px]">
            <span className="text-neutral-400 flex-1" title="Human-reviewer correctness rejection rate, split by whether the task enumerated edges.">
              correctness rej w/wo
            </span>
            <span className="font-mono tabular-nums text-neutral-300" title="correctness rejection rate when edges were enumerated">
              {withEdges != null ? `${Math.round(withEdges * 100)}%` : '–'}
            </span>
            <span className="text-neutral-600 text-[9px]">/</span>
            <span className="font-mono tabular-nums text-neutral-300" title="correctness rejection rate when edges were NOT enumerated">
              {withoutEdges != null ? `${Math.round(withoutEdges * 100)}%` : '–'}
            </span>
            {gap != null && (
              <span
                className={`font-mono tabular-nums text-[9px] ${deltaColorClass(-gap)}`}
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
          <div className="flex items-center gap-2 text-[10px]">
            <span
              className="text-neutral-400 flex-1"
              title="Higher rate-with-unhandled-flag than rate-with-all-handled means the agent's honesty about unhandled edges is informative — reviewers consistently catch issues the author flagged."
            >
              rej w/unhandled-flag vs all-handled
            </span>
            <span className="font-mono tabular-nums text-neutral-300" title="correctness rejection rate when ≥1 author-origin edge was flagged unhandled">
              {unhandled != null ? `${Math.round(unhandled * 100)}%` : '–'}
            </span>
            <span className="text-neutral-600 text-[9px]">/</span>
            <span className="font-mono tabular-nums text-neutral-300" title="correctness rejection rate when all author-origin edges were marked handled">
              {allHandled != null ? `${Math.round(allHandled * 100)}%` : '–'}
            </span>
            {gap != null && (
              <span
                className={`font-mono tabular-nums text-[9px] ${deltaColorClass(-gap)}`}
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
          <span className="text-[9px] text-neutral-600">history</span>
          <div className="flex gap-0.5">
            {history.map((rate, index) => (
              <span key={index} className="text-[9px] font-mono text-neutral-500">{Math.round(rate * 100)}%</span>
            ))}
          </div>
        </div>
      )}
      {prev && prev.tasks_with_edges_rate != null && (
        <div className="flex items-center gap-2 text-[10px] pt-1 border-t border-neutral-800/60">
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
              className="text-[9px] text-amber-400 font-mono"
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
                className="text-[9px] text-amber-500 font-mono"
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
                className={`text-[10px] text-neutral-300 truncate flex-1${onDirectionClick ? ' cursor-pointer hover:underline' : ''}`}
                title={direction.direction_text}
                onClick={() => onDirectionClick?.(direction.direction_id)}
                role={onDirectionClick ? 'button' : undefined}
              >
                {direction.direction_text || direction.direction_id}
              </span>
              {direction.mixed_prompt_versions && (
                <span className="text-[9px] text-amber-400 font-mono shrink-0" title="Tasks from multiple SR prompt versions — attribution unreliable">mixed v</span>
              )}
              {!direction.mixed_prompt_versions && direction.prompt_version && (
                <span className="text-[9px] text-amber-600 font-mono shrink-0" title="SR prompt version for this window">{`v=${direction.prompt_version.slice(0, 7)}`}</span>
              )}
              <span className="text-[9px] text-neutral-600 shrink-0">n={direction.sample_size}</span>
            </div>
            {categories.length > 0 && (
              <div className="flex flex-col gap-0.5">
                {categories.map(([cat, stat]) => {
                  const tooSmall = isSampleTooSmall(stat.sample_count);
                  return (
                    <div key={cat} className="text-[10px]">
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
                      {(stat.sr_alone_hit_rate != null || stat.reviewer_save_rate != null) && (
                        <div className="flex items-center gap-1 text-[9px] text-neutral-500 font-mono ml-3 mt-0.5">
                          <span>SR-alone {stat.sr_alone_hit_rate != null ? `${Math.round(stat.sr_alone_hit_rate * 100)}%` : '–'}</span>
                          <span className="text-neutral-700">·</span>
                          <span>reviewer-save {stat.reviewer_save_rate != null ? `${Math.round(stat.reviewer_save_rate * 100)}%` : '–'}</span>
                        </div>
                      )}
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

const DISAGREEMENT_SEV_ORDER: Record<string, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, LOW: 3 };

function disagreementBadgeClass(maxSev?: string | null): string {
  if (maxSev === 'CRITICAL') return 'bg-red-500/10 text-red-400 border-red-500/20';
  if (maxSev === 'HIGH') return 'bg-orange-500/10 text-orange-400 border-orange-500/20';
  if (maxSev === 'MEDIUM') return 'bg-amber-500/10 text-amber-400 border-amber-500/20';
  return 'bg-orange-500/10 text-orange-400 border-orange-500/20';
}

function sortDisagreements(items: SrReviewerDisagreement[]): SrReviewerDisagreement[] {
  return [...items].sort((a, b) => {
    const left = DISAGREEMENT_SEV_ORDER[a.max_severity ?? ''] ?? 4;
    const right = DISAGREEMENT_SEV_ORDER[b.max_severity ?? ''] ?? 4;
    return left !== right ? left - right : b.count - a.count;
  });
}

function exampleSeverityColor(severity: SrDisagreementExample['severity']): string {
  if (severity === 'CRITICAL') return 'text-red-400';
  if (severity === 'HIGH') return 'text-orange-400';
  if (severity === 'MEDIUM' || severity === 'MAJOR') return 'text-amber-400';
  return 'text-neutral-400';
}

export function SrReviewerDisagreementsPanel({
  data,
  onSelectTask,
}: {
  data: SrReviewerDisagreementsData;
  onSelectTask?: (taskId: string) => void;
}) {
  const [expandedKey, setExpandedKey] = useState<string | null>(null);
  if (!data.disagreements.length) return null;
  const sorted = sortDisagreements(data.disagreements);
  return (
    <div data-testid="sr-reviewer-disagreements" className="flex flex-col gap-1.5">
      <div className="flex flex-wrap gap-1.5">
        {sorted.slice(0, 8).map((disagreement, index) => {
          const severity = disagreement.max_severity;
          const breakdown = disagreement.severity_breakdown;
          const showSevDetail = (severity === 'CRITICAL' || severity === 'HIGH') && breakdown;
          const sevDetail = showSevDetail && breakdown
            ? (['CRITICAL', 'HIGH'] as const)
                .filter(key => (breakdown[key] ?? 0) > 0)
                .map(key => `${breakdown[key]} ${key}`)
                .join(', ')
            : null;
          const key = `${disagreement.category}:${disagreement.task_type}:${index}`;
          const isExpanded = expandedKey === key;
          const hasExamples = (disagreement.examples?.length ?? 0) > 0;
          const fp = disagreement.false_positive_rate;
          return (
            // eslint-disable-next-line viewport/min-touch-target -- dense chip list mirrors the existing panel behavior
            <span
              key={index}
              title={`${disagreement.count} task${disagreement.count !== 1 ? 's' : ''} where self-review passed ${disagreement.category} but reviewer flagged it${severity ? ` (max: ${severity})` : ''}${hasExamples ? ' — click to expand' : ''}`}
              onClick={() => hasExamples ? setExpandedKey(isExpanded ? null : key) : undefined}
              className={`inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-0.5 rounded border ${disagreementBadgeClass(severity)}${hasExamples ? ' cursor-pointer' : ''}`}
            >
              <span className="truncate max-w-[120px]">{disagreement.category}</span>
              {disagreement.task_type && <span className="opacity-60 font-normal">·</span>}
              {disagreement.task_type && <span className="opacity-70 font-normal">{disagreement.task_type}</span>}
              <span className="font-bold shrink-0">×{disagreement.count}</span>
              {fp != null && (
                <span className={`font-mono font-normal ${fpRateColorClass(fp)}`} title="false-positive rate">fp:{Math.round(fp * 100)}%</span>
              )}
              {sevDetail && <span className="font-normal opacity-80">({sevDetail})</span>}
              {hasExamples && <span className="opacity-50 font-normal">{isExpanded ? '▲' : '▼'}</span>}
            </span>
          );
        })}
      </div>
      {expandedKey !== null && (() => {
        const index = sorted.slice(0, 8).findIndex((disagreement, itemIndex) => `${disagreement.category}:${disagreement.task_type}:${itemIndex}` === expandedKey);
        const disagreement = index >= 0 ? sorted[index] : null;
        if (!disagreement?.examples?.length) return null;
        const examples = [...disagreement.examples].sort((a, b) => {
          const order: Record<string, number> = { CRITICAL: 0, HIGH: 1, MEDIUM: 2, MAJOR: 2, LOW: 3, MINOR: 3 };
          return (order[a.severity] ?? 4) - (order[b.severity] ?? 4);
        });
        return (
          <div
            data-testid="sr-disagreement-examples"
            className="mt-0.5 rounded border border-neutral-800/60 bg-neutral-900/60 divide-y divide-neutral-800/40 text-[10px]"
          >
            {examples.map((example, exampleIndex) => (
              <div
                key={exampleIndex}
                onClick={() => onSelectTask && example.task_id ? onSelectTask(example.task_id) : undefined}
                className={`flex items-start gap-2 px-2.5 py-1.5${onSelectTask && example.task_id ? ' cursor-pointer hover:bg-neutral-800/40' : ''}`}
              >
                <span className={`shrink-0 font-bold ${exampleSeverityColor(example.severity)}`}>{example.severity}</span>
                <span className="text-neutral-300 leading-snug flex-1">{example.description}</span>
                {example.task_id && onSelectTask && (
                  <span className="shrink-0 text-neutral-600 font-mono">{example.task_id.slice(0, 8)}</span>
                )}
              </div>
            ))}
          </div>
        );
      })()}
    </div>
  );
}

const _DRILL_PAGE_SIZE = 20;

export function SrReviewerDisagreementDrillPanel({
  category, taskType, days, fetchFn, onSelectTask,
}: {
  category: string;
  taskType: string;
  days: number;
  fetchFn: (p: { category: string; task_type: string; days: number; page: number; page_size: number }) => Promise<SrReviewerDisagreementTasksResult>;
  onSelectTask?: (taskId: string) => void;
}) {
  const [items, setItems] = useState<SrReviewerDisagreementTask[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(false);
  const [page, setPage] = useState(1);
  useEffect(() => {
    let alive = true;
    async function load() {
      setLoading(true);
      try {
        const r = await fetchFn({ category, task_type: taskType, days, page: page - 1, page_size: _DRILL_PAGE_SIZE });
        if (alive) { setItems(r.items); setTotal(r.total); }
      } finally {
        if (alive) setLoading(false);
      }
    }
    load();
    return () => { alive = false; };
  }, [fetchFn, category, taskType, days, page]);
  const totalPages = Math.ceil(total / _DRILL_PAGE_SIZE);
  if (loading && !items.length) return <div className="text-xs text-neutral-500 p-2">Loading…</div>;
  if (!items.length) return <div data-testid="sr-disagreement-drill-empty" className="text-xs text-neutral-500 p-2">No tasks found.</div>;
  return (
    <div data-testid="sr-disagreement-drill-panel">
      {items.map(t => (
        // eslint-disable-next-line viewport/min-touch-target
        <div key={t.task_id} className="py-1 border-b border-neutral-800 text-xs cursor-pointer hover:bg-neutral-800/30" onClick={() => onSelectTask?.(t.task_id)}>
          <div className="font-medium text-neutral-200 truncate">{t.title}</div>
          <div className="text-neutral-500 flex gap-2 flex-wrap">
            <span>{t.task_type}</span>
            {t.scope && <span>{t.scope}</span>}
            <span>{t.severity}</span>
            <span className="ml-auto">{new Date(t.created_at).toLocaleDateString()}</span>
            {t.pr_url && <a href={t.pr_url} onClick={e => e.stopPropagation()} className="text-blue-400 hover:underline" target="_blank" rel="noreferrer">PR</a>}
          </div>
          {t.description && <div className="text-neutral-600 truncate">{t.description}</div>}
        </div>
      ))}
      {totalPages > 1 && (
        <div className="flex gap-2 mt-1 text-xs text-neutral-500">
          <button disabled={page <= 1} onClick={() => setPage(p => p - 1)} className="disabled:opacity-40">prev</button>
          <span>{page}/{totalPages}</span>
          <button disabled={page >= totalPages} onClick={() => setPage(p => p + 1)} className="disabled:opacity-40">next</button>
        </div>
      )}
    </div>
  );
}

export function SrScopeDriftVsExpansionCard({ data }: { data: SrScopeDriftVsExpansion | null | undefined }) {
  if (!data) return null;
  const rate = data.miss_by_expansion_rate;
  const rateColor = rate == null
    ? 'text-neutral-500'
    : rate >= 0.3
      ? 'text-red-400'
      : rate >= 0.15
        ? 'text-amber-400'
        : 'text-emerald-400';
  return (
    <div data-testid="sr-scope-drift-vs-expansion" className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[220px]">
      <div className="flex items-baseline gap-2 mb-1">
        <p className="text-[9px] text-neutral-500 uppercase tracking-wide">SR Scope-Drift vs Expansion</p>
        {data.sample_size != null && (
          <span className="text-[9px] text-neutral-600 font-mono ml-auto">n={data.sample_size}</span>
        )}
      </div>
      <div className="flex items-center gap-2 text-[10px] mb-0.5">
        <span className="text-neutral-400 flex-1">miss-by-expansion rate</span>
        <span className={`font-mono font-bold tabular-nums ${rateColor}`}>
          {rate != null ? `${Math.round(rate * 100)}%` : '–'}
        </span>
        {data.numerator != null && data.denominator != null && (
          <span className="text-neutral-600 font-mono text-[9px]">{data.numerator}/{data.denominator}</span>
        )}
      </div>
      <div className="flex flex-wrap gap-x-3 gap-y-0 text-[9px] text-neutral-500">
        {data.consistent != null && (
          <span title="SR passed + ratio ≤ 1.5 (true-pass)">consistent: {data.consistent}</span>
        )}
        {data.miss_by_expansion != null && (
          <span title="SR passed + ratio ≥ 2.0 (scope-drift miss)" className="text-red-400/70">miss: {data.miss_by_expansion}</span>
        )}
        {data.hit_by_expansion != null && (
          <span title="SR failed scope-drift + ratio ≥ 1.5 (SR caught it)" className="text-emerald-400/70">hit: {data.hit_by_expansion}</span>
        )}
        {data.fp_candidate != null && (
          <span title="SR failed scope-drift + ratio ≤ 1.0 (likely false positive)">fp: {data.fp_candidate}</span>
        )}
        {(data.gray_zone ?? 0) > 0 && (
          <span title="SR passed + 1.5 < ratio < 2.0 (ambiguous)">gray: {data.gray_zone}</span>
        )}
      </div>
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
      <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-1">Co-failures</p>
      <div className="flex flex-col gap-0.5">
        {top.map((pair, index) => {
          const liftColor = pair.lift >= 2.5 ? 'text-red-400' : pair.lift >= 1.8 ? 'text-amber-400' : 'text-neutral-400';
          return (
            <div key={index} className="flex items-center gap-1.5 text-[10px] min-w-0">
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
              <span className="font-mono tabular-nums text-neutral-600 shrink-0 text-[9px]">n={pair.co_fail_count}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}
