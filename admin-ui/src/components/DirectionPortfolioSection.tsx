import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle, ChevronLeft, ChevronRight, RefreshCw, X } from 'lucide-react';
import { STAGE_DROPOUT_LABELS, TAXONOMY_COLORS, TAXONOMY_LABELS, TAXONOMY_ORDER } from './dashboardConstants';
import { StrategicAuditPanel } from './StrategicAuditPanel';
import { isSafeUrl } from './TaskCard';
import { apiFetch, fetchEvictedFindings, type EvictedFinding } from '../api';
import { timeAgo } from '../utils/time';
import {
  hasNonCodeMergeOutcomes,
  healthRateColorClass,
  mergeOutcomeKindBreakdowns,
  summarizeOutcomeKinds,
  type OutcomeKindBreakdown,
} from '../utils/outcomeKinds';

export interface DirectionProgress {
  direction_id: string;
  direction_text: string;
  priority: string;
  cycles_in_window: number;
  tasks_created: number;
  prs_merged: number;
  prs_rejected?: number | null;
  autonomous_prs_merged?: number | null;
  assisted_prs_merged?: number | null;
  possibly_saturated: boolean;
  auto_paused?: boolean;
  paused_reason?: string | null;
  needs_manual_intervention?: boolean;
  conversion_rate?: number | null;
  acceptance_rate?: number | null;
  first_attempt_success_rate?: number | null;
  first_attempt_sample_size?: number | null;
  product_ratio?: number | null;
  estimated_cost_usd?: number | null;
  cost_budget_usd?: number | null;
  budget_utilization_pct?: number | null;
  budget_exceeded?: boolean;
  cost_per_merged_pr?: number | null;
  cost_per_merge_exceeds_threshold?: boolean | null;
  avg_time_to_merge_hours?: number | null;
  cost_by_scope?: Record<string, number> | null;
  cost_by_type?: Record<string, number> | null;
  cost_by_provider_model?: Record<string, number> | null;
  spot_cost_usd?: number | null;
  on_demand_cost_usd?: number | null;
  scope_breakdown?: Record<string, { created: number; merged: number; failed: number; failed_infra?: number; failed_code_error?: number; failed_scope_overflow?: number; calibrated_in?: number; calibrated_out?: number }> | null;
  taxonomy_summary?: Record<string, number> | null;
  review_quality?: { reviews_dismissed?: number; reviews_overridden?: number; user_issues_caught?: number } | null;
  stage_dropout?: Record<string, number> | null;
  recent_failure_titles?: string[] | null;
  history_aggregate?: { total_autonomous_prs_merged?: number | null; total_operator_actions?: number | null; evicted_findings?: number | null; [key: string]: unknown } | null;
  research_history?: Array<{ autonomous_prs_merged?: number | null; operator_action_count?: number | null; [key: string]: unknown }> | null;
  evicted_findings_count?: number | null;
  // Generalized cross-task-type success (code merges + research/audit/memo/
  // verification successes). Surfaced alongside — never instead of — PR metrics.
  generalized_successes?: number | null;
  generalized_terminal_outcomes?: number | null;
  generalized_health_rate?: number | null;
  outcome_kind_breakdown?: OutcomeKindBreakdown | null;
  lifetime_generalized_successes?: number | null;
  lifetime_generalized_health_rate?: number | null;
}

interface DirectionPortfolioSectionProps {
  directions: DirectionProgress[];
  loading: boolean;
  productPct: number | null;
  totalDirPRs: number;
  hasAttribution: boolean;
  totalAutonomousPRs: number | null;
  totalAssistedPRs: number | null;
  selfSufficientCount: number;
  predictionCalibrationFactor?: number | null;
  predictionCalibrationNote?: string | null;
  predictionCalibrationSampleCount?: number | null;
  medianCostPerMergedPr?: number | null;
  onDirectionClick?: (directionId: string) => void;
  projectId?: string | null;
  calibrationFloor?: number;
}

function priorityBadge(p: string) {
  switch (p) {
    case 'high': return 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30';
    case 'medium': return 'bg-amber-500/20 text-amber-400 border-amber-500/30';
    default: return 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30';
  }
}

