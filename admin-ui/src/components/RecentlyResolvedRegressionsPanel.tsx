import { useState, useEffect, useCallback } from 'react';
import { CheckCircle, Minus, Shield } from 'lucide-react';
import { apiFetch, fetchCrossLayerCompliance, type CrossLayerComplianceResponse } from '../api';
import { EscalationOutcomePanel } from './PipelineRegressionsPanel';
import { timeAgo, formatDurationSec, parseTimestamp, formatTimeToDetect, formatTimeToInvestigate } from '../utils/time';
import { parseViewportSubkey } from '../utils/viewportRegression';
import { LayersAffectedBadges, UntargetedLayersBadge, type LayerSummary } from './LayersAffectedBadges';
import { PhaseChip } from './regressionPhaseChips';

interface RecurrenceEntry {
  merged_count: number;
  retripped_count: number;
  recurrence_rate: number | null;
  median_hold_days: number | null;
}

type ComplianceWithRecurrence = CrossLayerComplianceResponse & {
  recurrence_by_bucket?: Record<string, RecurrenceEntry>;
};

const LAYER_SHORT: Record<string, string> = { CI_WORKFLOW: 'CI', AGENT_VM: 'VM', AGENT_VERB: 'VERB', BASELINE_GATE: 'BASELINE', REVIEWER_PASS: 'REVIEWER' };

const CLASS_PREFIX_CHIPS: Record<string, string> = {
  viewport_regression: 'bg-violet-900/60 text-violet-300 border-violet-700/50',
  fix_loop_divergence: 'bg-amber-900/60 text-amber-300 border-amber-700/50',
  sr_false_positive: 'bg-rose-900/60 text-rose-300 border-rose-700/50',
  sr_catch_rate: 'bg-sky-900/60 text-sky-300 border-sky-700/50',
};

const ASSERTION_TYPE_CHIPS: Record<string, string> = {
  overflow: 'bg-rose-900/60 text-rose-300 border-rose-700/50',
  text_truncation: 'bg-amber-900/60 text-amber-300 border-amber-700/50',
  touch_target: 'bg-blue-900/60 text-blue-300 border-blue-700/50',
};

function RegressionClassLabel({ cls }: { cls: string }) {
  const colon = cls.indexOf(':');
  if (colon === -1) return <span className="font-mono text-emerald-300">{cls}</span>;
  const prefix = cls.slice(0, colon);
  const subkey = cls.slice(colon + 1);
  const chipCls = CLASS_PREFIX_CHIPS[prefix];
  if (!chipCls) return <span className="font-mono text-emerald-300">{cls}</span>;

  if (prefix === 'viewport_regression') {
    const { component, assertionType, width } = parseViewportSubkey(subkey);
    const assertionChipCls = assertionType ? ASSERTION_TYPE_CHIPS[assertionType] : undefined;
    return (
      <span className="inline-flex items-center gap-1 min-w-0 flex-wrap">
        <span className={`inline-block text-[9px] font-semibold px-1 py-px rounded border ${chipCls} shrink-0`}>
          {prefix.replace(/_/g, ' ')}
        </span>
        <span className="font-mono text-emerald-300">{component}</span>
        {assertionType && (
          <span
            data-testid="assertion-type-chip"
            className={`inline-block text-[9px] font-semibold px-1 py-px rounded border shrink-0 ${assertionChipCls ?? 'bg-neutral-800/60 text-neutral-400 border-neutral-700/50'}`}
          >
            {assertionType.replace(/_/g, ' ')}
          </span>
        )}
        {width && <span className="font-mono text-neutral-500 text-[9px]">@{width}</span>}
      </span>
    );
  }

  return (
    <span className="inline-flex items-center gap-1 min-w-0">
      <span className={`inline-block text-[9px] font-semibold px-1 py-px rounded border ${chipCls} shrink-0`}>
        {prefix.replace(/_/g, ' ')}
      </span>
      <span className="font-mono text-emerald-300 truncate">{subkey}</span>
    </span>
  );
}

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
  untargeted_layers?: string[] | null;
}

interface LayerDefenseEntry {
  layer: string;
  fix_count: number;
  last_fix_at: string | null;
}

function layerMismatchBadge(c: RecentlyClosedCase) {
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

interface Props {
  projectId: string;
}


function getStalenessColor(lastFixAt: string | null): { bgClass: string; isStale: boolean; severity: 'high' | 'medium' | null } {
  if (!lastFixAt) {
    return { bgClass: '', isStale: false, severity: null };
  }

  const lastFixDate = parseTimestamp(lastFixAt);
  const now = new Date();
  const daysAgo = (now.getTime() - lastFixDate.getTime()) / (1000 * 60 * 60 * 24);

  if (daysAgo >= 90) {
    return { bgClass: 'bg-red-950/20', isStale: true, severity: 'high' };
  } else if (daysAgo >= 60) {
    return { bgClass: 'bg-amber-950/20', isStale: true, severity: 'medium' };
  } else if (daysAgo >= 30) {
    return { bgClass: 'bg-amber-950/10', isStale: false, severity: null };
  } else {
    return { bgClass: '', isStale: false, severity: null };
  }
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
                    <span className="text-[10px] break-all min-w-0"><RegressionClassLabel cls={c.regression_class} /></span>
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
                    <UntargetedLayersBadge untargeted_layers={c.untargeted_layers} />
                    {layerMismatchBadge(c)}
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
          <div className="mb-2">
            <h3 className="text-[10px] font-bold text-neutral-500 uppercase tracking-widest flex items-center gap-1">
              <Shield className="w-3 h-3" />
              Layer Defense Density (30d)
            </h3>
            <p className="text-[9px] text-neutral-600 mt-0.5">staleness ≥60d highlighted</p>
          </div>
          <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
            <table className="w-full text-[10px]">
              <tbody className="divide-y divide-neutral-800/30">
                {layerDensity.map((row) => {
                  const { bgClass, isStale, severity } = getStalenessColor(row.last_fix_at);
                  return (
                    <tr key={row.layer} className={bgClass}>
                      <td className="px-3 py-1 font-mono text-neutral-400 truncate max-w-[120px]">{row.layer}</td>
                      <td className="px-2 py-1 text-right">
                        <span className={row.fix_count === 0 ? 'text-neutral-600' : 'text-white font-medium'}>{row.fix_count}</span>
                      </td>
                      <td className="px-3 py-1 text-right text-neutral-600 flex items-center justify-end gap-2">
                        {row.last_fix_at ? timeAgo(row.last_fix_at) : '—'}
                        {isStale && (
                          <span className={`px-1.5 py-0.5 rounded text-[9px] font-medium whitespace-nowrap ${severity === 'high' ? 'bg-red-500/20 text-red-300 border border-red-500/30' : 'bg-amber-500/20 text-amber-300 border border-amber-500/30'}`}>
                            stale
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}
      <div className="mt-4">
        <EscalationOutcomePanel projectId={projectId} />
      </div>
    </section>
  );
}
