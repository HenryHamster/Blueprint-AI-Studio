import { useState, useEffect, useCallback, Fragment } from 'react';
import { AlertTriangle, CheckCircle, RotateCcw, Activity, ChevronDown, ChevronRight, Shield, Clock } from 'lucide-react';
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip } from 'recharts';
import { apiFetch, fetchCanonicalRegressionLayers, CANONICAL_LAYER_NAMES } from '../api';
import type { MttaClassRow, RegressionMttaResponse, CanonicalRegressionLayer } from '../api';
import { timeAgo, parseTimestamp, formatTimeToDetect, formatTimeToInvestigate } from '../utils/time';
import { parseViewportSubkey } from '../utils/viewportRegression';
import { LayersAffectedBadges, type LayerSummary } from './LayersAffectedBadges';
import { PhaseChip } from './regressionPhaseChips';

/** Prefix-to-chip color mapping for recognized composite regression classes. */
const CLASS_PREFIX_CHIPS: Record<string, string> = {
  viewport_regression: 'bg-violet-900/60 text-violet-300 border-violet-700/50',
  fix_loop_divergence: 'bg-amber-900/60 text-amber-300 border-amber-700/50',
  sr_false_positive: 'bg-rose-900/60 text-rose-300 border-rose-700/50',
  sr_catch_rate: 'bg-sky-900/60 text-sky-300 border-sky-700/50',
};

/**
 * Format a regression_class string for display. For recognized composite
 * classes (prefix:subkey), renders a colored chip + subkey identifier.
 * All other classes render as raw monospace text (unchanged behavior).
 */
function RegressionClassLabel({ cls }: { cls: string }) {
  const colon = cls.indexOf(':');
  if (colon === -1) {
    return <span className="font-mono text-neutral-300">{cls}</span>;
  }
  const prefix = cls.slice(0, colon);
  const subkey = cls.slice(colon + 1);
  const chipCls = CLASS_PREFIX_CHIPS[prefix];
  if (!chipCls) {
    return <span className="font-mono text-neutral-300">{cls}</span>;
  }
  return (
    <span className="inline-flex items-center gap-1 min-w-0">
      <span className={`inline-block text-[9px] font-semibold px-1 py-px rounded border ${chipCls} shrink-0`}>
        {prefix.replace(/_/g, ' ')}
      </span>
      <span className="font-mono text-neutral-300 truncate">{subkey}</span>
    </span>
  );
}

interface DispatcherCycleClass {
  project_id: string;
  regression_class: string;
  status: string;
  last_seen_at: string | null;
  attempts: number;
  in_cooldown_until: string | null;
}