function CostBreakdownPill({ direction: d }: { direction: DirectionProgress }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const total = d.estimated_cost_usd ?? 0;

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [open]);

  const byScope = d.cost_by_scope ?? null;
  const byType = d.cost_by_type ?? null;
  const spot = d.spot_cost_usd;
  const onDemand = d.on_demand_cost_usd;
  const sortedByTypeEntries = useMemo(
    () => (byType ? Object.entries(byType).sort((a, b) => b[1] - a[1]) : []),
    [byType]
  );
  const hasBreakdown =
    (byScope && Object.keys(byScope).length > 0) ||
    (byType && Object.keys(byType).length > 0) ||
    (spot != null && onDemand != null && (spot > 0 || onDemand > 0));

  return (
    <div ref={ref} className="relative inline-block">
      <button
        type="button"
        className="text-neutral-400 hover:text-neutral-200 cursor-pointer"
        title="Cost breakdown"
        onClick={e => { e.stopPropagation(); setOpen(v => !v); }}
      >
        ${total.toFixed(2)}
      </button>
      {open && hasBreakdown && (
        // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
        <div
          className="absolute z-20 top-full mt-1 left-0 w-56 bg-neutral-900 border border-neutral-700 rounded-lg p-2 shadow-lg text-[10px] text-neutral-300 space-y-1.5"
          onClick={e => e.stopPropagation()}
        >
          {byScope && Object.keys(byScope).length > 0 && (
            <div>
              <p className="text-neutral-500 uppercase tracking-wide text-[9px] mb-0.5">By scope</p>
              {(['small', 'medium', 'large'] as const).map(s => byScope[s] != null ? (
                <div key={s} className="flex justify-between"><span>{s}</span><span className="font-mono">${byScope[s].toFixed(2)}</span></div>
              ) : null)}
            </div>
          )}
          {byType && Object.keys(byType).length > 0 && (
            <div>
              <p className="text-neutral-500 uppercase tracking-wide text-[9px] mb-0.5">By type</p>
              {sortedByTypeEntries.map(([t, v]) => (
                <div key={t} className="flex justify-between"><span>{t}</span><span className="font-mono">${v.toFixed(2)}</span></div>
              ))}
            </div>
          )}
          {spot != null && onDemand != null && (spot > 0 || onDemand > 0) && (
            <div>
              <p className="text-neutral-500 uppercase tracking-wide text-[9px] mb-0.5">VM type</p>
              <div className="flex justify-between"><span>spot</span><span className="font-mono">${spot.toFixed(2)}</span></div>
              <div className="flex justify-between"><span>on-demand</span><span className="font-mono">${onDemand.toFixed(2)}</span></div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CalibrationChip({ factor, note, sampleCount }: { factor: number | null | undefined; note: string | null | undefined; sampleCount: number | null | undefined }) {
  if (factor == null || sampleCount == null || sampleCount < 3) return null;
  const diff = factor - 1.0;
  let label: string;
  let colorClass: string;
  if (Math.abs(diff) < 0.05) {
    label = 'Predictions: well-calibrated';
    colorClass = 'bg-emerald-500/15 text-emerald-400 border-emerald-500/30';
  } else if (diff < 0) {
    const pct = Math.round((1.0 - factor) * 100);
    label = `Predictions: ~${pct}% optimistic`;
    colorClass = 'bg-amber-500/15 text-amber-400 border-amber-500/30';
  } else {
    const pct = Math.round((factor - 1.0) * 100);
    label = `Predictions: ~${pct}% pessimistic`;
    colorClass = 'bg-blue-500/15 text-blue-400 border-blue-500/30';
  }
  return (
    <span
      className={`text-[9px] font-medium px-1.5 py-0.5 rounded border ${colorClass} cursor-default`}
      title={note ?? undefined}
    >
      {label}
    </span>
  );
}

interface DirectionSrCatStat {
  miss_rate: number | null;
  sample_count: number;
  examples?: Array<{ desc: string; severity: string }>;
}

interface DirectionSrEntry {
  direction_id: string;
  sample_size: number;
  overall_catch_rate: number | null;
  worst_category: string | null;
  categories: Record<string, DirectionSrCatStat>;
  rate_history?: number[] | null;
  previous_miss_rate?: number | null;
  trend?: 'increasing' | 'decreasing' | 'stable' | null;
}

function CatchRateSparkline({ history, trend }: { history: number[]; trend?: string | null }) {
  if (history.length < 2) return null;
  const w = 28; const h = 10; const dx = w / (history.length - 1);
  const pts = history.map((v, i) => `${(i * dx).toFixed(1)},${(h - Math.max(0, Math.min(1, v)) * h).toFixed(1)}`).join(' ');
  const stroke = trend === 'increasing' ? '#f87171' : trend === 'decreasing' ? '#34d399' : '#737373';
  return <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="inline-block align-middle ml-1" aria-hidden="true"><polyline fill="none" stroke={stroke} strokeWidth="1" points={pts} /></svg>;
}

function DirectionSrPill({ entry }: { entry: DirectionSrEntry }) {
  const [open, setOpen] = useState(false);
  const sortedCategoryEntries = useMemo(
    () => Object.entries(entry.categories).sort(([, a], [, b]) => (b.miss_rate ?? 0) - (a.miss_rate ?? 0)),
    [entry.categories]
  );
  const catchPct = entry.overall_catch_rate != null ? Math.round(entry.overall_catch_rate * 100) : null;
  if (catchPct == null) return null;
  const catchColor = catchPct >= 70 ? 'text-emerald-400' : catchPct >= 50 ? 'text-amber-400' : 'text-red-400';
  const worstCat = entry.worst_category;
  const worstMiss = worstCat ? entry.categories[worstCat]?.miss_rate : null;
  const label = `SR: ${catchPct}% catch${worstCat && worstMiss != null ? ` · ${worstCat} ${Math.round(worstMiss * 100)}% miss` : ''}`;
  const trend = entry.trend;
  const prevCatchPct = entry.previous_miss_rate != null ? Math.round((1 - entry.previous_miss_rate) * 100) : null;
  const deltaPp = prevCatchPct != null ? catchPct - prevCatchPct : null;
  const trendArrow = trend === 'increasing' ? '↓' : trend === 'decreasing' ? '↑' : null;
  const trendColor = trend === 'increasing' ? 'text-red-400' : trend === 'decreasing' ? 'text-emerald-400' : null;
  return (
    <div className="mt-1 text-[9px]">
      <button type="button" className={`${catchColor} hover:underline cursor-pointer`} title={`SR catch rate (n=${entry.sample_size})`} onClick={e => { e.stopPropagation(); setOpen(v => !v); }}>{label}</button>
      {trendArrow && trendColor && (
        <span className={`ml-1 font-mono ${trendColor}`} title={prevCatchPct != null ? `Prev: ${prevCatchPct}%` : 'trend direction'}>
          {trendArrow}{deltaPp != null ? `${deltaPp > 0 ? '+' : ''}${deltaPp}pp` : ''}
        </span>
      )}
      {entry.rate_history && entry.rate_history.length >= 2 && (
        <CatchRateSparkline history={entry.rate_history} trend={trend} />
      )}
      {open && (
        // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
        <div className="mt-0.5 flex flex-col gap-0.5 pl-2 border-l border-neutral-800" onClick={e => e.stopPropagation()}>
          {sortedCategoryEntries.map(([cat, s]) => (
            <div key={cat} className="text-neutral-400">
              <span className="text-neutral-300">{cat}</span>{' '}{s.miss_rate != null ? `${Math.round(s.miss_rate * 100)}%` : '–'} miss
              {(s.examples ?? []).map((ex, i) => <div key={i} className="pl-1.5 text-neutral-500 line-clamp-1">{ex.desc}</div>)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function SectionSkeleton() {
  return <div className="h-28 w-full animate-pulse rounded bg-neutral-900/50 border border-neutral-800/50" />;
}

const EVICTED_FINDINGS_PAGE_SIZE = 10;

interface EvictedDrawerState {
  directionId: string;
  directionText: string;
  evictedCount: number;
}

export function DirectionPortfolioSection({
  directions,
  loading,
  productPct,
  totalDirPRs,
  hasAttribution,
  totalAutonomousPRs,
  totalAssistedPRs,
  selfSufficientCount,
  predictionCalibrationFactor,
  predictionCalibrationNote,
  predictionCalibrationSampleCount,
  medianCostPerMergedPr,
  onDirectionClick,
  projectId,
  calibrationFloor,
}: DirectionPortfolioSectionProps) {
  const [srBreakdown, setSrBreakdown] = useState<Record<string, DirectionSrEntry>>({});
  const [evictedDrawer, setEvictedDrawer] = useState<EvictedDrawerState | null>(null);
  const [evictedItems, setEvictedItems] = useState<EvictedFinding[]>([]);
  const [evictedTotal, setEvictedTotal] = useState(0);
  const [evictedOffset, setEvictedOffset] = useState(0);
  const [evictedLoading, setEvictedLoading] = useState(false);
  const [evictedError, setEvictedError] = useState<string | null>(null);
  const [evictedRefreshTick, setEvictedRefreshTick] = useState(0);
  const [evictedIsStale, setEvictedIsStale] = useState(false);
  const evictedLoadedAtRef = useRef<number>(0);
  const evictedDrawerDirectionId = evictedDrawer?.directionId ?? null;

  // Aggregate generalized (cross-type) success across directions, for the
  // header summary line. Only surfaced when at least one non-code-merge lane
  // has terminal outcomes — otherwise the count equals PR merges and the
  // existing "PRs merged" stat already covers it (honest legacy degrade).
  const generalizedSummary = useMemo(() => {
    const merged = mergeOutcomeKindBreakdowns(directions.map(d => d.outcome_kind_breakdown ?? null));
    if (!hasNonCodeMergeOutcomes(merged)) return null;
    const successes = directions.reduce((sum, d) => sum + (d.generalized_successes ?? 0), 0);
    const terminal = directions.reduce((sum, d) => sum + (d.generalized_terminal_outcomes ?? 0), 0);
    const rate = terminal > 0 ? successes / terminal : null;
    return { successes, terminal, rate };
  }, [directions]);

  const taxonomyVisibleKeysByDir = useMemo(() => {
    const result = new Map<string, Array<typeof TAXONOMY_ORDER[number]>>();
    for (const d of directions) {
      if (d.taxonomy_summary) {
        result.set(d.direction_id, TAXONOMY_ORDER.filter(k => (d.taxonomy_summary?.[k] ?? 0) > 0));
      }
    }
    return result;
  }, [directions]);

  const sortedStageDropoutsByDir = useMemo(() => {
    const result = new Map<string, [string, number][]>();
    for (const d of directions) {
      if (d.stage_dropout && Object.values(d.stage_dropout).some(v => v)) {
        result.set(
          d.direction_id,
          Object.entries(d.stage_dropout).filter(([, v]) => v).sort((a, b) => b[1] - a[1])
        );
      }
    }
    return result;
  }, [directions]);

  useEffect(() => {
    if (!projectId) return;
    apiFetch(`/api/admin/projects/${encodeURIComponent(projectId)}/direction-sr-breakdown`)
      .then(r => r.ok ? r.json() : null)
      .then((data: { directions?: DirectionSrEntry[] } | null) => {
        if (!data?.directions) return;
        const map: Record<string, DirectionSrEntry> = {};
        for (const entry of data.directions) map[entry.direction_id] = entry;
        setSrBreakdown(map);
      })
      .catch(() => {});
  }, [projectId]);

  /* eslint-disable react-hooks/set-state-in-effect -- sync drawer-local state when the selected project changes */
  useEffect(() => {
    setEvictedDrawer(null);
    setEvictedItems([]);
    setEvictedTotal(0);
    setEvictedOffset(0);
    setEvictedError(null);
    setEvictedLoading(false);
    setEvictedRefreshTick(0);
    evictedLoadedAtRef.current = 0;
  }, [projectId]);
  /* eslint-enable react-hooks/set-state-in-effect */

  /* eslint-disable react-hooks/set-state-in-effect -- async fetch updates local drawer state after the request resolves */
  useEffect(() => {
    if (!projectId || !evictedDrawerDirectionId) return;
    let cancelled = false;
    setEvictedLoading(true);
    setEvictedError(null);
    fetchEvictedFindings(projectId, evictedDrawerDirectionId, { offset: evictedOffset, limit: EVICTED_FINDINGS_PAGE_SIZE })
      .then(data => {
        if (cancelled) return;
        setEvictedItems(data.evicted_findings ?? []);
        setEvictedTotal(data.total ?? 0);
        evictedLoadedAtRef.current = Date.now();
      })
      .catch(e => {
        if (cancelled) return;
        setEvictedError(e instanceof Error ? e.message : 'network error');
      })
      .finally(() => {
        if (!cancelled) setEvictedLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, evictedDrawerDirectionId, evictedOffset, evictedRefreshTick]);
  /* eslint-enable react-hooks/set-state-in-effect */

  /* eslint-disable react-hooks/set-state-in-effect -- interval syncs stale badge state for the open drawer */
  useEffect(() => {
    if (!evictedDrawerDirectionId) {
      setEvictedIsStale(false);
      return;
    }
    const update = () => {
      setEvictedIsStale(
        evictedLoadedAtRef.current > 0 && (Date.now() - evictedLoadedAtRef.current) > 300_000,
      );
    };
    update();
    const timer = window.setInterval(update, 60_000);
    return () => {
      window.clearInterval(timer);
    };
  }, [evictedDrawerDirectionId, evictedOffset, evictedTotal, evictedRefreshTick]);
  /* eslint-enable react-hooks/set-state-in-effect */

  const openEvictedDrawer = useCallback((d: DirectionProgress) => {
    const evictedCount = Math.max(
      0,
      Math.trunc(d.evicted_findings_count ?? d.history_aggregate?.evicted_findings ?? 0),
    );
    setEvictedDrawer(prev => (
      prev?.directionId === d.direction_id
        ? null
        : { directionId: d.direction_id, directionText: d.direction_text, evictedCount }
    ));
    setEvictedOffset(0);
    setEvictedItems([]);
    setEvictedTotal(0);
    setEvictedError(null);
    setEvictedLoading(false);
    evictedLoadedAtRef.current = 0;
  }, []);

  const closeEvictedDrawer = useCallback(() => {
    setEvictedDrawer(null);
    setEvictedItems([]);
    setEvictedTotal(0);
    setEvictedOffset(0);
    setEvictedError(null);
    setEvictedLoading(false);
    evictedLoadedAtRef.current = 0;
  }, []);

  const refreshEvictedDrawer = useCallback(() => {
    setEvictedRefreshTick(t => t + 1);
  }, []);

  const evictedTotalPages = Math.max(1, Math.ceil(evictedTotal / EVICTED_FINDINGS_PAGE_SIZE));
  const evictedPage = Math.min(evictedTotalPages, Math.floor(evictedOffset / EVICTED_FINDINGS_PAGE_SIZE) + 1);
  const evictedMaxOffset = evictedTotal > 0
    ? Math.floor((evictedTotal - 1) / EVICTED_FINDINGS_PAGE_SIZE) * EVICTED_FINDINGS_PAGE_SIZE
    : 0;
  const evictedStart = evictedTotal === 0 ? 0 : evictedOffset + 1;
  const evictedEnd = Math.min(evictedOffset + EVICTED_FINDINGS_PAGE_SIZE, evictedTotal);

  return (
    <>
    <section>
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Direction Portfolio</h2>
      {loading ? (
        <SectionSkeleton />
      ) : directions.length === 0 ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 text-neutral-500 text-xs">No active directions</div>
      ) : (
        <>
          <div className="flex items-center gap-3 text-xs text-neutral-400 mb-2">
            <span>{directions.length} direction{directions.length !== 1 ? 's' : ''}</span>
            {productPct != null && (
              <>
                <span className="text-neutral-600">&middot;</span>
                <span className="flex items-center gap-1">
                  {productPct}% product
                  <div className="w-16 h-1.5 rounded-full bg-neutral-800 overflow-hidden">
                    <div className="h-full rounded-full bg-emerald-500" style={{ width: `${productPct}%` }} />
                  </div>
                </span>
              </>
            )}
            <span className="text-neutral-600">&middot;</span>
            <span>{totalDirPRs} PR{totalDirPRs !== 1 ? 's' : ''} merged</span>
            {generalizedSummary && (
              <>
                <span className="text-neutral-600">&middot;</span>
                <span
                  className="flex items-center gap-1"
                  title="Generalized success across all task types: evaluator-positive outcomes (code merges + research/audit/memo/verification) divided by terminal outcomes"
                >
                  <span className={healthRateColorClass(generalizedSummary.rate)}>
                    {generalizedSummary.successes} eval-success
                    {generalizedSummary.rate != null ? ` · ${Math.round(generalizedSummary.rate * 100)}%` : ''}
                  </span>
                </span>
              </>
            )}
            {hasAttribution && (totalAutonomousPRs != null || totalAssistedPRs != null) && (
              <>
                <span className="text-neutral-600">&middot;</span>
                <span className="text-emerald-400">{totalAutonomousPRs ?? 0} autonomous</span>
                <span className="text-neutral-600">·</span>
                <span className="text-blue-400">{totalAssistedPRs ?? 0} assisted</span>
              </>
            )}
            {directions.length > 0 && (
              <>
                <span className="text-neutral-600">&middot;</span>
                <span className={selfSufficientCount === 0 ? 'text-neutral-500' : 'text-emerald-400'}>
                  Self-sufficient: {selfSufficientCount} / {directions.length}
                </span>
              </>
            )}
            <CalibrationChip
              factor={predictionCalibrationFactor}
              note={predictionCalibrationNote}
              sampleCount={predictionCalibrationSampleCount}
            />
          </div>

          <div className="flex gap-2 overflow-x-auto pb-1 scrollbar-hide" style={{ scrollbarWidth: 'none' }}>
            {directions.map(d => {
              const cvr = d.conversion_rate != null ? Math.round(d.conversion_rate * 100) : null;
              const cvrColor = cvr == null ? 'text-neutral-500' : cvr >= 60 ? 'text-emerald-400' : cvr >= 30 ? 'text-amber-400' : 'text-red-400';
              const acc = d.acceptance_rate != null ? Math.round(d.acceptance_rate * 100) : null;
              const accColor = acc == null ? 'text-neutral-500' : acc >= 60 ? 'text-emerald-400' : acc >= 30 ? 'text-amber-400' : 'text-red-400';
              const fas = d.first_attempt_success_rate != null ? Math.round(d.first_attempt_success_rate * 100) : null;
              const fasColor = fas == null ? 'text-neutral-500' : fas >= 60 ? 'text-emerald-400' : fas >= 30 ? 'text-amber-400' : 'text-red-400';
              const evictedCount = Math.max(
                0,
                Math.trunc(d.evicted_findings_count ?? d.history_aggregate?.evicted_findings ?? 0),
              );
              const outcomeRows = summarizeOutcomeKinds(d.outcome_kind_breakdown);
              const showGeneralized = outcomeRows.some(r => r.kind !== 'code_merge');
              return (
                <div
                  key={d.direction_id}
                  className="min-w-[200px] max-w-[220px] bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 shrink-0 cursor-pointer hover:border-neutral-700 transition-colors"
                  onClick={() => onDirectionClick?.(d.direction_id)}
                >
                  <div className="flex items-center gap-1.5 mb-1.5">
                    <span className={`text-[9px] font-bold uppercase px-1.5 py-0.5 rounded border ${priorityBadge(d.priority)}`}>{d.priority}</span>
                    {d.possibly_saturated && <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 border border-amber-500/30">Saturated</span>}
                    {d.needs_manual_intervention && <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded bg-red-500/20 text-red-400 border border-red-500/30">Needs Intervention</span>}
                    {d.auto_paused && <span className="text-[9px] font-bold uppercase px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 border border-amber-500/30" title={d.paused_reason ?? undefined}>Paused</span>}
                  </div>
                  <p className="text-xs text-neutral-200 line-clamp-2 mb-2">{d.direction_text}</p>
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[10px] text-neutral-500">
                    {cvr != null && <span className={cvrColor}>{cvr}% conversion</span>}
                    {acc != null && <span className={accColor} title="PRs merged / (merged + rejected)">{acc}% acc</span>}
                    {fas != null && <span className={fasColor} title={`First-attempt success rate${d.first_attempt_sample_size != null ? ` (n=${d.first_attempt_sample_size})` : ''}`}>{fas}% 1st</span>}
                    <span>{d.prs_merged} PR{d.prs_merged !== 1 ? 's' : ''}{d.prs_rejected != null && d.prs_rejected > 0 && <span className="text-red-400" title={`${d.prs_rejected} PR${d.prs_rejected !== 1 ? 's' : ''} rejected`}> · {d.prs_rejected} rej</span>}</span>
                    {d.product_ratio != null && (
                      <span className={d.product_ratio >= 0.6 ? 'text-emerald-400' : d.product_ratio >= 0.3 ? 'text-amber-400' : 'text-red-400'}>{Math.round(d.product_ratio * 100)}% prod</span>
                    )}
                    {showGeneralized && (
                      <span
                        className={healthRateColorClass(d.generalized_health_rate)}
                        title="Generalized success across task types (code merges + research/audit/memo/verification), distinct from the PR-merge count"
                      >
                        {d.generalized_successes ?? 0} eval{d.generalized_health_rate != null ? ` · ${Math.round(d.generalized_health_rate * 100)}%` : ''}
                      </span>
                    )}
                    {evictedCount > 0 && (
                      // eslint-disable-next-line viewport/min-touch-target -- dense inline badge inside compact direction card
                      <button
                        type="button"
                        className="text-[9px] leading-none px-1.5 py-0.5 rounded border border-violet-500/20 bg-violet-500/10 text-violet-300 hover:bg-violet-500/20"
                        title="View evicted findings for this direction"
                        aria-label="View evicted findings for this direction"
                        onClick={e => { e.stopPropagation(); openEvictedDrawer(d); }}
                      >
                        {evictedCount} evicted
                      </button>
                    )}
                    {d.estimated_cost_usd != null && (
                      <CostBreakdownPill direction={d} />
                    )}
                    {d.cost_per_merged_pr != null && (
                      <span
                        className={d.cost_per_merge_exceeds_threshold ? 'text-red-400 font-semibold' : 'text-neutral-400'}
                        title={d.cost_per_merge_exceeds_threshold
                          ? `Cost per merged PR exceeds threshold${medianCostPerMergedPr != null ? ` (project median: $${medianCostPerMergedPr.toFixed(2)})` : ''}`
                          : 'Cost per merged PR'}
                      >
                        {d.cost_per_merge_exceeds_threshold && <span className="mr-0.5">⚠</span>}${d.cost_per_merged_pr.toFixed(2)}/PR
                      </span>
                    )}
                    {d.avg_time_to_merge_hours != null && (
                      <span className={d.avg_time_to_merge_hours < 2 ? 'text-emerald-400' : d.avg_time_to_merge_hours <= 6 ? 'text-amber-400' : 'text-red-400'} title="Average time from VM start to PR merge">{d.avg_time_to_merge_hours.toFixed(1)}h avg merge</span>
                    )}
                  </div>
                  {d.cost_budget_usd != null && d.cost_budget_usd > 0 && (() => {
                    const pct = d.budget_utilization_pct != null
                      ? d.budget_utilization_pct
                      : ((d.estimated_cost_usd ?? 0) / d.cost_budget_usd) * 100;
                    const clamped = Math.min(100, Math.max(0, pct));
                    const barColor = pct > 80 ? 'bg-red-500' : pct >= 60 ? 'bg-amber-500' : 'bg-emerald-500';
                    return (
                      <div className="mt-1.5" title={`$${(d.estimated_cost_usd ?? 0).toFixed(2)} / $${d.cost_budget_usd.toFixed(2)} budget (${Math.round(pct)}%)`}>
                        <div className="h-1 w-full rounded bg-neutral-800 overflow-hidden">
                          <div className={`h-full ${barColor}`} style={{ width: `${clamped}%` }} />
                        </div>
                        <div className="flex justify-between text-[9px] text-neutral-500 mt-0.5">
                          <span>Budget</span>
                          <span className={pct > 80 ? 'text-red-400' : pct >= 60 ? 'text-amber-400' : 'text-emerald-400'}>{Math.round(pct)}%{d.budget_exceeded ? ' · over' : ''}</span>
                        </div>
                      </div>
                    );
                  })()}
                  {taxonomyVisibleKeysByDir.has(d.direction_id) && <div className="flex gap-1 mt-1 flex-wrap">{(taxonomyVisibleKeysByDir.get(d.direction_id) ?? []).map(k => { const v = d.taxonomy_summary?.[k] ?? 0; return <span key={k} title={`${TAXONOMY_LABELS[k]}: ${v}`} className={`text-[9px] leading-none px-1 py-0.5 rounded border ${TAXONOMY_COLORS[k]}`}>{TAXONOMY_LABELS[k]} {v}</span>; })}</div>}
                  {d.scope_breakdown && Object.keys(d.scope_breakdown).length > 0 && <div className="flex gap-1 mt-1 flex-wrap">{(['small', 'medium', 'large'] as const).map(sz => { const s = d.scope_breakdown?.[sz]; if (!s || (!s.merged && !s.failed)) return null; const total = s.merged + s.failed; const rate = total > 0 ? s.merged / total : 0; const color = rate >= 0.7 ? 'border-emerald-700 bg-emerald-900/40 text-emerald-300' : rate >= 0.4 ? 'border-amber-700 bg-amber-900/40 text-amber-300' : 'border-red-700 bg-red-900/40 text-red-300'; const subtypes = [[s.failed_code_error, 'code'], [s.failed_infra, 'infra'], [s.failed_scope_overflow, 'scope']].filter(([v]) => typeof v === 'number' && (v as number) > 0).map(([v, l]) => `${v} ${l}`).join(', '); const subtypeStr = subtypes ? ` (${subtypes})` : ''; return <span key={sz} title={`${sz}: ${s.merged} merged / ${s.failed} failed${subtypeStr} (${Math.round(rate * 100)}%)`} className={`text-[9px] leading-none px-1 py-0.5 rounded border ${color}`}>{sz[0].toUpperCase()} {s.merged}/{s.failed}</span>; })}</div>}
                  {showGeneralized && (
                    <div className="flex gap-1 mt-1 flex-wrap">
                      {outcomeRows.map(row => {
                        const rate = row.terminal > 0 ? row.success / row.terminal : 0;
                        const color = rate >= 0.7 ? 'border-emerald-700 bg-emerald-900/40 text-emerald-300' : rate >= 0.4 ? 'border-amber-700 bg-amber-900/40 text-amber-300' : 'border-slate-600 bg-slate-800/60 text-slate-300';
                        return (
                          <span
                            key={row.kind}
                            title={`${row.label}: ${row.success} success / ${row.neutral} neutral / ${row.failure} failure (${Math.round(rate * 100)}%)`}
                            className={`text-[9px] leading-none px-1 py-0.5 rounded border ${color}`}
                          >
                            {row.label} {row.success}/{row.terminal}
                          </span>
                        );
                      })}
                    </div>
                  )}
                  {d.review_quality && (d.review_quality.reviews_dismissed || d.review_quality.reviews_overridden || d.review_quality.user_issues_caught) ? (
                    <div className="flex gap-1 mt-1 flex-wrap">
                      {d.review_quality.reviews_dismissed ? <span title="Automated reviews dismissed by the system" className="text-[9px] leading-none px-1 py-0.5 rounded border border-amber-700 bg-amber-900/40 text-amber-300">{d.review_quality.reviews_dismissed} dismissed</span> : null}
                      {d.review_quality.reviews_overridden ? <span title="Reviews overridden by an operator" className="text-[9px] leading-none px-1 py-0.5 rounded border border-red-700 bg-red-900/40 text-red-300">{d.review_quality.reviews_overridden} overridden</span> : null}
                      {d.review_quality.user_issues_caught ? <span title="Real issues caught by review" className="text-[9px] leading-none px-1 py-0.5 rounded border border-emerald-700 bg-emerald-900/40 text-emerald-300">{d.review_quality.user_issues_caught} caught</span> : null}
                    </div>
                  ) : null}
                  {sortedStageDropoutsByDir.has(d.direction_id) && <div className="flex gap-1 mt-1 flex-wrap">{(sortedStageDropoutsByDir.get(d.direction_id) ?? []).map(([stage, v]) => <span key={stage} title={`Failed at ${stage}: ${v}`} className="text-[9px] leading-none px-1 py-0.5 rounded border border-slate-600 bg-slate-800/60 text-slate-300">{STAGE_DROPOUT_LABELS[stage] ?? stage} {v}</span>)}</div>}
                  {d.recent_failure_titles && d.recent_failure_titles.length > 0 && (
                    <ul className="mt-1 space-y-0.5">
                      {d.recent_failure_titles.slice(0, 3).map((t, i) => (
                        <li key={i} className="text-[10px] text-red-300/80 truncate" title={t}>• {t}</li>
                      ))}
                    </ul>
                  )}
                  {srBreakdown[d.direction_id] && <DirectionSrPill entry={srBreakdown[d.direction_id]} />}
                </div>
              );
            })}
          </div>
        </>
      )}
      {!!projectId && (
        <StrategicAuditPanel projectId={projectId} calibrationFloor={calibrationFloor} />
      )}
    </section>
    {evictedDrawer && (
      <div className="fixed inset-0 z-50 flex">
        <div className="flex-1 bg-black/40" onClick={closeEvictedDrawer} />
        <div className="w-full max-w-[720px] bg-neutral-950 border-l border-neutral-800 flex flex-col shadow-2xl">
          <div className="flex items-start justify-between gap-3 p-4 border-b border-neutral-800 shrink-0">
            <div className="min-w-0">
              <p className="text-[9px] uppercase tracking-widest text-neutral-500">Evicted findings</p>
              <h3 className="text-sm font-semibold text-neutral-100 truncate">{evictedDrawer.directionText}</h3>
              <p className="text-[10px] text-neutral-500 mt-0.5">
                {evictedTotal === 0
                  ? 'No evicted findings stored for this direction'
                  : `${evictedTotal} total · showing ${evictedStart}–${evictedEnd}`}
              </p>
            </div>
            <div className="flex items-center gap-2 shrink-0">
              {evictedIsStale && (
                // eslint-disable-next-line viewport/min-touch-target -- dense refresh affordance matches adjacent retrospective panels
                <button
                  type="button"
                  onClick={refreshEvictedDrawer}
                  disabled={evictedLoading}
                  className="p-1 text-neutral-500 hover:text-neutral-300 disabled:opacity-40"
                  title="Refresh evicted findings"
                  aria-label="Refresh evicted findings"
                >
                  <RefreshCw className="w-3 h-3" />
                </button>
              )}
              <button
                type="button"
                onClick={closeEvictedDrawer}
                className="text-neutral-500 hover:text-neutral-300 transition-colors"
                aria-label="Close evicted findings drawer"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
          </div>

          <div className="flex-1 overflow-y-auto p-4">
            {evictedLoading ? (
              <div className="h-28 w-full animate-pulse rounded bg-neutral-900/50 border border-neutral-800/50" />
            ) : evictedError ? (
              <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-xs text-neutral-400 flex items-center gap-2" role="status">
                <AlertTriangle className="w-3.5 h-3.5 text-neutral-500 shrink-0" aria-hidden="true" />
                <span>Error fetching evicted findings</span>
                <span className="text-[10px] text-neutral-600 truncate" title={evictedError}>{evictedError}</span>
              </div>
            ) : evictedTotal === 0 ? (
              <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-xs text-emerald-400 flex items-center gap-2" role="status">
                <CheckCircle className="w-3.5 h-3.5 shrink-0" aria-hidden="true" />
                <span>No evicted findings for this direction</span>
              </div>
            ) : (
              <div className="flex flex-col gap-2">
                {evictedItems.map((finding, index) => (
                  <div key={`${finding.title}-${index}`} className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3">
                    <div className="flex flex-wrap items-start gap-2">
                      <div className="min-w-0 flex-1">
                        <h4 className="text-sm text-neutral-100 font-medium break-words">{finding.title}</h4>
                        <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] text-neutral-500">
                          <span className="px-1.5 py-0.5 rounded border border-neutral-700/60 bg-neutral-900 text-neutral-300 uppercase tracking-wide">{finding.status}</span>
                          <span title={finding.evicted_at}>{timeAgo(finding.evicted_at)}</span>
                        </div>
                      </div>
                    </div>

                    {finding.claims.length > 0 && (
                      <div className="mt-2">
                        <div className="text-[10px] uppercase tracking-wide text-neutral-500 mb-0.5">Claims</div>
                        <ul className="list-disc list-inside text-[11px] text-neutral-300 space-y-0.5">
                          {finding.claims.map((claim, claimIndex) => (
                            <li key={claimIndex}>{claim}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {finding.source_refs.length > 0 && (
                      <div className="mt-2">
                        <div className="text-[10px] uppercase tracking-wide text-neutral-500 mb-0.5">Source refs</div>
                        <ul className="space-y-0.5 text-[11px]">
                          {finding.source_refs.map((ref, refIndex) => (
                            <li key={refIndex} className="leading-snug break-all">
                              {/^https?:\/\//i.test(ref) && isSafeUrl(ref) ? (
                                <a href={ref} target="_blank" rel="noopener noreferrer" className="font-mono text-blue-400 hover:text-blue-300">
                                  {ref}
                                </a>
                              ) : (
                                <span className="font-mono text-neutral-300">{ref}</span>
                              )}
                            </li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                ))}

                {evictedTotalPages > 1 && (
                  <div className="flex flex-wrap items-center gap-2 mt-1 text-[10px] text-neutral-400">
                    {/* eslint-disable-next-line viewport/min-touch-target -- compact pager in dense drawer footer */}
                    <button
                      type="button"
                      disabled={evictedOffset === 0 || evictedLoading}
                      onClick={() => setEvictedOffset(offset => Math.max(0, offset - EVICTED_FINDINGS_PAGE_SIZE))}
                      className="px-1.5 py-0.5 border border-neutral-700 rounded disabled:opacity-40 hover:border-neutral-500"
                      aria-label="Previous page"
                    >
                      <ChevronLeft className="w-3 h-3" />
                    </button>
                    <span>
                      Page {evictedPage} of {evictedTotalPages} · {evictedTotal} total
                    </span>
                    {/* eslint-disable-next-line viewport/min-touch-target -- compact pager in dense drawer footer */}
                    <button
                      type="button"
                      disabled={evictedOffset >= evictedMaxOffset || evictedLoading}
                      onClick={() => setEvictedOffset(offset => Math.min(evictedMaxOffset, offset + EVICTED_FINDINGS_PAGE_SIZE))}
                      className="px-1.5 py-0.5 border border-neutral-700 rounded disabled:opacity-40 hover:border-neutral-500"
                      aria-label="Next page"
                    >
                      <ChevronRight className="w-3 h-3" />
                    </button>
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    )}
    </>
  );
}
