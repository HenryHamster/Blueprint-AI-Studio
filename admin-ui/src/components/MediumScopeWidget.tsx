import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip } from 'recharts';

export interface WindowStats {
  merge_rate: number | null;
  created: number;
  merged: number;
  window_start: string | null;
  window_end: string | null;
}

export interface WeeklySeries {
  week: string;
  created: number;
  merged: number;
  merge_rate: number | null;
  direction_merges: number;
  merge_rate_delta: number | null;
}

export interface DailySeries {
  day: string;
  created: number;
  merged: number;
  merge_rate: number | null;
  direction_merges: number;
  merge_rate_delta: number | null;
}

export interface CorrelatedChange {
  title: string;
  week: string;
  merge_rate_delta: number | null;
}

export interface MediumScopeMeasurement {
  medium_scope_conversion_rate: number | null;
  total_created: number;
  total_merged: number;
  investment_start: string | null;
  pre_investment: WindowStats | null;
  post_investment: WindowStats | null;
  recent_30d: WindowStats | null;
  recent_7d: WindowStats | null;
  recent_3d: WindowStats | null;
  recent_24h: WindowStats | null;
  weekly_series: WeeklySeries[];
  daily_series: DailySeries[];
  correlated_changes: CorrelatedChange[];
}

const TOOLTIP_STYLE = {
  contentStyle: { background: '#171717', border: '1px solid #404040', borderRadius: 4 },
  itemStyle: { color: '#f5f5f5' },
  labelStyle: { color: '#a3a3a3' },
};

function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`animate-pulse bg-workbench-panel/60 rounded ${className}`} />;
}

