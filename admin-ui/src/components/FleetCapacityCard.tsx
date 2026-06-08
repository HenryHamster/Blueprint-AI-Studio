import { AlertTriangle } from 'lucide-react';
import { Skeleton } from './DashboardMetricCards';
import type { FleetUtilizationResponse } from '../api';

export type { FleetUtilizationResponse };

const _SAT_STYLE: Record<string, { badge: string; label: string }> = {
  healthy:   { badge: 'bg-emerald-500/10 border-emerald-500/30 text-emerald-400', label: 'Healthy' },
  busy:      { badge: 'bg-amber-500/10 border-amber-500/30 text-amber-400',       label: 'Busy' },
  saturated: { badge: 'bg-red-500/10 border-red-500/30 text-red-400',             label: 'Saturated' },
};

function pct(v: number | null | undefined): string {
  if (v == null) return '—';
  return `${Math.round(v * 100)}%`;
}

export function FleetCapacityCard({
  data,
  loading,
}: {
  data: FleetUtilizationResponse | null;
  loading: boolean;
}) {
  const satStyle = data?.saturation ? (_SAT_STYLE[data.saturation] ?? _SAT_STYLE.healthy) : null;
  const cooldownNote = data && data.cooled_accounts > 0
    ? `${data.cooled_accounts} account${data.cooled_accounts > 1 ? 's' : ''} rate-limited`
    : null;

  return (
    <section>
      <div className="flex items-center gap-2 mb-2">
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest">Fleet Capacity</h2>
        {!loading && data && satStyle && (
          <span className={`text-[10px] font-semibold px-2 py-0.5 rounded border ${satStyle.badge}`}>
            {satStyle.label}
          </span>
        )}
        {!loading && data && data.saturation == null && (
          <span className="text-[10px] font-semibold px-2 py-0.5 rounded border bg-neutral-800/50 border-neutral-700/50 text-neutral-500">
            Unknown
          </span>
        )}
      </div>
      {loading ? (
        <div className="space-y-2"><Skeleton className="h-10 w-full" /></div>
      ) : !data ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-4 flex items-center gap-2 text-neutral-400 text-xs">
          <AlertTriangle className="w-4 h-4" />
          Fleet capacity data unavailable
        </div>
      ) : (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 space-y-2">
          <div className="flex flex-wrap gap-2">
            <div className="flex items-center gap-1.5 text-xs">
              <span className="text-neutral-500">Active</span>
              <span className="font-mono font-bold text-neutral-200">
                {data.active_now}
                {data.nominal_capacity != null ? ` / ${data.nominal_capacity}` : ''}
              </span>
            </div>
            {data.usable_capacity != null && data.cooled_accounts > 0 && (
              <div className="flex items-center gap-1.5 text-xs">
                <span className="text-neutral-500">Usable</span>
                <span className="font-mono text-amber-400" title={`${data.cooled_accounts} account(s) cooling down`}>
                  {data.usable_capacity}
                </span>
              </div>
            )}
            <div className="flex items-center gap-1.5 text-xs">
              <span className="text-neutral-500">Avg util</span>
              <span className="font-mono text-neutral-300">{pct(data.util_avg)}</span>
            </div>
            <div className="flex items-center gap-1.5 text-xs">
              <span className="text-neutral-500">Now</span>
              <span className="font-mono text-neutral-300">{pct(data.util_now)}</span>
            </div>
          </div>
          {cooldownNote && (
            <p className="text-[10px] text-amber-400/80">{cooldownNote} — usable capacity is lower than nominal</p>
          )}
          {data.saturation == null && (
            <p className="text-[10px] text-neutral-500">No enabled accounts — capacity unknown</p>
          )}
        </div>
      )}
    </section>
  );
}
