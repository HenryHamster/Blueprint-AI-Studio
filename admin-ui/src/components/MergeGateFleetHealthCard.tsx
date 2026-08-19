import { ShieldCheck, ShieldAlert } from 'lucide-react';
import { humanizeUnverifiedReason } from '../utils/unverifiedReasonLabels';
import type { MergeGateFleetHealth } from '../types/digest';

/**
 * Fleet-wide no-CI merge-gate verification health. Read-only observability: it
 * answers what fraction of merges across all no-CI projects actually ran real
 * tests vs. fell open to an UNVERIFIED merge, which fall-open reason dominates,
 * and how many projects have a degraded gate. Renders nothing when there is no
 * gate activity (so the dashboard stays clean for fleets without no-CI repos).
 */
export function MergeGateFleetHealthCard({ data, stale }: { data?: MergeGateFleetHealth | null; stale?: boolean }) {
  if (!data && stale) {
    return (
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3 opacity-50">
        <div className="flex items-center gap-2 mb-2">
          <ShieldCheck className="w-3.5 h-3.5 text-neutral-500 shrink-0" />
          <h3 className="text-ui-md font-bold text-neutral-500 uppercase tracking-widest">Merge Gate Health</h3>
        </div>
        <div className="text-ui-sm text-neutral-600">Data unavailable</div>
      </div>
    );
  }
  if (!data || data.project_count <= 0) {
    return null;
  }

  const { verified_count, fell_open_count, verification_rate, most_common_reason, degraded_project_count, project_count } = data;
  const ratePct = verification_rate != null ? Math.round(verification_rate * 100) : null;
  const degraded = degraded_project_count > 0;
  const rateColor =
    ratePct == null ? 'text-neutral-400' : ratePct >= 90 ? 'text-emerald-400' : ratePct >= 70 ? 'text-amber-400' : 'text-red-400';

  return (
    <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3">
      <div className="flex items-center gap-2 mb-2">
        {degraded ? (
          <ShieldAlert className="w-3.5 h-3.5 text-amber-400 shrink-0" />
        ) : (
          <ShieldCheck className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
        )}
        <h3 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest">Merge Gate Health</h3>
      </div>

      <div className="flex items-baseline gap-2">
        <span className={`text-2xl font-bold ${rateColor}`}>{ratePct != null ? `${ratePct}%` : '—'}</span>
        <span className="text-ui-sm text-neutral-500">verified across {project_count} project{project_count === 1 ? '' : 's'}</span>
      </div>

      <div className="flex items-center gap-3 mt-1.5 text-ui-sm text-neutral-400">
        <span className="text-emerald-400/90">{verified_count} verified</span>
        <span className="text-amber-400/90">{fell_open_count} fell open</span>
        {degraded && (
          <span className="text-red-400/90">{degraded_project_count} degraded</span>
        )}
      </div>

      {most_common_reason && fell_open_count > 0 && (
        <div className="mt-2 text-ui-sm text-neutral-500">
          <span className="text-neutral-400">Top fall-open reason:</span>{' '}
          <span className="text-amber-400/80">{humanizeUnverifiedReason(most_common_reason)}</span>
        </div>
      )}
    </div>
  );
}
