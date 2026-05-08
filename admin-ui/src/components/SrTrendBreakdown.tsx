import { useState } from 'react';
import { TrendingUp, TrendingDown, Minus, ChevronRight, ChevronDown, AlertTriangle } from 'lucide-react';

export interface SrMissExample {
  severity: 'CRITICAL' | 'MAJOR' | 'MINOR';
  desc: string;
}

export interface SrCategoryTrend {
  current_hit_rate?: number | null;
  previous_hit_rate?: number | null;
  current_human_count?: number | null;
  previous_human_count?: number | null;
  miss_rate?: number | null;
  all_iterations_miss_rate?: number | null;
  weighted_miss_rate?: number | null;
  false_positive_rate?: number | null;
  precision?: number | null;
  sr_alone_hit_count?: number | null;
  reviewer_save_hit_count?: number | null;
  sr_alone_hit_rate?: number | null;
  reviewer_save_rate?: number | null;
  trend?: 'increasing' | 'decreasing' | 'stable';
  rate_history?: number[];
  /** True when the SR prompt version changed within the rate_history window — attribution breaks here. */
  prompt_version_changed?: boolean | null;
  examples?: SrMissExample[];
  /** Backend-computed flag: hit-rate stuck below floor for min_windows cycles. */
  below_floor?: boolean | null;
  /** Reasoning engagement: fraction of PASS verdicts with substantial reasoning text. */
  reasoning_engagement_rate?: number | null;
  /** Reasoning quality score: average quality score of reasoning text (0–1 scale). */
  reasoning_quality_rate?: number | null;
  /** Count of PASS verdicts with reasoning. */
  reasoning_engagement_with?: number | null;
  /** Total PASS verdicts (denominator for engagement rate). */
  reasoning_engagement_total?: number | null;
  /** Precision/recall quadrant classification for this category. */
  precision_recall_quadrant?: 'healthy' | 'conservative_shift' | 'noisy_shift' | 'both_declining' | 'mixed' | 'flat' | null;
  /** Cycle-over-cycle change in precision (hits / sr_failed). */
  precision_delta?: number | null;
  /** Cycle-over-cycle change in recall (hit_rate). */
  recall_delta?: number | null;
  /** Median seconds between SR completion and reviewer rejection for misses in this category. */
  time_to_miss_detect_p50_sec?: number | null;
  /** 95th-percentile seconds between SR completion and reviewer rejection for misses. */
  time_to_miss_detect_p95_sec?: number | null;
  /** Number of miss samples used to compute the latency percentiles. */
  time_to_miss_detect_sample_count?: number | null;
  /** True when sample_count >= 2 AND p95 > 7 days — SR is effectively the sole checker. */
  time_to_miss_detect_lone_checker?: boolean | null;
  /** Severity calibration: how often agent severity matched/under-rated/over-rated reviewer severity for the same category. */
  severity_calibration?: {
    under_rated: number;
    over_rated: number;
    matched: number;
    total: number;
  } | null;
}

export interface SrTaskTypeCategoryStats {
  miss_rate: number;
  all_iterations_miss_rate?: number | null;
  sample_count: number;
  examples?: SrMissExample[];
  rate_history?: number[];
  trend?: 'increasing' | 'decreasing' | 'stable';
  prompt_version_changed?: boolean | null;
}

function severityColor(sev: SrMissExample['severity']): string {
  if (sev === 'CRITICAL') return 'text-red-400';
  if (sev === 'MAJOR') return 'text-amber-400';
  return 'text-neutral-400';
}

export interface SrCheckResultEntry {
  passed: boolean;
  issue?: string | null;
  origin?: string | null;
}

export interface SrReviewerFinding {
  category: string;
  severity?: string | null;
  description: string;
}

function reviewerSeverityColor(severity?: string | null): string {
  if (severity === 'CRITICAL' || severity === 'HIGH') return severityColor('CRITICAL');
  if (severity === 'MAJOR' || severity === 'MEDIUM') return severityColor('MAJOR');
  return severityColor('MINOR');
}

function ExampleList({ examples, indent }: { examples: SrMissExample[]; indent?: boolean }) {
  return (
    <div className={`flex flex-col gap-0.5 mt-0.5 ${indent ? 'pl-4' : 'pl-3'} border-l border-neutral-800/60`}>
      {examples.map((ex, i) => (
        <div key={i} className="flex items-start gap-1 text-[10px] leading-tight">
          <span
            className={`font-mono shrink-0 ${severityColor(ex.severity)}`}
            title={ex.severity}
          >
            {ex.severity[0]}
          </span>
          <span className="text-neutral-400 line-clamp-2">{ex.desc}</span>
        </div>
      ))}
    </div>
  );
}

export interface SrSeverityBucket {
  current_miss_rate?: number | null;
  previous_miss_rate?: number | null;
  current_hit_rate?: number | null;
  previous_hit_rate?: number | null;
  current_sample?: number | null;
  previous_sample?: number | null;
  trend?: 'improving' | 'stable' | 'regressing';
}

export interface ExecutionEvidenceMissRate {
  miss_rate?: number | null;
  miss_count?: number | null;
  sample_count?: number | null;
}

