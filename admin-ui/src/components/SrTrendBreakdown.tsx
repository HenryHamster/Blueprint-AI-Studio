/* eslint-disable react-refresh/only-export-components */
import { memo, useMemo, useState } from 'react';
import { ChevronRight, ChevronDown, AlertTriangle, ArrowDownRight, ArrowUpRight, Search, Loader2, TrendingUp, TrendingDown, Minus } from 'lucide-react';
import type { SrCatchRateHealthResponse } from '../api';
import {
  ExampleList,
  EXECUTION_EVIDENCE_CATS,
  FLOOR_HIT_RATE,
  FLOOR_MIN_WINDOWS,
  MissRateSparkline,
  SKIP_RATE_AMBER_THRESHOLD,
  SrCatchRateFreshnessPill,
  TrendIcon,
  calculateMissRateDelta,
  deltaColorClass,
  deriveMissRate,
  formatDelta,
  formatLatencySec,
  fpRateColorClass,
  isBelowFloor,
  isEngagementThinning,
  isSampleTooSmall,
  sortedEntries,
  trendColorClass,
  trendStroke,
} from './srTrendBreakdown/primitives';
import { CoFailurePairsSection } from './srTrendBreakdown/panels';
import type {
  SrCategoryTrend,
  SrInvestigationSignal,
  SrSeverityBucket,
  SrTaskTypeCategoryStats,
  SrTrendData,
} from './srTrendBreakdown/types';

export type {
  DirectionSrEntry,
  DirectionSrCatStat,
  EdgesTrend,
  EdgesTrendStats,
  SrCategoryTrend,
  SrCheckResultEntry,
  SrCoFailurePair,
  SrInvestigationSignal,
  SrMissExample,
  SrMissedTask,
  SrScopeDriftVsExpansion,
  SrSeverityBucket,
  SrTaskTypeCategoryStats,
  SrTrendData,
  SrUnhandledEdge,
  SrUnhandledEdgesTask,
} from './srTrendBreakdown/types';
export { SR_CANONICAL_CATEGORIES } from './srTrendBreakdown/types';
export {
  MissRateSparkline,
  SrCatchRateFreshnessPill,
  TrendIcon,
  deltaColorClass,
  formatDelta,
  trendColorClass,
  trendStroke,
} from './srTrendBreakdown/primitives';
export {
  DirectionSrBreakdownPanel,
  EdgesTrendBreakdown,
} from './srTrendBreakdown/panels';
export { SrMissedRetrospective, SrUnhandledEdgesRetrospective } from './srTrendBreakdown/retrospectives';

function ByTaskTypeRow({ ttype, cats }: { ttype: string; cats: Record<string, SrTaskTypeCategoryStats> }) {
  const [openCat, setOpenCat] = useState<string | null>(null);
  const sorted = Object.entries(cats).sort(([, a], [, b]) => b.miss_rate - a.miss_rate);
  const openExamples = openCat ? cats[openCat]?.examples ?? [] : [];
  return (
    <div className="text-ui-sm mb-0.5">
      <div className="flex items-start gap-1">
        <span className="text-neutral-400 w-12 shrink-0 font-medium">{ttype}</span>
        <span className="text-neutral-500 flex flex-wrap gap-x-1.5 gap-y-0">
          {sorted.map(([cat, stats]) => {
            const hasExamples = (stats.examples?.length ?? 0) > 0;
            const isOpen = openCat === cat;
            const tooSmall = isSampleTooSmall(stats.sample_count);
            const inner = (
              <>
                <span className="text-neutral-400">{cat}</span>{' '}
                {tooSmall ? (
                  <span className="font-mono text-neutral-500">{Math.round(stats.miss_rate * 100)}/{stats.sample_count} — too small</span>
                ) : (
                  <span className="font-mono text-orange-400">{Math.round(stats.miss_rate * 100)}%</span>
                )}
                {stats.all_iterations_miss_rate != null && (
                  <span className="font-mono text-sky-600" title="all-iterations miss rate">{` all:${Math.round(stats.all_iterations_miss_rate * 100)}%`}</span>
                )}
                {(stats.rate_history?.length ?? 0) >= 2 && (
                  <MissRateSparkline
                    history={stats.rate_history ?? []}
                    width={32}
                    height={10}
                    stroke={trendStroke(stats.trend ?? 'stable', stats.prompt_version_changed)}
                  />
                )}
              </>
            );
            return hasExamples ? (
              <button
                key={cat}
                type="button"
                onClick={() => setOpenCat(isOpen ? null : cat)}
                aria-expanded={isOpen}
                title={`${stats.sample_count} samples — click for examples`}
                className={`hover:text-neutral-300 ${isOpen ? 'underline' : ''}`}
              >{inner}</button>
            ) : (
              <span key={cat} title={`${stats.sample_count} samples`}>{inner}</span>
            );
          })}
        </span>
      </div>
      {openCat && openExamples.length > 0 && <ExampleList examples={openExamples} indent />}
    </div>
  );
}

function ByTaskTypeSection({ byTaskType }: { byTaskType: Record<string, Record<string, SrTaskTypeCategoryStats>> }) {
  const types = Object.keys(byTaskType).sort();
  if (types.length === 0) return null;
  return (
    <div className="mt-1.5 pt-1.5 border-t border-neutral-800/60">
      <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-1">By Task Type</p>
      {types.map(ttype => (
        <ByTaskTypeRow key={ttype} ttype={ttype} cats={byTaskType[ttype]} />
      ))}
    </div>
  );
}

const FIX_ITER_ORDER = ['1', '2', '3+'] as const;

