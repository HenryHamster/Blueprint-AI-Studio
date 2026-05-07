import { useState, useEffect, useCallback } from 'react';
import { AlertTriangle, CheckCircle, RotateCcw, Activity } from 'lucide-react';
import { apiFetch } from '../api';
import { timeAgo } from '../utils/time';

interface DispatcherCycleClass {
  regression_class: string;
  last_spawn_at: string | null;
  last_rejection_reason: string | null;
  in_cooldown_until: string | null;
}

interface DispatcherHealthData {
  cycles_last_24h: number;
  signals_tripped_last_24h: number;
  spawns_last_24h: number;
  cooldown_blocks_last_24h: number;
  cap_blocks_last_24h: number;
  classes: DispatcherCycleClass[];
}

export function DispatcherHealthPanel({ projectId }: Props) {
  const [data, setData] = useState<DispatcherHealthData | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/admin/regression-dispatcher/health?project_id=${encodeURIComponent(projectId)}`)
      .then(async (res) => {
        if (!cancelled && res.ok) setData(await res.json());
      })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoaded(true); });
    return () => { cancelled = true; };
  }, [projectId]);

  if (!loaded) return null;

  return (
    <section data-testid="dispatcher-health-panel" className="mt-4">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2 flex items-center gap-1.5">
        <Activity className="w-3 h-3" />
        Dispatcher Health
      </h2>
      {!data || data.cycles_last_24h === 0 ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-neutral-500 text-xs">
          No dispatcher cycles in the last 24h
        </div>
      ) : (
        <div className="space-y-2">
          <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-xs text-neutral-400 flex flex-wrap gap-x-4 gap-y-1">
            <span><span className="text-white font-medium">{data.cycles_last_24h}</span> cycles</span>
            <span><span className="text-white font-medium">{data.signals_tripped_last_24h}</span> signals tripped</span>
            <span><span className="text-emerald-400 font-medium">{data.spawns_last_24h}</span> spawns</span>
            <span><span className="text-amber-400 font-medium">{data.cooldown_blocks_last_24h}</span> cooldown</span>
            <span><span className="text-red-400 font-medium">{data.cap_blocks_last_24h}</span> cap</span>
          </div>
          {data.classes.length > 0 && (
            <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg divide-y divide-neutral-800/50 overflow-hidden">
              {data.classes.map((c) => (
                <div key={c.regression_class} className="px-3 py-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5">
                  <span className="text-[10px] font-mono text-neutral-300 min-w-0 flex-1 truncate">{c.regression_class}</span>
                  {c.last_spawn_at && (
                    <span className="text-[10px] text-neutral-500">spawn {timeAgo(c.last_spawn_at)}</span>
                  )}
                  {c.in_cooldown_until && new Date(c.in_cooldown_until) > new Date() && (
                    <span className="text-[10px] text-amber-400/80">cooldown {timeAgo(c.in_cooldown_until)}</span>
                  )}
                  {c.last_rejection_reason && (
                    <span className="text-[10px] text-neutral-600 font-mono">{c.last_rejection_reason}</span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

interface FixLogEntry {
  fix_pr_number?: number;
  merged_at?: string;
  recovered_at?: string;
  post_merge_value?: number;
  reason?: 'recovered_unattended';
}

interface ManualInterventionCase {
  id: string;
  regression_class: string;
  signal_name: string;
  status: string;
  attempts: number;
  last_remediation_pr_number: number | null;
  last_failure_class: string | null;
  signal_snapshot_at_spawn: Record<string, unknown> | null;
  signal_snapshot_at_merge: Record<string, unknown> | null;
  fingerprint_to_fix_log: FixLogEntry[];
  last_seen_at: string | null;
  needs_manual_intervention_at: string | null;
  signal_first_seen_at: string | null;
  time_to_detect_sec: number | null;
  escalation_reason: string | null;
}

const ESCALATION_REASON_LABELS: Record<string, string> = {
  investigations_diverged: 'Diverged diagnoses',
  attempts_exhausted: 'Max attempts',
  stale_active: 'Stale (watchdog)',
  stale_merged: 'Stale (watchdog)',
  recovered_unattended: 'Auto-recovered (24h healthy)',
};

interface Props {
  projectId: string;
}

function formatMttd(sec: number | null | undefined): string {
  if (sec === null || sec === undefined) return 'unknown';
  if (sec < 0) return '—';
  if (sec < 60) return `${Math.round(sec)}s`;
  if (sec < 3600) return `${Math.round(sec / 60)}m`;
  return `${(sec / 3600).toFixed(1)}h`;
}

function snapshotSummary(snap: Record<string, unknown> | null): string {
  if (!snap) return '—';
  const entries = Object.entries(snap)
    .filter(([, v]) => typeof v === 'number' || typeof v === 'string')
    .slice(0, 3)
    .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${typeof v === 'number' ? Math.round(Number(v) * 100) / 100 : v}`);
  return entries.length ? entries.join(', ') : '—';
}

