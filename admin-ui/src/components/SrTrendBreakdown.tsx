import { TrendingUp, TrendingDown, Minus } from 'lucide-react';

export interface SrCategoryTrend {
  current_hit_rate?: number | null;
  previous_hit_rate?: number | null;
  miss_rate?: number | null;
  trend?: 'increasing' | 'decreasing' | 'stable';
  rate_history?: number[];
}

export interface SrTrendData {
  computed_at?: string | null;
  categories?: Record<string, SrCategoryTrend> | null;
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

function trendColorClass(t: SrCategoryTrend['trend']): string {
  if (t === 'decreasing') return 'text-red-400';
  if (t === 'increasing') return 'text-emerald-400';
  return 'text-neutral-500';
}

function trendStroke(t: SrCategoryTrend['trend']): string {
  if (t === 'decreasing') return '#f87171';
  if (t === 'increasing') return '#34d399';
  return '#737373';
}

// SR `trend` `decreasing` = hit-rate falling = miss-rate rising = regression (red).
// `increasing` hit-rate = miss-rate dropping (green). `stable` is neutral.
function TrendIcon({ trend }: { trend: SrCategoryTrend['trend'] }) {
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
function MissRateSparkline({
  history,
  width,
  height,
  stroke,
}: {
  history: number[];
  width: number;
  height: number;
  stroke: string;
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
    >
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
    const ma = deriveMissRate(a) ?? 0;
    const mb = deriveMissRate(b) ?? 0;
    return mb - ma;
  });
}

interface SrTrendBreakdownProps {
  trends: SrTrendData | null | undefined;
  variant: 'card' | 'inline';
}

export function SrTrendBreakdown({ trends, variant }: SrTrendBreakdownProps) {
  const categories = trends?.categories;
  if (!categories || Object.keys(categories).length === 0) return null;
  const entries = sortedEntries(categories);

  if (variant === 'card') {
    return (
      <div
        data-testid="sr-trend-breakdown"
        className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 min-w-[220px]"
      >
        <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-1">
          SR Miss Rate by Category
        </p>
        <div className="flex flex-col gap-0.5">
          {entries.map(([cat, c]) => {
            const miss = deriveMissRate(c);
            const color = trendColorClass(c.trend);
            return (
              <div key={cat} className="flex items-center gap-2 text-[10px]">
                <span className="text-neutral-300 truncate flex-1" title={cat}>
                  {cat}
                </span>
                <span className={`font-mono font-bold tabular-nums ${color}`}>
                  {miss != null ? `${Math.round(miss * 100)}%` : '–'}
                </span>
                <TrendIcon trend={c.trend} />
                <MissRateSparkline
                  history={c.rate_history ?? []}
                  width={40}
                  height={12}
                  stroke={trendStroke(c.trend)}
                />
              </div>
            );
          })}
        </div>
      </div>
    );
  }

  return (
    <div data-testid="sr-trend-breakdown" className="flex flex-col gap-0.5 ml-1">
      {entries.map(([cat, c]) => {
        const miss = deriveMissRate(c);
        const color = trendColorClass(c.trend);
        return (
          <div key={cat} className="flex items-center gap-1.5 text-[10px]">
            <span className="text-neutral-500">·</span>
            <span className="text-neutral-300">{cat}</span>
            <span className={`font-mono ${color}`}>
              {miss != null ? `${Math.round(miss * 100)}%` : '–'} miss
            </span>
            <TrendIcon trend={c.trend} />
            <MissRateSparkline
              history={c.rate_history ?? []}
              width={32}
              height={10}
              stroke={trendStroke(c.trend)}
            />
          </div>
        );
      })}
    </div>
  );
}
