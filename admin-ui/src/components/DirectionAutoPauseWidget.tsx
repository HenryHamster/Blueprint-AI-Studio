import { useState, useCallback } from 'react';
import { TrendingUp, TrendingDown, Minus, PauseCircle } from 'lucide-react';
import { apiFetch } from '../api';
import type { DirectionAutoPauseStats } from '../types/diagnostics';
import { usePoller } from '../hooks/usePoller';

interface DirectionAutoPauseWidgetProps {
  selectedProjectId: string | null;
}

const POLL_INTERVAL_MS = 30_000;
const WINDOW_OPTIONS: { label: string; value: number }[] = [
  { label: '24h', value: 24 },
  { label: '7d', value: 168 },
];

export function DirectionAutoPauseWidget({ selectedProjectId }: DirectionAutoPauseWidgetProps) {
  const [data, setData] = useState<DirectionAutoPauseStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [windowHours, setWindowHours] = useState(24);

  const fetchStats = useCallback(async () => {
    try {
      const params = new URLSearchParams({ window_hours: String(windowHours) });
      if (selectedProjectId) params.set('project_id', selectedProjectId);
      const res = await apiFetch(`/api/admin/direction-auto-pause-stats?${params}`);
      if (res.ok) setData(await res.json());
    } catch {
      // silent — keep last-known state
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId, windowHours]);

  usePoller(fetchStats, { intervalMs: POLL_INTERVAL_MS, deps: [fetchStats] });

  if (loading && !data) {
    return (
      <section>
        <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">
          Direction Auto-Pause
        </h2>
        <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3 h-20 animate-pulse" />
      </section>
    );
  }
  if (!data) return null;

  const hasActivity =
    data.auto_paused_count > 0 ||
    data.auto_retired_count > 0 ||
    data.currently_auto_paused > 0;

  const TrendIcon = data.trend === 'up' ? TrendingUp : data.trend === 'down' ? TrendingDown : Minus;
  // "up" = more auto-pauses than previous window = worse self-healing signal → red
  const trendColor =
    data.trend === 'up' ? 'text-red-400' : data.trend === 'down' ? 'text-emerald-400' : 'text-neutral-500';

  return (
    <section>
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest">
          Direction Auto-Pause
        </h2>
        <select
          value={windowHours}
          onChange={e => setWindowHours(Number(e.target.value))}
          className="text-ui-sm bg-workbench-panel text-neutral-300 border border-neutral-700 rounded px-1.5 py-0.5 workbench-focus"
        >
          {WINDOW_OPTIONS.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
      </div>
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3 space-y-2">
        {!hasActivity ? (
          <div className="flex items-center gap-1.5 text-neutral-500 text-xs">
            <PauseCircle className="w-3.5 h-3.5 shrink-0" />
            <span>No auto-pause activity</span>
          </div>
        ) : (
          <>
            <div className="flex items-baseline gap-3">
              <div>
                <p className="text-ui-sm text-neutral-500 uppercase tracking-wide">Paused</p>
                <div className="flex items-center gap-1">
                  <p className="text-xl font-bold font-mono text-amber-400">{data.auto_paused_count}</p>
                  <TrendIcon className={`w-3.5 h-3.5 ${trendColor}`} />
                </div>
              </div>
              <div>
                <p className="text-ui-sm text-neutral-500 uppercase tracking-wide">Recovered</p>
                <p className="text-xl font-bold font-mono text-emerald-400">{data.auto_recovered_count}</p>
              </div>
              <div>
                <p className="text-ui-sm text-neutral-500 uppercase tracking-wide">Retired</p>
                <p className="text-xl font-bold font-mono text-red-400">{data.auto_retired_count}</p>
              </div>
            </div>

            {data.currently_auto_paused > 0 && (
              <p className="text-ui-sm text-neutral-500">
                <span className="text-amber-400 font-medium">{data.currently_auto_paused}</span>{' '}
                currently paused
              </p>
            )}

            {data.top_reasons.length > 0 && (
              <div>
                <p className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1">Top Reasons</p>
                <ul className="space-y-0.5">
                  {data.top_reasons.map(r => (
                    <li key={r.reason} className="flex items-start gap-2 text-ui-sm">
                      <span className="text-neutral-300 truncate flex-1" title={r.reason}>{r.reason}</span>
                      <span className="text-neutral-500 font-mono shrink-0">{r.count}</span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}
