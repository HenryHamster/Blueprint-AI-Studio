/* eslint-disable react-refresh/only-export-components */
import { Check, Minus, TrendingDown, TrendingUp } from 'lucide-react';
import type { SrCatchRateHealthResponse } from '../../api';
import type { SrCategoryTrend, SrMissExample, SrSeverityBucket } from './types';

export function severityColor(sev: SrMissExample['severity']): string {
  if (sev === 'CRITICAL') return 'text-red-400';
  if (sev === 'MAJOR') return 'text-amber-400';
  return 'text-neutral-400';
}

export function ExampleList({ examples, indent }: { examples: SrMissExample[]; indent?: boolean }) {
  return (
    <div className={`flex flex-col gap-0.5 mt-0.5 ${indent ? 'pl-4' : 'pl-3'} border-l border-neutral-800/60`}>
      {examples.map((ex, i) => (
        <div key={i} className="flex items-start gap-1 text-ui-sm leading-tight">
          <span className={`font-mono shrink-0 ${severityColor(ex.severity)}`} title={ex.severity}>
            {ex.severity[0]}
          </span>
          <span className="text-neutral-400 line-clamp-2">{ex.desc}</span>
        </div>
      ))}
    </div>
  );
}

const CATCH_RATE_HEALTH_STATUS_STYLE: Record<SrCatchRateHealthResponse['status'], { badge: string; label: string; titlePrefix: string }> = {
  healthy: {
    badge: 'bg-emerald-500/10 text-emerald-300 border-emerald-500/20',
    label: 'fresh',
    titlePrefix: 'Fresh snapshot',
  },
  stale: {
    badge: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
    label: 'snapshot stale',
    titlePrefix: 'Snapshot stale',
  },
  frozen: {
    badge: 'bg-amber-500/10 text-amber-400 border-amber-500/20',
    label: 'snapshot stale',
    titlePrefix: 'Frozen snapshot',
  },
  low_data: {
    badge: 'bg-workbench-panel/70 text-neutral-300 border-neutral-700/70',
    label: 'low data',
    titlePrefix: 'Low data',
  },
  never_sufficient: {
    badge: 'bg-orange-500/10 text-orange-400 border-orange-500/20',
    label: 'never sufficient',
    titlePrefix: 'Never sufficient',
  },
  missing: {
    badge: 'bg-red-500/10 text-red-400 border-red-500/20',
    label: 'no snapshot',
    titlePrefix: 'No snapshot',
  },
};

function formatHealthAge(hours: number | null | undefined): string | null {
  if (hours == null || Number.isNaN(hours)) return null;
  const rounded = hours >= 10 ? Math.round(hours) : Math.round(hours * 10) / 10;
  return `${rounded}h`;
}

function healthSampleLabel(health: SrCatchRateHealthResponse): string {
  return `${health.categories_with_hit_rate}/${health.category_count} categories with hit rate`;
}

function healthTooltip(health: SrCatchRateHealthResponse): string {
  const style = CATCH_RATE_HEALTH_STATUS_STYLE[health.status];
  const lines = [style.titlePrefix];
  const signalAge = formatHealthAge(health.signal_age_hours);
  const sufficientAge = formatHealthAge(health.last_sufficient_age_hours);
  if (signalAge) lines.push(`signal age ${signalAge}`);
  if (sufficientAge) lines.push(`sufficient age ${sufficientAge}`);
  lines.push(`sample ${healthSampleLabel(health)}`);
  if (health.overall_hit_rate != null) {
    lines.push(`overall ${Math.round(health.overall_hit_rate * 100)}% hit rate`);
  }
  return lines.join(' · ');
}

export function SrCatchRateFreshnessPill({ health }: { health?: SrCatchRateHealthResponse | null }) {
  if (!health) return null;
  const style = CATCH_RATE_HEALTH_STATUS_STYLE[health.status];
  const sufficientAge = formatHealthAge(health.last_sufficient_age_hours);
  const label = (() => {
    if (health.status === 'healthy') return style.label;
    if ((health.status === 'stale' || health.status === 'frozen') && sufficientAge) return `${style.label} (${sufficientAge})`;
    if (health.status === 'low_data') return `${style.label} (n=${health.categories_with_hit_rate})`;
    return style.label;
  })();

  return (
    <span
      data-testid={`sr-catch-rate-health-${health.status}`}
      className={`inline-flex items-center gap-1.5 px-1.5 py-0.5 rounded border text-ui-xs font-mono ${style.badge}`}
      title={healthTooltip(health)}
    >
      {health.status === 'healthy' && <Check className="w-2.5 h-2.5 shrink-0" aria-hidden="true" />}
      <span>{label}</span>
    </span>
  );
}

export const SKIP_RATE_AMBER_THRESHOLD = 0.25;
export const MIN_SAMPLE_SIZE = 5;
export const EXECUTION_EVIDENCE_CATS = [
  'tests_executed_cleanly',
  'lint_executed_cleanly',
  'typecheck_executed_cleanly',
] as const;

export function isSampleTooSmall(sampleCount: number | null | undefined): boolean {
  return sampleCount == null || sampleCount < MIN_SAMPLE_SIZE;
}

function trendPriority(t: SrCategoryTrend['trend']): number {
  if (t === 'decreasing') return 0;
  if (t === 'stable') return 1;
  return 2;
}