function ByFixIterationRow({
  bucket,
  cats,
  baselineCats,
}: {
  bucket: string;
  cats: Record<string, SrTaskTypeCategoryStats>;
  baselineCats: Record<string, SrTaskTypeCategoryStats> | null;
}) {
  const [openCat, setOpenCat] = useState<string | null>(null);
  const sorted = Object.entries(cats).sort(([, a], [, b]) => b.miss_rate - a.miss_rate);
  const openExamples = openCat ? cats[openCat]?.examples ?? [] : [];
  return (
    <div className="text-ui-sm mb-0.5">
      <div className="flex items-start gap-1">
        <span className="text-neutral-400 w-12 shrink-0 font-medium" title={`Fix iteration ${bucket}`}>{`iter ${bucket}`}</span>
        <span className="text-neutral-500 flex flex-wrap gap-x-1.5 gap-y-0">
          {sorted.map(([cat, stats]) => {
            const hasExamples = (stats.examples?.length ?? 0) > 0;
            const isOpen = openCat === cat;
            const baseline = baselineCats?.[cat]?.miss_rate;
            const delta = baseline != null ? stats.miss_rate - baseline : null;
            const tooSmall = isSampleTooSmall(stats.sample_count);
            const inner = (
              <>
                <span className="text-neutral-400">{cat}</span>{' '}
                {tooSmall ? (
                  <span className="font-mono text-neutral-500">{Math.round(stats.miss_rate * 100)}/{stats.sample_count} — too small</span>
                ) : (
                  <span className="font-mono text-orange-400">{Math.round(stats.miss_rate * 100)}%</span>
                )}
                {delta != null && Math.abs(delta) >= 0.05 && (
                  <span
                    className={`font-mono tabular-nums text-ui-xs ml-0.5 ${deltaColorClass(delta)}`}
                    title={`vs iter 1: ${formatDelta(delta)}`}
                  >
                    {formatDelta(delta)}
                  </span>
                )}
              </>
            );
            return hasExamples ? (
              <button
                key={cat}
                type="button"
                onClick={() => setOpenCat(isOpen ? null : cat)}
                aria-expanded={isOpen}
                title={`${stats.sample_count} samples — click for examples`}
                className={`hover:text-neutral-300 ${isOpen ? 'underline' : ''}`}
              >{inner}</button>
            ) : (
              <span key={cat} title={`${stats.sample_count} samples`}>{inner}</span>
            );
          })}
        </span>
      </div>
      {openCat && openExamples.length > 0 && <ExampleList examples={openExamples} indent />}
    </div>
  );
}

function ByFixIterationSection({ byFixIteration }: { byFixIteration: Record<string, Record<string, SrTaskTypeCategoryStats>> }) {
  const [open, setOpen] = useState(false);
  const buckets = FIX_ITER_ORDER.filter(b => byFixIteration[b] && Object.keys(byFixIteration[b]).length > 0);
  if (buckets.length === 0) return null;
  const baseline = byFixIteration['1'] ?? null;
  return (
    <div className="mt-1.5 pt-1.5 border-t border-neutral-800/60" data-testid="sr-by-fix-iteration-section">
      <button
        type="button"
        onClick={() => setOpen(o => !o)}
        aria-expanded={open}
        className="flex items-center gap-1 text-ui-xs text-neutral-500 uppercase tracking-wide mb-1 hover:text-neutral-300"
      >
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        By Fix Iteration
      </button>
      {open && buckets.map(bucket => (
        <ByFixIterationRow
          key={bucket}
          bucket={bucket}
          cats={byFixIteration[bucket]}
          baselineCats={bucket === '1' ? null : baseline}
        />
      ))}
    </div>
  );
}

const SCOPE_ORDER: readonly string[] = ['small', 'medium', 'large'];

function ByScopeSection({ byScope, onScopeClick }: { byScope: Record<string, Record<string, SrTaskTypeCategoryStats>>; onScopeClick?: (scope: string) => void }) {
  const known = SCOPE_ORDER.filter(s => byScope[s] && Object.keys(byScope[s]).length > 0);
  const extras = Object.keys(byScope)
    .filter(s => !SCOPE_ORDER.includes(s) && Object.keys(byScope[s]).length > 0)
    .sort();
  const scopes = [...known, ...extras];
  if (scopes.length === 0) return null;
  return (
    <div data-testid="sr-trend-by-scope" className="mt-1.5 pt-1.5 border-t border-neutral-800/60">
      <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-1">By Scope</p>
      {scopes.map(scope => (
        onScopeClick ? (
          <div
            key={scope}
            role="button"
            tabIndex={0}
            data-testid={`sr-trend-by-scope-row-${scope}`}
            onClick={() => onScopeClick(scope)}
            onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onScopeClick(scope); } }}
            className="cursor-pointer rounded hover:bg-workbench-hover/40 workbench-focus"
            aria-label={`Filter SR misses by scope ${scope}`}
          >
            <ByTaskTypeRow ttype={scope} cats={byScope[scope]} />
          </div>
        ) : (
          <ByTaskTypeRow key={scope} ttype={scope} cats={byScope[scope]} />
        )
      ))}
    </div>
  );
}

const SEV_ORDER = ['CRITICAL', 'MAJOR', 'MINOR'] as const;

function sevLabelColor(sev: string): string {
  if (sev === 'CRITICAL') return 'text-red-400';
  if (sev === 'MAJOR') return 'text-amber-400';
  return 'text-neutral-400';
}

function sevTrendColorClass(trend: SrSeverityBucket['trend']): string {
  if (trend === 'regressing') return 'text-red-400';
  if (trend === 'improving') return 'text-emerald-400';
  return 'text-neutral-500';
}

