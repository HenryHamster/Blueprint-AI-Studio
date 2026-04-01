import { useEffect, useState } from 'react';

interface StatusBarProps {
  apiHealthy: boolean;
  wsConnected: boolean;
  lastFetchTime: number;
}

function relativeTime(ts: number): string {
  if (!ts) return '—';
  const sec = Math.floor((Date.now() - ts) / 1000);
  if (sec < 10) return 'just now';
  if (sec < 60) return `${sec}s ago`;
  return `${Math.floor(sec / 60)}m ago`;
}

export function StatusBar({ apiHealthy, wsConnected, lastFetchTime }: StatusBarProps) {
  const [, setTick] = useState(0);

  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);

  return (
    <div className="h-6 flex items-center justify-between px-3 bg-neutral-900 border-b border-neutral-800 text-xs shrink-0">
      <div className="flex items-center gap-1.5">
        <span className={`w-1.5 h-1.5 rounded-full ${apiHealthy ? 'bg-emerald-500' : 'bg-red-500'}`} />
        <span className={apiHealthy ? 'text-neutral-400' : 'text-red-400'}>
          API: {apiHealthy ? 'Connected' : 'Error'}
        </span>
      </div>
      <div className="flex items-center gap-1.5">
        <span className={`w-1.5 h-1.5 rounded-full ${wsConnected ? 'bg-blue-500' : 'bg-neutral-600'}`} />
        <span className={wsConnected ? 'text-blue-400' : 'text-neutral-500'}>
          WS: {wsConnected ? 'Active' : 'Idle'}
        </span>
      </div>
      <span className="text-neutral-500">Updated {relativeTime(lastFetchTime)}</span>
    </div>
  );
}
