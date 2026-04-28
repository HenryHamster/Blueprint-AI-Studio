import { useState, useEffect, useCallback } from 'react';
import { AlertTriangle, ChevronDown, ChevronRight } from 'lucide-react';
import { apiFetch } from '../api';

interface DriftDirection {
  direction_id: string;
  direction_text: string;
  over_claim_flag: boolean;
  stalled_despite_churn_flag: boolean;
  claimed_changes_count: number;
  actually_merged_prs: number;
  claim_shipped_consistency: number;
}

export interface DriftSnapshot {
  flagged_directions: DriftDirection[];
  flagged_count: number;
  total_directions: number;
  any_system_flagged: boolean;
  generated_at: string;
  window_days: number;
}

export function DriftSignalsCard({ projectId }: { projectId: string }) {
  const [snapshot, setSnapshot] = useState<DriftSnapshot | null>(null);
  const [expanded, setExpanded] = useState(false);

  const fetchSnapshot = useCallback(async () => {
    try {
      const res = await apiFetch(`/api/admin/drift-snapshot?project_id=${encodeURIComponent(projectId)}`);
      if (res.ok) setSnapshot(await res.json());
    } catch { /* silent */ }
  }, [projectId]);

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch sets state after await, intentional
    fetchSnapshot();
    const id = setInterval(fetchSnapshot, 60_000);
    return () => clearInterval(id);
  }, [fetchSnapshot]);

  if (!snapshot || snapshot.flagged_count === 0) return null;

  const { flagged_directions, flagged_count } = snapshot;

  return (
    <section>
      <button
        type="button"
        className="w-full text-left"
        onClick={() => setExpanded(v => !v)}
        aria-expanded={expanded}
      >
        <div className="flex items-center gap-2 bg-amber-900/20 border border-amber-500/30 rounded-lg px-3 py-2">
          <AlertTriangle className="w-3.5 h-3.5 text-amber-400 shrink-0" />
          <span className="text-xs font-semibold text-amber-300">
            Drift Signals — {flagged_count} direction{flagged_count !== 1 ? 's' : ''} flagged
          </span>
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
                  <span className="text-[9px] px-1.5 py-0.5 rounded font-medium bg-amber-500/20 text-amber-400 border border-amber-500/30">
                    over-claim
                  </span>
                )}
                {d.stalled_despite_churn_flag && (
                  <span className="text-[9px] px-1.5 py-0.5 rounded font-medium bg-red-500/20 text-red-400 border border-red-500/30">
                    stalled
                  </span>
                )}
                <span className="text-[9px] text-neutral-500 ml-auto">
                  {d.actually_merged_prs}/{d.claimed_changes_count} merged
                </span>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
