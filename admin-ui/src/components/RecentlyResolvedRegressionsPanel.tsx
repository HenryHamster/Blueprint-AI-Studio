import { useState, useEffect, useCallback } from 'react';
import { CheckCircle, Minus, Shield } from 'lucide-react';
import { apiFetch, fetchCrossLayerCompliance, type CrossLayerComplianceResponse } from '../api';
import { timeAgo, formatDurationSec, parseTimestamp } from '../utils/time';
import { LayersAffectedBadges, type LayerSummary } from './LayersAffectedBadges';

interface RecurrenceEntry {
  merged_count: number;
  retripped_count: number;
  recurrence_rate: number | null;
  median_hold_days: number | null;
}

type ComplianceWithRecurrence = CrossLayerComplianceResponse & {
  recurrence_by_bucket?: Record<string, RecurrenceEntry>;
};

const BUCKET_LABEL: Record<string, string> = {
  compliant: 'compliant',
  doc_test_only: 'no-canonical',
  most_recent_layer_only: 'recent-only',
  awaiting_backfill: 'awaiting',
};

interface RecentlyClosedCase {
  id: string;
  regression_class: string;
  fix_pr_number: number | null;
  recovered_at: string | null;
  first_seen_at: string | null;
  signal_first_crossed_at?: string | null;
  investigate_spawned_at?: string | null;
  merged_at?: string | null;
  time_to_recover_sec: number | null;
  recurrence_count?: number;
  layer_summary?: LayerSummary | null;
  hold_days_history?: number[];
  is_fragile?: boolean;
  last_remediation_result?: string | null;
}

interface LayerDefenseEntry {
  layer: string;
  fix_count: number;
  last_fix_at: string | null;
}

interface Props {
  projectId: string;
}

function formatTimeToDetect(firstSeenAt: string | null, signalFirstCrossedAt?: string | null): string {
  if (!firstSeenAt || !signalFirstCrossedAt) return 'unknown';
  const seconds = (parseTimestamp(firstSeenAt).getTime() - parseTimestamp(signalFirstCrossedAt).getTime()) / 1000;
  return formatDurationSec(Math.max(0, seconds));
}

function formatTimeToInvestigate(firstSeenAt: string | null, investigateSpawnedAt?: string | null): string {
  if (!firstSeenAt || !investigateSpawnedAt) return 'unknown';
  const seconds = (parseTimestamp(investigateSpawnedAt).getTime() - parseTimestamp(firstSeenAt).getTime()) / 1000;
  return formatDurationSec(Math.max(0, seconds));
}

function PhaseChip({ label, duration }: { label: string; duration: string }) {
  return (
    <span className="flex items-center gap-1">
      <span className="text-neutral-600">{label}</span>
      <span className={duration === 'unknown' ? 'text-neutral-700 italic' : 'text-neutral-400'}>{duration}</span>
    </span>
  );
}

