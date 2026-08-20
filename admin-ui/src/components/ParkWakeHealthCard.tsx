import { AlertTriangle, CircleParking, RadioTower } from 'lucide-react';
import { formatDurationSec } from '../utils/time';
import type { ParkWakeHealth } from '../types/digest';

function ageLabel(seconds: number | null): string {
  return seconds == null ? '—' : formatDurationSec(seconds);
}

export function ParkWakeHealthCard({ data, stale }: { data?: ParkWakeHealth | null; stale?: boolean }) {
  if (!data && stale) {
    return (
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3 opacity-50">
        <div className="flex items-center gap-2 mb-2">
          <CircleParking className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
          <h3 className="text-ui-md font-bold text-neutral-500 uppercase tracking-widest">Park/Wake Health</h3>
        </div>
        <div className="text-ui-sm text-neutral-600">Data unavailable</div>
      </div>
    );
  }
  if (!data) return null;

  const stalled = data.delivery_stalled;
  const statusText = stalled
    ? `Delivery stalled past ${formatDurationSec(data.delivery_stall_threshold_seconds)}`
    : 'Wake delivery flowing';

  return (
    <div className={`bg-workbench-base/50 border rounded-lg p-3 ${stalled ? 'border-red-500/30' : 'border-neutral-800/50'}`}>
      <div className="flex items-center gap-2 mb-2">
        {stalled ? (
          <AlertTriangle className="w-3.5 h-3.5 text-red-400 shrink-0" />
        ) : (
          <CircleParking className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
        )}
        <h3 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest">Park/Wake Health</h3>
      </div>

      <div className="flex items-baseline gap-2">
        <span className={`text-2xl font-bold ${stalled ? 'text-red-400' : 'text-emerald-400'}`}>
          {data.parked_task_count}
        </span>
        <span className="text-ui-sm text-neutral-500">currently parked</span>
      </div>

      <div className="mt-2 grid grid-cols-2 gap-2 text-ui-sm">
        <div className="rounded border border-neutral-800/60 bg-workbench-shell/40 px-2 py-1.5">
          <div className="text-neutral-500">Oldest parked</div>
          <div className="font-mono text-neutral-200">{ageLabel(data.oldest_parked_age_seconds)}</div>
        </div>
        <div className="rounded border border-neutral-800/60 bg-workbench-shell/40 px-2 py-1.5">
          <div className="text-neutral-500">Oldest fired</div>
          <div className="font-mono text-neutral-200">{ageLabel(data.oldest_fired_age_seconds)}</div>
        </div>
      </div>

      <div className="mt-2 flex items-center justify-between text-ui-md">
        <span className="flex items-center gap-1 text-neutral-400">
          <RadioTower className="w-3 h-3" />
          Fired, undelivered
        </span>
        <span className={`font-bold ${data.fired_undelivered_count > 0 ? 'text-amber-400' : 'text-neutral-500'}`}>
          {data.fired_undelivered_count}
        </span>
      </div>

      <div className={`mt-2 text-ui-sm ${stalled ? 'text-red-300' : 'text-emerald-400'}`}>
        {stalled ? 'Warning: ' : ''}
        {statusText}
      </div>
    </div>
  );
}