export function deriveMissRate(c: SrCategoryTrend): number | null {
  if (c.miss_rate != null) return c.miss_rate;
  if (c.current_hit_rate != null) return 1 - c.current_hit_rate;
  return null;
}

export function trendColorClass(t: SrCategoryTrend['trend']): string {
  if (t === 'decreasing') return 'text-red-400';
  if (t === 'increasing') return 'text-emerald-400';
  return 'text-neutral-500';
}

export function calculateMissRateDelta(c: SrCategoryTrend): number | null {
  if (c.current_hit_rate == null || c.previous_hit_rate == null) return null;
  const currentMiss = 1 - c.current_hit_rate;
  const previousMiss = 1 - c.previous_hit_rate;
  return currentMiss - previousMiss;
}

export function deltaColorClass(delta: number): string {
  if (delta > 0.005) return 'text-red-400';
  if (delta < -0.005) return 'text-emerald-400';
  return 'text-neutral-500';
}

export function fpRateColorClass(fp: number): string {
  if (fp > 0.5) return 'text-red-400';
  if (fp > 0.3) return 'text-amber-400';
  return 'text-neutral-500';
}

export function formatDelta(delta: number): string {
  const pp = Math.round(delta * 100);
  if (pp > 0) return `+${pp}pp`;
  if (pp < 0) return `−${Math.abs(pp)}pp`;
  return '±0pp';
}

export function formatLatencySec(sec: number): string {
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  if (sec < 172800) return `${Math.round(sec / 3600)}h`;
  return `${Math.round(sec / 86400)}d`;
}

export function trendStroke(t: SrCategoryTrend['trend'], versionChanged?: boolean | null): string {
  if (versionChanged) return '#f59e0b';
  if (t === 'decreasing') return '#f87171';
  if (t === 'increasing') return '#34d399';
  return '#737373';
}

export const FLOOR_HIT_RATE = 0.5;
export const FLOOR_MIN_WINDOWS = 2;
const ENGAGEMENT_THINNING_THRESHOLD = 0.5;
const ENGAGEMENT_THINNING_MIN_PASSES = 10;

export function isBelowFloor(
  c: SrCategoryTrend,
  floorHitRate = FLOOR_HIT_RATE,
  minWindows = FLOOR_MIN_WINDOWS,
): boolean {
  const history = c.rate_history;
  if (!history || history.length < minWindows) return false;
  return history.slice(-minWindows).every(rate => rate < floorHitRate);
}

export function isEngagementThinning(
  c: SrCategoryTrend,
  thinningThreshold = ENGAGEMENT_THINNING_THRESHOLD,
  minPasses = ENGAGEMENT_THINNING_MIN_PASSES,
): boolean {
  return c.reasoning_engagement_rate != null
    && c.reasoning_engagement_rate < thinningThreshold
    && (c.reasoning_engagement_total ?? 0) >= minPasses;
}

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

export function MissRateSparkline({
  history,
  width,
  height,
  stroke,
  title,
  target,
}: {
  history: number[];
  width: number;
  height: number;
  stroke: string;
  title?: string;
  target?: number | null;
}) {
  if (!history || history.length < 2) {
    return <span className="inline-block" style={{ width, height }} aria-hidden="true" />;
  }
  const miss = history.map(rate => 1 - rate);
  const dx = width / (miss.length - 1);
  const points = miss
    .map((value, index) => {
      const clamped = Math.max(0, Math.min(1, value));
      const x = index * dx;
      const y = height - clamped * height;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');

  const targetMissRate = target != null ? 1 - target : null;
  const targetY = targetMissRate != null ? height - Math.max(0, Math.min(1, targetMissRate)) * height : null;
  const targetPercent = target != null ? Math.round(target * 100) : null;
  const combinedTitle = title
    ? targetPercent != null
      ? `${title} — target: ${targetPercent}% hit rate`
      : title
    : targetPercent != null
      ? `target: ${targetPercent}% hit rate`
      : undefined;

  return (
    <svg
      width={width}
      height={height}
      viewBox={`0 0 ${width} ${height}`}
      className="inline-block align-middle"
      aria-hidden="true"
      role={combinedTitle ? 'img' : undefined}
      aria-label={combinedTitle}
    >
      {combinedTitle && <title>{combinedTitle}</title>}
      <polyline fill="none" stroke={stroke} strokeWidth="1" points={points} />
      {targetY != null && (
        <line x1="0" y1={targetY} x2={width} y2={targetY} stroke="rgb(64, 64, 64)" strokeWidth="1" strokeDasharray="2,2" />
      )}
    </svg>
  );
}

export function sortedEntries(categories: Record<string, SrCategoryTrend>): [string, SrCategoryTrend][] {
  return Object.entries(categories).sort(([, a], [, b]) => {
    const pa = trendPriority(a.trend);
    const pb = trendPriority(b.trend);
    if (pa !== pb) return pa - pb;
    const ma = a.weighted_miss_rate ?? deriveMissRate(a) ?? 0;
    const mb = b.weighted_miss_rate ?? deriveMissRate(b) ?? 0;
    return mb - ma;
  });
}

export function sevTrendColorClass(trend: SrSeverityBucket['trend']): string {
  if (trend === 'regressing') return 'text-red-400';
  if (trend === 'improving') return 'text-emerald-400';
  return 'text-neutral-500';
}
