import { useEffect, useMemo, useState } from 'react';
import type { Task } from './TaskCard';
import { timeAgo, parseTimestamp } from '../utils/time';

interface StatusBarProps {
  apiHealthy: boolean;
  wsConnected: boolean;
  lastFetchTime: number;
  tasks: Task[];
}


export function StatusBar({ apiHealthy, wsConnected, lastFetchTime, tasks }: StatusBarProps) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, []);

  const staleHeartbeatCount = useMemo(() => {
    const fiveMinutesAgo = now - 5 * 60 * 1000;
    return tasks.filter(t =>
      t.status === 'IN_PROGRESS' &&
      t.last_heartbeat &&
      parseTimestamp(t.last_heartbeat).getTime() < fiveMinutesAgo
    ).length;
  }, [tasks, now]);

  return (
    <div className={`h-6 flex items-center justify-between px-3 bg-neutral-900 border-b border-neutral-800 text-xs shrink-0${apiHealthy ? '' : ' bg-red-950/20'}`}>
      <div className="flex items-center gap-1.5" title={apiHealthy ? undefined : 'API connection error — data may be stale'}>
        <span className={`w-1.5 h-1.5 rounded-full ${apiHealthy ? 'bg-emerald-500' : 'bg-red-500 animate-pulse'}`} />
        <span className={apiHealthy ? 'text-neutral-400' : 'text-red-400'}>
          API: {apiHealthy ? 'Connected' : 'Error'}
        </span>
      </div>
      <div className="flex items-center gap-3">
        {staleHeartbeatCount > 0 && (
          <span className="text-amber-400" title={`${staleHeartbeatCount} IN_PROGRESS task${staleHeartbeatCount !== 1 ? 's' : ''} with no heartbeat in the last 5 minutes`}>
            ⚠ {staleHeartbeatCount} stale heartbeat{staleHeartbeatCount !== 1 ? 's' : ''}
          </span>
        )}
        <div className="flex items-center gap-1.5">
          <span className={`w-1.5 h-1.5 rounded-full ${wsConnected ? 'bg-blue-500' : 'bg-neutral-600'}`} />
          <span className={wsConnected ? 'text-blue-400' : 'text-neutral-500'}>
            WS: {wsConnected ? 'Active' : 'Idle'}
          </span>
        </div>
      </div>
      <span className="text-neutral-500">Updated {lastFetchTime ? timeAgo(lastFetchTime) : '—'}</span>
    </div>
  );
}