function SevTrendIcon({ trend }: { trend: SrSeverityBucket['trend'] }) {
  const color = sevTrendColorClass(trend);
  if (trend === 'regressing') {
    return <TrendingUp className={`w-3 h-3 ${color}`} aria-label="miss-rate regressing" />;
  }
  if (trend === 'improving') {
    return <TrendingDown className={`w-3 h-3 ${color}`} aria-label="miss-rate improving" />;
  }
  return <Minus className={`w-3 h-3 ${color}`} aria-label="miss-rate stable" />;
}

function SeverityBucketItem({ sev, bucket }: { sev: string; bucket: SrSeverityBucket }) {
  const miss = bucket.current_miss_rate;
  const prevMiss = bucket.previous_miss_rate;
  const delta = miss != null && prevMiss != null ? miss - prevMiss : null;
  const trendColor = sevTrendColorClass(bucket.trend);
  const tooSmall = isSampleTooSmall(bucket.current_sample);
  return (
    <div className="flex items-center gap-1 text-ui-sm">
      <span className={`font-mono shrink-0 font-medium ${sevLabelColor(sev)}`} title={sev}>
        {sev[0]}
      </span>
      {tooSmall ? (
        <span className="font-mono tabular-nums text-neutral-500">
          {miss != null ? `${Math.round(miss * 100)}/${bucket.current_sample} — too small` : '–'}
        </span>
      ) : (
        <span className={`font-mono font-bold tabular-nums ${trendColor}`}>
          {miss != null ? `${Math.round(miss * 100)}%` : '–'}
        </span>
      )}
      {delta != null && !tooSmall && (
        <span className={`font-mono tabular-nums text-ui-xs ${deltaColorClass(delta)}`}>
          {formatDelta(delta)}
        </span>
      )}
      {!tooSmall && <SevTrendIcon trend={bucket.trend} />}
    </div>
  );
}

function SeveritySection({ bySeverity, inline }: { bySeverity: Record<string, SrSeverityBucket>; inline?: boolean }) {
  const visible = SEV_ORDER.filter(s => {
    const b = bySeverity[s];
    return b && ((b.current_sample ?? 0) > 0 || (b.previous_sample ?? 0) > 0);
  });
  if (visible.length === 0) return null;

  if (inline) {
    return (
      <div className="flex items-center gap-2 text-ui-sm ml-1">
        <span className="text-neutral-600 text-ui-xs uppercase tracking-wide shrink-0">sev</span>
        {visible.map(sev => (
          <SeverityBucketItem key={sev} sev={sev} bucket={bySeverity[sev]} />
        ))}
      </div>
    );
  }

  return (
    <div className="mb-1.5 pb-1.5 border-b border-neutral-800/60">
      <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-0.5">By Severity</p>
      <div className="flex gap-3 flex-wrap">
        {visible.map(sev => (
          <SeverityBucketItem key={sev} sev={sev} bucket={bySeverity[sev]} />
        ))}
      </div>
    </div>
  );
}

export interface TargetHealthStats {
  configured: number;
  belowTarget: number;
  newlyMissed: number;
  newlyRecovered: number;
  hasCycleData: boolean;
}

export function computeTargetHealth(
  categories: Record<string, SrCategoryTrend> | null | undefined,
  targets: Record<string, number> | null | undefined,
): TargetHealthStats | null {
  if (!targets || Object.keys(targets).length === 0) return null;
  const configured = Object.keys(targets).length;
  let belowTarget = 0;
  let newlyMissed = 0;
  let newlyRecovered = 0;
  let hasCycleData = false;

  for (const [cat, target] of Object.entries(targets)) {
    const c = categories?.[cat];
    if (!c) continue;
    const cur = c.current_hit_rate;
    const prev = c.previous_hit_rate;
    if (cur == null) continue;
    const nowBelow = cur < target;
    if (nowBelow) belowTarget++;
    if (prev != null) {
      hasCycleData = true;
      const wasBelow = prev < target;
      if (nowBelow && !wasBelow) newlyMissed++;
      if (!nowBelow && wasBelow) newlyRecovered++;
    }
  }

  return { configured, belowTarget, newlyMissed, newlyRecovered, hasCycleData };
}

function TargetHealthSummaryCard({ stats }: { stats: TargetHealthStats }) {
  const allHealthy = stats.belowTarget === 0;
  return (
    <div
      data-testid="target-health-summary"
      className={`flex items-center gap-3 px-2 py-1 rounded mb-1.5 text-ui-sm font-mono border ${
        allHealthy
          ? 'bg-emerald-950/40 border-emerald-800/30 text-emerald-400'
          : 'bg-red-950/30 border-red-800/30 text-red-400'
      }`}
    >
      <span title={`${stats.configured} categories have a configured target`}>
        {stats.configured} configured
      </span>
      <span className="text-neutral-600">·</span>
      <span
        title={`${stats.belowTarget} of ${stats.configured} categories are currently below their target hit rate`}
        className={stats.belowTarget > 0 ? 'text-red-400' : 'text-emerald-400'}
      >
        {stats.belowTarget} below target
      </span>
      {stats.hasCycleData && (
        <>
          {stats.newlyMissed > 0 && (
            <>
              <span className="text-neutral-600">·</span>
              <span
                data-testid="newly-missed-indicator"
                title={`${stats.newlyMissed} categories crossed below their target this cycle`}
                className="flex items-center gap-0.5 text-red-400"
              >
                <ArrowDownRight className="w-2.5 h-2.5" />
                {stats.newlyMissed} new miss
              </span>
            </>
          )}
          {stats.newlyRecovered > 0 && (
            <>
              <span className="text-neutral-600">·</span>
              <span
                data-testid="newly-recovered-indicator"
                title={`${stats.newlyRecovered} categories recovered above their target this cycle`}
                className="flex items-center gap-0.5 text-emerald-400"
              >
                <ArrowUpRight className="w-2.5 h-2.5" />
                {stats.newlyRecovered} recovered
              </span>
            </>
          )}
        </>
      )}
    </div>
  );
}