export interface SrTrendData {
  computed_at?: string | null;
  bucket_mode?: 'cycle' | 'calendar' | null;
  current_cycle_id?: string | null;
  previous_cycle_id?: string | null;
  /** Short version stamp of the SR prompt active in the current window (e.g. "a1b2c3"). */
  prompt_version?: string | null;
  /** True when the current measurement window contains tasks from multiple SR prompt regimes — within-window attribution is unreliable. */
  mixed_prompt_versions?: boolean | null;
  /** True when the current window's prompt version differs from the previous window's — cycle-over-cycle delta may reflect guidance change, not genuine catch-rate change. */
  prompt_version_changed?: boolean | null;
  categories?: Record<string, SrCategoryTrend> | null;
  by_task_type?: Record<string, Record<string, SrTaskTypeCategoryStats>> | null;
  by_scope?: Record<string, Record<string, SrTaskTypeCategoryStats>> | null;
  by_fix_iteration?: Record<string, Record<string, SrTaskTypeCategoryStats>> | null;
  sr_skip_rate?: number | null;
  sr_skip_reason_distribution?: Record<string, number> | null;
  by_severity?: Record<string, SrSeverityBucket> | null;
  by_direction?: Record<string, Record<string, DirectionSrCatStat>> | null;
  execution_evidence_miss_rates?: Record<string, ExecutionEvidenceMissRate> | null;
  edges_considered_stats?: Record<string, number> | null;
}

const SKIP_RATE_AMBER_THRESHOLD = 0.25;
const MIN_SAMPLE_SIZE = 5;

const EXECUTION_EVIDENCE_CATS = [
  'tests_executed_cleanly',
  'lint_executed_cleanly',
  'typecheck_executed_cleanly',
] as const;

function isSampleTooSmall(sampleCount: number | null | undefined): boolean {
  return sampleCount == null || sampleCount < MIN_SAMPLE_SIZE;
}

function trendPriority(t: SrCategoryTrend['trend']): number {
  if (t === 'decreasing') return 0;
  if (t === 'stable') return 1;
  return 2;
}

function deriveMissRate(c: SrCategoryTrend): number | null {
  if (c.miss_rate != null) return c.miss_rate;
  if (c.current_hit_rate != null) return 1 - c.current_hit_rate;
  return null;
}

// eslint-disable-next-line react-refresh/only-export-components
export function trendColorClass(t: SrCategoryTrend['trend']): string {
  if (t === 'decreasing') return 'text-red-400';
  if (t === 'increasing') return 'text-emerald-400';
  return 'text-neutral-500';
}

function calculateMissRateDelta(c: SrCategoryTrend): number | null {
  if (c.current_hit_rate == null || c.previous_hit_rate == null) return null;
  const currentMiss = 1 - c.current_hit_rate;
  const previousMiss = 1 - c.previous_hit_rate;
  return currentMiss - previousMiss;
}

// eslint-disable-next-line react-refresh/only-export-components
export function deltaColorClass(delta: number): string {
  if (delta > 0.005) return 'text-red-400'; // regression
  if (delta < -0.005) return 'text-emerald-400'; // improvement
  return 'text-neutral-500'; // stable
}

function fpRateColorClass(fp: number): string {
  if (fp > 0.5) return 'text-red-400';
  if (fp > 0.3) return 'text-amber-400';
  return 'text-neutral-500';
}

// eslint-disable-next-line react-refresh/only-export-components
export function formatDelta(delta: number): string {
  const pp = Math.round(delta * 100);
  if (pp > 0) return `+${pp}pp`;
  if (pp < 0) return `−${Math.abs(pp)}pp`;
  return '±0pp';
}

function formatLatencySec(sec: number): string {
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  if (sec < 172800) return `${Math.round(sec / 3600)}h`;
  return `${Math.round(sec / 86400)}d`;
}

// eslint-disable-next-line react-refresh/only-export-components
export function trendStroke(t: SrCategoryTrend['trend'], versionChanged?: boolean | null): string {
  if (versionChanged) return '#f59e0b'; // amber — attribution break mid-window
  if (t === 'decreasing') return '#f87171';
  if (t === 'increasing') return '#34d399';
  return '#737373';
}

const FLOOR_HIT_RATE = 0.5; // 50% hit-rate floor — categories below this are flagged
const FLOOR_MIN_WINDOWS = 2; // consecutive windows below floor required to flag
const ENGAGEMENT_THINNING_THRESHOLD = 0.5; // mirrors backend _SR_ENGAGEMENT_THRESHOLD
const ENGAGEMENT_THINNING_MIN_PASSES = 10; // mirrors backend _SR_ENGAGEMENT_MIN_PASSES

function isBelowFloor(c: SrCategoryTrend, floorHitRate = FLOOR_HIT_RATE, minWindows = FLOOR_MIN_WINDOWS): boolean {
  const history = c.rate_history;
  if (!history || history.length < minWindows) return false;
  return history.slice(-minWindows).every(r => r < floorHitRate);
}

// SR `trend` `decreasing` = hit-rate falling = miss-rate rising = regression (red).
// `increasing` hit-rate = miss-rate dropping (green). `stable` is neutral.
export function TrendIcon({ trend }: { trend: SrCategoryTrend['trend'] }) {
  const color = trendColorClass(trend);
  if (trend === 'decreasing') {
    return <TrendingUp className={`w-3 h-3 ${color}`} aria-label="miss-rate increasing" />;
  }
  if (trend === 'increasing') {
    return <TrendingDown className={`w-3 h-3 ${color}`} aria-label="miss-rate decreasing" />;
  }
  return <Minus className={`w-3 h-3 ${color}`} aria-label="miss-rate stable" />;
}

