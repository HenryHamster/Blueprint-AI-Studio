import { useState, useEffect, useRef } from 'react';
import { apiFetch } from '../api';
import { parseTimestamp } from '../utils/time';

interface RecentInvestigation {
  task_id: string;
  direction_id?: string | null;
  summary?: string | null;
  confidence?: string | null;
  severity?: string | null;
  completed_at?: string | null;
}

interface ActiveInvestigationDetail {
  task_id: string;
  direction_id?: string | null;
  elapsed_hours?: number | null;
}

interface RemediationFunnelData {
  investigations_spawned?: number;
  investigations_completed?: number;
  investigations_with_diagnosis?: number;
  remediations_spawned?: number;
  remediations_merged?: number;
  remediations_failed?: number;
  fixes_held?: number;
  avg_time_to_remediation_hours?: number | null;
  active_investigations?: number;
  investigations_by_confidence?: Record<string, number> | null;
  failed_remediations_by_class?: Record<string, number> | null;
  merged_remediations_by_trigger_class?: Record<string, number> | null;
  cumulative_merged_by_trigger_class?: Record<string, number> | null;
  investigations_spawned_by_class?: Record<string, number> | null;
  investigations_completed_by_class?: Record<string, number> | null;
  remediations_spawned_by_class?: Record<string, number> | null;
  remediation_success_by_confidence?: Record<string, { merged: number; failed: number }> | null;
  diagnosis_drop_off_by_reason?: Record<string, number> | null;
  ci_auto_remediation?: {
    spawned?: number;
    merged?: number;
    failed?: number;
    by_ci_failure_type?: Record<string, { spawned?: number; merged?: number; failed?: number }>;
  } | null;
  residual_remediation_funnel?: {
    spawned?: number;
    merged?: number;
    failed?: number;
    remediating?: number;
    open_cases_by_error_class?: {
      error_class: string;
      count: number;
      most_recent_at: string | null;
      cases: {
        fingerprint: string;
        status: string;
        hit_count: number;
        last_seen_at: string | null;
        last_spawned_at?: string | null;
        sample_message?: string | null;
        consecutive_failure_count?: number | null;
      }[];
    }[];
  } | null;
  prior_investigation_reuse?: {
    total_with_prior?: number;
    merged_with_prior?: number;
    failed_with_prior?: number;
    merge_rate?: number | null;
  } | null;
  recent_investigations?: RecentInvestigation[] | null;
  active_investigations_detail?: ActiveInvestigationDetail[] | null;
}

interface InvestigationConversion {
  total_completed?: number;
  parseable?: number;
  unparseable?: number;
  structural?: number;
  low_confidence?: number;
  actionable?: number;
  remediation_spawned?: number;
}

interface ResidualFingerprint {
  fingerprint: string;
  count: number;
  most_recent_at: string | null;
  sample_message: string;
  example_task_ids: string[];
  case_status?: string | null;
  last_spawned_pr_number?: number | null;
  consecutive_failure_count?: number | null;
  suspended_at?: string | null;
  audit_defer_count?: number | null;
  audit_deferred_system_id?: string | null;
}

interface ResidualFingerprintsData {
  window_hours: number;
  total_code_error_count: number;
  residual_count: number;
  top_residuals: ResidualFingerprint[];
}

export interface RemediationFunnelSectionProps {
  funnel?: RemediationFunnelData | null;
  conversion?: InvestigationConversion | null;
  loading?: boolean;
  projectId?: string | null;
  onViewTask?: (taskId: string) => void;
}

const CASE_STATUS_STYLES: Record<string, string> = {
  REMEDIATING: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  SUSPENDED: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  IGNORED: 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30',
  MERGED: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  FAILED: 'bg-red-500/20 text-red-400 border-red-500/30',
  ACTIVE: 'bg-blue-500/20 text-blue-400 border-blue-500/30',
};

function caseStatusLabel(r: ResidualFingerprint): string | null {
  if (!r.case_status) return null;
  if (r.case_status === 'REMEDIATING' && r.last_spawned_pr_number) return `REMEDIATING #${r.last_spawned_pr_number}`;
  if (r.case_status === 'SUSPENDED' && r.consecutive_failure_count) return `SUSPENDED ×${r.consecutive_failure_count}`;
  return r.case_status;
}

const FAILURE_CLASS_COLORS: Record<string, string> = {
  CODE_ERROR: 'bg-red-500/20 text-red-400 border-red-500/30',
  PROVISION_FAILURE: 'bg-orange-500/20 text-orange-400 border-orange-500/30',
  AGENT_TIMEOUT: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  RATE_LIMIT: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/30',
  EXTERNAL_API: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  SCOPE_OVERFLOW: 'bg-teal-500/20 text-teal-400 border-teal-500/30',
};

const DROP_OFF_LABELS: Record<string, string> = {
  structural: 'structural',
  no_recommended_fix: 'no fix',
  low_confidence: 'low conf',
  no_direction: 'no direction',
};