interface SrTrendBreakdownProps {
  trends: SrTrendData | null | undefined;
  variant: 'card' | 'inline';
  onCategoryClick?: (category: string) => void;
  /** Called when a row in the By Scope breakdown is clicked. Used to drill from a scope bucket into the SR-missed panel filtered to that scope. */
  onScopeClick?: (scope: string) => void;
  /** Per-category minimum hit-rate targets configured by the operator. When unset, falls back to the default 50% floor. */
  srCatchRateTargets?: Record<string, number> | null;
  /** Callback invoked when the user clicks the "configure targets" CTA. Parent is responsible for routing to the settings destination. */
  onConfigureTargets?: () => void;
  /** Called when operator requests an SR investigation for a category. Returns the spawned task id on success, null on failure. */
  onSpawnSrInvestigation?: (signal: SrInvestigationSignal) => Promise<string | null>;
  /** Map from category id → open investigation task id; used to dedup stacking. */
  activeInvestigationTaskIds?: Record<string, string> | null;
  srCatchRateHealth?: SrCatchRateHealthResponse | null;
}

function CategoryRow({
  cat,
  c,
  variant,
  onCategoryClick,
  targetHitRate,
  onSpawnSrInvestigation,
  existingInvestigationTaskId,
}: {
  cat: string;
  c: SrCategoryTrend;
  variant: 'card' | 'inline';
  targetHitRate?: number | null;
  onCategoryClick?: (category: string) => void;
  onSpawnSrInvestigation?: (signal: SrInvestigationSignal) => Promise<string | null>;
  existingInvestigationTaskId?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [spawning, setSpawning] = useState(false);
  const [spawnedTaskId, setSpawnedTaskId] = useState<string | null>(null);
  const activeTaskId = existingInvestigationTaskId ?? spawnedTaskId;
  const miss = deriveMissRate(c);
  const allIterMiss = c.all_iterations_miss_rate;
  const wmiss = c.weighted_miss_rate;
  const fp = c.false_positive_rate;
  const prec = c.precision;
  const color = trendColorClass(c.trend);
  const delta = calculateMissRateDelta(c);
  const deltaColor = delta != null ? deltaColorClass(delta) : '';
  const effectiveFloor = targetHitRate ?? FLOOR_HIT_RATE;
  // Use backend below_floor if provided; fall back to local derivation for backward compat
  const belowFloor = c.below_floor != null ? c.below_floor : isBelowFloor(c, effectiveFloor);
  const quadrant = c.precision_recall_quadrant;
  const quadrantBadge = (() => {
    if (!quadrant || quadrant === 'healthy' || quadrant === 'mixed' || quadrant === 'flat') return null;
    const label = quadrant === 'conservative_shift' ? 'conservative' : quadrant === 'noisy_shift' ? 'noisy' : 'declining';
    const cls = quadrant === 'both_declining' ? 'text-red-400' : 'text-amber-500';
    const pdStr = c.precision_delta != null ? formatDelta(c.precision_delta) : 'N/A';
    const rdStr = c.recall_delta != null ? formatDelta(c.recall_delta) : 'N/A';
    const numericDelta = `precision ${pdStr} / recall ${rdStr}`;
    const explanation =
      quadrant === 'conservative_shift' ? 'Self-review is catching fewer real issues while maintaining accuracy (under-flagging). Consider loosening criteria or investigating why issues are missed.'
      : quadrant === 'noisy_shift' ? 'Self-review is flagging more things reviewers don\'t confirm, indicating over-flagging. Review false-positive patterns or tighten flagging criteria.'
      : 'Both precision and recall have degraded, indicating the category needs recalibration. Investigate root cause of accuracy loss.';
    const tip = `${explanation}\n\n${numericDelta}`;
    return <span className={`font-mono text-ui-xs ${cls}`} title={tip}>{label}</span>;
  })();
  const examples = c.examples ?? [];
  const hasExamples = examples.length > 0;
  const clickable = !!onCategoryClick;
  const ChevronIcon = open ? ChevronDown : ChevronRight;
  const chevron = hasExamples ? (
    <button
      type="button"
      onClick={() => setOpen(!open)}
      aria-expanded={open}
      aria-label={`${open ? 'collapse' : 'expand'} ${cat} examples`}
      className="text-neutral-500 hover:text-neutral-300 shrink-0"
    >
      <ChevronIcon className="w-3 h-3" />
    </button>
  ) : null;

  const interactiveProps = clickable ? {
    role: 'button',
    tabIndex: 0,
    onClick: () => onCategoryClick(cat),
    onKeyDown: (e: React.KeyboardEvent) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onCategoryClick(cat); }
    },
  } : {};

  const sparklineTitle = c.prompt_version_changed ? 'prompt version changed mid-window — attribution may differ' : undefined;
  // Gate confidence on reviewer finding count (true denominator of the catch rate).
  // Fall back to older aliases for backward compatibility with stored payloads.
  const findingCount =
    c.current_reviewer_finding_count ??
    c.current_finding_count ??
    c.current_human_count;
  const prevFindingCount =
    c.previous_reviewer_finding_count ??
    c.previous_finding_count ??
    c.previous_human_count;
  const sampleTooSmall = isSampleTooSmall(findingCount);
  const deltaLowConfidence = sampleTooSmall || isSampleTooSmall(prevFindingCount);

  const thinning = c.engagement_thinning != null ? c.engagement_thinning : isEngagementThinning(c);
  const activeSignals: string[] = [
    ...(c.below_floor ? ['below_floor'] : []),
    ...(thinning ? ['thinning'] : []),
    ...(c.severity_calibration && c.severity_calibration.total >= 5 && c.severity_calibration.under_rated / c.severity_calibration.total >= 0.30 ? ['under_calibration'] : []),
    ...(c.time_to_miss_detect_lone_checker ? ['lone_checker'] : []),
  ];
  const handleSpawn = async () => {
    if (!onSpawnSrInvestigation || spawning) return;
    setSpawning(true);
    try {
      const id = await onSpawnSrInvestigation({ category: cat, current_hit_rate: c.current_hit_rate, previous_hit_rate: c.previous_hit_rate, sample_size: findingCount, signals: activeSignals });
      if (id) setSpawnedTaskId(id);
    } finally { setSpawning(false); }
  };
  const investigateButton = onSpawnSrInvestigation && activeSignals.length > 0 && !sampleTooSmall && (
    activeTaskId ? (
      <span className="font-mono text-ui-xs text-cyan-600 shrink-0 tabular-nums" title={`SR investigation: ${activeTaskId}`}>{activeTaskId.slice(0, 8)}</span>
    ) : (
      <button type="button" onClick={handleSpawn} disabled={spawning} title={`Investigate ${cat}: ${activeSignals.join(', ')}`} className="shrink-0 text-neutral-500 hover:text-amber-400 disabled:opacity-40" aria-label={`Spawn SR investigation for ${cat}`}>
        {spawning ? <Loader2 className="w-3 h-3 animate-spin" /> : <Search className="w-3 h-3" />}
      </button>
    )
  );

  if (variant === 'card') {
    const rowBody = (
      <>
        <span className="text-neutral-300 truncate flex-1" title={cat}>{cat}</span>
        {sampleTooSmall ? (
          <span className="font-mono tabular-nums text-neutral-500">
            {miss != null ? `${Math.round(miss * 100)}/${findingCount} — too small` : '–'}
          </span>
        ) : (
          <span className={`font-mono font-bold tabular-nums ${color}`}>
            {miss != null ? `${Math.round(miss * 100)}%` : '–'}
          </span>
        )}
        {allIterMiss != null && (
          <span className="font-mono tabular-nums text-sky-600" title="miss rate including iteration 2+ feedback (all-iterations)">
            {`all:${Math.round(allIterMiss * 100)}%`}
          </span>
        )}
        {delta != null && !deltaLowConfidence && (
          <span className={`font-mono tabular-nums text-xs ${deltaColor}`} title="cycle-over-cycle change">
            {formatDelta(delta)}
          </span>
        )}
        {wmiss != null && (
          <span className="font-mono tabular-nums text-neutral-500" title="severity-weighted miss rate">
            {`w:${Math.round(wmiss * 100)}%`}
          </span>
        )}
        {fp != null && (
          <span
            className={`font-mono tabular-nums ${fpRateColorClass(fp)}`}
            title="false-positive rate: SR failed but no reviewer/CI issue confirmed"
          >
            {`fp:${Math.round(fp * 100)}%`}
          </span>
        )}
        {prec != null && (
          <span className="font-mono tabular-nums text-neutral-500" title="precision: hits / (hits + false-positives)">
            {`prec:${Math.round(prec * 100)}%`}
          </span>
        )}
        {c.reasoning_engagement_rate != null && (c.reasoning_engagement_total ?? 0) > 0 && (() => {
          const displayThinning = c.engagement_thinning != null ? c.engagement_thinning : isEngagementThinning(c);
          return (
            <span
              className={`font-mono tabular-nums ${displayThinning ? 'text-amber-400' : 'text-cyan-500'}`}
              title={displayThinning
                ? `Reasoning thinning: only ${c.reasoning_engagement_with}/${c.reasoning_engagement_total} PASS verdicts include substantial reasoning — may regress soon`
                : `Reasoning engagement: ${c.reasoning_engagement_with}/${c.reasoning_engagement_total} PASS verdicts with substantial reasoning`}
            >
              {`eng:${Math.round(c.reasoning_engagement_rate * 100)}%`}
            </span>
          );
        })()}
        {c.severity_calibration != null && c.severity_calibration.total >= 5 &&
          c.severity_calibration.under_rated / c.severity_calibration.total >= 0.30 && (
          <span
            className="font-mono tabular-nums text-amber-400"
            title={`Severity under-rated: agent called ${c.severity_calibration.under_rated}/${c.severity_calibration.total} matched instances at lower severity than reviewer — under-rating suppresses escalation`}
          >
            {`undercal:${Math.round(c.severity_calibration.under_rated / c.severity_calibration.total * 100)}%`}
          </span>
        )}
        {c.time_to_miss_detect_p50_sec != null && (
          <span className="font-mono tabular-nums text-neutral-500" title={`Median time-to-detect SR miss: ${formatLatencySec(c.time_to_miss_detect_p50_sec)}`}>
            {`t50:${formatLatencySec(c.time_to_miss_detect_p50_sec)}`}
          </span>
        )}
        {c.time_to_miss_detect_p95_sec != null && (
          <span className="font-mono tabular-nums text-neutral-500" title={`p95 time-to-detect SR miss: ${formatLatencySec(c.time_to_miss_detect_p95_sec)}`}>
            {`t95:${formatLatencySec(c.time_to_miss_detect_p95_sec)}`}
          </span>
        )}
        {targetHitRate != null && c.current_hit_rate != null && (() => {
          const gap = (targetHitRate - c.current_hit_rate) * 100;
          const isMissing = gap > 0;
          const gapColor = isMissing ? 'text-red-400' : 'text-neutral-400';
          const gapSign = gap > 0 ? '−' : '';
          return (
            <span
              className={`font-mono tabular-nums text-xs ${gapColor}`}
              title={`Target: ${Math.round(targetHitRate * 100)}% hit rate — gap: ${gapSign}${Math.abs(Math.round(gap))}pp`}
            >
              {`target ${Math.round(targetHitRate * 100)}% (${gapSign}${Math.abs(Math.round(gap))}pp)`}
            </span>
          );
        })()}
        {!sampleTooSmall && <TrendIcon trend={c.trend} />}
        {belowFloor && (
          <span
            className="shrink-0"
            title={`Hit rate stuck below ${Math.round(effectiveFloor * 100)}%${targetHitRate != null ? ' (configured target)' : ''} for ${FLOOR_MIN_WINDOWS}+ windows`}
            aria-label="below floor"
          >
            <AlertTriangle className="w-3 h-3 text-orange-400" />
          </span>
        )}
        {quadrantBadge}
        {c.time_to_miss_detect_lone_checker && (
          <span
            className="shrink-0"
            title="SR is the lone checker for this category — p95 time-to-detect > 7 days"
            aria-label="lone checker"
          >
            <AlertTriangle className="w-3 h-3 text-red-400" />
          </span>
        )}
        <MissRateSparkline
          history={c.rate_history ?? []}
          width={40}
          height={12}
          stroke={trendStroke(c.trend, c.prompt_version_changed)}
          title={sparklineTitle}
          target={targetHitRate}
        />
      </>
    );
    return (
      <div>
        <div className="flex items-center gap-2 text-ui-sm min-w-0">
          {chevron}
          {clickable ? (
            <div
              className="flex items-center gap-2 flex-1 rounded px-0.5 cursor-pointer hover:bg-workbench-hover/60 min-w-0"
              {...interactiveProps}
            >
              {rowBody}
            </div>
          ) : (
            <div className="flex items-center gap-2 flex-1 rounded px-0.5 min-w-0">
              {rowBody}
            </div>
          )}
          {investigateButton}
        </div>
        {open && hasExamples && <ExampleList examples={examples} />}
      </div>
    );
  }

  const inlineBody = (
    <>
      <span className="text-neutral-300">{cat}</span>
      {sampleTooSmall ? (
        <span className="font-mono text-neutral-500">
          {miss != null ? `${Math.round(miss * 100)}/${findingCount} — too small` : '–'} miss
        </span>
      ) : (
        <span className={`font-mono ${color}`}>
          {miss != null ? `${Math.round(miss * 100)}%` : '–'} miss
        </span>
      )}
      {allIterMiss != null && (
        <span className="font-mono text-sky-600" title="miss rate including iteration 2+ feedback (all-iterations)">
          {`all:${Math.round(allIterMiss * 100)}%`}
        </span>
      )}
      {delta != null && !deltaLowConfidence && (
        <span className={`font-mono text-xs ${deltaColor}`} title="cycle-over-cycle change">
          {formatDelta(delta)}
        </span>
      )}
      {wmiss != null && (
        <span className="font-mono text-neutral-500" title="severity-weighted miss rate">
          {`w:${Math.round(wmiss * 100)}%`}
        </span>
      )}
      {fp != null && (
        <span
          className={`font-mono ${fpRateColorClass(fp)}`}
          title="false-positive rate: SR failed but no reviewer/CI issue confirmed"
        >
          {`fp:${Math.round(fp * 100)}%`}
        </span>
      )}
      {prec != null && (
        <span className="font-mono text-neutral-500" title="precision: hits / (hits + false-positives)">
          {`prec:${Math.round(prec * 100)}%`}
        </span>
      )}
      {c.reasoning_engagement_rate != null && (c.reasoning_engagement_total ?? 0) > 0 && (() => {
        const displayThinning = c.engagement_thinning != null ? c.engagement_thinning : isEngagementThinning(c);
        return (
          <span
            className={`font-mono ${displayThinning ? 'text-amber-400' : 'text-cyan-500'}`}
            title={displayThinning
              ? `Reasoning thinning: only ${c.reasoning_engagement_with}/${c.reasoning_engagement_total} PASS verdicts include substantial reasoning — may regress soon`
              : `Reasoning engagement: ${c.reasoning_engagement_with}/${c.reasoning_engagement_total} PASS verdicts with substantial reasoning`}
          >
            {`eng:${Math.round(c.reasoning_engagement_rate * 100)}%`}
          </span>
        );
      })()}
      {c.severity_calibration != null && c.severity_calibration.total >= 5 &&
        c.severity_calibration.under_rated / c.severity_calibration.total >= 0.30 && (
        <span
          className="font-mono text-amber-400"
          title={`Severity under-rated: agent called ${c.severity_calibration.under_rated}/${c.severity_calibration.total} matched instances at lower severity than reviewer — under-rating suppresses escalation`}
        >
          {`undercal:${Math.round(c.severity_calibration.under_rated / c.severity_calibration.total * 100)}%`}
        </span>
      )}
      {c.time_to_miss_detect_p50_sec != null && (
        <span className="font-mono text-neutral-500" title={`Median time-to-detect SR miss: ${formatLatencySec(c.time_to_miss_detect_p50_sec)}`}>
          {`t50:${formatLatencySec(c.time_to_miss_detect_p50_sec)}`}
        </span>
      )}
      {c.time_to_miss_detect_p95_sec != null && (
        <span className="font-mono text-neutral-500" title={`p95 time-to-detect SR miss: ${formatLatencySec(c.time_to_miss_detect_p95_sec)}`}>
          {`t95:${formatLatencySec(c.time_to_miss_detect_p95_sec)}`}
        </span>
      )}
      {targetHitRate != null && c.current_hit_rate != null && (() => {
        const gap = (targetHitRate - c.current_hit_rate) * 100;
        const isMissing = gap > 0;
        const gapColor = isMissing ? 'text-red-400' : 'text-neutral-400';
        const gapSign = gap > 0 ? '−' : '';
        return (
          <span
            className={`font-mono text-xs ${gapColor}`}
            title={`Target: ${Math.round(targetHitRate * 100)}% hit rate — gap: ${gapSign}${Math.abs(Math.round(gap))}pp`}
          >
            {`target ${Math.round(targetHitRate * 100)}% (${gapSign}${Math.abs(Math.round(gap))}pp)`}
          </span>
        );
      })()}
      {!sampleTooSmall && <TrendIcon trend={c.trend} />}
      {belowFloor && (
        <span
          className="shrink-0"
          title={`Hit rate stuck below ${Math.round(FLOOR_HIT_RATE * 100)}% for ${FLOOR_MIN_WINDOWS}+ windows`}
          aria-label="below floor"
        >
          <AlertTriangle className="w-3 h-3 text-orange-400" />
        </span>
      )}
      {quadrantBadge}
      {c.time_to_miss_detect_lone_checker && (
        <span
          className="shrink-0"
          title="SR is the lone checker for this category — p95 time-to-detect > 7 days"
          aria-label="lone checker"
        >
          <AlertTriangle className="w-3 h-3 text-red-400" />
        </span>
      )}
      <MissRateSparkline
        history={c.rate_history ?? []}
        width={32}
        height={10}
        stroke={trendStroke(c.trend, c.prompt_version_changed)}
        title={sparklineTitle}
        target={targetHitRate}
      />
    </>
  );

  return (
    <div>
      <div className="flex items-center gap-1.5 text-ui-sm">
        <span className="text-neutral-500">·</span>
        {chevron}
        {clickable ? (
          <div
            className="flex items-center gap-1.5 flex-1 rounded px-0.5 cursor-pointer hover:bg-workbench-hover/60"
            {...interactiveProps}
          >
            {inlineBody}
          </div>
        ) : (
          <div className="flex items-center gap-1.5 flex-1 rounded px-0.5">
            {inlineBody}
          </div>
        )}
      </div>
      {open && hasExamples && <ExampleList examples={examples} />}
    </div>
  );
}

