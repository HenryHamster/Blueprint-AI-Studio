import { useState, useEffect, useCallback } from 'react';
import { AlertTriangle, CheckCircle, RotateCcw, Activity, ChevronDown, ChevronRight, Shield } from 'lucide-react';
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip } from 'recharts';
import { apiFetch } from '../api';
import { timeAgo, parseTimestamp } from '../utils/time';
import { LayersAffectedBadges, type LayerSummary } from './LayersAffectedBadges';

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

export function ClassKpisPanel({ projectId, filterClass, onClassSelect }: Props) {
  const [data, setData] = useState<{ window_hours: number; class_count: number; classes: ClassKpiRow[] } | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [windowHours, setWindowHours] = useState(168);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/admin/regression-class-kpis?project_id=${encodeURIComponent(projectId)}&window_hours=${windowHours}`)
      .then(async (res) => { if (!cancelled && res.ok) setData(await res.json()); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoaded(true); });
    return () => { cancelled = true; };
  }, [projectId, windowHours]);

  if (!loaded || !data || data.class_count === 0) return null;

  return (
    <section data-testid="class-kpis-panel" className="mb-4">
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest">Per-Class KPIs</h2>
        <select
          value={windowHours}
          onChange={(e) => setWindowHours(Number(e.target.value))}
          className="text-[10px] bg-neutral-800 border border-neutral-700 rounded px-1.5 py-0.5 text-neutral-400"
        >
          <option value={24}>24h</option>
          <option value={168}>7d</option>
          <option value={720}>30d</option>
        </select>
      </div>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        <table className="w-full text-[10px]">
          <thead>
            <tr className="border-b border-neutral-800/50 text-neutral-500">
              <th className="text-left px-3 py-1.5 font-medium">Class</th>
              <th className="text-right px-2 py-1.5 font-medium">Opened</th>
              <th className="text-right px-2 py-1.5 font-medium">Closed</th>
              <th className="text-right px-2 py-1.5 font-medium">MTTD</th>
              <th className="text-right px-2 py-1.5 font-medium">MTTR</th>
              <th className="text-right px-3 py-1.5 font-medium">Success</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-neutral-800/30">
            {data.classes.map((row) => {
              const active = filterClass === row.regression_class;
              return (
                <tr
                  key={row.regression_class}
                  onClick={() => onClassSelect?.(active ? null : row.regression_class)}
                  className={`transition-colors ${onClassSelect ? 'cursor-pointer' : ''} ${active ? 'bg-blue-950/30' : onClassSelect ? 'hover:bg-neutral-800/30' : ''}`}
                >
                  <td className="px-3 py-1.5 font-mono text-neutral-300 max-w-[160px] truncate">{row.regression_class}</td>
                  <td className="px-2 py-1.5 text-right text-white">{row.cases_opened}</td>
                  <td className="px-2 py-1.5 text-right text-emerald-400">{row.cases_closed}</td>
                  <td className="px-2 py-1.5 text-right text-neutral-400">{formatMttd(row.mean_time_to_detect_seconds)}</td>
                  <td className="px-2 py-1.5 text-right text-neutral-400">{formatMttd(row.mean_time_to_remediate_seconds)}</td>
                  <td className="px-3 py-1.5 text-right">
                    {row.remediation_success_rate !== null
                      ? <span className={row.remediation_success_rate >= 0.5 ? 'text-emerald-400' : 'text-amber-400'}>{Math.round(row.remediation_success_rate * 100)}%</span>
                      : <span className="text-neutral-600">—</span>}
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
  recurrence_count?: number;
  layer_summary?: LayerSummary | null;
}

const ESCALATION_REASON_LABELS: Record<string, string> = {
  investigations_diverged: 'Diverged diagnoses',
  attempts_exhausted: 'Max attempts',
  stale_active: 'Stale (watchdog)',
  stale_merged: 'Stale (watchdog)',
  recovered_unattended: 'Auto-recovered (24h healthy)',
};

interface ClassKpiRow {
  regression_class: string;
  cases_opened: number;
  cases_closed: number;
  dispatch_attempt_count: number;
  remediation_success_rate: number | null;
  mean_time_to_detect_seconds: number | null;
  mean_time_to_remediate_seconds: number | null;
}

interface Props {
  projectId: string;
  filterClass?: string | null;
  onClassSelect?: (cls: string | null) => void;
}

interface LayerCoverageCombo { layers: string[]; count: number; }
interface LayerCoverageEntry { total_fixes_with_layers: number; combinations: LayerCoverageCombo[]; }
type LayerCoverageByClass = Record<string, LayerCoverageEntry>;

const LAYER_SHORT: Record<string, string> = { CI_WORKFLOW: 'CI', AGENT_VM: 'VM', AGENT_VERB: 'VERB', BASELINE_GATE: 'BASELINE', REVIEWER_PASS: 'REVIEWER' };
const CANONICAL_LAYERS = ['CI_WORKFLOW', 'AGENT_VM', 'AGENT_VERB', 'BASELINE_GATE', 'REVIEWER_PASS'];

export function LayerCoverageHistogramPanel({ projectId }: Props) {
  const [coverage, setCoverage] = useState<LayerCoverageByClass | null>(null);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/admin/regression-cases?project_id=${encodeURIComponent(projectId)}`)
      .then(async (res) => { if (!cancelled && res.ok) { const d = await res.json(); setCoverage(d.summary?.layer_coverage_by_class ?? null); } })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [projectId]);

  if (!coverage || Object.keys(coverage).length === 0) return null;

  return (
    <section data-testid="layer-coverage-histogram-panel" className="mb-4">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Layer Coverage by Class</h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg divide-y divide-neutral-800/30 overflow-hidden">
        {Object.entries(coverage).map(([cls, entry]) => {
          const targeted = new Set(entry.combinations.flatMap(c => c.layers));
          const missing = CANONICAL_LAYERS.filter(l => !targeted.has(l));
          const underDefended = targeted.size > 0 && missing.length > 0;
          const noLayerCount = entry.combinations.find(c => c.layers.length === 0)?.count ?? 0;
          const top2 = entry.combinations.filter(c => c.layers.length > 0).slice(0, 2);
          return (
            <div key={cls}
              className={`px-3 py-1.5 ${underDefended ? 'bg-amber-950/10 border-l-2 border-l-amber-500/60' : ''}`}
              title={underDefended ? `Under-defended: no fix has ever targeted ${missing.map(l => LAYER_SHORT[l] ?? l).join(', ')}` : undefined}
            >
              <div className="flex items-center gap-1.5 flex-wrap min-w-0">
                <span className="text-[10px] font-mono text-neutral-300 truncate flex-1 min-w-0">{cls}</span>
                {top2.map((c, i) => (
                  <span key={i} className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-800/60 text-neutral-300 border border-neutral-700 font-mono whitespace-nowrap">
                    {c.layers.map(l => LAYER_SHORT[l] ?? l).join('+')} ×{c.count}
                  </span>
                ))}
                {noLayerCount > 0 && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-500 border border-neutral-700/40 whitespace-nowrap" title="Fixes targeting no canonical layer (doc-only / test-only)">
                    no-layer ×{noLayerCount}
                  </span>
                )}
                {underDefended && <span className="text-[10px] text-amber-400/80" aria-label="under-defended">⚠</span>}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
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

function recurrenceBadge(count: number | undefined) {
  return count && count > 0 ? (
    <span className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-500/10 text-neutral-400 border border-neutral-500/20">
      recurred {count}x
    </span>
  ) : null;
}

export function ManualInterventionPanel({ projectId, filterClass }: Props) {
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

  const sorted = [...cases]
    .filter(c => !filterClass || c.regression_class === filterClass)
    .sort((a, b) => {
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
                    {recurrenceBadge(c.recurrence_count)}
                    {c.escalation_reason && ESCALATION_REASON_LABELS[c.escalation_reason] && (
                      <span
                        className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-300 border border-amber-500/20"
                        title={`escalation_reason: ${c.escalation_reason}`}
                      >
                        {ESCALATION_REASON_LABELS[c.escalation_reason]}
                      </span>
                    )}
                    <LayersAffectedBadges summary={c.layer_summary} />
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

interface AnomalyEntry {
  key: string;
  first_seen: string;
  last_seen: string;
  occurrence_count: number;
  severity: string;
  latest_details: string;
  fingerprint: string | null;
  affected_directions: string[] | null;
  last_dispatch_attempt_at: string | null;
  stuck: boolean;
}

interface AnomalyTrackerSnapshot {
  last_detection_run_at: string | null;
  freshness_window_hours: number;
  total: number;
  projects: { project_id: string; anomalies: AnomalyEntry[] }[];
}

// Detection sweep cadence is sub-minute; flagging amber after 30 min keeps the
// signal noticeable while staying well clear of normal jitter.
const DETECTION_STALE_MS = 30 * 60 * 1000;

export function AnomalyTrackerPanel({ projectId }: Props) {
  const [snap, setSnap] = useState<AnomalyTrackerSnapshot | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [nowMs, setNowMs] = useState(() => Date.now());

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/admin/anomaly-tracker/snapshot?project_id=${encodeURIComponent(projectId)}&freshness_hours=24`)
      .then(async (res) => { if (!cancelled && res.ok) setSnap(await res.json()); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoaded(true); });
    return () => { cancelled = true; };
  }, [projectId]);

  useEffect(() => {
    const id = setInterval(() => setNowMs(Date.now()), 60_000);
    return () => clearInterval(id);
  }, []);

  if (!loaded) return null;

  const lastRun = snap?.last_detection_run_at ?? null;
  const stale = !lastRun || (nowMs - parseTimestamp(lastRun).getTime()) > DETECTION_STALE_MS;
  const lastUpdateText = lastRun ? timeAgo(lastRun) : 'never';
  const project = snap?.projects.find(p => p.project_id === projectId);
  const anomalies = project?.anomalies ?? [];

  return (
    <section data-testid="anomaly-tracker-panel" className="mt-4">
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="w-full flex items-center gap-2 bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-2 text-left hover:bg-neutral-900/80 transition-colors min-h-[44px]"
        aria-expanded={expanded}
      >
        {expanded ? <ChevronDown className="w-3.5 h-3.5 text-neutral-500 shrink-0" /> : <ChevronRight className="w-3.5 h-3.5 text-neutral-500 shrink-0" />}
        <Activity className="w-3 h-3 text-neutral-400 shrink-0" />
        <span className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest">Anomaly Tracker</span>
        {anomalies.length > 0 && (
          <span className="bg-neutral-700/40 text-neutral-300 text-[10px] font-semibold px-1.5 py-0.5 rounded-full normal-case tracking-normal">
            {anomalies.length}
          </span>
        )}
        <span className="ml-auto text-[10px] text-neutral-500 truncate" title={lastRun ?? 'no detection run recorded'}>
          last-update:{' '}
          <span className={stale ? 'text-amber-400 font-medium' : 'text-neutral-300'}>
            {lastUpdateText}
          </span>
        </span>
      </button>
      {expanded && (
        <div className="mt-2">
          {anomalies.length === 0 ? (
            <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-neutral-500 text-xs">
              No tracked anomalies
            </div>
          ) : (
            <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg divide-y divide-neutral-800/50 overflow-hidden">
              {anomalies.map(a => (
                <div key={a.key} className="px-3 py-2 space-y-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="text-[10px] font-mono text-neutral-300 break-all min-w-0">{a.key}</span>
                    <span className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-700/40 text-neutral-300 border border-neutral-700/40 capitalize">
                      {a.severity}
                    </span>
                    <span className="text-[10px] text-neutral-400">
                      ×{a.occurrence_count}
                    </span>
                    {a.stuck && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded bg-amber-500/10 text-amber-300 border border-amber-500/20" title="Last seen older than the freshness window (default 24h)">
                        stuck
                      </span>
                    )}
                  </div>
                  <div className="flex items-center gap-3 text-[10px] text-neutral-500 flex-wrap">
                    <span>first {timeAgo(a.first_seen)}</span>
                    <span>last {timeAgo(a.last_seen)}</span>
                    <span>dispatch {a.last_dispatch_attempt_at ? timeAgo(a.last_dispatch_attempt_at) : '—'}</span>
                  </div>
                  {(a.fingerprint || (a.affected_directions && a.affected_directions.length > 0)) && (
                    <div className="text-[9px] text-neutral-600 font-mono break-all min-w-0">
                      {a.fingerprint && <span>fp: {a.fingerprint}</span>}
                      {a.fingerprint && a.affected_directions && a.affected_directions.length > 0 && <span> · </span>}
                      {a.affected_directions && a.affected_directions.length > 0 && (
                        <span>directions: {a.affected_directions.join(', ')}</span>
                      )}
                    </div>
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

interface TrendBucket {
  date: string;
  opened: number;
  closed: number;
}

export function RegressionRateTrendPanel({ projectId }: Props) {
  const [data, setData] = useState<{ daily: TrendBucket[] } | null>(null);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/admin/regression-cases/trend?project_id=${encodeURIComponent(projectId)}&days=30`)
      .then(async (res) => { if (!cancelled && res.ok) setData(await res.json()); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoaded(true); });
    return () => { cancelled = true; };
  }, [projectId]);

  if (!loaded || !data || data.daily.length === 0) return null;

  return (
    <section data-testid="regression-rate-trend-panel" className="mb-4">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">
        Regression Trend (30d)
      </h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3">
        <ResponsiveContainer width="100%" height={120}>
          <LineChart data={data.daily} margin={{ top: 4, right: 4, left: -28, bottom: 0 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#262626" />
            <XAxis dataKey="date" tick={{ fontSize: 9, fill: '#737373' }} tickFormatter={(v: string) => v.slice(5)} />
            <YAxis tick={{ fontSize: 9, fill: '#737373' }} allowDecimals={false} width={32} />
            <Tooltip contentStyle={{ background: '#171717', border: '1px solid #404040', fontSize: 10 }} />
            <Line type="monotone" dataKey="opened" stroke="#f87171" dot={false} strokeWidth={1.5} name="Opened" />
            <Line type="monotone" dataKey="closed" stroke="#34d399" dot={false} strokeWidth={1.5} name="Closed" />
          </LineChart>
        </ResponsiveContainer>
        <div className="flex items-center gap-3 mt-1 text-[10px] text-neutral-500">
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-3 h-0.5 bg-red-400 rounded-full" />
            Opened
          </span>
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-3 h-0.5 bg-emerald-400 rounded-full" />
            Closed
          </span>
        </div>
      </div>
    </section>
  );
}

interface LayerDefenseEntry {
  layer: string;
  fix_count: number;
  last_fix_at: string | null;
  sample_case_ids: string[];
}

export function LayerDefenseDensityPanel({ projectId }: Props) {
  const [data, setData] = useState<{ days: number; layers: LayerDefenseEntry[] } | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [days, setDays] = useState(30);

  useEffect(() => {
    let cancelled = false;
    apiFetch(`/api/admin/regression-cases/layer-defense-density?project_id=${encodeURIComponent(projectId)}&days=${days}`)
      .then(async (res) => { if (!cancelled && res.ok) setData(await res.json()); })
      .catch(() => {})
      .finally(() => { if (!cancelled) setLoaded(true); });
    return () => { cancelled = true; };
  }, [projectId, days]);

  if (!loaded) return null;

  const maxCount = data ? Math.max(...data.layers.map(l => l.fix_count), 1) : 1;

  return (
    <section data-testid="layer-defense-density-panel" className="mb-4">
      <div className="flex items-center justify-between mb-2">
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest flex items-center gap-1.5">
          <Shield className="w-3 h-3" />
          Layer Defense Density
        </h2>
        <select
          value={days}
          onChange={(e) => setDays(Number(e.target.value))}
          className="text-[10px] bg-neutral-800 border border-neutral-700 rounded px-1.5 py-0.5 text-neutral-400"
        >
          <option value={7}>7d</option>
          <option value={30}>30d</option>
          <option value={90}>90d</option>
        </select>
      </div>
      {!data || data.layers.every(l => l.fix_count === 0) ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-neutral-500 text-xs">
          No layer defenses recorded in the last {days} days
        </div>
      ) : (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
          <table className="w-full text-[10px]">
            <thead>
              <tr className="border-b border-neutral-800/50 text-neutral-500">
                <th className="text-left px-3 py-1.5 font-medium">Layer</th>
                <th className="text-right px-2 py-1.5 font-medium">Fixes</th>
                <th className="px-3 py-1.5 font-medium w-24 hidden sm:table-cell">Coverage</th>
                <th className="text-right px-3 py-1.5 font-medium">Last fix</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-neutral-800/30">
              {(data?.layers ?? []).map((row) => (
                <tr key={row.layer}>
                  <td className="px-3 py-1.5 font-mono text-neutral-300 truncate max-w-[120px]">{row.layer}</td>
                  <td className="px-2 py-1.5 text-right">
                    <span className={row.fix_count === 0 ? 'text-neutral-600' : 'text-white font-medium'}>
                      {row.fix_count}
                    </span>
                  </td>
                  <td className="px-3 py-1.5 hidden sm:table-cell">
                    <div className="h-1.5 bg-neutral-800 rounded-full overflow-hidden w-full">
                      <div
                        className="h-full rounded-full bg-blue-500/70"
                        style={{ width: `${Math.round((row.fix_count / maxCount) * 100)}%` }}
                      />
                    </div>
                  </td>
                  <td className="px-3 py-1.5 text-right text-neutral-500">
                    {row.last_fix_at ? timeAgo(row.last_fix_at) : <span className="text-neutral-700">—</span>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
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
                  {recurrenceBadge(c.recurrence_count)}
                  {c.escalation_reason && ESCALATION_REASON_LABELS[c.escalation_reason] && (
                    <span
                      className="text-[10px] px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-300 border border-emerald-500/20"
                      title={`escalation_reason: ${c.escalation_reason}`}
                    >
                      {ESCALATION_REASON_LABELS[c.escalation_reason]}
                    </span>
                  )}
                  <LayersAffectedBadges summary={c.layer_summary} />
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