export function MediumScopeWidget({ data, loading }: { data: MediumScopeMeasurement | null; loading: boolean }) {
  const rate = data?.medium_scope_conversion_rate;
  const rateColor = rate == null ? 'text-neutral-500' : rate >= 0.6 ? 'text-emerald-400' : rate >= 0.3 ? 'text-amber-400' : 'text-red-400';

  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Medium Scope Conversion Rate</h2>
      {loading ? (
        <Skeleton className="h-40 w-full" />
      ) : data == null || rate == null ? (
        <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-4 text-neutral-500 text-xs">
          No medium-scope task data yet
        </div>
      ) : (
        <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-4 space-y-4">
          {/* Headline rate */}
          <div className="flex flex-wrap items-center gap-4 min-w-0">
            <div className="min-w-0">
              <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-0.5">Overall Rate</p>
              <p className={`text-3xl font-bold font-mono ${rateColor}`}>{Math.round(rate * 100)}%</p>
              <p className="text-ui-sm text-neutral-500 mt-0.5">{data.total_merged} merged / {data.total_created} created</p>
            </div>
            {/* Pre/Post comparison */}
            {(data.pre_investment || data.post_investment) && (() => {
              const preRate = data.pre_investment?.merge_rate;
              const postRate = data.post_investment?.merge_rate;
              const deltaPp = preRate != null && postRate != null ? Math.round((postRate - preRate) * 100) : null;
              const deltaColor = deltaPp == null
                ? 'text-neutral-400'
                : Math.abs(deltaPp) <= 3 ? 'text-neutral-400' : deltaPp > 0 ? 'text-emerald-400' : 'text-red-400';
              return (
                <div className="flex flex-wrap gap-4 items-center min-w-0">
                  {data.pre_investment && (
                    <div className="text-center">
                      <p className="text-ui-sm text-neutral-500 mb-0.5">Pre-investment</p>
                      <p className="text-lg font-bold font-mono text-neutral-300">
                        {preRate != null ? `${Math.round(preRate * 100)}%` : '--'}
                      </p>
                      <p className="text-ui-xs text-neutral-600">{data.pre_investment.merged}/{data.pre_investment.created}</p>
                    </div>
                  )}
                  {data.pre_investment && data.post_investment && (
                    <div className="flex items-center text-neutral-600 text-xs">→</div>
                  )}
                  {data.post_investment && (
                    <div className="text-center">
                      <p className="text-ui-sm text-neutral-500 mb-0.5">Post-investment</p>
                      <p className={`text-lg font-bold font-mono ${
                        postRate != null && preRate != null
                          ? postRate > preRate ? 'text-emerald-400' : 'text-red-400'
                          : 'text-neutral-300'
                      }`}>
                        {postRate != null ? `${Math.round(postRate * 100)}%` : '--'}
                      </p>
                      <p className="text-ui-xs text-neutral-600">{data.post_investment.merged}/{data.post_investment.created}</p>
                    </div>
                  )}
                  {data.recent_24h && (
                    <div className="text-center">
                      <p className="text-ui-sm text-neutral-500 mb-0.5">Recent 24h</p>
                      <p className="text-lg font-bold font-mono text-neutral-300">
                        {data.recent_24h.merge_rate != null ? `${Math.round(data.recent_24h.merge_rate * 100)}%` : '--'}
                      </p>
                      <p className="text-ui-xs text-neutral-600">{data.recent_24h.merged}/{data.recent_24h.created}</p>
                    </div>
                  )}
                  {data.recent_3d && (
                    <div className="text-center">
                      <p className="text-ui-sm text-neutral-500 mb-0.5">Recent 3d</p>
                      <p className="text-lg font-bold font-mono text-neutral-300">
                        {data.recent_3d.merge_rate != null ? `${Math.round(data.recent_3d.merge_rate * 100)}%` : '--'}
                      </p>
                      <p className="text-ui-xs text-neutral-600">{data.recent_3d.merged}/{data.recent_3d.created}</p>
                    </div>
                  )}
                  {data.recent_7d && (
                    <div className="text-center">
                      <p className="text-ui-sm text-neutral-500 mb-0.5">Recent 7d</p>
                      <p className="text-lg font-bold font-mono text-neutral-300">
                        {data.recent_7d.merge_rate != null ? `${Math.round(data.recent_7d.merge_rate * 100)}%` : '--'}
                      </p>
                      <p className="text-ui-xs text-neutral-600">{data.recent_7d.merged}/{data.recent_7d.created}</p>
                    </div>
                  )}
                  {data.recent_30d && (
                    <div className="text-center">
                      <p className="text-ui-sm text-neutral-500 mb-0.5">Recent 30d</p>
                      <p className="text-lg font-bold font-mono text-neutral-300">
                        {data.recent_30d.merge_rate != null ? `${Math.round(data.recent_30d.merge_rate * 100)}%` : '--'}
                      </p>
                      <p className="text-ui-xs text-neutral-600">{data.recent_30d.merged}/{data.recent_30d.created}</p>
                    </div>
                  )}
                  {deltaPp != null && (
                    <div className="text-center">
                      <p className="text-ui-sm text-neutral-500 mb-0.5">Delta</p>
                      <p className={`text-lg font-bold font-mono ${deltaColor}`}>
                        {deltaPp > 0 ? '+' : ''}{deltaPp}pp
                      </p>
                    </div>
                  )}
                </div>
              );
            })()}
          </div>
          {data.pre_investment?.merge_rate != null && data.post_investment?.merge_rate != null && (() => {
            const deltaPp = Math.round((data.post_investment.merge_rate - data.pre_investment.merge_rate) * 100);
            const within = Math.abs(deltaPp) <= 3;
            const color = within ? 'text-neutral-400' : deltaPp > 0 ? 'text-emerald-400' : 'text-red-400';
            const text = within
              ? `No significant change detected (within 3pp, ${deltaPp > 0 ? '+' : ''}${deltaPp}pp)`
              : deltaPp > 0
                ? `Medium-scope conversion rate improved by ${deltaPp} percentage points since investment began`
                : `Medium-scope conversion rate declined by ${Math.abs(deltaPp)} percentage points since investment began`;
            return <p className={`text-ui-md ${color}`}>{text}</p>;
          })()}

          {/* Daily trend chart (swapped from weekly 2026-04-17) — weekly buckets
              are too coarse for this pipeline's velocity; within-week movement
              was invisible. Daily dots preserve the same line shape but show
              the real cadence. */}
          {data.daily_series.length >= 2 && (
            <div className="h-32">
              <ResponsiveContainer width="100%" height="100%" minWidth={1} minHeight={1}>
                <LineChart data={data.daily_series} margin={{ top: 4, right: 4, left: -20, bottom: 0 }}>
                  <CartesianGrid strokeDasharray="3 3" stroke="#262626" />
                  <XAxis
                    dataKey="day"
                    tick={{ fill: '#737373', fontSize: 9 }}
                    tickFormatter={(v: string) => v.slice(5)}
                  />
                  <YAxis
                    tick={{ fill: '#737373', fontSize: 9 }}
                    tickFormatter={(v: number) => `${Math.round(v * 100)}%`}
                    domain={[0, 1]}
                  />
                  <Tooltip
                    {...TOOLTIP_STYLE}
                    formatter={(v: unknown) => [typeof v === 'number' ? `${Math.round(v * 100)}%` : '--', 'Conversion rate']}
                    labelFormatter={(label: unknown) => `Day ${label}`}
                  />
                  <Line
                    type="monotone"
                    dataKey="merge_rate"
                    stroke="#34d399"
                    strokeWidth={1.5}
                    dot={false}
                    isAnimationActive={false}
                    connectNulls
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}

          {/* Correlated changes */}
          {data.correlated_changes.length > 0 && (
            <div>
              <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1.5">Correlated Changes</p>
              <div className="space-y-1">
                {data.correlated_changes.slice(0, 5).map((c, i) => {
                  const delta = c.merge_rate_delta;
                  const deltaColor = delta == null ? 'text-neutral-500' : delta > 0 ? 'text-emerald-400' : 'text-red-400';
                  return (
                    <div key={i} className="flex items-center gap-2 text-ui-sm">
                      <span className="text-neutral-500 font-mono shrink-0">{c.week.slice(5)}</span>
                      <span className="text-neutral-300 truncate flex-1">{c.title}</span>
                      {delta != null && (
                        <span className={`font-mono shrink-0 ${deltaColor}`}>
                          {delta > 0 ? '+' : ''}{Math.round(delta * 100)}pp
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