export const SrTrendBreakdown = memo(function SrTrendBreakdown({ trends, variant, onCategoryClick, onScopeClick, srCatchRateTargets, onConfigureTargets, onSpawnSrInvestigation, activeInvestigationTaskIds, srCatchRateHealth }: SrTrendBreakdownProps) {
  const categories = trends?.categories;
  const entries = useMemo(() => categories ? sortedEntries(categories) : [], [categories]);

  const cycleLabel = useMemo(() => {
    if (trends?.bucket_mode === 'cycle' && trends.current_cycle_id) {
      const cur = trends.current_cycle_id.slice(-6);
      const prev = trends.previous_cycle_id ? trends.previous_cycle_id.slice(-6) : '–';
      return `cycle ${cur} vs ${prev}`;
    }
    return null;
  }, [trends]);
  const promptVersionLabel = trends?.prompt_version
    ? `prompt v=${trends.prompt_version.slice(0, 7)}`
    : null;

  const skipRate = trends?.sr_skip_rate;
  const skipDist = trends?.sr_skip_reason_distribution;
  const topSkipReason = useMemo(() => skipDist && Object.keys(skipDist).length > 0
    ? Object.entries(skipDist).sort(([, a], [, b]) => b - a)[0]
    : null, [skipDist]);

  if (!categories || Object.keys(categories).length === 0) return null;

  if (variant === 'card') {
    return (
      <div
        data-testid="sr-trend-breakdown"
        className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[220px]"
      >
        <div className="flex items-baseline gap-2 mb-1">
          <p className="text-ui-xs text-neutral-500 uppercase tracking-wide">
            SR Miss Rate by Category
          </p>
          {cycleLabel && (
            <span className="text-ui-xs text-neutral-600 font-mono">{cycleLabel}</span>
          )}
          {promptVersionLabel && (
            <span className="text-ui-xs text-amber-600 font-mono" title="SR prompt version for this window">{promptVersionLabel}</span>
          )}
          <SrCatchRateFreshnessPill health={srCatchRateHealth} />
          {trends?.mixed_prompt_versions && (
            <span
              data-testid="mixed-prompt-versions-badge"
              className="text-ui-xs text-amber-400 font-mono ml-auto"
              title="This window contains tasks from multiple SR prompt regimes — attribution is unreliable"
            >
              mixed versions
            </span>
          )}
          {!trends?.mixed_prompt_versions && trends?.prompt_version_changed && (
            <span
              data-testid="prompt-version-changed-badge"
              className="text-ui-xs text-amber-600 font-mono ml-auto"
              title="SR prompt version changed since the previous window — cycle-over-cycle delta may reflect guidance change, not genuine catch-rate shift"
            >
              version changed
            </span>
          )}
          {trends?.is_stale && (
            <span
              data-testid="sr-trend-stale-badge"
              className="text-ui-xs text-amber-400 font-mono ml-auto"
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
                className="text-ui-xs text-amber-500 font-mono ml-auto"
                title="Most recent measurement windows had insufficient reviewer-finding data — displayed numbers are frozen and may not reflect current production."
              >
                frozen · last real data {lastRealDate}
              </span>
            );
          })()}
        </div>
        {!srCatchRateTargets || Object.keys(srCatchRateTargets).length === 0 ? (
          <p className="text-ui-md text-neutral-400 hover:text-neutral-200 mb-1">
            <button
              onClick={onConfigureTargets}
              className="hover:underline cursor-pointer"
              data-testid="configure-targets-cta"
            >
              Configure SR catch-rate targets →
            </button>
          </p>
        ) : (() => {
          const healthStats = computeTargetHealth(categories, srCatchRateTargets);
          return healthStats ? <TargetHealthSummaryCard stats={healthStats} /> : null;
        })()}
        {skipRate != null && (
          <p
            data-testid="sr-skip-rate-line"
            className={`text-ui-xs mb-1 font-mono ${skipRate >= SKIP_RATE_AMBER_THRESHOLD ? 'text-amber-400' : 'text-neutral-500'}`}
          >
            {`Skipped ${Math.round(skipRate * 100)}%`}
            {topSkipReason && skipDist && ` (top: ${topSkipReason[0]} ${Math.round(topSkipReason[1] / Object.values(skipDist).reduce((a, b) => a + b, 0) * 100)}%)`}
          </p>
        )}
        {trends?.by_severity && Object.keys(trends.by_severity).length > 0 && (
          <SeveritySection bySeverity={trends.by_severity} />
        )}
        {(() => {
          const execEntries = entries.filter(([cat]) => (EXECUTION_EVIDENCE_CATS as readonly string[]).includes(cat));
          const regularEntries = entries.filter(([cat]) => !(EXECUTION_EVIDENCE_CATS as readonly string[]).includes(cat));
          return (
            <>
              <div className="flex flex-col gap-0.5">
                {regularEntries.map(([cat, c]) => (
                  <CategoryRow key={cat} cat={cat} c={c} variant="card" onCategoryClick={onCategoryClick} targetHitRate={srCatchRateTargets?.[cat] ?? null} onSpawnSrInvestigation={onSpawnSrInvestigation} existingInvestigationTaskId={activeInvestigationTaskIds?.[cat] ?? null} />
                ))}
              </div>
              {execEntries.length > 0 && (
                <div className="mt-1.5 pt-1.5 border-t border-neutral-800/60" data-testid="execution-evidence-section">
                  <p className="text-ui-xs text-neutral-500 uppercase tracking-wide mb-0.5">Execution Evidence</p>
                  <div className="flex flex-col gap-0.5">
                    {execEntries.map(([cat, c]) => (
                      <CategoryRow key={cat} cat={cat} c={c} variant="card" onCategoryClick={onCategoryClick} targetHitRate={srCatchRateTargets?.[cat] ?? null} onSpawnSrInvestigation={onSpawnSrInvestigation} existingInvestigationTaskId={activeInvestigationTaskIds?.[cat] ?? null} />
                    ))}
                  </div>
                </div>
              )}
            </>
          );
        })()}
        {trends?.by_scope && Object.keys(trends.by_scope).length > 0 && (
          <ByScopeSection byScope={trends.by_scope} onScopeClick={onScopeClick} />
        )}
        {trends?.by_task_type && Object.keys(trends.by_task_type).length > 0 && (
          <ByTaskTypeSection byTaskType={trends.by_task_type} />
        )}
        {trends?.by_fix_iteration && Object.keys(trends.by_fix_iteration).length > 0 && (
          <ByFixIterationSection byFixIteration={trends.by_fix_iteration} />
        )}
        {trends?.co_failure_pairs && trends.co_failure_pairs.length > 0 && (
          <CoFailurePairsSection pairs={trends.co_failure_pairs} />
        )}
      </div>
    );
  }

  return (
    <div data-testid="sr-trend-breakdown" className="flex flex-col gap-0.5 ml-1">
      {trends?.is_stale && (
        <span
          data-testid="sr-trend-stale-badge"
          className="text-ui-xs text-amber-400 font-mono"
          title={`Trend data is stale — last computed ${trends.computed_at ? new Date(trends.computed_at).toLocaleDateString() : 'unknown'}`}
        >
          [stale]
        </span>
      )}
      {trends?.data_sufficient === false && (() => {
        const lastRealAt = trends.last_sufficient_computed_at ?? trends.computed_at;
        const lastRealDate = lastRealAt ? new Date(lastRealAt).toLocaleDateString() : '?';
        return (
          <span
            data-testid="sr-trend-insufficient-badge"
            className="text-ui-xs text-amber-500 font-mono"
            title="Most recent measurement windows had insufficient reviewer-finding data — displayed numbers are frozen."
          >
            [frozen · last real data {lastRealDate}]
          </span>
        );
      })()}
      <SrCatchRateFreshnessPill health={srCatchRateHealth} />
      {trends?.by_severity && Object.keys(trends.by_severity).length > 0 && (
        <SeveritySection bySeverity={trends.by_severity} inline />
      )}
      {entries.map(([cat, c]) => (
        <CategoryRow key={cat} cat={cat} c={c} variant="inline" onCategoryClick={onCategoryClick} targetHitRate={srCatchRateTargets?.[cat] ?? null} />
      ))}
    </div>
  );
});
