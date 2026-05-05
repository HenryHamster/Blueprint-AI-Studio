import { useState } from 'react';
import { TrendingUp, TrendingDown, Minus } from 'lucide-react';

export interface SrCategoryTrend {
  current_hit_rate?: number | null;
  previous_hit_rate?: number | null;
  miss_rate?: number | null;
  weighted_miss_rate?: number | null;
  trend?: 'increasing' | 'decreasing' | 'stable';
  rate_history?: number[];
}

export interface SrTaskTypeCategoryStats {
  miss_rate: number;
  sample_count: number;
}

export interface SrTrendData {
  computed_at?: string | null;
  categories?: Record<string, SrCategoryTrend> | null;
  by_task_type?: Record<string, Record<string, SrTaskTypeCategoryStats>> | null;
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
    // Sort by severity-weighted miss rate when available — high-impact misses first
    const ma = a.weighted_miss_rate ?? deriveMissRate(a) ?? 0;
    const mb = b.weighted_miss_rate ?? deriveMissRate(b) ?? 0;
    return mb - ma;
  });
}

function ByTaskTypeSection({ byTaskType }: { byTaskType: Record<string, Record<string, SrTaskTypeCategoryStats>> }) {
  const types = Object.keys(byTaskType).sort();
  if (types.length === 0) return null;
  return (
    <div className="mt-1.5 pt-1.5 border-t border-neutral-800/60">
      <p className="text-[9px] text-neutral-500 uppercase tracking-wide mb-1">By Task Type</p>
      {types.map(ttype => {
        const cats = byTaskType[ttype];
        const sorted = Object.entries(cats).sort(([, a], [, b]) => b.miss_rate - a.miss_rate);
        return (
          <div key={ttype} className="flex items-start gap-1 text-[10px] mb-0.5">
            <span className="text-neutral-400 w-12 shrink-0 font-medium">{ttype}</span>
            <span className="text-neutral-500 flex flex-wrap gap-x-1.5 gap-y-0">
              {sorted.map(([cat, stats]) => (
                <span key={cat} title={`${stats.sample_count} samples`}>
                  <span className="text-neutral-400">{cat}</span>{' '}
                  <span className="font-mono text-orange-400">{Math.round(stats.miss_rate * 100)}%</span>
                </span>
              ))}
            </span>
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
            const wmiss = c.weighted_miss_rate;
            const color = trendColorClass(c.trend);
            return (
              <div key={cat} className="flex items-center gap-2 text-[10px]">
                <span className="text-neutral-300 truncate flex-1" title={cat}>
                  {cat}
                </span>
                <span className={`font-mono font-bold tabular-nums ${color}`}>
                  {miss != null ? `${Math.round(miss * 100)}%` : '–'}
                </span>
                {wmiss != null && (
                  <span className="font-mono tabular-nums text-neutral-500" title="severity-weighted miss rate">
                    {`w:${Math.round(wmiss * 100)}%`}
                  </span>
                )}
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
        {trends?.by_task_type && Object.keys(trends.by_task_type).length > 0 && (
          <ByTaskTypeSection byTaskType={trends.by_task_type} />
        )}
      </div>
    );
  }

  return (
    <div data-testid="sr-trend-breakdown" className="flex flex-col gap-0.5 ml-1">
      {entries.map(([cat, c]) => {
        const miss = deriveMissRate(c);
        const wmiss = c.weighted_miss_rate;
        const color = trendColorClass(c.trend);
        return (
          <div key={cat} className="flex items-center gap-1.5 text-[10px]">
            <span className="text-neutral-500">·</span>
            <span className="text-neutral-300">{cat}</span>
            <span className={`font-mono ${color}`}>
              {miss != null ? `${Math.round(miss * 100)}%` : '–'} miss
            </span>
            {wmiss != null && (
              <span className="font-mono text-neutral-500" title="severity-weighted miss rate">
                {`w:${Math.round(wmiss * 100)}%`}
              </span>
            )}
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

const WINDOW_OPTIONS: { label: string; days: number }[] = [
  { label: '7d', days: 7 },
  { label: '14d', days: 14 },
  { label: '30d', days: 30 },
];

interface SrMissedRetrospectiveProps {
  tasks: SrMissedTask[];
  onTaskClick?: (taskId: string) => void;
}

export function SrMissedRetrospective({ tasks, onTaskClick }: SrMissedRetrospectiveProps) {
  const [categoryFilter, setCategoryFilter] = useState('');
  const [typeFilter, setTypeFilter] = useState('');
  const [windowDays, setWindowDays] = useState(14);
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

  return (
    <div data-testid="sr-missed-retrospective" className="flex flex-col gap-2">
      <div className="flex flex-wrap items-center gap-2 text-[10px]">
        <span className="text-neutral-500 uppercase tracking-wide">Time</span>
        {WINDOW_OPTIONS.map(w => (
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
            <div
              key={t.task_id}
              className={`bg-neutral-900/50 border border-neutral-800/50 rounded px-2 py-1.5 text-[10px] ${onTaskClick ? 'cursor-pointer hover:border-neutral-600' : ''}`}
              onClick={() => onTaskClick?.(t.task_id)}
            >
              <div className="flex items-center gap-2 mb-0.5">
                <span className="text-neutral-200 truncate flex-1 font-medium" title={t.title}>{t.title}</span>
                {t.task_type && <span className="text-neutral-500 shrink-0">{t.task_type}</span>}
                {t.scope && <span className="text-neutral-600 shrink-0">{t.scope}</span>}
                {t.created_at && (
                  <span className="text-neutral-600 shrink-0" title={t.created_at}>
                    {new Date(t.created_at).toLocaleDateString()}
                  </span>
                )}
              </div>
              <div className="flex flex-wrap gap-x-3 gap-y-0.5">
                {t.missed_categories.length > 0 && (
                  <span className="text-red-400">
                    missed: {t.missed_categories.join(', ')}
                  </span>
                )}
                {t.caught_categories.length > 0 && (
                  <span className="text-emerald-600">
                    caught: {t.caught_categories.join(', ')}
                  </span>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
