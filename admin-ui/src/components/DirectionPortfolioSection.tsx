import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { AlertTriangle, CheckCircle, ChevronLeft, ChevronRight, RefreshCw, X } from 'lucide-react';
import { STAGE_DROPOUT_LABELS, TAXONOMY_COLORS, TAXONOMY_LABELS, TAXONOMY_ORDER } from './dashboardConstants';
import { StrategicAuditPanel } from './StrategicAuditPanel';
import { isSafeUrl } from './TaskCard';
import { apiFetch, fetchEvictedFindings, type EvictedFinding } from '../api';
import { timeAgo } from '../utils/time';
import type {
  DirectionPortfolio as DigestDirectionPortfolio,
  DirectionPortfolioCompositionEntry,
} from '../types/digest';
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
  proven_prs_merged?: number | null;
  consumed_prs_merged?: number | null;
  prs_rejected?: number | null;
  autonomous_prs_merged?: number | null;
  assisted_prs_merged?: number | null;
  possibly_saturated: boolean;
  auto_paused?: boolean;
  paused_reason?: string | null;
  needs_manual_intervention?: boolean;
  retirement_eligible?: boolean;
  conversion_rate?: number | null;
  acceptance_rate?: number | null;
  first_attempt_success_rate?: number | null;
  first_attempt_sample_size?: number | null;
  product_ratio?: number | null;
  estimated_cost_usd?: number | null;
  cost_budget_usd?: number | null;
  budget_utilization_pct?: number | null;
  budget_exceeded?: boolean;
  // Lifetime lineage spend — the meter budget_utilization_pct and
  // budget_exceeded are computed from (lifetime basis: VM + LLM + compute).
  // Additive fields; absent on legacy snapshots that only carry the
  // rolling-window figures, so every consumer must degrade gracefully.
  lineage_spend_usd?: number | null;
  lineage_vm_cost_usd?: number | null;
  lineage_llm_cost_usd?: number | null;
  lineage_compute_cost_usd?: number | null;
  lineage_task_count?: number | null;
  lineage_llm_unpriced?: boolean;
  budget_basis?: string | null;
  // Rolling-window cost split behind estimated_cost_usd (window basis).
  window_vm_cost_usd?: number | null;
  window_llm_cost_usd?: number | null;
  window_llm_unpriced?: boolean;
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
  verified_prs_merged?: number | null;
  reachable_prs_merged?: number | null;
  acceptance_declaration_coverage?: number | null;
  lifetime_acceptance_declaration_coverage?: number | null;
}