// Renders miss-rate (= 1 - hit-rate) so up = worse, matching the "miss-rate
// trending up = regression" framing in surrounding cards.
export function MissRateSparkline({
  history,
  width,
  height,
  stroke,
  title,
}: {
  history: number[];
  width: number;
  height: number;
  stroke: string;
  title?: string;
}) {
  if (!history || history.length < 2) {
    return <span className="inline-block" style={{ width, height }} aria-hidden="true" />;
  }
  const miss = history.map(h => 1 - h);
  const dx = width / (miss.length - 1);
  const points = miss
    .map((v, i) => {
      const clamped = Math.max(0, Math.min(1, v));
      const x = i * dx;
      const y = height - clamped * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className="inline-block align-middle"
      aria-hidden="true"
      role={title ? 'img' : undefined}
      aria-label={title}
    >
      {title && <title>{title}</title>}
      <polyline fill="none" stroke={stroke} strokeWidth="1" points={points} />
    </svg>
  );
}

function sortedEntries(
  categories: Record<string, SrCategoryTrend>,
): [string, SrCategoryTrend][] {
  return Object.entries(categories).sort(([, a], [, b]) => {
    const pa = trendPriority(a.trend);
    const pb = trendPriority(b.trend);
    if (pa !== pb) return pa - pb;
    // Sort by severity-weighted miss rate when available — high-impact misses first
    const ma = a.weighted_miss_rate ?? deriveMissRate(a) ?? 0;
    const mb = b.weighted_miss_rate ?? deriveMissRate(b) ?? 0;
    return mb - ma;
  });
}

function ByTaskTypeRow({ ttype, cats }: { ttype: string; cats: Record<string, SrTaskTypeCategoryStats> }) {
  const [openCat, setOpenCat] = useState<string | null>(null);
  const sorted = Object.entries(cats).sort(([, a], [, b]) => b.miss_rate - a.miss_rate);
  const openExamples = openCat ? cats[openCat]?.examples ?? [] : [];
  return (
    <div className="text-[10px] mb-0.5">
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
      <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-1">By Task Type</p>
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
    <div className="text-[10px] mb-0.5">
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
                    className={`font-mono tabular-nums text-[9px] ml-0.5 ${deltaColorClass(delta)}`}
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
        className="flex items-center gap-1 text-[9px] text-neutral-500 uppercase tracking-wide mb-1 hover:text-neutral-300"
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

function ByScopeSection({ byScope }: { byScope: Record<string, Record<string, SrTaskTypeCategoryStats>> }) {
  const known = SCOPE_ORDER.filter(s => byScope[s] && Object.keys(byScope[s]).length > 0);
  const extras = Object.keys(byScope)
    .filter(s => !SCOPE_ORDER.includes(s) && Object.keys(byScope[s]).length > 0)
    .sort();
  const scopes = [...known, ...extras];
  if (scopes.length === 0) return null;
  return (
    <div data-testid="sr-trend-by-scope" className="mt-1.5 pt-1.5 border-t border-neutral-800/60">
      <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-1">By Scope</p>
      {scopes.map(scope => (
        <ByTaskTypeRow key={scope} ttype={scope} cats={byScope[scope]} />
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
    <div className="flex items-center gap-1 text-[10px]">
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
        <span className={`font-mono tabular-nums text-[9px] ${deltaColorClass(delta)}`}>
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
      <div className="flex items-center gap-2 text-[10px] ml-1">
        <span className="text-neutral-600 text-[9px] uppercase tracking-wide shrink-0">sev</span>
        {visible.map(sev => (
          <SeverityBucketItem key={sev} sev={sev} bucket={bySeverity[sev]} />
        ))}
      </div>
    );
  }

  return (
    <div className="mb-1.5 pb-1.5 border-b border-neutral-800/60">
      <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-0.5">By Severity</p>
      <div className="flex gap-3 flex-wrap">
        {visible.map(sev => (
          <SeverityBucketItem key={sev} sev={sev} bucket={bySeverity[sev]} />
        ))}
      </div>
    </div>
  );
}

export interface FreshReviewerMetricEntry {
  current: number | null;
  previous: number | null;
  delta?: number | null;
  trend?: 'improving' | 'regressing' | 'stable' | null;
}

export interface FreshReviewerTrend {
  computed_at?: string | null;
  bucket_mode?: 'cycle' | 'calendar' | null;
  current_cycle_id?: string | null;
  previous_cycle_id?: string | null;
  current_sample_size?: number | null;
  previous_sample_size?: number | null;
  per_category_hit_rate?: Record<string, FreshReviewerMetricEntry> | null;
  added_value?: Record<string, { current: number; previous: number; trend?: string | null }> | null;
  approval_rate_lift?: FreshReviewerMetricEntry | null;
}

function frTrendColor(trend: string | null | undefined): string {
  if (trend === 'improving') return 'text-emerald-400';
  if (trend === 'regressing') return 'text-red-400';
  return 'text-neutral-500';
}

export function FreshReviewerTrendBreakdown({ trend }: { trend: FreshReviewerTrend | null | undefined }) {
  if (!trend) return null;
  const cats = trend.per_category_hit_rate;
  const lift = trend.approval_rate_lift;
  const hasCats = cats && Object.keys(cats).length > 0;
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
          {Object.entries(cats).map(([cat, m]) => (
            <div key={cat} className="flex items-center gap-2 text-[10px]">
              <span className="text-neutral-300 flex-1 truncate">{cat}</span>
              <span className={`font-mono tabular-nums ${frTrendColor(m.trend)}`}>
                {m.current != null ? `${Math.round(m.current * 100)}%` : '–'}
              </span>
              {m.delta != null && (
                <span className={`font-mono tabular-nums text-xs ${deltaColorClass(m.delta)}`}>
                  {formatDelta(m.delta)}
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

export interface EdgesTrendStats {
  sample_size?: number | null;
  tasks_with_edges_rate?: number | null;
  avg_edges_per_task?: number | null;
  unhandled_edge_flagged_rate?: number | null;
  correctness_rejection_rate_with_edges?: number | null;
  correctness_rejection_rate_without_edges?: number | null;
}

export interface EdgesTrend {
  computed_at?: string | null;
  bucket_mode?: 'cycle' | 'calendar' | null;
  current_cycle_id?: string | null;
  previous_cycle_id?: string | null;
  current?: EdgesTrendStats | null;
  previous?: EdgesTrendStats | null;
  tasks_with_edges_rate_delta?: number | null;
  trend?: 'improving' | 'regressing' | 'stable' | null;
  rate_history?: number[] | null;
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
        const rWith = curr.correctness_rejection_rate_with_edges;
        const rWithout = curr.correctness_rejection_rate_without_edges;
        const gap = rWith != null && rWithout != null ? rWithout - rWith : null;
        return (
          <div className="flex items-center gap-2 text-[10px]">
            <span className="text-neutral-400 flex-1" title="Human-reviewer correctness rejection rate, split by whether the task enumerated edges.">
              correctness rej w/wo
            </span>
            <span className="font-mono tabular-nums text-neutral-300" title="correctness rejection rate when edges were enumerated">
              {rWith != null ? `${Math.round(rWith * 100)}%` : '–'}
            </span>
            <span className="text-neutral-600 text-[9px]">/</span>
            <span className="font-mono tabular-nums text-neutral-300" title="correctness rejection rate when edges were NOT enumerated">
              {rWithout != null ? `${Math.round(rWithout * 100)}%` : '–'}
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
      {history.length > 0 && (
        <div className="flex items-center gap-1 mt-1">
          <span className="text-[9px] text-neutral-600">history</span>
          <div className="flex gap-0.5">
            {history.map((r, i) => (
              <span key={i} className="text-[9px] font-mono text-neutral-500">{Math.round(r * 100)}%</span>
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

export interface DirectionSrCatStat {
  sample_count: number;
  hit_rate?: number | null;
  miss_rate: number | null;
  all_iterations_miss_rate?: number | null;
  weighted_miss_rate?: number | null;
  false_positive_rate?: number | null;
  precision?: number | null;
  sr_alone_hit_count?: number | null;
  reviewer_save_hit_count?: number | null;
  sr_alone_hit_rate?: number | null;
  reviewer_save_rate?: number | null;
  examples?: Array<{ desc: string; severity: string }>;
}

export interface DirectionSrEntry {
  direction_id: string;
  direction_text?: string;
  sample_size: number;
  overall_catch_rate: number | null;
  worst_category: string | null;
  prompt_version?: string | null;
  mixed_prompt_versions?: boolean | null;
  prompt_version_changed?: boolean | null;
  categories: Record<string, DirectionSrCatStat>;
}

export function DirectionSrBreakdownPanel({
  entries,
  onDirectionClick,
}: {
  entries: DirectionSrEntry[];
  onDirectionClick?: (directionId: string) => void;
}) {
  const sorted = [...entries].sort(
    (a, b) => (a.overall_catch_rate ?? 1) - (b.overall_catch_rate ?? 1),
  );
  if (sorted.length === 0) return null;
  return (
    <div data-testid="direction-sr-breakdown-panel" className="flex flex-col gap-1.5">
      {sorted.map(d => {
        const rate = d.overall_catch_rate;
        const rateColor =
          rate == null ? 'text-neutral-500' :
          rate >= 0.7 ? 'text-emerald-400' :
          rate >= 0.4 ? 'text-amber-400' : 'text-red-400';
        const cats = Object.entries(d.categories).sort(
          ([, a], [, b]) => (b.miss_rate ?? 0) - (a.miss_rate ?? 0),
        );
        return (
          <div key={d.direction_id} className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2">
            <div className="flex items-baseline gap-2 mb-0.5">
              <span
                className={`font-mono font-bold tabular-nums ${rateColor}`}
                title="overall SR catch rate for this direction"
              >
                {rate != null ? `${Math.round(rate * 100)}%` : '–'}
              </span>
              <span
                className={`text-[10px] text-neutral-300 truncate flex-1${onDirectionClick ? ' cursor-pointer hover:underline' : ''}`}
                title={d.direction_text}
                onClick={() => onDirectionClick?.(d.direction_id)}
                role={onDirectionClick ? 'button' : undefined}
              >
                {d.direction_text || d.direction_id}
              </span>
              {d.mixed_prompt_versions && (
                <span className="text-[9px] text-amber-400 font-mono shrink-0" title="Tasks from multiple SR prompt versions — attribution unreliable">mixed v</span>
              )}
              {!d.mixed_prompt_versions && d.prompt_version && (
                <span className="text-[9px] text-amber-600 font-mono shrink-0" title="SR prompt version for this window">{`v=${d.prompt_version.slice(0, 7)}`}</span>
              )}
              <span className="text-[9px] text-neutral-600 shrink-0">n={d.sample_size}</span>
            </div>
            {cats.length > 0 && (
              <div className="flex flex-col gap-0.5">
                {cats.map(([cat, s]) => {
                  const tooSmall = isSampleTooSmall(s.sample_count);
                  return (
                  <div key={cat} className="text-[10px]">
                    <div className="flex items-center gap-1.5">
                      <span className="text-neutral-400">{cat}</span>
                      {s.miss_rate != null && (
                        tooSmall ? (
                          <span className="font-mono text-neutral-500">{Math.round(s.miss_rate * 100)}/{s.sample_count} — too small</span>
                        ) : (
                          <span className="font-mono text-orange-400">{Math.round(s.miss_rate * 100)}% miss</span>
                        )
                      )}
                      {s.all_iterations_miss_rate != null && (
                        <span className="font-mono text-sky-600" title="all-iterations miss rate">{`all:${Math.round(s.all_iterations_miss_rate * 100)}%`}</span>
                      )}
                      {s.false_positive_rate != null && (
                        <span className={`font-mono ${fpRateColorClass(s.false_positive_rate)}`} title="false-positive rate">{`fp:${Math.round(s.false_positive_rate * 100)}%`}</span>
                      )}
                      <span className="text-neutral-600 ml-auto">n={s.sample_count}</span>
                    </div>
                    {(s.sr_alone_hit_rate != null || s.reviewer_save_rate != null) && (
                      <div className="flex items-center gap-1 text-[9px] text-neutral-500 font-mono ml-3 mt-0.5">
                        <span>SR-alone {s.sr_alone_hit_rate != null ? `${Math.round(s.sr_alone_hit_rate * 100)}%` : '–'}</span>
                        <span className="text-neutral-700">·</span>
                        <span>reviewer-save {s.reviewer_save_rate != null ? `${Math.round(s.reviewer_save_rate * 100)}%` : '–'}</span>
                      </div>
                    )}
                    {(s.examples ?? []).length > 0 && (
                      <ExampleList
                        examples={(s.examples ?? []).map(e => ({
                          severity: e.severity as SrMissExample['severity'],
                          desc: e.desc,
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

export interface SrMissedTask {
  task_id: string;
  title: string;
  task_type?: string | null;
  scope?: string | null;
  created_at?: string | null;
  missed_categories: string[];
  caught_categories: string[];
  pr_url?: string | null;
  sr_check_results?: Record<string, SrCheckResultEntry>;
  reviewer_findings?: SrReviewerFinding[];
}

interface SrTrendBreakdownProps {
  trends: SrTrendData | null | undefined;
  variant: 'card' | 'inline';
  onCategoryClick?: (category: string) => void;
}

function CategoryRow({
  cat,
  c,
  variant,
  onCategoryClick,
}: {
  cat: string;
  c: SrCategoryTrend;
  variant: 'card' | 'inline';
  onCategoryClick?: (category: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const miss = deriveMissRate(c);
  const allIterMiss = c.all_iterations_miss_rate;
  const wmiss = c.weighted_miss_rate;
  const fp = c.false_positive_rate;
  const prec = c.precision;
  const color = trendColorClass(c.trend);
  const delta = calculateMissRateDelta(c);
  const deltaColor = delta != null ? deltaColorClass(delta) : '';
  // Use backend below_floor if provided; fall back to local derivation for backward compat
  const belowFloor = c.below_floor != null ? c.below_floor : isBelowFloor(c);
  const quadrant = c.precision_recall_quadrant;
  const quadrantBadge = (() => {
    if (!quadrant || quadrant === 'healthy' || quadrant === 'mixed' || quadrant === 'flat') return null;
    const label = quadrant === 'conservative_shift' ? 'conservative' : quadrant === 'noisy_shift' ? 'noisy' : 'declining';
    const cls = quadrant === 'both_declining' ? 'text-red-400' : 'text-amber-500';
    const pdStr = c.precision_delta != null ? formatDelta(c.precision_delta) : 'N/A';
    const rdStr = c.recall_delta != null ? formatDelta(c.recall_delta) : 'N/A';
    const tip = `precision ${pdStr} / recall ${rdStr}`;
    return <span className={`font-mono text-[9px] ${cls}`} title={tip}>{label}</span>;
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
  const sampleTooSmall = isSampleTooSmall(c.current_human_count);
  if (variant === 'card') {
    const srAlone = c.sr_alone_hit_rate;
    const reviewerSave = c.reviewer_save_rate;
    const combined = c.current_hit_rate;
    const showSplit = srAlone != null || reviewerSave != null;

    const rowBody = (
      <>
        <span className="text-neutral-300 truncate flex-1" title={cat}>{cat}</span>
        {sampleTooSmall ? (
          <span className="font-mono tabular-nums text-neutral-500">
            {miss != null ? `${Math.round(miss * 100)}/${c.current_human_count} — too small` : '–'}
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
        {delta != null && (
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
          const thinning = c.reasoning_engagement_rate < ENGAGEMENT_THINNING_THRESHOLD && (c.reasoning_engagement_total ?? 0) >= ENGAGEMENT_THINNING_MIN_PASSES;
          return (
            <span
              className={`font-mono tabular-nums ${thinning ? 'text-amber-400' : 'text-cyan-500'}`}
              title={thinning
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
          width={40}
          height={12}
          stroke={trendStroke(c.trend, c.prompt_version_changed)}
          title={sparklineTitle}
        />
      </>
    );
    return (
      <div>
        <div className="flex items-center gap-2 text-[10px]">
          {chevron}
          {clickable ? (
            <div
              className="flex items-center gap-2 flex-1 rounded px-0.5 cursor-pointer hover:bg-neutral-800/60"
              {...interactiveProps}
            >
              {rowBody}
            </div>
          ) : (
            <div className="flex items-center gap-2 flex-1 rounded px-0.5">
              {rowBody}
            </div>
          )}
        </div>
        {showSplit && (
          <div className="flex items-center gap-2 text-xs text-neutral-500 ml-5 mt-0.5">
            <span className="font-mono">
              SR alone {srAlone != null ? `${Math.round(srAlone * 100)}%` : '–'} ·{' '}
              reviewer-save {reviewerSave != null ? `${Math.round(reviewerSave * 100)}%` : '–'} ·{' '}
              combined {combined != null ? `${Math.round(combined * 100)}%` : '–'} ·{' '}
              miss {miss != null ? `${Math.round(miss * 100)}%` : '–'}
            </span>
          </div>
        )}
        {open && hasExamples && <ExampleList examples={examples} />}
      </div>
    );
  }

  const inlineBody = (
    <>
      <span className="text-neutral-300">{cat}</span>
      {sampleTooSmall ? (
        <span className="font-mono text-neutral-500">
          {miss != null ? `${Math.round(miss * 100)}/${c.current_human_count} — too small` : '–'} miss
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
      {delta != null && (
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
        const thinning = c.reasoning_engagement_rate < ENGAGEMENT_THINNING_THRESHOLD && (c.reasoning_engagement_total ?? 0) >= ENGAGEMENT_THINNING_MIN_PASSES;
        return (
          <span
            className={`font-mono ${thinning ? 'text-amber-400' : 'text-cyan-500'}`}
            title={thinning
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
      />
    </>
  );

  return (
    <div>
      <div className="flex items-center gap-1.5 text-[10px]">
        <span className="text-neutral-500">·</span>
        {chevron}
        {clickable ? (
          <div
            className="flex items-center gap-1.5 flex-1 rounded px-0.5 cursor-pointer hover:bg-neutral-800/60"
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

export function SrTrendBreakdown({ trends, variant, onCategoryClick }: SrTrendBreakdownProps) {
  const categories = trends?.categories;
  if (!categories || Object.keys(categories).length === 0) return null;
  const entries = sortedEntries(categories);

  const cycleLabel = (() => {
    if (trends?.bucket_mode === 'cycle' && trends.current_cycle_id) {
      const cur = trends.current_cycle_id.slice(-6);
      const prev = trends.previous_cycle_id ? trends.previous_cycle_id.slice(-6) : '–';
      return `cycle ${cur} vs ${prev}`;
    }
    return null;
  })();
  const promptVersionLabel = trends?.prompt_version
    ? `prompt v=${trends.prompt_version.slice(0, 7)}`
    : null;

  const skipRate = trends?.sr_skip_rate;
  const skipDist = trends?.sr_skip_reason_distribution;
  const topSkipReason = skipDist && Object.keys(skipDist).length > 0
    ? Object.entries(skipDist).sort(([, a], [, b]) => b - a)[0]
    : null;

  if (variant === 'card') {
    return (
      <div
        data-testid="sr-trend-breakdown"
        className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[220px]"
      >
        <div className="flex items-baseline gap-2 mb-1">
          <p className="text-[9px] text-neutral-500 uppercase tracking-wide">
            SR Miss Rate by Category
          </p>
          {cycleLabel && (
            <span className="text-[9px] text-neutral-600 font-mono">{cycleLabel}</span>
          )}
          {promptVersionLabel && (
            <span className="text-[9px] text-amber-600 font-mono" title="SR prompt version for this window">{promptVersionLabel}</span>
          )}
          {trends?.mixed_prompt_versions && (
            <span
              data-testid="mixed-prompt-versions-badge"
              className="text-[9px] text-amber-400 font-mono ml-auto"
              title="This window contains tasks from multiple SR prompt regimes — attribution is unreliable"
            >
              mixed versions
            </span>
          )}
          {!trends?.mixed_prompt_versions && trends?.prompt_version_changed && (
            <span
              data-testid="prompt-version-changed-badge"
              className="text-[9px] text-amber-600 font-mono ml-auto"
              title="SR prompt version changed since the previous window — cycle-over-cycle delta may reflect guidance change, not genuine catch-rate shift"
            >
              version changed
            </span>
          )}
        </div>
        {skipRate != null && (
          <p
            data-testid="sr-skip-rate-line"
            className={`text-[9px] mb-1 font-mono ${skipRate >= SKIP_RATE_AMBER_THRESHOLD ? 'text-amber-400' : 'text-neutral-500'}`}
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
                  <CategoryRow key={cat} cat={cat} c={c} variant="card" onCategoryClick={onCategoryClick} />
                ))}
              </div>
              {execEntries.length > 0 && (
                <div className="mt-1.5 pt-1.5 border-t border-neutral-800/60" data-testid="execution-evidence-section">
                  <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-0.5">Execution Evidence</p>
                  <div className="flex flex-col gap-0.5">
                    {execEntries.map(([cat, c]) => (
                      <CategoryRow key={cat} cat={cat} c={c} variant="card" onCategoryClick={onCategoryClick} />
                    ))}
                  </div>
                </div>
              )}
            </>
          );
        })()}
        {trends?.by_scope && Object.keys(trends.by_scope).length > 0 && (
          <ByScopeSection byScope={trends.by_scope} />
        )}
        {trends?.by_task_type && Object.keys(trends.by_task_type).length > 0 && (
          <ByTaskTypeSection byTaskType={trends.by_task_type} />
        )}
        {trends?.by_fix_iteration && Object.keys(trends.by_fix_iteration).length > 0 && (
          <ByFixIterationSection byFixIteration={trends.by_fix_iteration} />
        )}
      </div>
    );
  }

  return (
    <div data-testid="sr-trend-breakdown" className="flex flex-col gap-0.5 ml-1">
      {trends?.by_severity && Object.keys(trends.by_severity).length > 0 && (
        <SeveritySection bySeverity={trends.by_severity} inline />
      )}
      {entries.map(([cat, c]) => (
        <CategoryRow key={cat} cat={cat} c={c} variant="inline" onCategoryClick={onCategoryClick} />
      ))}
    </div>
  );
}

export interface SrReviewerDisagreement {
  category: string;
  task_type: string;
  count: number;
}

export interface SrReviewerDisagreementsData {
  window_days: number;
  total_entries: number;
  disagreements: SrReviewerDisagreement[];
}

export function SrReviewerDisagreementsPanel({ data }: { data: SrReviewerDisagreementsData }) {
  if (!data.disagreements.length) return null;
  return (
    <div data-testid="sr-reviewer-disagreements" className="flex flex-wrap gap-1.5">
      {data.disagreements.slice(0, 8).map((d, i) => (
        <span
          key={i}
          title={`${d.count} task${d.count !== 1 ? 's' : ''} where self-review passed ${d.category} but reviewer flagged it`}
          className="inline-flex items-center gap-1.5 text-[10px] font-semibold px-2 py-0.5 rounded border bg-orange-500/10 text-orange-400 border-orange-500/20"
        >
          <span className="truncate max-w-[120px]">{d.category}</span>
          {d.task_type && <span className="text-orange-600 font-normal">{d.task_type}</span>}
          <span className="font-bold shrink-0">×{d.count}</span>
        </span>
      ))}
    </div>
  );
}

export interface SrScopeDriftVsExpansion {
  consistent?: number | null;
  miss_by_expansion?: number | null;
  hit_by_expansion?: number | null;
  fp_candidate?: number | null;
  gray_zone?: number | null;
  miss_by_expansion_rate?: number | null;
  numerator?: number | null;
  denominator?: number | null;
  sample_size?: number | null;
}

export function SrScopeDriftVsExpansionCard({ data }: { data: SrScopeDriftVsExpansion | null | undefined }) {
  if (!data) return null;
  const rate = data.miss_by_expansion_rate;
  const rateColor =
    rate == null ? 'text-neutral-500' :
    rate >= 0.3 ? 'text-red-400' :
    rate >= 0.15 ? 'text-amber-400' :
    'text-emerald-400';
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

const WINDOW_OPTIONS: { label: string; days: number }[] = [
  { label: '7d', days: 7 },
  { label: '14d', days: 14 },
  { label: '30d', days: 30 },
];

interface SrMissedRetrospectiveProps {
  tasks: SrMissedTask[];
  onTaskClick?: (taskId: string) => void;
  initialCategory?: string;
}

export function SrMissedRetrospective({ tasks, onTaskClick, initialCategory }: SrMissedRetrospectiveProps) {
  const [categoryFilter, setCategoryFilter] = useState(initialCategory ?? '');
  const [typeFilter, setTypeFilter] = useState('');
  const [windowDays, setWindowDays] = useState(14);
  const [openTaskIds, setOpenTaskIds] = useState<Set<string>>(() => new Set());
  const [mountedAt] = useState(() => Date.now());

  const allCategories = Array.from(new Set(tasks.flatMap(t => t.missed_categories))).sort();
  const allTypes = Array.from(new Set(tasks.map(t => t.task_type).filter(Boolean) as string[])).sort();

  const cutoff = mountedAt - windowDays * 86400_000;
  const visible = tasks.filter(t => {
    if (t.created_at && new Date(t.created_at).getTime() < cutoff) return false;
    if (categoryFilter && !t.missed_categories.includes(categoryFilter)) return false;
    if (typeFilter && t.task_type !== typeFilter) return false;
    return true;
  });
  const toggleTask = (taskId: string) => setOpenTaskIds(prev => {
    const next = new Set(prev);
    if (next.has(taskId)) next.delete(taskId); else next.add(taskId);
    return next;
  });

  return (
    <div data-testid="sr-missed-retrospective" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-[10px]">
        <span className="text-neutral-500 uppercase tracking-wide">Time</span>
        {WINDOW_OPTIONS.map(w => (
          // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
          <button
            key={w.days}
            onClick={() => setWindowDays(w.days)}
            className={`px-1.5 py-0.5 rounded border ${windowDays === w.days ? 'border-blue-500 text-blue-300' : 'border-neutral-700 text-neutral-400 hover:border-neutral-500'}`}
          >{w.label}</button>
        ))}
        {allCategories.length > 0 && (
          <select
            value={categoryFilter}
            onChange={e => setCategoryFilter(e.target.value)}
            className="bg-neutral-900 border border-neutral-700 rounded px-1 py-0.5 text-neutral-300"
          >
            <option value="">All categories</option>
            {allCategories.map(c => <option key={c} value={c}>{c}</option>)}
          </select>
        )}
        {allTypes.length > 0 && (
          <select
            value={typeFilter}
            onChange={e => setTypeFilter(e.target.value)}
            className="bg-neutral-900 border border-neutral-700 rounded px-1 py-0.5 text-neutral-300"
          >
            <option value="">All types</option>
            {allTypes.map(t => <option key={t} value={t}>{t}</option>)}
          </select>
        )}
        <span className="text-neutral-600">{visible.length} task{visible.length !== 1 ? 's' : ''}</span>
      </div>
      {visible.length === 0 ? (
        <p className="text-[10px] text-neutral-600 italic">No missed tasks for this filter.</p>
      ) : (
        <div className="flex flex-col gap-1">
          {visible.map(t => (
            // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
            <div key={t.task_id} className={`bg-neutral-900/50 border border-neutral-800/50 rounded px-2 py-1.5 text-[10px] ${onTaskClick ? 'cursor-pointer hover:border-neutral-600' : ''}`} onClick={() => onTaskClick?.(t.task_id)}>
              <div className="flex items-center gap-1.5 mb-0.5">
                <button type="button" className="text-neutral-500 hover:text-neutral-300 shrink-0" aria-label={`${openTaskIds.has(t.task_id) ? 'collapse' : 'expand'} ${t.title}`} aria-expanded={openTaskIds.has(t.task_id)} onClick={e => { e.stopPropagation(); toggleTask(t.task_id); }}>
                  {openTaskIds.has(t.task_id) ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
                </button>
                <span className="text-neutral-200 truncate flex-1 font-medium" title={t.title}>{t.title}</span>
                {t.task_type && <span className="text-neutral-500 shrink-0">{t.task_type}</span>}
                {t.scope && <span className="text-neutral-600 shrink-0">{t.scope}</span>}
                {t.created_at && <span className="text-neutral-600 shrink-0" title={t.created_at}>{new Date(t.created_at).toLocaleDateString()}</span>}
                {t.pr_url && <a href={t.pr_url} target="_blank" rel="noopener noreferrer" onClick={e => e.stopPropagation()} className="text-blue-400 hover:underline shrink-0" title={t.pr_url}>PR</a>}
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                {t.missed_categories.length > 0 && <span className="text-red-400">missed: {t.missed_categories.join(', ')}</span>}
                {t.caught_categories.length > 0 && <span className="text-emerald-600">caught: {t.caught_categories.join(', ')}</span>}
              </div>
              {openTaskIds.has(t.task_id) && (
                <div className="mt-1.5 flex flex-col gap-1">
                  {t.missed_categories.map(cat => {
                    const sr = t.sr_check_results?.[cat];
                    const reviewer = t.reviewer_findings?.find(f => f.category === cat);
                    return (
                      <div key={cat} className="rounded border border-neutral-800/60 bg-neutral-950/50 p-1.5">
                        <div className="mb-1 text-neutral-400">{cat}</div>
                        <div className="grid gap-2 md:grid-cols-2">
                          <div className="rounded border border-neutral-800/50 bg-neutral-900/60 p-1.5">
                            <div className="mb-0.5 text-[9px] uppercase tracking-wide text-neutral-500">SR</div>
                            {sr ? (
                              <>
                                <div className={`font-mono ${sr.passed ? 'text-emerald-400' : 'text-red-400'}`}>{sr.passed ? 'passed' : 'failed'}</div>
                                {sr.issue && <div className="text-neutral-400">{sr.issue}</div>}
                                {sr.origin && <div className="text-neutral-600">origin: {sr.origin}</div>}
                              </>
                            ) : (
                              <div className="text-neutral-500">not recorded</div>
                            )}
                          </div>
                          <div className="rounded border border-neutral-800/50 bg-neutral-900/60 p-1.5">
                            <div className="mb-0.5 text-[9px] uppercase tracking-wide text-neutral-500">Reviewer</div>
                            {reviewer ? (
                              <>
                                <div className={`inline-flex rounded px-1 py-0.5 text-[9px] font-mono ${reviewerSeverityColor(reviewer.severity)}`}>{reviewer.severity ?? 'unknown'}</div>
                                <div className="text-neutral-400">{reviewer.description}</div>
                              </>
                            ) : (
                              <div className="text-neutral-500">{cat}</div>
                            )}
                          </div>
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