export function ManualInterventionPanel({ projectId }: Props) {
  const [cases, setCases] = useState<ManualInterventionCase[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [clearing, setClearing] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<'last_seen_at' | 'mttd'>('last_seen_at');

  const fetchCases = useCallback(async () => {
    try {
      const res = await apiFetch(
        `/api/admin/regression-cases/manual-intervention?project_id=${encodeURIComponent(projectId)}`
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

  const handleClear = useCallback(async (regressionClass: string) => {
    setClearing(regressionClass);
    try {
      const res = await apiFetch(
        `/api/admin/regression-cases/${encodeURIComponent(regressionClass)}/clear-manual-intervention?project_id=${encodeURIComponent(projectId)}`,
        { method: 'POST' }
      );
      if (res.ok) {
        setCases(prev => prev.filter(c => c.regression_class !== regressionClass));
      }
    } catch { /* silent */ }
    setClearing(null);
  }, [projectId]);

  if (!loaded) return null;

  const sorted = [...cases].sort((a, b) => {
    if (sortKey === 'mttd') return (b.time_to_detect_sec ?? -1) - (a.time_to_detect_sec ?? -1);
    return (b.last_seen_at ?? '').localeCompare(a.last_seen_at ?? '');
  });

  return (
    <section data-testid="manual-intervention-panel">
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest">
          Manual Intervention Required
          {cases.length > 0 && (
            <span className="ml-2 bg-red-500/20 text-red-400 text-[10px] font-semibold px-1.5 py-0.5 rounded-full normal-case tracking-normal">
              {cases.length}
            </span>
          )}
        </h2>
        {cases.length > 0 && (
          <div className="flex items-center gap-1 text-[10px] text-neutral-500">
            <span>Sort:</span>
            {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
            <button
              onClick={() => setSortKey('last_seen_at')}
              className={`px-1.5 py-0.5 rounded ${sortKey === 'last_seen_at' ? 'text-white bg-neutral-700' : 'hover:text-neutral-300'}`}
            >recent</button>
            {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
            <button
              onClick={() => setSortKey('mttd')}
              className={`px-1.5 py-0.5 rounded ${sortKey === 'mttd' ? 'text-white bg-neutral-700' : 'hover:text-neutral-300'}`}
            >MTTD</button>
          </div>
        )}
      </div>
      {cases.length === 0 ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 flex items-center gap-2 text-emerald-400 text-xs">
          <CheckCircle className="w-4 h-4 shrink-0" />
          No regression cases need manual intervention
        </div>
      ) : (
        <div className="space-y-2">
          {sorted.map(c => (
            <div
              key={c.id}
              className="bg-red-950/20 border border-red-900/40 border-l-2 border-l-red-500 rounded-lg px-3 py-2"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <AlertTriangle className="w-3.5 h-3.5 text-red-400 shrink-0" />
                    <span className="text-xs text-red-300 font-medium capitalize">
                      {c.signal_name}
                    </span>
                    <span className="text-[10px] text-neutral-500 font-mono break-all min-w-0">
                      {c.regression_class}
                    </span>
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-400 border border-red-500/20">
                      {c.attempts} attempt{c.attempts !== 1 ? 's' : ''}
                    </span>
                    {c.escalation_reason && ESCALATION_REASON_LABELS[c.escalation_reason] && (
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-300 border border-amber-500/20"
                        title={`escalation_reason: ${c.escalation_reason}`}
                      >
                        {ESCALATION_REASON_LABELS[c.escalation_reason]}
                      </span>
                    )}
                  </div>
                  <div className="mt-1.5 space-y-0.5">
                    <div className="text-[10px] text-neutral-500">
                      <span className="text-neutral-400 font-medium">At spawn:</span>{' '}
                      {snapshotSummary(c.signal_snapshot_at_spawn)}
                    </div>
                    <div className="text-[10px] text-neutral-500">
                      <span className="text-neutral-400 font-medium">At merge:</span>{' '}
                      {snapshotSummary(c.signal_snapshot_at_merge)}
                    </div>
                    {c.fingerprint_to_fix_log.length > 0 && (
                      <div className="mt-1.5 border-t border-red-900/30 pt-1">
                        <div className="text-[10px] text-neutral-400 font-medium mb-0.5">Prior attempts:</div>
                        <div className="space-y-0.5">
                          {c.fingerprint_to_fix_log.map((entry, idx) => (
                            <div key={idx} className="text-[9px] text-neutral-500">
                              {entry.fix_pr_number && entry.merged_at && (
                                <span>
                                  PR #{entry.fix_pr_number} merged {new Date(entry.merged_at).toLocaleDateString()}
                                  {entry.recovered_at && ' → recovered'}
                                </span>
                              )}
                              {entry.recovered_at && !entry.fix_pr_number && (
                                <span>Recovered {new Date(entry.recovered_at).toLocaleDateString()}</span>
                              )}
                            </div>
                          ))}
                        </div>
                      </div>
                    )}
                    <div className="flex items-center gap-3 mt-1 flex-wrap">
                      {c.last_remediation_pr_number != null && (
                        <span className="text-[10px] text-neutral-400">
                          Last PR: #{c.last_remediation_pr_number}
                        </span>
                      )}
                      {c.last_failure_class && (
                        <span className="text-[10px] text-neutral-500 font-mono break-all min-w-0">
                          last fail: {c.last_failure_class}
                        </span>
                      )}
                      {c.last_seen_at && (
                        <span className="text-[10px] text-neutral-500">
                          last seen {timeAgo(c.last_seen_at)}
                        </span>
                      )}
                      <span className="text-[10px] text-amber-500/80" title="Time from signal crossing threshold to case opening (MTTD)">
                        MTTD {formatMttd(c.time_to_detect_sec)}
                      </span>
                    </div>
                  </div>
                </div>
                {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                <button
                  onClick={() => handleClear(c.regression_class)}
                  disabled={clearing === c.regression_class}
                  className="shrink-0 flex items-center gap-1.5 text-[10px] px-2 py-1 rounded border border-neutral-700 text-neutral-300 hover:text-white hover:border-neutral-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                  title="Clear manual intervention and re-arm autonomous remediation"
                >
                  <RotateCcw className={`w-3 h-3 ${clearing === c.regression_class ? 'animate-spin' : ''}`} />
                  Clear &amp; re-arm
                </button>
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export function AutoRecoveredPanel({ projectId }: Props) {
  const [cases, setCases] = useState<ManualInterventionCase[]>([]);
  const [loaded, setLoaded] = useState(false);

  const fetchCases = useCallback(async () => {
    try {
      const res = await apiFetch(
        `/api/admin/regression-cases/auto-recovered?project_id=${encodeURIComponent(projectId)}`
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
    <section data-testid="auto-recovered-panel">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Auto-Recovered
        {cases.length > 0 && (
          <span className="ml-2 bg-emerald-500/20 text-emerald-400 text-[10px] font-semibold px-1.5 py-0.5 rounded-full normal-case tracking-normal">
            {cases.length}
          </span>
        )}
      </h2>
      {cases.length === 0 ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-neutral-500 text-xs">
          No auto-recovered cases
        </div>
      ) : (
        <div className="space-y-2">
          {cases.map(c => (
            <div
              key={c.id}
              className="bg-emerald-950/20 border border-emerald-900/40 border-l-2 border-l-emerald-500 rounded-lg px-3 py-2"
            >
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2 flex-wrap">
                  <CheckCircle className="w-3.5 h-3.5 text-emerald-400 shrink-0" />
                  <span className="text-xs text-emerald-300 font-medium capitalize">
                    {c.signal_name}
                  </span>
                  <span className="text-[10px] text-neutral-500 font-mono">{c.regression_class}</span>
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20">
                    {c.attempts} attempt{c.attempts !== 1 ? 's' : ''}
                  </span>
                  {c.escalation_reason && ESCALATION_REASON_LABELS[c.escalation_reason] && (
                    <span
                      className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-300 border border-emerald-500/20"
                      title={`escalation_reason: ${c.escalation_reason}`}
                    >
                      {ESCALATION_REASON_LABELS[c.escalation_reason]}
                    </span>
                  )}
                </div>
                <div className="mt-1.5 space-y-0.5">
                  <div className="text-[10px] text-neutral-500">
                    <span className="text-neutral-400 font-medium">At spawn:</span>{' '}
                    {snapshotSummary(c.signal_snapshot_at_spawn)}
                  </div>
                  <div className="text-[10px] text-neutral-500">
                    <span className="text-neutral-400 font-medium">At merge:</span>{' '}
                    {snapshotSummary(c.signal_snapshot_at_merge)}
                  </div>
                  {c.fingerprint_to_fix_log.length > 0 && (
                    <div className="mt-1.5 border-t border-emerald-900/30 pt-1">
                      <div className="text-[10px] text-neutral-400 font-medium mb-0.5">Prior attempts:</div>
                      <div className="space-y-0.5">
                        {c.fingerprint_to_fix_log.map((entry, idx) => (
                          <div key={idx} className="text-[9px] text-neutral-500">
                            {entry.fix_pr_number && entry.merged_at && (
                              <span>
                                PR #{entry.fix_pr_number} merged {new Date(entry.merged_at).toLocaleDateString()}
                                {entry.recovered_at && ' → recovered'}
                              </span>
                            )}
                            {entry.reason === 'recovered_unattended' && (
                              <span>Auto-recovered {new Date(entry.recovered_at || '').toLocaleDateString()}</span>
                            )}
                          </div>
                        ))}
                      </div>
                    </div>
                  )}
                  <div className="flex items-center gap-3 mt-1">
                    {c.last_remediation_pr_number != null && (
                      <span className="text-[10px] text-neutral-400">
                        Last PR: #{c.last_remediation_pr_number}
                      </span>
                    )}
                    {c.last_failure_class && (
                      <span className="text-[10px] text-neutral-500 font-mono">
                        last fail: {c.last_failure_class}
                      </span>
                    )}
                    {c.time_to_detect_sec !== null && c.time_to_detect_sec !== undefined && (
                      <span className="text-[10px] text-emerald-500/80" title="Time from signal crossing threshold to case opening (MTTD)">
                        MTTD {formatMttd(c.time_to_detect_sec)}
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
