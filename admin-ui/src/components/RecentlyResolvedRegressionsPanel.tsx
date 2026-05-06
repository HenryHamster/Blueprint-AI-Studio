import { useState, useEffect, useCallback } from 'react';
import { CheckCircle, Minus } from 'lucide-react';
import { apiFetch } from '../api';
import { timeAgo, formatDurationSec, parseTimestamp } from '../utils/time';

interface RecentlyClosedCase {
  id: string;
  regression_class: string;
  fix_pr_number: number | null;
  recovered_at: string | null;
  first_seen_at: string | null;
  signal_first_crossed_at?: string | null;
  time_to_recover_sec: number | null;
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

  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect -- async fetch sets state after await, intentional
    fetchCases();
    const id = setInterval(fetchCases, 60_000);
    return () => clearInterval(id);
  }, [fetchCases]);

  if (!loaded) return null;

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
