import { useState, useEffect, useCallback } from 'react';
import { CheckCircle, Minus } from 'lucide-react';
import { apiFetch, fetchCrossLayerCompliance, type CrossLayerComplianceResponse } from '../api';
import { timeAgo, formatDurationSec, parseTimestamp } from '../utils/time';
import { LayersAffectedBadges, type LayerSummary } from './LayersAffectedBadges';

interface RecentlyClosedCase {
  id: string;
  regression_class: string;
  fix_pr_number: number | null;
  recovered_at: string | null;
  first_seen_at: string | null;
  signal_first_crossed_at?: string | null;
  time_to_recover_sec: number | null;
  recurrence_count?: number;
  layer_summary?: LayerSummary | null;
  hold_days_history?: number[];
  is_fragile?: boolean;
}

interface Props {
  projectId: string;
}

function formatTimeToDetect(firstSeenAt: string | null, signalFirstCrossedAt?: string | null): string {
  if (!firstSeenAt || !signalFirstCrossedAt) return 'unknown';
  const seconds = (parseTimestamp(firstSeenAt).getTime() - parseTimestamp(signalFirstCrossedAt).getTime()) / 1000;
  return formatDurationSec(Math.max(0, seconds));
}

export function RecentlyResolvedRegressionsPanel({ projectId }: Props) {
  const [cases, setCases] = useState<RecentlyClosedCase[]>([]);
  const [compliance, setCompliance] = useState<CrossLayerComplianceResponse | null>(null);
  const [loaded, setLoaded] = useState(false);

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
              className="bg-emerald-950/20 border border-emerald-900/40 border-l-2 border-l-emerald-500 rounded-lg px-3 py-2"
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
                    <LayersAffectedBadges summary={c.layer_summary} />
                  </div>
                  <div className="mt-1.5 flex items-center gap-3 text-[10px] text-neutral-500">
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
                    {c.recovered_at && (
                      <span>
                        recovered {timeAgo(c.recovered_at)}
                      </span>
                    )}
                  </div>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