interface DispatcherHealthData {
  window_hours: number;
  aggregate: {
    cycles: number;
    signals_evaluated: number;
    signals_tripped: number;
    spawns: number;
    mttd_p50_sec: number | null;
    mtti_p50_sec?: number | null;
    recoveries: {
      marginal: number;
      confirmed: number;
    };
    rejections: {
      cooldown: number;
      dedup: number;
      system_cap: number;
      manual_intervention_suppressed: number;
    };
  };
  spawns_sparkline: Array<{
    hour_start: string;
    spawns: number;
    cycles: number;
  }>;
  per_class: DispatcherCycleClass[];
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
      {!data || data.aggregate.cycles === 0 ? (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-neutral-500 text-xs">
          No dispatcher cycles in the last 24h
        </div>
      ) : (
        <div className="space-y-2">
          <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 text-xs text-neutral-400 flex flex-wrap gap-x-4 gap-y-1">
            <span><span className="text-white font-medium">{data.aggregate.cycles}</span> cycles</span>
            <span><span className="text-white font-medium">{data.aggregate.signals_tripped}</span> signals tripped</span>
            <span><span className="text-emerald-400 font-medium">{data.aggregate.spawns}</span> spawns</span>
            <span><span className="text-amber-400 font-medium">{data.aggregate.rejections.cooldown}</span> cooldown</span>
            <span><span className="text-red-400 font-medium">{data.aggregate.rejections.system_cap}</span> cap</span>
            <span>mttd p50: <span className="text-neutral-300 font-medium">{data.aggregate.mttd_p50_sec !== null ? `${data.aggregate.mttd_p50_sec.toFixed(1)}s` : '—'}</span></span>
            <span>mtti p50: <span className="text-neutral-300 font-medium">{data.aggregate.mtti_p50_sec != null ? `${data.aggregate.mtti_p50_sec.toFixed(1)}s` : '—'}</span></span>
            <span>recoveries: <span className="text-neutral-300 font-medium">{data.aggregate.recoveries.marginal} marginal / {data.aggregate.recoveries.confirmed} confirmed</span></span>
          </div>
          {data.per_class.length > 0 && (
            <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg divide-y divide-neutral-800/50 overflow-hidden">
              {data.per_class.map((c) => (
                <div key={c.regression_class} className="px-3 py-1.5 flex flex-wrap items-center gap-x-3 gap-y-0.5">
                  <span className="text-[10px] min-w-0 flex-1 overflow-hidden"><RegressionClassLabel cls={c.regression_class} /></span>
                  {c.last_seen_at && (
                    <span className="text-[10px] text-neutral-500">seen {timeAgo(c.last_seen_at)}</span>
                  )}
                  {c.in_cooldown_until && new Date(c.in_cooldown_until) > new Date() && (
                    <span className="text-[10px] text-amber-400/80">cooldown {timeAgo(c.in_cooldown_until)}</span>
                  )}
                  {c.attempts > 0 && (
                    <span className="text-[10px] text-neutral-600 font-mono">{c.attempts} attempts</span>
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
                  <td className="px-3 py-1.5 max-w-[160px] overflow-hidden"><RegressionClassLabel cls={row.regression_class} /></td>
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
  first_seen_at?: string | null;
  signal_first_crossed_at?: string | null;
  time_to_detect_sec: number | null;
  investigate_spawned_at?: string | null;
  time_to_investigate_sec?: number | null;
  escalation_reason: string | null;
  operator_acknowledged_at?: string | null;
  recurrence_count?: number;
  layer_summary?: LayerSummary | null;
  untargeted_layers?: string[];
  failure_screenshot_url?: string | null;
}

const ESCALATION_REASON_LABELS: Record<string, string> = {
  investigations_diverged: 'Diverged diagnoses',
  attempts_exhausted: 'Max attempts',
  stale_active: 'Stale (watchdog)',
  stale_merged: 'Stale (watchdog)',
  recovered_unattended: 'Auto-recovered (24h healthy)',
  escalated_cross_layer: 'Meta: cross-layer',
  escalated_layer_concentration: 'Meta: layer concentration',
  escalated_cross_case_fingerprint: 'Meta: cross-case fingerprint',
  escalated_recurrence: 'Meta: recurrence',
  recurring_class_meta: 'Meta: recurring cross-layer class',
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

interface RecoveryMismatch {
  type: string;
  evaluated_at: string;
  fix_layers: string[];
  symptom_layers: string[];
  unaddressed_layers: string[];
}

const LAYER_SHORT: Record<string, string> = { CI_WORKFLOW: 'CI', AGENT_VM: 'VM', AGENT_VERB: 'VERB', BASELINE_GATE: 'BASELINE', REVIEWER_PASS: 'REVIEWER', VIEWPORT: 'VIEWPORT' };

export function LayerCoverageHistogramPanel({ projectId }: Props) {
  const [coverage, setCoverage] = useState<LayerCoverageByClass | null>(null);
  const [canonicalLayers, setCanonicalLayers] = useState<CanonicalRegressionLayer[]>([]);
  const [recoveryMismatches, setRecoveryMismatches] = useState<Record<string, RecoveryMismatch[]>>({});

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      apiFetch(`/api/admin/regression-cases?project_id=${encodeURIComponent(projectId)}`)
        .then(async (res) => { if (!cancelled && res.ok) { const d = await res.json(); setCoverage(d.summary?.layer_coverage_by_class ?? null); setRecoveryMismatches(d.summary?.recovery_mismatches_by_class ?? {}); } })
        .catch(() => {}),
      fetchCanonicalRegressionLayers()
        .then((data) => { if (!cancelled) setCanonicalLayers(data.layers); })
        .catch(() => {}),
    ]);
    return () => { cancelled = true; };
  }, [projectId]);

  if (!coverage || Object.keys(coverage).length === 0) return null;

  const canonicalLayerNames = canonicalLayers.map(l => l.name);

  return (
    <section data-testid="layer-coverage-histogram-panel" className="mb-4">
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Layer Coverage by Class</h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg divide-y divide-neutral-800/30 overflow-hidden">
        {Object.entries(coverage).map(([cls, entry]) => {
          const targeted = new Set(entry.combinations.flatMap(c => c.layers));
          const missing = canonicalLayerNames.filter(l => !targeted.has(l));
          const underDefended = targeted.size > 0 && missing.length > 0;
          const noLayerCount = entry.combinations.find(c => c.layers.length === 0)?.count ?? 0;
          const top2 = entry.combinations.filter(c => c.layers.length > 0).slice(0, 2);
          const mismatches = recoveryMismatches[cls] ?? [];
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
                {mismatches.length > 0 && (
                  <span
                    data-testid="recovery-mismatch-badge"
                    className="text-[10px] px-1.5 py-0.5 rounded bg-orange-500/10 text-orange-300 border border-orange-500/20 whitespace-nowrap"
                    title={`${mismatches.length} recovery: fix layers didn't cover all symptom layers (unaddressed: ${[...new Set(mismatches.flatMap(m => m.unaddressed_layers))].map(l => LAYER_SHORT[l] ?? l).join(', ')})`}
                  >
                    {mismatches.length} recovery mismatch{mismatches.length !== 1 ? 'es' : ''}
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

/** Collapsible image disclosure for viewport_regression cases. Shows the saved
 * baseline PNG (written by viewport.ts when VITE_CAPTURE_BASELINES=true) and,
 * when available, the most-recent failure PNG side by side so the operator can
 * eyeball the diff without leaving the admin UI. */
function ViewportBaselineDisclosure({ regressionClass, failureScreenshotUrl }: {
  regressionClass: string;
  failureScreenshotUrl?: string | null;
}) {
  const [open, setOpen] = useState(false);
  const [missing, setMissing] = useState(false);
  const [failureMissing, setFailureMissing] = useState(false);

  // regression_class format: "viewport_regression:ComponentName@375" or
  // "viewport_regression:ComponentName:assertion_type@375"
  const inner = regressionClass.slice('viewport_regression:'.length);
  const { component, width = '375' } = parseViewportSubkey(inner);
  // viewport.ts strips ".viewport.test.tsx" suffix when naming the file
  const stem = component.replace(/\.viewport\.test\.tsx$/i, '');
  const baselineUrl = `/viewport-baselines/${stem}-${width}px.png`;

  return (
    <div className="mt-1.5">
      <button type="button" onClick={() => setOpen(v => !v)}
        className="flex items-center gap-1 text-[10px] text-violet-400 hover:text-violet-300 transition-colors">
        {open ? <ChevronDown className="w-3 h-3" /> : <ChevronRight className="w-3 h-3" />}
        images
      </button>
      {open && (
        <div className="mt-1 flex gap-4 flex-wrap">
          <div className="space-y-1">
            <div className="text-[9px] text-neutral-500 font-medium uppercase tracking-wide">Baseline (passing at {width}px)</div>
            {missing ? (
              <div className="text-[9px] text-neutral-600 italic">
                no baseline yet — captured on main when VITE_CAPTURE_BASELINES=true
              </div>
            ) : (
              <img src={baselineUrl} alt={`${stem} baseline at ${width}px`}
                style={{ maxWidth: `${width}px` }}
                className="rounded border border-neutral-700/50 max-w-full"
                onError={() => setMissing(true)} />
            )}
          </div>
          {failureScreenshotUrl && !failureMissing && (
            <div className="space-y-1">
              <div className="text-[9px] text-red-400 font-medium uppercase tracking-wide">Latest failure</div>
              <img src={failureScreenshotUrl} alt={`${stem} failure at ${width}px`}
                style={{ maxWidth: `${width}px` }}
                className="rounded border border-red-900/40 max-w-full"
                onError={() => setFailureMissing(true)} />
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function recurrenceBadge(count: number | undefined) {
  return count && count > 0 ? (
    <span className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-500/10 text-neutral-400 border border-neutral-500/20">
      recurred {count}x
    </span>
  ) : null;
}

function untargetedLayersBadge(c: ManualInterventionCase) {
  const layers = c.untargeted_layers ?? [];
  // Suppress when nothing is untargeted (full coverage) or everything is
  // untargeted (brand-new case with no fix history) — both states are noisy.
  if (layers.length === 0 || layers.length >= CANONICAL_LAYER_NAMES.length) return null;
  const labels = layers.map(l => LAYER_SHORT[l] ?? l);
  return (
    <span
      data-testid="untargeted-layers-badge"
      className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-500/10 text-neutral-400 border border-neutral-500/20"
      title={`Layers no prior fix has targeted: ${layers.join(', ')}`}
    >
      never defended in: {labels.join(', ')}
    </span>
  );
}

function layerMismatchBadge(c: ManualInterventionCase) {
  if (!c.layer_summary?.layer_mismatch) return null;
  const fmt = (layers: string[] | null | undefined) =>
    (layers ?? []).map(l => LAYER_SHORT[l] ?? l).join(', ') || '?';
  const tip = `prior fixes targeted ${fmt(c.layer_summary.prior_fix_layers)}; current symptoms on ${fmt(c.layer_summary.current_symptom_layers)}`;
  return (
    <span
      data-testid="layer-mismatch-badge"
      className="text-[10px] px-1.5 py-0.5 rounded bg-orange-500/10 text-orange-300 border border-orange-500/20"
      title={tip}
    >
      layer mismatch
    </span>
  );
}

export function ManualInterventionPanel({ projectId, filterClass }: Props) {
  const [cases, setCases] = useState<ManualInterventionCase[]>([]);
  const [loaded, setLoaded] = useState(false);
  const [clearing, setClearing] = useState<string | null>(null);
  const [acknowledging, setAcknowledging] = useState<string | null>(null);
  const [ackedAt, setAckedAt] = useState<Record<string, string>>({});
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

  const handleAcknowledge = useCallback(async (caseId: string) => {
    setAcknowledging(caseId);
    try {
      const res = await apiFetch(
        `/api/admin/regression-cases/${encodeURIComponent(caseId)}/acknowledge?project_id=${encodeURIComponent(projectId)}`,
        { method: 'POST' }
      );
      if (res.ok) {
        const body = await res.json();
        const ts: string = body.operator_acknowledged_at ?? new Date().toISOString();
        setAckedAt(prev => ({ ...prev, [caseId]: ts }));
      }
    } catch { /* silent */ }
    setAcknowledging(null);
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
                    <span className="text-[10px] break-all min-w-0">
                      <RegressionClassLabel cls={c.regression_class} />
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
                    {layerMismatchBadge(c)}
                    {untargetedLayersBadge(c)}
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
                    {(() => {
                      const refAt = c.signal_first_seen_at ?? c.first_seen_at ?? null;
                      const investigateDuration = c.investigate_spawned_at
                        ? formatTimeToInvestigate(refAt, c.investigate_spawned_at)
                        : 'pending';
                      const isAmber = !c.investigate_spawned_at && refAt != null
                        && (Date.now() - parseTimestamp(refAt).getTime()) > 2 * 60 * 60 * 1000;
                      return (
                        <div className="mt-1 flex flex-wrap items-center gap-1 text-[9px]" data-testid="active-phase-timeline">
                          <PhaseChip label="detect→" duration={formatTimeToDetect(refAt, c.signal_first_crossed_at)} />
                          <span className="text-neutral-800">·</span>
                          <PhaseChip
                            label="investigate→"
                            duration={investigateDuration}
                            tone={isAmber ? 'amber' : undefined}
                          />
                        </div>
                      );
                    })()}
                    {c.regression_class.startsWith('viewport_regression:') && (
                      <ViewportBaselineDisclosure regressionClass={c.regression_class} failureScreenshotUrl={c.failure_screenshot_url} />
                    )}
                  </div>
                </div>
                <div className="shrink-0 flex flex-col gap-1">
                  {(() => {
                    const acked = ackedAt[c.id] ?? c.operator_acknowledged_at ?? null;
                    return acked ? (
                      <span className="flex items-center gap-1 text-[10px] px-2 py-1 text-emerald-400" title={`Acknowledged at ${acked}`}>
                        <Shield className="w-3 h-3" />
                        Acknowledged
                      </span>
                    ) : (
                      <button
                        onClick={() => handleAcknowledge(c.id)}
                        disabled={acknowledging === c.id}
                        className="flex items-center gap-1.5 text-[10px] px-2 py-1 min-h-11 rounded border border-neutral-700 text-neutral-300 hover:text-white hover:border-neutral-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                        title="Acknowledge — stamps operator review timestamp for MTTI metric"
                      >
                        <Shield className={`w-3 h-3 ${acknowledging === c.id ? 'animate-pulse' : ''}`} />
                        Acknowledge
                      </button>
                    );
                  })()}
                  <button
                    onClick={() => handleClear(c.regression_class)}
                    disabled={clearing === c.regression_class}
                    className="flex items-center gap-1.5 text-[10px] px-2 py-1 min-h-11 rounded border border-neutral-700 text-neutral-300 hover:text-white hover:border-neutral-500 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
                    title="Clear manual intervention and re-arm autonomous remediation"
                  >
                    <RotateCcw className={`w-3 h-3 ${clearing === c.regression_class ? 'animate-spin' : ''}`} />
                    Clear &amp; re-arm
                  </button>
                </div>
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
  avg_hold_days?: number | null;
  still_holding_count?: number;
  held_gt_7d_count?: number;
  retripped_within_48h_count?: number;
  single_layer_fix_count?: number;
  single_layer_avg_hold_days?: number | null;
  multi_layer_fix_count?: number;
  multi_layer_avg_hold_days?: number | null;
}

function isFragileLayer(row: LayerDefenseEntry): boolean {
  return (
    (row.avg_hold_days != null && row.avg_hold_days < 7) ||
    (row.retripped_within_48h_count ?? 0) >= 2
  );
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
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        {!data ? (
          <div className="p-3 text-neutral-600 text-[10px]">No data</div>
        ) : (
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
              {data.layers.map((row) => {
                const fragile = isFragileLayer(row);
                const hasFragStats = row.fix_count > 0 && (
                  row.avg_hold_days != null ||
                  (row.still_holding_count ?? 0) > 0 ||
                  (row.held_gt_7d_count ?? 0) > 0 ||
                  (row.retripped_within_48h_count ?? 0) > 0
                );
                return (
                  <Fragment key={row.layer}>
                    <tr className={fragile ? 'bg-amber-950/20' : ''}>
                      <td className={`px-3 py-1.5 font-mono truncate max-w-[120px] ${fragile ? 'text-amber-300' : row.fix_count === 0 ? 'text-neutral-600' : 'text-neutral-300'}`}>
                        {row.layer}
                      </td>
                      <td className="px-2 py-1.5 text-right">
                        <span className={row.fix_count === 0 ? 'text-neutral-600' : 'text-white font-medium'}>
                          {row.fix_count === 0 ? '—' : row.fix_count}
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
                    {hasFragStats && (
                      <tr>
                        <td colSpan={4} className="px-3 pb-1.5 text-[9px] text-neutral-500">
                          <span className="flex flex-wrap gap-x-2">
                            {row.avg_hold_days != null && <span>avg hold {row.avg_hold_days.toFixed(1)}d</span>}
                            {(row.still_holding_count ?? 0) > 0 && <span>{row.still_holding_count} holding</span>}
                            {(row.held_gt_7d_count ?? 0) > 0 && <span>{row.held_gt_7d_count} &gt;7d</span>}
                            {(row.retripped_within_48h_count ?? 0) > 0 && (
                              <span className="text-amber-400">{row.retripped_within_48h_count} re-tripped &lt;48h</span>
                            )}
                            {(row.single_layer_avg_hold_days != null || row.multi_layer_avg_hold_days != null) && (
                              <span className="text-neutral-400">
                                single {row.single_layer_avg_hold_days != null ? `${row.single_layer_avg_hold_days.toFixed(1)}d` : '—'}
                                {' / '}
                                multi {row.multi_layer_avg_hold_days != null ? `${row.multi_layer_avg_hold_days.toFixed(1)}d` : '—'}
                              </span>
                            )}
                          </span>
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
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
        <span className="text-[10px] flex-1 min-w-0 overflow-hidden"><RegressionClassLabel cls={row.regression_class} /></span>
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
        <div className="border-t border-neutral-800/50 pt-1 space-y-0.5">
          {row.active_cases.map((ac) => {
            const p50Total = totalP50 > 0 ? totalP50 : null;
            const elapsed = ac.live_elapsed_sec;
            const isStalled = p50Total !== null && elapsed !== null && elapsed > p50Total * 1.5;
            return (
              <div key={ac.case_id} className="flex items-center gap-2 flex-wrap text-[10px]">
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
            );
          })}
        </div>
      )}
    </div>
  );
}

export function MttaTimelinePanel({ projectId }: Props) {
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
        <table className="w-full text-[10px]">
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
                  <span className="text-[10px] text-neutral-500"><RegressionClassLabel cls={c.regression_class} /></span>
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
                  {layerMismatchBadge(c)}
                  {untargetedLayersBadge(c)}
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