export function RecentlyResolvedRegressionsPanel({ projectId }: Props) {
  const [cases, setCases] = useState<RecentlyClosedCase[]>([]);
  const [compliance, setCompliance] = useState<ComplianceWithRecurrence | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [layerDensity, setLayerDensity] = useState<LayerDefenseEntry[] | null>(null);

  const fetchCases = useCallback(async () => {
    try {
      const res = await apiFetch(
        `/api/admin/regression-cases/recently-closed?project_id=${encodeURIComponent(projectId)}`
      );
      if (res.ok) {
        const data = await res.json();
        setCases(data.cases ?? []);
        setLoaded(true);
      } else if (res.status !== 200) {
        setLoaded(true);
      }
    } catch { /* silent */ }
  }, [projectId]);

  const fetchCompliance = useCallback(async () => {
    try {
      const data = await fetchCrossLayerCompliance(projectId);
      setCompliance(data);
    } catch { /* silent */ }
  }, [projectId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch sets state after await, intentional
    fetchCases();
    fetchCompliance();
    const id = setInterval(() => {
      fetchCases();
      fetchCompliance();
    }, 60_000);
    return () => clearInterval(id);
  }, [fetchCases, fetchCompliance]);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/admin/regression-cases/layer-defense-density?project_id=${encodeURIComponent(projectId)}&days=30`)
      .then(async (res) => { if (!cancelled && res.ok) { const d = await res.json(); setLayerDensity(d.layers ?? []); } })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [projectId]);

  if (!loaded) return null;

  const renderComplianceMetric = () => {
    if (!compliance) return null;

    if (compliance.total_classified === 0) {
      if (compliance.awaiting_backfill_count > 0) {
        return (
          <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-neutral-500 text-xs mb-3">
            <span className="text-neutral-400">Pending classification</span>
            <span className="text-neutral-600 ml-2">({compliance.awaiting_backfill_count} awaiting backfill)</span>
          </div>
        );
      }
      return (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-neutral-500 text-xs mb-3">
          No cross-layer remediations classified yet
        </div>
      );
    }

    return (
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 mb-3">
        <div className="flex items-center justify-between mb-2">
          <span className="text-[10px] font-semibold text-neutral-400 uppercase tracking-widest">Cross-layer Compliance</span>
          <span className="text-[11px] font-medium text-emerald-400">
            {compliance.compliant} / {compliance.total_classified} ({Math.round((compliance.compliance_rate ?? 0) * 100)}%)
          </span>
        </div>
        <div className="flex gap-4 text-[10px] text-neutral-500">
          {compliance.failure_modes.doc_test_only > 0 && (
            <span className="text-neutral-600">no-canonical: {compliance.failure_modes.doc_test_only}</span>
          )}
          {compliance.failure_modes.most_recent_layer_only > 0 && (
            <span className="text-neutral-600">recent-layer-only: {compliance.failure_modes.most_recent_layer_only}</span>
          )}
        </div>
        {compliance.awaiting_backfill_count > 0 && (
          <div className="mt-1.5 text-[9px] text-neutral-600">
            {compliance.awaiting_backfill_count} awaiting backfill
          </div>
        )}
        {cases.filter(c => c.last_remediation_result === 'recovered_marginal').length > 0 && (
          <div className="mt-1.5 text-[9px] text-amber-500" data-testid="marginal-count">
            {cases.filter(c => c.last_remediation_result === 'recovered_marginal').length} marginal recovery (signal may re-trip)
          </div>
        )}
        {compliance.recurrence_by_bucket && Object.keys(compliance.recurrence_by_bucket).length > 0 && (
          <div className="mt-2 pt-2 border-t border-neutral-800/50" data-testid="recurrence-by-bucket">
            <div className="text-[9px] font-semibold text-neutral-500 uppercase tracking-widest mb-1">
              Recurrence by compliance bucket
            </div>
            <div className="space-y-0.5">
              {Object.entries(compliance.recurrence_by_bucket)
                .filter(([, v]) => v.merged_count > 0)
                .map(([bucket, v]) => (
                  <div key={bucket} className="flex items-center gap-2 text-[10px]">
                    <span className="text-neutral-500 w-24 shrink-0">{BUCKET_LABEL[bucket] ?? bucket}</span>
                    <span className="text-neutral-400">
                      {v.retripped_count}/{v.merged_count}
                    </span>
                    {v.recurrence_rate != null && (
                      <span className={v.recurrence_rate > 0.5 ? 'text-red-400' : v.recurrence_rate > 0.2 ? 'text-amber-400' : 'text-emerald-500'}>
                        {Math.round(v.recurrence_rate * 100)}%
                      </span>
                    )}
                    {v.median_hold_days != null && (
                      <span className="text-neutral-600">· {v.median_hold_days}d median</span>
                    )}
                  </div>
                ))}
            </div>
          </div>
        )}
      </div>
    );
  };

  return (
    <section data-testid="recently-resolved-regressions-panel">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Recently Resolved Regressions
        {cases.length > 0 && (
          <span className="ml-2 bg-emerald-500/20 text-emerald-400 text-[10px] font-semibold px-1.5 py-0.5 rounded-full normal-case tracking-normal">
            {cases.length}
          </span>
        )}
      </h2>
      {renderComplianceMetric()}
      {cases.length === 0 ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 flex items-center gap-2 text-neutral-500 text-xs">
          <Minus className="w-4 h-4 shrink-0" />
          No resolved regression cases
        </div>
      ) : (
        <div className="space-y-2">
          {cases.map(c => (
            <div
              key={c.id}
              className={`rounded-lg px-3 py-2 border border-l-2 ${c.last_remediation_result === 'recovered_marginal' ? 'bg-amber-950/20 border-amber-900/40 border-l-amber-500' : 'bg-emerald-950/20 border-emerald-900/40 border-l-emerald-500'}`}
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <CheckCircle className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                    <span className="text-[10px] text-emerald-300 font-mono break-all">{c.regression_class}</span>
                    {c.fix_pr_number != null && (
                      <span className="text-[10px] text-neutral-400">
                        PR #{c.fix_pr_number}
                      </span>
                    )}
                    {(c.recurrence_count ?? 0) > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-500/10 text-neutral-400 border border-neutral-500/20">
                        recurred {c.recurrence_count}x
                      </span>
                    )}
                    {c.is_fragile && (
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/20"
                        data-testid="fragile-chip"
                        title={c.hold_days_history && c.hold_days_history.length > 0
                          ? `${c.hold_days_history.map(d => `${d}d`).join(', ')} before re-trip`
                          : undefined
                        }
                      >
                        fragile
                      </span>
                    )}
                    {c.last_remediation_result === 'recovered_marginal' && (
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/20 text-amber-300 border border-amber-500/20"
                        data-testid="marginal-chip"
                        title="Marginal improvement — signal may re-trip on noise"
                      >
                        marginal
                      </span>
                    )}
                    <LayersAffectedBadges summary={c.layer_summary} />
                  </div>
                  <div className="mt-1.5 flex flex-wrap items-center gap-3 text-[10px] text-neutral-500">
                    {c.time_to_recover_sec != null && (
                      <span>
                        <span className="text-neutral-400 font-medium">time-to-recover:</span>{' '}
                        {formatDurationSec(c.time_to_recover_sec)}
                      </span>
                    )}
                    <span>
                      <span className="text-neutral-400 font-medium">time-to-detect:</span>{' '}
                      {formatTimeToDetect(c.first_seen_at, c.signal_first_crossed_at)}
                    </span>
                    <span data-testid="time-to-investigate">
                      <span className="text-neutral-400 font-medium">time-to-investigate:</span>{' '}
                      {formatTimeToInvestigate(c.first_seen_at, c.investigate_spawned_at)}
                    </span>
                    {c.recovered_at && (
                      <span>
                        recovered {timeAgo(c.recovered_at)}
                      </span>
                    )}
                  </div>
                  <div className="mt-1 flex flex-wrap items-center gap-1 text-[9px]" data-testid="timeline-breakdown">
                    <PhaseChip label="detect→" duration={formatTimeToDetect(c.first_seen_at, c.signal_first_crossed_at)} />
                    <span className="text-neutral-800">·</span>
                    <PhaseChip label="investigate→" duration={formatTimeToInvestigate(c.first_seen_at, c.investigate_spawned_at)} />
                    <span className="text-neutral-800">·</span>
                    <PhaseChip label="merge→" duration={c.merged_at && c.investigate_spawned_at ? formatDurationSec(Math.max(0, (parseTimestamp(c.merged_at).getTime() - parseTimestamp(c.investigate_spawned_at).getTime()) / 1000)) : 'unknown'} />
                    <span className="text-neutral-800">·</span>
                    <PhaseChip label="recover" duration={c.time_to_recover_sec != null ? formatDurationSec(c.time_to_recover_sec) : 'unknown'} />
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
      {layerDensity !== null && (
        <div className="mt-4" data-testid="layer-defense-density-section">
          <h3 className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest mb-1.5 flex items-center gap-1">
            <Shield className="w-3 h-3" />
            Layer Defense Density (30d)
          </h3>
          <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
            <table className="w-full text-[10px]">
              <tbody className="divide-y divide-neutral-800/30">
                {layerDensity.map((row) => (
                  <tr key={row.layer}>
                    <td className="px-3 py-1 font-mono text-neutral-400 truncate max-w-[120px]">{row.layer}</td>
                    <td className="px-2 py-1 text-right">
                      <span className={row.fix_count === 0 ? 'text-neutral-600' : 'text-white font-medium'}>{row.fix_count}</span>
                    </td>
                    <td className="px-3 py-1 text-right text-neutral-600">
                      {row.last_fix_at ? timeAgo(row.last_fix_at) : '—'}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </section>
  );
}
