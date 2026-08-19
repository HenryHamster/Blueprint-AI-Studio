import { useMemo } from 'react';
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip, Legend } from 'recharts';

const SCOPE_ORDER = ['small', 'medium', 'large'];

export interface ScopeBaselinesSeriesPoint {
  week: string;
  scope: string;
  created: number;
  merged: number;
  merge_rate: number | null;
}

export interface ScopeBaselinesSummary {
  merge_rate: number | null;
  created: number;
  merged: number;
  trend: 'improving' | 'stable' | 'declining' | null;
}

export interface ScopeBaselinesResponse {
  series: ScopeBaselinesSeriesPoint[];
  summary: Record<string, ScopeBaselinesSummary>;
  lookback_days: number;
}

const SCOPE_TOOLTIP_CONTENT_STYLE = { background: '#171717', border: '1px solid #404040', fontSize: 11 };
const scopeTooltipFormatter = (v: unknown): string => (v == null ? '—' : `${v}%`);

const SCOPE_LINE_COLORS: Record<string, string> = {
  small: '#34d399',
  medium: '#fbbf24',
  large: '#f87171',
};

function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`animate-pulse bg-workbench-panel/60 rounded ${className}`} />;
}

export function ScopeTrendsSection({
  data,
  loading,
  open,
  onToggle,
}: {
  data: ScopeBaselinesResponse | null;
  loading: boolean;
  open: boolean;
  onToggle: () => void;
}) {
  const { scopes, chartData, summary } = useMemo(() => {
    const series = data?.series ?? [];
    const summary = data?.summary ?? {};
    const scopes = SCOPE_ORDER.filter(s => summary[s] != null);
    const weeks = Array.from(new Set(series.map(p => p.week))).sort();
    const chartData = weeks.map(week => {
      const row: Record<string, string | number | null> = { week };
      for (const scope of scopes) {
        const pt = series.find(p => p.week === week && p.scope === scope);
        row[scope] = pt?.merge_rate != null ? Math.round(pt.merge_rate * 100) : null;
      }
      return row;
    });
    return { scopes, weeks, chartData, summary };
  }, [data]);

  if (loading && !data) {
    return (
      <section>
        <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Scope Trends</h2>
        <Skeleton className="h-6 w-full" />
      </section>
    );
  }

  return (
    <section>
      <button
        type="button"
        onClick={onToggle}
        className="flex items-center gap-1.5 text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2 hover:text-neutral-200"
        aria-expanded={open}
      >
        <span>{open ? '▾' : '▸'}</span>
        <span>Scope Trends</span>
        <span className="flex gap-1 normal-case tracking-normal font-normal">
          {scopes.map(s => {
            const t = summary[s]?.trend;
            const color = t === 'improving' ? 'text-emerald-400 border-emerald-700 bg-emerald-900/40' : t === 'declining' ? 'text-red-400 border-red-700 bg-red-900/40' : t === 'stable' ? 'text-neutral-400 border-neutral-700 bg-workbench-base/40' : 'text-neutral-600 border-neutral-800 bg-workbench-base/40';
            return (
              <span key={s} className={`text-ui-xs px-1 py-0.5 rounded border ${color}`}>
                {s} {t ?? '—'}
              </span>
            );
          })}
        </span>
      </button>
      {open && (
        <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3">
          {chartData.length === 0 || scopes.length === 0 ? (
            <p className="text-ui-md text-neutral-500">No weekly data in the last {data?.lookback_days ?? 30} days.</p>
          ) : (
            <ResponsiveContainer width="100%" height={200}>
              <LineChart data={chartData} margin={{ top: 8, right: 8, left: -12, bottom: 0 }}>
                <CartesianGrid strokeDasharray="3 3" stroke="#262626" />
                <XAxis dataKey="week" tick={{ fontSize: 10, fill: '#737373' }} />
                <YAxis domain={[0, 100]} tick={{ fontSize: 10, fill: '#737373' }} unit="%" />
                <Tooltip
                  contentStyle={SCOPE_TOOLTIP_CONTENT_STYLE}
                  formatter={scopeTooltipFormatter}
                />
                <Legend wrapperStyle={{ fontSize: 10 }} />
                {scopes.map(s => (
                  <Line
                    key={s}
                    type="monotone"
                    dataKey={s}
                    stroke={SCOPE_LINE_COLORS[s] ?? '#a3a3a3'}
                    strokeWidth={2}
                    dot={{ r: 2 }}
                    connectNulls
                  />
                ))}
              </LineChart>
            </ResponsiveContainer>
          )}
        </div>
      )}
    </section>
  );
}
