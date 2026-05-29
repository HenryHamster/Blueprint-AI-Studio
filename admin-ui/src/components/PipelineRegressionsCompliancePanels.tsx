import { useEffect, useState } from 'react';
import { Shield } from 'lucide-react';
import { apiFetch } from '../api';

type Props = {
  projectId: string;
};

interface EscalationOutcomeBucket {
  merged_count: number;
  retripped_within_30d_count: number;
  durable_resolution_rate: number | null;
  median_hold_days: number | null;
  in_flight_excluded_count: number;
  prevention_rate_30d?: number | null;
  recurrences_observed?: number;
}

interface EscalationOutcomeData {
  by_reason: Record<string, EscalationOutcomeBucket>;
  lookback_days: number;
}

const ESCALATION_BUCKET_LABELS: Record<string, string> = {
  escalated_cross_layer: 'Cross-layer',
  compliance_followup: '↳ Compliance re-fix',
  escalated_layer_concentration: 'Layer conc.',
  escalated_cross_case_fingerprint: 'Cross-case fp',
  escalated_recurrence: 'Recurrence',
  all_other: 'All other',
};

export function EscalationOutcomePanel({ projectId }: Props) {
  const [data, setData] = useState<EscalationOutcomeData | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/admin/regression-dispatcher/escalation-outcomes?project_id=${encodeURIComponent(projectId)}`)
      .then(async (res) => { if (!cancelled && res.ok) setData(await res.json()); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoaded(true); });
    return () => { cancelled = true; };
  }, [projectId]);

  if (!loaded || !data) return null;

  const buckets = Object.entries(data.by_reason);
  if (!buckets.some(([, b]) => b.merged_count > 0)) return null;

  return (
    <section data-testid="escalation-outcome-panel" className="mb-4">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Meta-Escalation Outcomes ({data.lookback_days}d)
      </h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        {/* Mobile: stacked card layout */}
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {buckets.map(([reason, b]) => (
            <div key={reason} className={`px-3 py-2 text-[10px] ${reason === 'compliance_followup' ? 'bg-neutral-800/20' : ''} ${reason === 'all_other' ? 'border-t border-neutral-700/50' : ''}`}>
              <div className={`font-medium mb-1.5 ${reason === 'compliance_followup' ? 'text-neutral-400 italic' : 'text-neutral-300'}`}>
                {ESCALATION_BUCKET_LABELS[reason] ?? reason}
              </div>
              <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-500">
                <span>Merged: <span className="text-white">{b.merged_count || '—'}</span></span>
                <span>Retripped: {b.retripped_within_30d_count > 0 ? <span className="text-amber-400">{b.retripped_within_30d_count}</span> : <span className="text-neutral-600">—</span>}</span>
                <span>Durable: {b.durable_resolution_rate !== null
                  ? <span className={b.durable_resolution_rate >= 0.7 ? 'text-emerald-400' : 'text-amber-400'}>{Math.round(b.durable_resolution_rate * 100)}%</span>
                  : <span className="text-neutral-600">—</span>}
                </span>
                <span>Prevented: {b.prevention_rate_30d != null
                  ? <span className={b.prevention_rate_30d < 0.6 ? 'text-red-400' : 'text-emerald-400'}>
                      {Math.round(b.prevention_rate_30d * 100)}%
                      {(b.recurrences_observed ?? 0) > 0 && <span className="text-neutral-500 ml-1">&middot; {b.recurrences_observed}r</span>}
                    </span>
                  : <span className="text-neutral-600">—</span>}
                </span>
                <span>Hold: <span className="text-neutral-400">{b.median_hold_days !== null ? `${b.median_hold_days.toFixed(1)}d` : '—'}</span></span>
              </div>
            </div>
          ))}
        </div>
        {/* Desktop: table layout */}
        <table className="hidden sm:table w-full text-[10px]">
          <thead>
            <tr className="border-b border-neutral-800/50 text-neutral-500">
              <th className="text-left px-3 py-1.5 font-medium">Reason</th>
              <th className="text-right px-2 py-1.5 font-medium">Merged</th>
              <th className="text-right px-2 py-1.5 font-medium">Retripped</th>
              <th className="text-right px-2 py-1.5 font-medium">Durable</th>
              <th className="text-right px-2 py-1.5 font-medium hidden sm:table-cell">Prevented (30d)</th>
              <th className="text-right px-3 py-1.5 font-medium">Med. hold</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {buckets.map(([reason, b]) => (
              <tr key={reason} className={`${reason === 'all_other' ? 'border-t border-neutral-700/50' : ''} ${reason === 'compliance_followup' ? 'bg-neutral-800/20' : ''}`}>
                <td className={`py-1.5 truncate max-w-[120px] ${reason === 'compliance_followup' ? 'px-3 text-neutral-400 italic' : 'px-3 text-neutral-300'}`}>
                  {ESCALATION_BUCKET_LABELS[reason] ?? reason}
                </td>
                <td className="px-2 py-1.5 text-right text-white">{b.merged_count || <span className="text-neutral-600">—</span>}</td>
                <td className="px-2 py-1.5 text-right">
                  {b.retripped_within_30d_count > 0 ? <span className="text-amber-400">{b.retripped_within_30d_count}</span> : <span className="text-neutral-600">—</span>}
                </td>
                <td className="px-2 py-1.5 text-right">
                  {b.durable_resolution_rate !== null
                    ? <span className={b.durable_resolution_rate >= 0.7 ? 'text-emerald-400' : 'text-amber-400'}>{Math.round(b.durable_resolution_rate * 100)}%</span>
                    : <span className="text-neutral-600">—</span>}
                </td>
                <td className="px-2 py-1.5 text-right hidden sm:table-cell">
                  {b.prevention_rate_30d != null
                    ? (
                      <span className={b.prevention_rate_30d < 0.6 ? 'text-red-400' : 'text-emerald-400'}>
                        {Math.round(b.prevention_rate_30d * 100)}%
                        {(b.recurrences_observed ?? 0) > 0 && (
                          <span className="text-neutral-500 ml-1">&middot; {b.recurrences_observed}r</span>
                        )}
                      </span>
                    )
                    : <span className="text-neutral-600">—</span>}
                </td>
                <td className="px-3 py-1.5 text-right text-neutral-400">
                  {b.median_hold_days !== null ? `${b.median_hold_days.toFixed(1)}d` : <span className="text-neutral-600">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

interface ComplianceTrendWindow {
  window_start: string;
  window_end: string;
  first_attempt_compliant: number;
  followup_rescued: number;
  awaiting_backfill: number;
  rate: number | null;
}

interface FirstAttemptComplianceTrendData {
  windows: ComplianceTrendWindow[];
  num_windows: number;
  window_days: number;
  generated_at: string;
}

export function FirstAttemptComplianceTrendPanel({ projectId }: Props) {
  const [data, setData] = useState<FirstAttemptComplianceTrendData | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/admin/regression-cases/first-attempt-compliance-trend?project_id=${encodeURIComponent(projectId)}`)
      .then(async (res) => { if (!cancelled && res.ok) setData(await res.json()); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoaded(true); });
    return () => { cancelled = true; };
  }, [projectId]);

  if (!loaded || !data || !Array.isArray(data.windows)) return null;
  if (!data.windows.some(w => w.first_attempt_compliant + w.followup_rescued + w.awaiting_backfill > 0)) return null;

  return (
    <section data-testid="first-attempt-compliance-trend-panel" className="mb-4">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2 flex items-center gap-1.5">
        <Shield className="w-3 h-3" />
        Cross-Layer First-Attempt Rate ({data.window_days}d windows)
      </h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        {/* Mobile: stacked card layout */}
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {data.windows.map((w) => {
            const label = new Date(w.window_end).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
            const rateColor = w.rate === null ? 'text-neutral-600' : w.rate >= 0.8 ? 'text-emerald-400' : w.rate >= 0.5 ? 'text-amber-400' : 'text-red-400';
            const anyData = w.first_attempt_compliant + w.followup_rescued + w.awaiting_backfill > 0;
            return (
              <div key={w.window_end} className={`px-3 py-2 text-[10px] ${!anyData ? 'opacity-30' : ''}`}>
                <div className="font-medium text-neutral-400 mb-1.5">{label}</div>
                <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-500">
                  <span>1st-ok: <span className="text-emerald-400">{w.first_attempt_compliant > 0 ? w.first_attempt_compliant : '—'}</span></span>
                  <span>rescued: <span className="text-amber-400">{w.followup_rescued > 0 ? w.followup_rescued : '—'}</span></span>
                  <span>pending: <span className="text-neutral-500">{w.awaiting_backfill > 0 ? w.awaiting_backfill : '—'}</span></span>
                  <span>Rate: <span className={`font-medium ${rateColor}`}>{w.rate !== null ? `${Math.round(w.rate * 100)}%` : '—'}</span></span>
                </div>
              </div>
            );
          })}
        </div>
        {/* Desktop: table layout */}
        <table className="hidden sm:table w-full text-[10px]">
          <thead>
            <tr className="border-b border-neutral-800/50 text-neutral-500">
              <th className="text-left px-3 py-1.5 font-medium">Window end</th>
              <th className="text-right px-2 py-1.5 font-medium">1st-ok</th>
              <th className="text-right px-2 py-1.5 font-medium">rescued</th>
              <th className="text-right px-2 py-1.5 font-medium">pending</th>
              <th className="text-right px-3 py-1.5 font-medium">Rate</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {data.windows.map((w) => {
              const label = new Date(w.window_end).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
              const rateColor = w.rate === null ? 'text-neutral-600' : w.rate >= 0.8 ? 'text-emerald-400' : w.rate >= 0.5 ? 'text-amber-400' : 'text-red-400';
              const anyData = w.first_attempt_compliant + w.followup_rescued + w.awaiting_backfill > 0;
              return (
                <tr key={w.window_end} className={anyData ? '' : 'opacity-30'}>
                  <td className="px-3 py-1.5 text-neutral-400">{label}</td>
                  <td className="px-2 py-1.5 text-right text-emerald-400">{w.first_attempt_compliant > 0 ? w.first_attempt_compliant : <span className="text-neutral-700">—</span>}</td>
                  <td className="px-2 py-1.5 text-right text-amber-400">{w.followup_rescued > 0 ? w.followup_rescued : <span className="text-neutral-700">—</span>}</td>
                  <td className="px-2 py-1.5 text-right text-neutral-500">{w.awaiting_backfill > 0 ? w.awaiting_backfill : <span className="text-neutral-700">—</span>}</td>
                  <td className={`px-3 py-1.5 text-right font-medium ${rateColor}`}>
                    {w.rate !== null ? `${Math.round(w.rate * 100)}%` : '—'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