const CONFIDENCE_ORDER = ['high', 'medium', 'low', 'unknown'] as const;

function pct(numerator: number, denominator: number): number {
  if (denominator <= 0) return 0;
  return Math.round((numerator / denominator) * 100);
}

function classBadge(cls: string): string {
  return FAILURE_CLASS_COLORS[cls] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30';
}

const CONFIDENCE_BADGE: Record<string, string> = {
  high: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
  medium: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  low: 'bg-red-500/20 text-red-400 border-red-500/30',
  unknown: 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30',
};

function confidenceBadge(conf?: string | null): string {
  return CONFIDENCE_BADGE[conf ?? 'unknown'] ?? CONFIDENCE_BADGE.unknown;
}

function relativeTime(iso?: string | null): string {
  if (!iso) return '';
  const then = parseTimestamp(iso).getTime();
  if (Number.isNaN(then)) return '';
  const diffSec = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  return `${diffDay}d ago`;
}

function truncate(s: string, n: number): string {
  if (s.length <= n) return s;
  return s.slice(0, n).trimEnd() + '…';
}

export function RemediationFunnelSection({ funnel, conversion, loading, projectId, onViewTask }: RemediationFunnelSectionProps) {
  const [investigationsOpen, setInvestigationsOpen] = useState(false);
  const [residuals, setResiduals] = useState<ResidualFingerprintsData | null>(null);
  const [residualsOpen, setResidualsOpen] = useState(false);
  const firstOverdueRef = useRef<HTMLDivElement>(null);
  const [ignoredFingerprints, setIgnoredFingerprints] = useState<Set<string>>(new Set());
  const [undoFingerprints, setUndoFingerprints] = useState<Map<string, ResidualFingerprint>>(new Map());
  const [ignoreErrors, setIgnoreErrors] = useState<Map<string, string>>(new Map());
  const [showIgnored, setShowIgnored] = useState(true);
  const [unsuspendedFingerprints, setUnsuspendedFingerprints] = useState<Set<string>>(new Set());
  const [unsuspendErrors, setUnsuspendErrors] = useState<Map<string, string>>(new Map());

  function handleIgnore(fp: ResidualFingerprint) {
    const fingerprint = fp.fingerprint;
    setIgnoredFingerprints(prev => new Set([...prev, fingerprint]));
    setUndoFingerprints(prev => new Map([...prev, [fingerprint, fp]]));
    setIgnoreErrors(prev => { const m = new Map(prev); m.delete(fingerprint); return m; });

    const timer = setTimeout(() => {
      setUndoFingerprints(prev => { const m = new Map(prev); m.delete(fingerprint); return m; });
    }, 5000);

    apiFetch(`/api/admin/residual-cases/${encodeURIComponent(fingerprint)}/ignore`, { method: 'POST' })
      .then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
      })
      .catch((err: unknown) => {
        clearTimeout(timer);
        setIgnoredFingerprints(prev => { const s = new Set(prev); s.delete(fingerprint); return s; });
        setUndoFingerprints(prev => { const m = new Map(prev); m.delete(fingerprint); return m; });
        setIgnoreErrors(prev => new Map([...prev, [fingerprint, err instanceof Error ? err.message : 'error']]));
      });
  }

  function handleUnsuspend(fp: ResidualFingerprint) {
    const fingerprint = fp.fingerprint;
    setUnsuspendedFingerprints(prev => new Set([...prev, fingerprint]));
    setUnsuspendErrors(prev => { const m = new Map(prev); m.delete(fingerprint); return m; });

    const qs = projectId ? `?project_id=${encodeURIComponent(projectId)}` : '';
    apiFetch(`/api/admin/residual-cases/${encodeURIComponent(fingerprint)}/unsuspend${qs}`, { method: 'POST' })
      .then(r => {
        if (!r.ok) throw new Error(`${r.status}`);
      })
      .catch((err: unknown) => {
        setUnsuspendedFingerprints(prev => { const s = new Set(prev); s.delete(fingerprint); return s; });
        setUnsuspendErrors(prev => new Map([...prev, [fingerprint, err instanceof Error ? err.message : 'error']]));
      });
  }

  function handleUndo(fingerprint: string) {
    setIgnoredFingerprints(prev => { const s = new Set(prev); s.delete(fingerprint); return s; });
    setUndoFingerprints(prev => { const m = new Map(prev); m.delete(fingerprint); return m; });
  }

  useEffect(() => {
    let cancelled = false;
    const qs = projectId ? `?hours=24&top_n=10&project_id=${encodeURIComponent(projectId)}` : '?hours=24&top_n=10';
    apiFetch(`/api/admin/residual-fingerprints${qs}`)
      .then(r => r.ok ? r.json() : null)
      .then((data: ResidualFingerprintsData | null) => { if (!cancelled && data) setResiduals(data); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [projectId]);

  if (loading) {
    return (
      <section>
        <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Remediation Funnel</h2>
        <div className="animate-pulse bg-workbench-panel/60 rounded h-24 w-full" />
      </section>
    );
  }

  if (!funnel) return null;

  const spawned = funnel.investigations_spawned ?? 0;
  if (spawned === 0) return null;

  const completed = funnel.investigations_completed ?? 0;
  const diagnosed = funnel.investigations_with_diagnosis ?? 0;
  const remSpawned = funnel.remediations_spawned ?? 0;
  const remMerged = funnel.remediations_merged ?? 0;
  const remFailed = funnel.remediations_failed ?? 0;
  const fixesHeld = funnel.fixes_held ?? 0;
  const active = funnel.active_investigations ?? 0;
  const avgHours = funnel.avg_time_to_remediation_hours ?? null;
  const OVERDUE_HOURS = 2;
  const overdueCount = (funnel.active_investigations_detail ?? []).filter(a => (a.elapsed_hours ?? 0) > OVERDUE_HOURS).length;

  const stages: { key: string; label: string; count: number; prev: number | null }[] = [
    { key: 'spawned', label: 'Spawned', count: spawned, prev: null },
    { key: 'completed', label: 'Completed', count: completed, prev: spawned },
    { key: 'diagnosed', label: 'Diagnosed', count: diagnosed, prev: completed },
    { key: 'remediations', label: 'Remediations', count: remSpawned, prev: diagnosed },
    { key: 'merged', label: 'Merged', count: remMerged, prev: remSpawned },
  ];

  const classKeys = Array.from(new Set([
    ...Object.keys(funnel.investigations_spawned_by_class ?? {}),
    ...Object.keys(funnel.investigations_completed_by_class ?? {}),
    ...Object.keys(funnel.remediations_spawned_by_class ?? {}),
    ...Object.keys(funnel.merged_remediations_by_trigger_class ?? {}),
    ...Object.keys(funnel.failed_remediations_by_class ?? {}),
  ])).sort((a, b) => {
    const av = (funnel.investigations_spawned_by_class?.[a] ?? 0);
    const bv = (funnel.investigations_spawned_by_class?.[b] ?? 0);
    return bv - av;
  });

  const dropOff = Object.entries(funnel.diagnosis_drop_off_by_reason ?? {})
    .filter(([, v]) => v > 0)
    .sort((a, b) => b[1] - a[1]);

  const confDist = Object.entries(funnel.investigations_by_confidence ?? {})
    .filter(([, v]) => v > 0);

  const successByConf = Object.entries(funnel.remediation_success_by_confidence ?? {})
    .filter(([, v]) => (v.merged + v.failed) > 0)
    .sort((a, b) => {
      const ai = CONFIDENCE_ORDER.indexOf(a[0] as typeof CONFIDENCE_ORDER[number]);
      const bi = CONFIDENCE_ORDER.indexOf(b[0] as typeof CONFIDENCE_ORDER[number]);
      return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
    });

  const ciRem = funnel.ci_auto_remediation ?? null;
  const ciSpawned = ciRem?.spawned ?? 0;
  const ciMerged = ciRem?.merged ?? 0;
  const ciFailed = ciRem?.failed ?? 0;
  const ciResolved = ciMerged + ciFailed;
  const ciSuccessRate = ciResolved > 0 ? pct(ciMerged, ciResolved) : null;
  const ciTypeEntries = Object.entries(ciRem?.by_ci_failure_type ?? {})
    .filter(([, v]) => (v.spawned ?? 0) > 0)
    .sort((a, b) => (b[1].spawned ?? 0) - (a[1].spawned ?? 0));

  const rrFunnel = funnel.residual_remediation_funnel ?? null;
  const rrSpawned = rrFunnel?.spawned ?? 0;
  const rrMerged = rrFunnel?.merged ?? 0;
  const rrFailed = rrFunnel?.failed ?? 0;
  const rrRemediating = rrFunnel?.remediating ?? 0;
  const rrResolved = rrMerged + rrFailed;

  const reuse = funnel.prior_investigation_reuse ?? null;
  const reuseMergeRate = reuse?.merge_rate != null ? Math.round(reuse.merge_rate * 100) : null;
  const reuseTotal = reuse?.total_with_prior ?? 0;

  const structuralFilterPassRate = conversion && (conversion.parseable ?? 0) > 0
    ? pct((conversion.parseable ?? 0) - (conversion.structural ?? 0), conversion.parseable ?? 0)
    : null;

  return (
    <section>
      <div className="flex flex-wrap items-center gap-2 mb-2 min-w-0">
        <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest">Remediation Funnel</h2>
        {active > 0 && (
          <span className="text-ui-sm text-amber-400" title="Investigations currently in flight">
            {active} active
          </span>
        )}
        {avgHours != null && (
          <span className="text-ui-sm text-neutral-500" title="Average time from investigation spawn to remediation merge">
            ~{avgHours.toFixed(1)}h avg time to remediation
          </span>
        )}
        {remFailed > 0 && (
          <span className="text-ui-sm text-red-400" title="Remediations that failed">
            {remFailed} failed
          </span>
        )}
        {fixesHeld > 0 && (
          <span
            className="text-ui-sm text-emerald-400"
            title="Merged remediations whose error fingerprint did not recur during the 30-day monitoring window"
          >
            {fixesHeld} held
          </span>
        )}
        {overdueCount > 0 && (
          <button
            data-viewport-allow-small-target
            type="button"
            onClick={() => {
              setInvestigationsOpen(true);
              setTimeout(() => firstOverdueRef.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' }), 50);
            }}
            className="text-ui-sm font-semibold px-1.5 py-0.5 rounded border bg-amber-500/20 text-amber-400 border-amber-500/30 hover:bg-amber-500/30 transition-colors"
            title={`${overdueCount} investigation${overdueCount > 1 ? 's' : ''} running longer than ${OVERDUE_HOURS}h`}
          >
            {overdueCount} overdue
          </button>
        )}
      </div>

      {(() => {
        const deferred = (residuals?.top_residuals ?? []).filter(r => (r.audit_defer_count ?? 0) > 0);
        if (deferred.length === 0) return null;
        const systemIds = Array.from(new Set(deferred.map(r => r.audit_deferred_system_id).filter(Boolean) as string[]));
        const sysLabel = systemIds.length > 0 && systemIds.length <= 3
          ? ` on systems ${systemIds.join(', ')}`
          : '';
        return (
          <div className="mb-2 text-ui-sm">
            <span className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded border bg-amber-500/20 text-amber-400 border-amber-500/30">
              {deferred.length} {deferred.length === 1 ? 'case' : 'cases'} audit-deferred{sysLabel}
            </span>
          </div>
        );
      })()}

      {/* Horizontal funnel */}
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3 mb-2">
        <div className="flex items-stretch gap-1 overflow-x-auto" data-viewport-allow-overflow>
          {stages.map((s, i) => {
            const conv = s.prev != null && s.prev > 0 ? pct(s.count, s.prev) : null;
            return (
              <div key={s.key} className="flex items-center gap-1 shrink-0">
                {i > 0 && (
                  <span className="text-ui-sm font-mono text-neutral-500" title={`${s.count} / ${s.prev} = ${conv}%`}>
                    &rarr; {conv}%
                  </span>
                )}
                <div className="min-w-[86px] px-2 py-1.5 rounded border border-neutral-700 bg-workbench-panel/40 text-center">
                  <div className="text-ui-xs uppercase tracking-wide text-neutral-500">{s.label}</div>
                  <div className="text-sm font-bold text-neutral-100">{s.count}</div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Per-failure-class breakdown */}
      {classKeys.length > 0 && (() => {
        const bottleneckCandidates = classKeys
          .map(cls => {
            const r = funnel.remediations_spawned_by_class?.[cls] ?? 0;
            const m = funnel.merged_remediations_by_trigger_class?.[cls] ?? 0;
            return { cls, r, rate: r > 0 ? m / r : null };
          })
          .filter(x => x.r >= 2 && x.rate != null);
        const bottleneckCls = bottleneckCandidates.length > 0
          ? bottleneckCandidates.reduce((a, b) => ((a.rate ?? 1) <= (b.rate ?? 1) ? a : b)).cls
          : null;
        return (
        <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3 mb-2">
          <div className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1.5">By failure class</div>
          {/* Mobile: stacked cards, one per failure class */}
          <div className="md:hidden space-y-2">
            {classKeys.map(cls => {
              const s = funnel.investigations_spawned_by_class?.[cls] ?? 0;
              const c = funnel.investigations_completed_by_class?.[cls] ?? 0;
              const r = funnel.remediations_spawned_by_class?.[cls] ?? 0;
              const m = funnel.merged_remediations_by_trigger_class?.[cls] ?? 0;
              const f = funnel.failed_remediations_by_class?.[cls] ?? 0;
              const cumM = funnel.cumulative_merged_by_trigger_class?.[cls] ?? 0;
              const complRate = s > 0 ? pct(c, s) : null;
              const mergeRate = r > 0 ? pct(m, r) : null;
              const rateColor = (v: number) => v >= 60 ? 'text-emerald-400' : v >= 30 ? 'text-amber-400' : 'text-red-400';
              const isBottleneck = cls === bottleneckCls;
              return (
                <div
                  key={cls}
                  className={`rounded border border-neutral-800/50 p-2 ${isBottleneck ? 'border-l-2 border-red-500/60 bg-red-500/5' : ''}`}
                  title={isBottleneck ? 'Lowest merge rate among classes with ≥2 remediations (bottleneck)' : undefined}
                >
                  <div className="flex items-center justify-between mb-1.5">
                    <span className={`text-ui-xs font-semibold px-1.5 py-0.5 rounded border ${classBadge(cls)}`}>{cls}</span>
                    {isBottleneck && <span className="text-ui-xs text-red-400 font-semibold">bottleneck</span>}
                  </div>
                  <div className="grid grid-cols-2 gap-x-2 gap-y-0.5 text-ui-md">
                    <span className="text-neutral-600">Spawned</span>
                    <span className="font-mono text-neutral-300 text-right">{s}</span>
                    <span className="text-neutral-600">Completed</span>
                    <span className="font-mono text-neutral-300 text-right">{c}</span>
                    <span className="text-neutral-600">Compl%</span>
                    <span className={`font-mono text-right ${complRate == null ? 'text-neutral-600' : rateColor(complRate)}`}>
                      {complRate == null ? '—' : `${complRate}%`}
                    </span>
                    <span className="text-neutral-600">Remediated</span>
                    <span className="font-mono text-neutral-300 text-right">{r}</span>
                    <span className="text-neutral-600">Merged</span>
                    <span className="font-mono text-emerald-400 text-right">{m}</span>
                    <span className="text-neutral-600">Failed</span>
                    <span className="font-mono text-red-400 text-right">{f}</span>
                    <span className="text-neutral-600">Merge%</span>
                    <span className={`font-mono text-right ${mergeRate == null ? 'text-neutral-600' : rateColor(mergeRate)}`}>
                      {mergeRate == null ? '—' : `${mergeRate}%`}
                    </span>
                    <span className="text-neutral-600">All-time</span>
                    <span className="font-mono text-neutral-400 text-right" title="Cumulative all-time merged">{cumM}</span>
                  </div>
                </div>
              );
            })}
          </div>
          {/* Desktop: table grid layout */}
          <div className="hidden md:block space-y-1">
            <div className="grid grid-cols-[minmax(110px,1fr)_repeat(8,minmax(44px,1fr))] gap-2 text-ui-xs uppercase tracking-wide text-neutral-600">
              <span>Class</span>
              <span className="text-right">Spawned</span>
              <span className="text-right">Completed</span>
              <span className="text-right" title="Completed / Spawned">Compl%</span>
              <span className="text-right">Remediated</span>
              <span className="text-right">Merged</span>
              <span className="text-right">Failed</span>
              <span className="text-right" title="Merged / Remediated">Merge%</span>
              <span className="text-right" title="All-time cumulative merged remediations">All-time</span>
            </div>
            {classKeys.map(cls => {
              const s = funnel.investigations_spawned_by_class?.[cls] ?? 0;
              const c = funnel.investigations_completed_by_class?.[cls] ?? 0;
              const r = funnel.remediations_spawned_by_class?.[cls] ?? 0;
              const m = funnel.merged_remediations_by_trigger_class?.[cls] ?? 0;
              const f = funnel.failed_remediations_by_class?.[cls] ?? 0;
              const cumM = funnel.cumulative_merged_by_trigger_class?.[cls] ?? 0;
              const complRate = s > 0 ? pct(c, s) : null;
              const mergeRate = r > 0 ? pct(m, r) : null;
              const rateColor = (v: number) => v >= 60 ? 'text-emerald-400' : v >= 30 ? 'text-amber-400' : 'text-red-400';
              const isBottleneck = cls === bottleneckCls;
              return (
                <div
                  key={cls}
                  className={`grid grid-cols-[minmax(110px,1fr)_repeat(8,minmax(44px,1fr))] gap-2 text-ui-md items-center ${isBottleneck ? 'border-l-2 border-red-500/60 bg-red-500/5 pl-1 rounded' : ''}`}
                  title={isBottleneck ? 'Lowest merge rate among classes with ≥2 remediations (bottleneck)' : undefined}
                >
                  <span className={`inline-flex justify-center text-ui-xs font-semibold px-1.5 py-0.5 rounded border ${classBadge(cls)}`}>
                    {cls}
                  </span>
                  <span className="text-right font-mono text-neutral-300">{s}</span>
                  <span className="text-right font-mono text-neutral-300">{c}</span>
                  <span className={`text-right font-mono ${complRate == null ? 'text-neutral-600' : rateColor(complRate)}`}>
                    {complRate == null ? '—' : `${complRate}%`}
                  </span>
                  <span className="text-right font-mono text-neutral-300">{r}</span>
                  <span className="text-right font-mono text-emerald-400">{m}</span>
                  <span className="text-right font-mono text-red-400">{f}</span>
                  <span className={`text-right font-mono ${mergeRate == null ? 'text-neutral-600' : rateColor(mergeRate)}`}>
                    {mergeRate == null ? '—' : `${mergeRate}%`}
                  </span>
                  <span className="text-right font-mono text-neutral-400" title="Cumulative all-time merged">{cumM}</span>
                </div>
              );
            })}
          </div>
        </div>
        );
      })()}

      {/* Drop-off + confidence */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
        {(dropOff.length > 0 || structuralFilterPassRate != null || reuseTotal > 0) && (
          <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3">
            <div className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1.5">Diagnosis drop-off</div>
            {structuralFilterPassRate != null && (
              <div className="flex justify-between text-ui-md text-neutral-400 mb-1">
                <span>structural filter pass</span>
                <span className="font-mono text-neutral-200">{structuralFilterPassRate}%</span>
              </div>
            )}
            {reuseTotal > 0 && (
              <div
                className="flex justify-between text-ui-md text-neutral-400 mb-1"
                title="Merge rate of auto-remediations that reused a prior diagnosis"
              >
                <span>diagnosis reuse</span>
                <span className="font-mono text-neutral-200">
                  {reuse?.merged_with_prior ?? 0}/{reuseTotal}
                  {reuseMergeRate != null && <span className="text-neutral-500"> ({reuseMergeRate}%)</span>}
                </span>
              </div>
            )}
            <div className="space-y-0.5">
              {dropOff.map(([reason, count]) => (
                <div key={reason} className="flex justify-between text-ui-md text-neutral-400">
                  <span>{DROP_OFF_LABELS[reason] ?? reason}</span>
                  <span className="font-mono text-neutral-300">{count}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {confDist.length > 0 && (
          <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3">
            <div className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1.5">Confidence distribution</div>
            <div className="space-y-0.5">
              {confDist
                .sort((a, b) => {
                  const ai = CONFIDENCE_ORDER.indexOf(a[0] as typeof CONFIDENCE_ORDER[number]);
                  const bi = CONFIDENCE_ORDER.indexOf(b[0] as typeof CONFIDENCE_ORDER[number]);
                  return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
                })
                .map(([conf, count]) => (
                  <div key={conf} className="flex justify-between text-ui-md text-neutral-400">
                    <span>{conf}</span>
                    <span className="font-mono text-neutral-300">{count}</span>
                  </div>
                ))}
            </div>
          </div>
        )}

        {ciSpawned > 0 && (
          <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3">
            <div className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1.5">CI auto-remediation</div>
            <div className="flex justify-between text-ui-md text-neutral-400 mb-1">
              <span>spawned</span>
              <span className="font-mono text-neutral-200">{ciSpawned}</span>
            </div>
            <div className="flex justify-between text-ui-md text-neutral-400 mb-1">
              <span>merged / failed</span>
              <span>
                <span className="font-mono text-emerald-400">{ciMerged}</span>
                <span className="text-neutral-600"> / </span>
                <span className="font-mono text-red-400">{ciFailed}</span>
                {ciSuccessRate != null && (
                  <span className="font-mono text-neutral-500"> ({ciSuccessRate}%)</span>
                )}
              </span>
            </div>
            {ciTypeEntries.length > 0 && (
              <div className="mt-1.5 pt-1.5 border-t border-neutral-800/60 space-y-0.5">
                {ciTypeEntries.map(([type, v]) => (
                  <div key={type} className="flex justify-between text-ui-sm text-neutral-500">
                    <span className="truncate pr-2">{type}</span>
                    <span className="font-mono">
                      <span className="text-neutral-400">{v.spawned ?? 0}</span>
                      <span className="text-neutral-700"> · </span>
                      <span className="text-emerald-400">{v.merged ?? 0}</span>
                      <span className="text-neutral-700">/</span>
                      <span className="text-red-400">{v.failed ?? 0}</span>
                    </span>
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {rrSpawned > 0 && (
          <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3">
            <div className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1.5">Residual remediation</div>
            <div className="flex justify-between text-ui-md text-neutral-400 mb-1">
              <span>spawned</span>
              <span className="font-mono text-neutral-200">{rrSpawned}</span>
            </div>
            <div className="flex justify-between text-ui-md text-neutral-400 mb-1">
              <span>merged / failed</span>
              <span>
                <span className="font-mono text-emerald-400">{rrMerged}</span>
                <span className="text-neutral-600"> / </span>
                <span className="font-mono text-red-400">{rrFailed}</span>
                {rrResolved > 0 && (
                  <span className="font-mono text-neutral-500"> ({pct(rrMerged, rrResolved)}%)</span>
                )}
              </span>
            </div>
            {rrRemediating > 0 && (
              <div className="flex justify-between text-ui-md text-neutral-400">
                <span>remediating</span>
                <span className="font-mono text-amber-400">{rrRemediating}</span>
              </div>
            )}
          </div>
        )}

        {successByConf.length > 0 && (
          <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg p-3">
            <div className="text-ui-sm text-neutral-500 uppercase tracking-wide mb-1.5">Success by confidence</div>
            <div className="space-y-0.5">
              {successByConf.map(([conf, { merged, failed }]) => {
                const total = merged + failed;
                const rate = pct(merged, total);
                const rateColor = rate >= 60 ? 'text-emerald-400' : rate >= 30 ? 'text-amber-400' : 'text-red-400';
                return (
                  <div key={conf} className="flex justify-between text-ui-md text-neutral-400">
                    <span>{conf}</span>
                    <span>
                      <span className="font-mono text-neutral-300">{merged}/{total}</span>{' '}
                      <span className={`font-mono ${rateColor}`}>({rate}%)</span>
                    </span>
                  </div>
                );
              })}
            </div>
          </div>
        )}
      </div>

      {(() => {
        const recent = funnel.recent_investigations ?? [];
        const activeDetails = funnel.active_investigations_detail ?? [];
        if (recent.length === 0 && activeDetails.length === 0) return null;
        return (
          <div className="mt-2 bg-workbench-base/50 border border-neutral-800/50 rounded-lg">
            <button
              data-viewport-allow-small-target
              type="button"
              onClick={() => setInvestigationsOpen(v => !v)}
              className="w-full flex items-center justify-between px-3 py-2 text-left"
              aria-expanded={investigationsOpen}
            >
              <span className="text-ui-sm text-neutral-500 uppercase tracking-wide">
                Recent Investigations ({recent.length}{activeDetails.length > 0 ? ` · ${activeDetails.length} active` : ''})
              </span>
              <span className="text-ui-sm text-neutral-500">{investigationsOpen ? '▾' : '▸'}</span>
            </button>
            {investigationsOpen && (
              <div className="px-3 pb-3 space-y-2">
                {recent.length > 0 && (
                  <div className="space-y-1">
                    {recent.map(inv => (
                      <div key={inv.task_id} className="flex items-start gap-2 text-ui-md">
                        <span className={`shrink-0 text-ui-xs font-semibold px-1.5 py-0.5 rounded border ${confidenceBadge(inv.confidence)}`}>
                          {inv.confidence ?? 'unknown'}
                        </span>
                        {inv.severity && (
                          <span className="shrink-0 text-ui-xs text-neutral-500 uppercase tracking-wide pt-0.5">
                            {inv.severity}
                          </span>
                        )}
                        <span className="flex-1 text-neutral-300 leading-snug">
                          {truncate(inv.summary ?? '(no summary)', 80)}
                        </span>
                        <span className="shrink-0 text-ui-sm text-neutral-500 pt-0.5" title={inv.completed_at ?? ''}>
                          {relativeTime(inv.completed_at)}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
                {activeDetails.length > 0 && (
                  <div>
                    <div className="text-ui-xs uppercase tracking-wide text-neutral-600 mb-1">Active</div>
                    <div className="space-y-0.5">
                      {activeDetails.map((a, idx) => {
                        const overdue = (a.elapsed_hours ?? 0) > OVERDUE_HOURS;
                        const isFirstOverdue = overdue && activeDetails.slice(0, idx).every(b => (b.elapsed_hours ?? 0) <= OVERDUE_HOURS);
                        return (
                          <div key={a.task_id} ref={isFirstOverdue ? firstOverdueRef : undefined} className="flex justify-between text-ui-md">
                            <span className="font-mono text-neutral-400 truncate">
                              {a.direction_id ?? '(no direction)'}
                            </span>
                            <span className={`font-mono ${overdue ? 'text-amber-400' : 'text-neutral-400'}`}>
                              {a.elapsed_hours != null ? `${a.elapsed_hours.toFixed(1)}h` : '—'}
                            </span>
                          </div>
                        );
                      })}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        );
      })()}

      {residuals && residuals.top_residuals.length > 0 && (() => {
        const apiIgnoredCount = residuals.top_residuals.filter(
          r => r.case_status === 'IGNORED' && !ignoredFingerprints.has(r.fingerprint)
        ).length;
        const visibleResiduals = residuals.top_residuals.filter(
          r => r.case_status !== 'IGNORED' || showIgnored || ignoredFingerprints.has(r.fingerprint)
        );
        return (
          <div className="mt-2 bg-workbench-base/50 border border-amber-800/30 rounded-lg">
            <button
              data-viewport-allow-small-target
              type="button"
              onClick={() => setResidualsOpen(v => !v)}
              className="w-full flex items-center justify-between px-3 py-2 text-left"
              aria-expanded={residualsOpen}
            >
              <span className="text-ui-sm text-amber-500/80 uppercase tracking-wide">
                Unclassified CODE_ERROR patterns — {residuals.top_residuals.length} clusters · {residuals.total_code_error_count} occurrences (last {residuals.window_hours}h)
              </span>
              <span className="text-ui-sm text-neutral-500">{residualsOpen ? '▾' : '▸'}</span>
            </button>
            {residualsOpen && (
              <div className="px-3 pb-3 space-y-2">
                {apiIgnoredCount > 0 && (
                  <div className="flex justify-end">
                    <button
                      data-viewport-allow-small-target
                      type="button"
                      aria-pressed={!showIgnored}
                      onClick={() => setShowIgnored(v => !v)}
                      className="text-ui-sm text-neutral-500 hover:text-neutral-300 underline underline-offset-2"
                    >
                      {showIgnored ? `Hide ignored (${apiIgnoredCount})` : `Show ignored (${apiIgnoredCount})`}
                    </button>
                  </div>
                )}
                {visibleResiduals.map(r => {
                  const isIgnored = ignoredFingerprints.has(r.fingerprint);
                  const undoFp = undoFingerprints.get(r.fingerprint);
                  const ignoreError = ignoreErrors.get(r.fingerprint);
                  const canIgnore = !r.case_status || r.case_status === 'ACTIVE' || r.case_status === 'REMEDIATING';
                  const isUnsuspended = unsuspendedFingerprints.has(r.fingerprint);
                  const unsuspendError = unsuspendErrors.get(r.fingerprint);
                  const canUnsuspend = r.case_status === 'SUSPENDED' && !isUnsuspended;

                  if (isIgnored && undoFp) {
                    return (
                      <div key={r.fingerprint} className="border border-neutral-800/40 rounded p-2 flex items-center justify-between gap-2">
                        <span className="text-ui-md text-neutral-500 italic">Ignored</span>
                        <button
                          data-viewport-allow-small-target
                          type="button"
                          onClick={() => handleUndo(r.fingerprint)}
                          className="text-ui-sm text-amber-400 hover:text-amber-300 underline underline-offset-2"
                        >
                          Undo
                        </button>
                      </div>
                    );
                  }
                  if (isIgnored) return null;

                  return (
                    <div key={r.fingerprint} className="border border-neutral-800/40 rounded p-2">
                      <div className="flex items-center gap-2 mb-1">
                        <span className="font-mono text-ui-sm bg-red-500/10 text-red-400 border border-red-500/20 px-1.5 py-0.5 rounded shrink-0">
                          {r.count}×
                        </span>
                        <span className="text-ui-sm text-neutral-500 shrink-0">{relativeTime(r.most_recent_at)}</span>
                        {(() => {
                          const effectiveR = isUnsuspended ? { ...r, case_status: 'ACTIVE', consecutive_failure_count: 0 } : r;
                          const label = caseStatusLabel(effectiveR);
                          if (!label) return null;
                          const style = CASE_STATUS_STYLES[effectiveR.case_status ?? ''] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30';
                          return (
                            <span className={`font-mono text-ui-xs font-semibold px-1.5 py-0.5 rounded border shrink-0 ${style}`}>
                              {label}
                            </span>
                          );
                        })()}
                        {(r.audit_defer_count ?? 0) > 0 && (
                          <span
                            className={`font-mono text-ui-xs font-semibold px-1.5 py-0.5 rounded border shrink-0 ${(r.audit_defer_count ?? 0) >= 3 ? 'bg-violet-500/30 text-violet-300 border-violet-500/50' : 'bg-violet-500/15 text-violet-400 border-violet-500/30'}`}
                            title={`Auto-remediation is held until${r.audit_deferred_system_id ? ` system ${r.audit_deferred_system_id}` : ' the named system'} completes its audit`}
                          >
                            DEFERRED ×{r.audit_defer_count}{r.audit_deferred_system_id ? ` · ${r.audit_deferred_system_id}` : ''}
                          </span>
                        )}
                        <span className="font-mono text-ui-xs text-neutral-600 truncate flex-1">{r.fingerprint}</span>
                        {canUnsuspend && (
                          <button
                            data-viewport-allow-small-target
                            type="button"
                            onClick={() => handleUnsuspend(r)}
                            className="shrink-0 text-ui-sm px-1.5 py-0.5 rounded border border-orange-700/60 text-orange-400 hover:text-orange-200 hover:border-orange-500 transition-colors"
                            aria-label={`Unsuspend fingerprint ${r.fingerprint}`}
                          >
                            Unsuspend
                          </button>
                        )}
                        {canIgnore && (
                          <button
                            data-viewport-allow-small-target
                            type="button"
                            onClick={() => handleIgnore(r)}
                            className="shrink-0 text-ui-sm px-1.5 py-0.5 rounded border border-neutral-700 text-neutral-500 hover:text-neutral-300 hover:border-neutral-500 transition-colors"
                            aria-label={`Ignore fingerprint ${r.fingerprint}`}
                          >
                            Ignore
                          </button>
                        )}
                      </div>
                      {unsuspendError && (
                        <div className="text-ui-sm text-red-400 mb-1">Failed to unsuspend: {unsuspendError}</div>
                      )}
                      {ignoreError && (
                        <div className="text-ui-sm text-red-400 mb-1">Failed to ignore: {ignoreError}</div>
                      )}
                      <div className="text-ui-md text-neutral-400 leading-snug mb-1.5">{truncate(r.sample_message, 140)}</div>
                      {(r.audit_defer_count ?? 0) > 0 && (
                        <div className="text-ui-sm text-violet-400/70 mb-1.5">
                          held by audit{r.audit_deferred_system_id ? ` on ${r.audit_deferred_system_id}` : ''} — spawning paused until audit completes
                        </div>
                      )}
                      <div className="flex gap-2 flex-wrap">
                        {r.example_task_ids.map(id => (
                          <button
                            key={id}
                            type="button"
                            onClick={() => onViewTask?.(id)}
                            className="font-mono text-ui-xs text-neutral-500 hover:text-neutral-300 underline underline-offset-2 cursor-pointer"
                          >
                            {id.slice(0, 8)}
                          </button>
                        ))}
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </div>
        );
      })()}
    </section>
  );
}
