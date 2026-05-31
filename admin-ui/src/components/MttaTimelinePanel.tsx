import { useEffect, useState } from 'react';
import { Clock, ChevronDown, ChevronRight } from 'lucide-react';
import { apiFetch } from '../api';
import type { MttaClassRow, RegressionMttaResponse } from '../api';
import { RegressionClassLabel } from './regressionClassLabel';

function formatMttd(sec: number | null | undefined): string {
  if (sec == null) return '—';
  if (sec < 60) return `${Math.round(sec)}s`;
  const min = Math.floor(sec / 60);
  const rem = Math.round(sec % 60);
  if (min < 60) return `${min}m${rem ? ` ${rem}s` : ''}`;
  const hr = Math.floor(min / 60);
  const m = min % 60;
  return `${hr}h${m ? ` ${m}m` : ''}`;
}

const STAGE_LABELS: { key: keyof MttaClassRow['stages']; label: string }[] = [
  { key: 'signal_trip_to_detect', label: 'Trip→Detect' },
  { key: 'detect_to_investigate', label: 'Detect→Investigate' },
  { key: 'investigate_to_merge', label: 'Investigate→Merge' },
  { key: 'merge_to_recovery', label: 'Merge→Recovery' },
];

function MttaClassCard({ row }: { row: MttaClassRow }) {
  const totalP50 = STAGE_LABELS.reduce((sum, s) => sum + (row.stages[s.key].p50_sec ?? 0), 0);
  return (
    <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 space-y-1.5">
      <div className="flex items-center gap-2 flex-wrap">
        <span className="text-[10px] flex-1 min-w-0 truncate"><RegressionClassLabel cls={row.regression_class} /></span>
        <span className="text-[10px] text-neutral-500">{row.sample_count} cases</span>
        {totalP50 > 0 && (
          <span className="text-[10px] text-neutral-400" title="Estimated p50 end-to-end auto-fix time">
            p50 total: {formatMttd(totalP50)}
          </span>
        )}
      </div>
      <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 sm:grid-cols-4">
        {STAGE_LABELS.map(({ key, label }) => {
          const s = row.stages[key];
          return (
            <div key={key} className="text-[9px] text-neutral-500">
              <span className="text-neutral-600">{label}:</span>{' '}
              {s.p50_sec !== null ? (
                <span className="text-neutral-400">{formatMttd(s.p50_sec)} <span className="text-neutral-600">/ {formatMttd(s.p90_sec ?? null)}</span></span>
              ) : (
                <span className="text-neutral-700">—</span>
              )}
            </div>
          );
        })}
      </div>
      {row.active_cases.length > 0 && (
        <div className="border-t border-neutral-800/50 pt-1 space-y-1">
          {row.active_cases.map((ac) => {
            const p50Total = totalP50 > 0 ? totalP50 : null;
            const elapsed = ac.live_elapsed_sec;
            const isStalled = p50Total !== null && elapsed !== null && elapsed > p50Total * 1.5;
            return (
              <div key={ac.case_id} className="space-y-0.5">
                <div className="flex items-center gap-2 flex-wrap text-[10px]">
                  <span className={`px-1.5 py-0.5 rounded border font-mono ${isStalled ? 'bg-amber-950/30 border-amber-700/50 text-amber-300' : 'bg-neutral-800/40 border-neutral-700/40 text-neutral-400'}`}>
                    {ac.status}
                  </span>
                  {elapsed !== null && (
                    <span className={isStalled ? 'text-amber-400' : 'text-neutral-400'}>
                      elapsed: {formatMttd(elapsed)}
                      {isStalled && ' ⚠ stalled'}
                      {!isStalled && p50Total !== null && ` (eta ~${formatMttd(Math.max(0, p50Total - elapsed))})`}
                    </span>
                  )}
                  {ac.recovery_check_in_sec !== null && (
                    <span className="text-blue-400">
                      recovery check in {formatMttd(ac.recovery_check_in_sec)}
                    </span>
                  )}
                  {ac.recovery_check_in_sec === null && ac.status === 'MERGED' && (
                    <span className="text-emerald-400/80">recovery window active</span>
                  )}
                </div>
                <div className="text-[9px] text-neutral-500 flex flex-wrap gap-2 pl-1">
                  {ac.live_elapsed_sec !== null && ac.eta_to_investigate_sec !== null && (
                    <span>detect: {formatMttd(ac.live_elapsed_sec)} / eta {formatMttd(ac.eta_to_investigate_sec)}</span>
                  )}
                  {ac.live_elapsed_investigate_sec !== null && ac.eta_to_merge_sec !== null && (
                    <span>investigate: {formatMttd(ac.live_elapsed_investigate_sec)} / eta {formatMttd(ac.eta_to_merge_sec)}</span>
                  )}
                  {ac.elapsed_since_merge_sec !== null && ac.eta_to_recovery_sec !== null && (
                    <span>merge: {formatMttd(ac.elapsed_since_merge_sec)} / eta {formatMttd(ac.eta_to_recovery_sec)}</span>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

export function MttaTimelinePanel({ projectId }: { projectId: string }) {
  const [data, setData] = useState<RegressionMttaResponse | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [expanded, setExpanded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/admin/regression-dispatcher/mtta?project_id=${encodeURIComponent(projectId)}`)
      .then(async (res) => { if (!cancelled && res.ok) setData(await res.json()); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoaded(true); });
    return () => { cancelled = true; };
  }, [projectId]);

  if (!loaded) return null;
  if (!data || data.class_count === 0) return null;

  const activeCount = data.classes.reduce((n, c) => n + c.active_cases.length, 0);

  return (
    <section data-testid="mtta-timeline-panel" className="mt-4">
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="w-full flex items-center gap-2 bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 text-left hover:bg-neutral-900/80 transition-colors min-h-[44px]"
        aria-expanded={expanded}
      >
        {expanded ? <ChevronDown className="w-3.5 h-3.5 text-neutral-500 shrink-0" /> : <ChevronRight className="w-3.5 h-3.5 text-neutral-500 shrink-0" />}
        <Clock className="w-3 h-3 text-neutral-400 shrink-0" />
        <span className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest">MTTA Timeline</span>
        {activeCount > 0 && (
          <span className="bg-blue-700/30 text-blue-300 text-[10px] font-semibold px-1.5 py-0.5 rounded-full normal-case tracking-normal">
            {activeCount} active
          </span>
        )}
        <span className="ml-auto text-[10px] text-neutral-600">{data.class_count} class{data.class_count !== 1 ? 'es' : ''} · {data.window_days}d window</span>
      </button>
      {expanded && (
        <div className="mt-2 space-y-2">
          {data.classes.map((row) => (
            <MttaClassCard key={row.regression_class} row={row} />
          ))}
        </div>
      )}
    </section>
  );
}