interface DirectionPortfolioSectionProps {
  directions: DirectionProgress[];
  directionPortfolio?: DigestDirectionPortfolio;
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

type ScopeBreakdownMap = Record<string, { merged: number; failed: number; failed_infra?: number; failed_code_error?: number; failed_scope_overflow?: number }>;

const ScopeBreakdownPills = memo(function ScopeBreakdownPills({ scope_breakdown }: { scope_breakdown: ScopeBreakdownMap }) {
  return (
    <div className="flex gap-1 mt-1 flex-wrap">
      {(['small', 'medium', 'large'] as const).map(sz => {
        const s = scope_breakdown[sz];
        if (!s || (!s.merged && !s.failed)) return null;
        const total = s.merged + s.failed;
        const rate = total > 0 ? s.merged / total : 0;
        const color = rate >= 0.7 ? 'border-emerald-700 bg-emerald-900/40 text-emerald-300' : rate >= 0.4 ? 'border-amber-700 bg-amber-900/40 text-amber-300' : 'border-red-700 bg-red-900/40 text-red-300';
        const subtypes = [[s.failed_code_error, 'code'], [s.failed_infra, 'infra'], [s.failed_scope_overflow, 'scope']]
          .filter(([v]) => typeof v === 'number' && (v as number) > 0)
          .map(([v, l]) => `${v} ${l}`)
          .join(', ');
        const subtypeStr = subtypes ? ` (${subtypes})` : '';
        return (
          <span
            key={sz}
            title={`${sz}: ${s.merged} merged / ${s.failed} failed${subtypeStr} (${Math.round(rate * 100)}%)`}
            className={`text-ui-xs leading-none px-1 py-0.5 rounded border ${color}`}
          >
            {sz[0].toUpperCase()} {s.merged}/{s.failed}
          </span>
        );
      })}
    </div>
  );
});

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
          className="absolute z-20 top-full mt-1 left-0 w-56 bg-neutral-900 border border-neutral-700 rounded-lg p-2 shadow-lg text-ui-sm text-neutral-300 space-y-1.5"
          onClick={e => e.stopPropagation()}
        >
          {byScope && Object.keys(byScope).length > 0 && (
            <div>
              <p className="text-neutral-500 uppercase tracking-wide text-ui-xs mb-0.5">By scope</p>
              {(['small', 'medium', 'large'] as const).map(s => byScope[s] != null ? (
                <div key={s} className="flex justify-between"><span>{s}</span><span className="font-mono">${byScope[s].toFixed(2)}</span></div>
              ) : null)}
            </div>
          )}
          {byType && Object.keys(byType).length > 0 && (
            <div>
              <p className="text-neutral-500 uppercase tracking-wide text-ui-xs mb-0.5">By type</p>
              {sortedByTypeEntries.map(([t, v]) => (
                <div key={t} className="flex justify-between"><span>{t}</span><span className="font-mono">${v.toFixed(2)}</span></div>
              ))}
            </div>
          )}
          {spot != null && onDemand != null && (spot > 0 || onDemand > 0) && (
            <div>
              <p className="text-neutral-500 uppercase tracking-wide text-ui-xs mb-0.5">VM type</p>
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
      className={`text-ui-xs font-medium px-1.5 py-0.5 rounded border ${colorClass} cursor-default`}
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
    <div className="mt-1 text-ui-xs">
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

function QuietDirectionCard({ direction }: { direction: DirectionPortfolioCompositionEntry }) {
  return (
    <div className="min-w-[200px] max-w-[220px] shrink-0 rounded-lg border border-dashed border-neutral-800 bg-neutral-950/50 p-3 opacity-80">
      <div className="flex items-center gap-1.5 mb-1.5">
        <span className="text-ui-xs font-bold uppercase px-1.5 py-0.5 rounded border bg-neutral-800/70 text-neutral-300 border-neutral-700">
          Quiet
        </span>
        <span className={`text-ui-xs font-bold uppercase px-1.5 py-0.5 rounded border ${priorityBadge(direction.priority)}`}>
          {direction.priority}
        </span>
        <span className="text-ui-xs font-medium uppercase tracking-wide text-neutral-500">
          {direction.status ?? 'active'}
        </span>
      </div>
      <p className="text-xs text-neutral-300 line-clamp-2 mb-2">{direction.direction_text}</p>
      <div className="text-ui-sm text-neutral-500">
        Quiet this window · no cycles or new tasks
      </div>
    </div>
  );
}

const EVICTED_FINDINGS_PAGE_SIZE = 10;

interface EvictedDrawerState {
  directionId: string;
  directionText: string;
  evictedCount: number;
}

export function DirectionPortfolioSection({
  directions,
  directionPortfolio,
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
  const activeDirectionCount = directionPortfolio?.active_count ?? directions.length;
  const quietDirections = directionPortfolio?.composition.filter(d => d.quiet_this_window) ?? [];
  const quietDirectionCount = quietDirections.length;

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

  const proofSummary = useMemo(() => {
    const proven = directions.reduce((sum, d) => sum + Math.max(0, Math.trunc(d.proven_prs_merged ?? 0)), 0);
    const consumed = directions.reduce((sum, d) => sum + Math.max(0, Math.trunc(d.consumed_prs_merged ?? 0)), 0);
    return proven > 0 || consumed > 0 ? { proven, consumed } : null;
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
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Direction Portfolio</h2>
      {loading ? (
        <SectionSkeleton />
      ) : activeDirectionCount === 0 ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 text-neutral-500 text-xs">No active directions</div>
      ) : (
        <>
          <div className="flex items-center gap-3 text-xs text-neutral-400 mb-2">
            <span>{activeDirectionCount} direction{activeDirectionCount !== 1 ? 's' : ''}</span>
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
            {proofSummary && (
              <>
                <span className="text-neutral-600">&middot;</span>
                <span className="text-emerald-300" title="Machine-proven merged PRs">
                  {proofSummary.proven} proven
                </span>
                {proofSummary.consumed > 0 && (
                  <>
                    <span className="text-neutral-600">·</span>
                    <span className="text-sky-300" title="Downstream-artifact proof-by-consumption subset of proven merges">
                      {proofSummary.consumed} consumed
                    </span>
                  </>
                )}
              </>
            )}
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
            {directionPortfolio && quietDirectionCount > 0 && (
              <>
                <span className="text-neutral-600">&middot;</span>
                <span className="text-neutral-500">{quietDirectionCount} quiet this window</span>
              </>
            )}
            {activeDirectionCount > 0 && (
              <>
                <span className="text-neutral-600">&middot;</span>
                <span className={selfSufficientCount === 0 ? 'text-neutral-500' : 'text-emerald-400'}>
                  Self-sufficient: {selfSufficientCount} / {activeDirectionCount}
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
              const provenMerged = Math.max(0, Math.trunc(d.proven_prs_merged ?? 0));
              const consumedMerged = Math.max(0, Math.trunc(d.consumed_prs_merged ?? 0));
              const verifiedMerged = typeof d.verified_prs_merged === 'number' ? Math.max(0, Math.trunc(d.verified_prs_merged)) : null;
              const reachableMerged = typeof d.reachable_prs_merged === 'number' ? Math.max(0, Math.trunc(d.reachable_prs_merged)) : null;
              const declCoverage = typeof d.acceptance_declaration_coverage === 'number' ? d.acceptance_declaration_coverage : null;
              const showProofSplit = provenMerged > 0 || consumedMerged > 0;
              const outcomeRows = summarizeOutcomeKinds(d.outcome_kind_breakdown);
              const showGeneralized = outcomeRows.some(r => r.kind !== 'code_merge');
              return (
                <div
                  key={d.direction_id}
                  className="min-w-[200px] max-w-[220px] bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 shrink-0 cursor-pointer hover:border-neutral-700 transition-colors"
                  onClick={() => onDirectionClick?.(d.direction_id)}
                >
                  <div className="flex items-center gap-1.5 mb-1.5">
                    <span className={`text-ui-xs font-bold uppercase px-1.5 py-0.5 rounded border ${priorityBadge(d.priority)}`}>{d.priority}</span>
                    {d.possibly_saturated && <span className="text-ui-xs font-bold uppercase px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 border border-amber-500/30">Saturated</span>}
                    {d.needs_manual_intervention && <span className="text-ui-xs font-bold uppercase px-1.5 py-0.5 rounded bg-red-500/20 text-red-400 border border-red-500/30">Needs Intervention</span>}
                    {d.auto_paused && <span className="text-ui-xs font-bold uppercase px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-400 border border-amber-500/30" title={d.paused_reason ?? undefined}>Paused</span>}
                    {d.retirement_eligible && <span className="text-ui-xs font-bold uppercase px-1.5 py-0.5 rounded bg-emerald-500/20 text-emerald-400 border border-emerald-500/30" title="Direction is eligible for retirement">Retirement Eligible</span>}
                  </div>
                  <p className="text-xs text-neutral-200 line-clamp-2 mb-2">{d.direction_text}</p>
                  <div className="flex flex-wrap items-center gap-x-2 gap-y-1 text-ui-sm text-neutral-500">
                    {cvr != null && <span className={cvrColor}>{cvr}% conversion</span>}
                    {acc != null && <span className={accColor} title="PRs merged / (merged + rejected)">{acc}% acc</span>}
                    {fas != null && <span className={fasColor} title={`First-attempt success rate${d.first_attempt_sample_size != null ? ` (n=${d.first_attempt_sample_size})` : ''}`}>{fas}% 1st</span>}
                    <span title={reachableMerged == null ? 'Recorded merge badges; ancestry was not checked' : `${d.prs_merged} recorded merge badge${d.prs_merged !== 1 ? 's' : ''}`}>
                      {reachableMerged == null ? `${d.prs_merged} recorded` : `${reachableMerged} landed`}
                      {d.prs_rejected != null && d.prs_rejected > 0 && <span className="text-red-400" title={`${d.prs_rejected} PR${d.prs_rejected !== 1 ? 's' : ''} rejected`}> · {d.prs_rejected} rej</span>}
                    </span>
                    {showProofSplit && <span className="text-emerald-300" title="Machine-proven merged PRs">{provenMerged} proven</span>}
                    {consumedMerged > 0 && <span className="text-sky-300" title="Downstream-artifact proof-by-consumption subset of proven merges">{consumedMerged} consumed</span>}
                    {verifiedMerged != null && verifiedMerged > 0 && <span className="text-teal-300" title="Verification-aware subset of merged PRs driving the conversion rate">{verifiedMerged} verified</span>}
                    {declCoverage != null && (
                      <span
                        className={declCoverage >= 0.7 ? 'text-emerald-400' : declCoverage >= 0.4 ? 'text-amber-400' : 'text-red-400'}
                        title="Fraction of merged write-PRs that committed acceptance evidence (acceptance-declaration coverage)"
                      >
                        {Math.round(declCoverage * 100)}% decl
                      </span>
                    )}
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
                        className="text-ui-xs leading-none px-1.5 py-0.5 rounded border border-violet-500/20 bg-violet-500/10 text-violet-300 hover:bg-violet-500/20"
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
                          ? `Cost per autonomous merge exceeds threshold${medianCostPerMergedPr != null ? ` (project median: $${medianCostPerMergedPr.toFixed(2)})` : ''}`
                          : 'Cost per autonomous merge — operator-assisted merges are excluded from the denominator'}
                      >
                        {d.cost_per_merge_exceeds_threshold && <span className="mr-0.5">⚠</span>}${d.cost_per_merged_pr.toFixed(2)}/autonomous
                      </span>
                    )}
                    {d.avg_time_to_merge_hours != null && (
                      <span className={d.avg_time_to_merge_hours < 2 ? 'text-emerald-400' : d.avg_time_to_merge_hours <= 6 ? 'text-amber-400' : 'text-red-400'} title="Average time from VM start to PR merge">{d.avg_time_to_merge_hours.toFixed(1)}h avg merge</span>
                    )}
                  </div>
                  {d.cost_budget_usd != null && d.cost_budget_usd > 0 && (() => {
                    const hasLifetime = d.lineage_spend_usd != null;
                    const spend = hasLifetime ? d.lineage_spend_usd : d.estimated_cost_usd;
                    const pct = d.budget_utilization_pct != null
                      ? d.budget_utilization_pct
                      : ((spend ?? 0) / d.cost_budget_usd) * 100;
                    const clamped = Math.min(100, Math.max(0, pct));
                    const barColor = pct > 80 ? 'bg-red-500' : pct >= 60 ? 'bg-amber-500' : 'bg-emerald-500';
                    const basisLabel = d.budget_basis
                      ?? (hasLifetime ? 'lifetime lineage' : 'rolling window');
                    const prefix = hasLifetime ? 'Lifetime' : 'Window';
                    const tooltipLines = [
                      `${prefix} $${(spend ?? 0).toFixed(2)} / $${d.cost_budget_usd.toFixed(2)} budget (${Math.round(pct)}%)`,
                    ];
                    if (hasLifetime) {
                      tooltipLines.push(
                        `VM $${(d.lineage_vm_cost_usd ?? 0).toFixed(2)} · LLM $${(d.lineage_llm_cost_usd ?? 0).toFixed(2)} · Compute $${(d.lineage_compute_cost_usd ?? 0).toFixed(2)}`
                      );
                      if (d.lineage_task_count != null) {
                        tooltipLines.push(`${d.lineage_task_count} lineage task${d.lineage_task_count === 1 ? '' : 's'}`);
                      }
                      if (d.estimated_cost_usd != null) {
                        tooltipLines.push(`Rolling window: $${d.estimated_cost_usd.toFixed(2)}`);
                      }
                    }
                    tooltipLines.push(`Basis: ${basisLabel}`);
                    return (
                      <div className="mt-1.5" title={tooltipLines.join('\n')}>
                        <div className="h-1 w-full rounded bg-neutral-800 overflow-hidden">
                          <div className={`h-full ${barColor}`} style={{ width: `${clamped}%` }} />
                        </div>
                        <div className="flex justify-between text-ui-xs text-neutral-500 mt-0.5">
                          <span>Budget</span>
                          <span className={pct > 80 ? 'text-red-400' : pct >= 60 ? 'text-amber-400' : 'text-emerald-400'}>{Math.round(pct)}%{d.budget_exceeded ? ' · over' : ''}</span>
                        </div>
                      </div>
                    );
                  })()}
                  {taxonomyVisibleKeysByDir.has(d.direction_id) && <div className="flex gap-1 mt-1 flex-wrap">{(taxonomyVisibleKeysByDir.get(d.direction_id) ?? []).map(k => { const v = d.taxonomy_summary?.[k] ?? 0; return <span key={k} title={`${TAXONOMY_LABELS[k]}: ${v}`} className={`text-ui-xs leading-none px-1 py-0.5 rounded border ${TAXONOMY_COLORS[k]}`}>{TAXONOMY_LABELS[k]} {v}</span>; })}</div>}
                  {d.scope_breakdown && Object.keys(d.scope_breakdown).length > 0 && <ScopeBreakdownPills scope_breakdown={d.scope_breakdown} />}
                  {showGeneralized && (
                    <div className="flex gap-1 mt-1 flex-wrap">
                      {outcomeRows.map(row => {
                        const rate = row.terminal > 0 ? row.success / row.terminal : 0;
                        const color = rate >= 0.7 ? 'border-emerald-700 bg-emerald-900/40 text-emerald-300' : rate >= 0.4 ? 'border-amber-700 bg-amber-900/40 text-amber-300' : 'border-slate-600 bg-slate-800/60 text-slate-300';
                        return (
                          <span
                            key={row.kind}
                            title={`${row.label}: ${row.success} success / ${row.neutral} neutral / ${row.failure} failure (${Math.round(rate * 100)}%)`}
                            className={`text-ui-xs leading-none px-1 py-0.5 rounded border ${color}`}
                          >
                            {row.label} {row.success}/{row.terminal}
                          </span>
                        );
                      })}
                    </div>
                  )}
                  {d.review_quality && (d.review_quality.reviews_dismissed || d.review_quality.reviews_overridden || d.review_quality.user_issues_caught) ? (
                    <div className="flex gap-1 mt-1 flex-wrap">
                      {d.review_quality.reviews_dismissed ? <span title="Automated reviews dismissed by the system" className="text-ui-xs leading-none px-1 py-0.5 rounded border border-amber-700 bg-amber-900/40 text-amber-300">{d.review_quality.reviews_dismissed} dismissed</span> : null}
                      {d.review_quality.reviews_overridden ? <span title="Reviews overridden by an operator" className="text-ui-xs leading-none px-1 py-0.5 rounded border border-red-700 bg-red-900/40 text-red-300">{d.review_quality.reviews_overridden} overridden</span> : null}
                      {d.review_quality.user_issues_caught ? <span title="Real issues caught by review" className="text-ui-xs leading-none px-1 py-0.5 rounded border border-emerald-700 bg-emerald-900/40 text-emerald-300">{d.review_quality.user_issues_caught} caught</span> : null}
                    </div>
                  ) : null}
                  {sortedStageDropoutsByDir.has(d.direction_id) && <div className="flex gap-1 mt-1 flex-wrap">{(sortedStageDropoutsByDir.get(d.direction_id) ?? []).map(([stage, v]) => <span key={stage} title={`Failed at ${stage}: ${v}`} className="text-ui-xs leading-none px-1 py-0.5 rounded border border-slate-600 bg-slate-800/60 text-slate-300">{STAGE_DROPOUT_LABELS[stage] ?? stage} {v}</span>)}</div>}
                  {d.recent_failure_titles && d.recent_failure_titles.length > 0 && (
                    <ul className="mt-1 space-y-0.5">
                      {d.recent_failure_titles.slice(0, 3).map((t, i) => (
                        <li key={i} className="text-ui-sm text-red-300/80 truncate" title={t}>• {t}</li>
                      ))}
                    </ul>
                  )}
                  {srBreakdown[d.direction_id] && <DirectionSrPill entry={srBreakdown[d.direction_id]} />}
                </div>
              );
            })}
          </div>

          {directionPortfolio && quietDirectionCount > 0 && (
            <div className="space-y-2">
              <div className="text-ui-sm font-bold uppercase tracking-widest text-neutral-500">Quiet this window</div>
              <div className="flex gap-2 overflow-x-auto pb-1 scrollbar-hide" style={{ scrollbarWidth: 'none' }}>
                {quietDirections.map(direction => (
                  <QuietDirectionCard key={direction.direction_id} direction={direction} />
                ))}
              </div>
            </div>
          )}
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
              <p className="text-ui-xs uppercase tracking-widest text-neutral-500">Evicted findings</p>
              <h3 className="text-sm font-semibold text-neutral-100 truncate">{evictedDrawer.directionText}</h3>
              <p className="text-ui-sm text-neutral-500 mt-0.5">
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
                <span className="text-ui-sm text-neutral-600 truncate" title={evictedError}>{evictedError}</span>
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
                        <div className="mt-1 flex flex-wrap items-center gap-2 text-ui-sm text-neutral-500">
                          <span className="px-1.5 py-0.5 rounded border border-neutral-700/60 bg-neutral-900 text-neutral-300 uppercase tracking-wide">{finding.status}</span>
                          <span title={finding.evicted_at}>{timeAgo(finding.evicted_at)}</span>
                        </div>
                      </div>
                    </div>

                    {finding.claims.length > 0 && (
                      <div className="mt-2">
                        <div className="text-ui-sm uppercase tracking-wide text-neutral-500 mb-0.5">Claims</div>
                        <ul className="list-disc list-inside text-ui-md text-neutral-300 space-y-0.5">
                          {finding.claims.map((claim, claimIndex) => (
                            <li key={claimIndex}>{claim}</li>
                          ))}
                        </ul>
                      </div>
                    )}

                    {finding.source_refs.length > 0 && (
                      <div className="mt-2">
                        <div className="text-ui-sm uppercase tracking-wide text-neutral-500 mb-0.5">Source refs</div>
                        <ul className="space-y-0.5 text-ui-md">
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
                  <div className="flex flex-wrap items-center gap-2 mt-1 text-ui-sm text-neutral-400">
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
