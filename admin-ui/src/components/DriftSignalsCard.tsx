import { useState, useCallback, useRef, useEffect } from 'react';
import { AlertTriangle, ChevronDown, ChevronRight } from 'lucide-react';
import { apiFetch } from '../api';
import { usePoller } from '../hooks/usePoller';
import { timeAgo } from '../utils/time';

interface DriftHistoryPoint {
  snapshot_at: string;
  claim_shipped_consistency: number;
  over_claim_flag: boolean;
  stalled_despite_churn_flag: boolean;
}

interface DriftSignal {
  over_claim_flag: boolean;
  stalled_despite_churn_flag: boolean;
  claimed_changes_count: number;
  actually_merged_prs: number;
  claim_shipped_consistency?: number;
}

interface DriftDirection extends DriftSignal {
  direction_id: string;
  direction_text: string;
  drift_history?: DriftHistoryPoint[];
}

function DriftSparkline({ points }: { points: DriftHistoryPoint[] }) {
  const values = points.map(p => p.claim_shipped_consistency);
  if (values.length < 2) return null;
  const min = Math.min(...values), max = Math.max(...values);
  const range = max - min || 1;
  const W = 48, H = 14, P = 1;
  const pts = values
    .map((v, i) =>
      `${(P + (i / (values.length - 1)) * (W - P * 2)).toFixed(1)},${(P + (1 - (v - min) / range) * (H - P * 2)).toFixed(1)}`
    )
    .join(' ');
  const latest = values[values.length - 1];
  const stroke = latest < 0.4 ? '#f87171' : latest < 0.7 ? '#fbbf24' : '#34d399';
  return (
    <svg width={W} height={H} viewBox={`0 0 ${W} ${H}`} role="img" aria-label="consistency trend" className="inline-block align-middle shrink-0">
      <polyline points={pts} fill="none" stroke={stroke} strokeWidth="1.5" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

export interface DriftSnapshot {
  flagged_directions: DriftDirection[];
  flagged_count: number;
  total_directions: number;
  any_system_flagged?: boolean;
  by_system?: Record<string, DriftSignal>;
  generated_at: string;
  window_days: number;
}

function scoreBadgeClass(score: number): string {
  if (score >= 0.6) return 'bg-emerald-500/10 text-emerald-400 border-emerald-500/20';
  if (score >= 0.3) return 'bg-amber-500/10 text-amber-500 border-amber-500/20';
  return 'bg-red-500/10 text-red-400 border-red-500/20';
}

export function DriftSignalsCard({ projectId }: { projectId: string }) {
  const [snapshot, setSnapshot] = useState<DriftSnapshot | null>(null);
  const [pollFailed, setPollFailed] = useState(false);
  const [lastSuccessAt, setLastSuccessAt] = useState<number | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [systemsExpanded, setSystemsExpanded] = useState(false);
  // Project-identity guard: discard stale async responses if projectId
  // changed while the fetch was in-flight.
  const currentProjectRef = useRef(projectId);
  useEffect(() => {
    currentProjectRef.current = projectId;
  }, [projectId]);

  const fetchSnapshot = useCallback(async (signal?: AbortSignal) => {
    try {
      const res = await apiFetch(`/api/admin/drift-snapshot?project_id=${encodeURIComponent(projectId)}`);
      if (signal?.aborted) return;
      if (projectId !== currentProjectRef.current) return;
      if (res.ok) {
        setSnapshot(await res.json());
        setLastSuccessAt(Date.now());
        setPollFailed(false);
      } else {
        setPollFailed(true);
      }
    } catch {
      setPollFailed(true);
    }
  }, [projectId]);

  usePoller(fetchSnapshot, { intervalMs: 60_000, deps: [fetchSnapshot] });

  if (!snapshot || snapshot.flagged_count === 0) return null;

  const { flagged_directions, flagged_count } = snapshot;
  const staleLabel = pollFailed && lastSuccessAt != null ? `as of ${timeAgo(lastSuccessAt)}` : null;
  const flaggedSystems = Object.entries(snapshot.by_system ?? {}).filter(
    ([, signal]) => signal.over_claim_flag || signal.stalled_despite_churn_flag,
  );
  const showSystemSummary = snapshot.any_system_flagged === true && flaggedSystems.length > 0;

  return (
    <section>
      <button
        type="button"
        className="w-full text-left"
        onClick={() => setExpanded(v => !v)}
        aria-expanded={expanded}
        title={staleLabel ? `Last drift poll failed; showing snapshot ${staleLabel}.` : undefined}
      >
        <div className={`flex items-center gap-2 bg-amber-900/20 border rounded-lg px-3 py-2 ${pollFailed ? 'border-amber-400/50 opacity-75' : 'border-amber-500/30'}`}>
          <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
          <span className="text-xs font-semibold text-amber-300">
            Drift Signals — {flagged_count} direction{flagged_count !== 1 ? 's' : ''} flagged
          </span>
          {staleLabel && (
            <span className="text-ui-sm text-amber-300">stale · {staleLabel}</span>
          )}
          {expanded
            ? <ChevronDown className="w-3.5 h-3.5 text-amber-400 ml-auto" />
            : <ChevronRight className="w-3.5 h-3.5 text-amber-400 ml-auto" />}
        </div>
      </button>
      {expanded && (
        <div className="mt-1 space-y-1">
          {flagged_directions.map(d => (
            <div
              key={d.direction_id}
              className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2"
            >
              <p className="text-xs text-neutral-200 truncate">{d.direction_text || d.direction_id}</p>
              <div className="flex gap-1.5 mt-1 flex-wrap items-center">
                {d.over_claim_flag && (
                  <span className="text-ui-xs px-1.5 py-0.5 rounded font-medium bg-amber-500/20 text-amber-400 border border-amber-500/30">
                    over-claim
                  </span>
                )}
                {d.stalled_despite_churn_flag && (
                  <span className="text-ui-xs px-1.5 py-0.5 rounded font-medium bg-red-500/20 text-red-400 border border-red-500/30">
                    stalled
                  </span>
                )}
                {typeof d.claim_shipped_consistency === 'number' && (
                  <span
                    className={`text-ui-xs px-1.5 py-0.5 rounded-full border ${scoreBadgeClass(d.claim_shipped_consistency)}`}
                    title="Claim-vs-shipped consistency"
                  >
                    {Math.round(d.claim_shipped_consistency * 100)}% consistent
                  </span>
                )}
                {d.drift_history && d.drift_history.length >= 2 && (
                  <DriftSparkline points={d.drift_history} />
                )}
                <span className="text-ui-xs text-neutral-500 ml-auto">
                  {d.actually_merged_prs}/{d.claimed_changes_count} merged
                </span>
              </div>
            </div>
          ))}
          {showSystemSummary && (
            <div className="bg-neutral-900/40 border border-neutral-800/50 rounded-lg">
              <button
                type="button"
                className="w-full flex items-center gap-2 px-3 py-2 text-left"
                onClick={() => setSystemsExpanded(v => !v)}
                aria-expanded={systemsExpanded}
              >
                <span className="text-ui-md font-medium text-neutral-300">System drift summary</span>
                {systemsExpanded
                  ? <ChevronDown className="w-3.5 h-3.5 text-neutral-500 ml-auto" />
                  : <ChevronRight className="w-3.5 h-3.5 text-neutral-500 ml-auto" />}
              </button>
              {systemsExpanded && (
                <div className="px-3 pb-2 space-y-1">
                  {flaggedSystems.map(([systemId, signal]) => (
                    <div key={systemId} className="flex gap-1.5 flex-wrap items-center text-ui-sm">
                      <span className="text-neutral-300 font-mono">{systemId}</span>
                      {signal.over_claim_flag && <span className="px-1.5 py-0.5 rounded font-medium bg-amber-500/20 text-amber-400 border border-amber-500/30">over-claim</span>}
                      {signal.stalled_despite_churn_flag && <span className="px-1.5 py-0.5 rounded font-medium bg-red-500/20 text-red-400 border border-red-500/30">stalled</span>}
                      {typeof signal.claim_shipped_consistency === 'number' && (
                        <span className={`px-1.5 py-0.5 rounded-full border ${scoreBadgeClass(signal.claim_shipped_consistency)}`}>
                          {Math.round(signal.claim_shipped_consistency * 100)}% consistent
                        </span>
                      )}
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
