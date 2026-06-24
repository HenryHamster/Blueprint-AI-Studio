import { useState } from 'react';
import { AlertTriangle } from 'lucide-react';
import { apiFetch } from '../api';
import { timeAgo } from '../utils/time';
import type { DigestData } from './DashboardHome';

export function AnomalyThrottleBanner({ digest, onRestored }: { digest: DigestData | null; onRestored?: () => void }) {
  const [restoring, setRestoring] = useState(false);
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const adj = digest?.anomaly_adjustment;
  if (!adj || !adj.active) return null;
  const triggers = adj.trigger_anomalies ?? [];
  const pre = adj.pre_adjustment_max_vms;
  const post = adj.adjusted_max_vms;
  const gatePre = adj.pre_adjustment_failure_gate;
  const gateNow = adj.failure_gate_threshold;
  const handleRestore = async () => {
    setRestoring(true);
    setRestoreError(null);
    try {
      const res = await apiFetch('/api/admin/anomaly-throttle/restore', { method: 'POST' });
      if (!res.ok) {
        setRestoreError(`Restore failed (${res.status})`);
        return;
      }
      onRestored?.();
    } catch {
      setRestoreError('Restore failed');
    } finally {
      setRestoring(false);
    }
  };
  return (
    <section>
      <div className="bg-amber-500/10 border border-amber-500/30 rounded-lg p-3 flex items-start gap-2">
        <AlertTriangle className="w-4 h-4 text-amber-400 shrink-0 mt-0.5" />
        <div className="flex-1 min-w-0">
          <div className="text-xs font-semibold text-amber-300">
            Pipeline auto-throttled
            {adj.adjusted_at && (
              <span className="ml-2 text-ui-sm font-normal text-amber-400/70">
                {timeAgo(adj.adjusted_at)}
              </span>
            )}
          </div>
          <div className="text-ui-md text-amber-200/80 mt-1 space-y-0.5">
            {triggers.length > 0 && (
              <div>
                <span className="text-amber-400/60">Triggers: </span>
                <span className="font-mono">{triggers.join(', ')}</span>
              </div>
            )}
            {pre != null && post != null && (
              <div>
                <span className="text-amber-400/60">max_vms: </span>
                <span className="font-mono">{pre} → {post}</span>
              </div>
            )}
            {gatePre != null && gateNow != null && gatePre !== gateNow && (
              <div>
                <span className="text-amber-400/60">failure_gate: </span>
                <span className="font-mono">{gatePre} → {gateNow}</span>
              </div>
            )}
            {(adj.forced_restoration_count ?? 0) > 0 && (
              <div className="text-amber-400/60">
                Forced restorations: {adj.forced_restoration_count}
                {adj.forced_restored_at && ` (last ${timeAgo(adj.forced_restored_at)})`}
              </div>
            )}
          </div>
          <div className="mt-2 flex items-center gap-2 flex-wrap">
            {/* eslint-disable-next-line viewport/min-touch-target -- dense banner control, mirrors Reset Circuit Breaker affordance */}
            <button
              type="button"
              onClick={handleRestore}
              disabled={restoring}
              className="text-ui-md font-medium px-2 py-1 rounded border border-amber-500/40 text-amber-200 hover:bg-amber-500/20 disabled:opacity-50 disabled:cursor-not-allowed"
            >
              {restoring ? 'Restoring…' : 'Clear throttle / Restore now'}
            </button>
            {restoreError && (
              <span className="text-ui-md text-red-400">{restoreError}</span>
            )}
          </div>
          <div className="text-ui-sm text-amber-400/60 mt-1">
            Otherwise auto-restores once the triggering anomalies clear — this is an early restore, not the only path.
          </div>
        </div>
      </div>
    </section>
  );
}
