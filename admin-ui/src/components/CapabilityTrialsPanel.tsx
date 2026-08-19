import { useState } from 'react';
import type { DigestData } from '../types/digest';
import { declareCapabilityVerdict } from '../api';

function fmtLift(lift: number | null): string { return lift == null ? '—' : lift >= 0 ? `+${lift.toFixed(3)}` : lift.toFixed(3); }
function fmtDate(ts?: string): string { return ts ? new Date(ts).toLocaleString() : '—'; }
function fmtRate(rate?: number | null): string { return rate == null ? '—' : `${(rate * 100).toFixed(1)}%`; }
function fmtMoney(value?: number | null): string { return value == null ? '—' : `$${value.toFixed(2)}`; }
function fmtSeconds(s?: number | null): string { return s == null ? '—' : `${s.toFixed(0)}s`; }
function fmtSecondsDelta(d?: number | null): string { if (d == null) return '—'; return d >= 0 ? `+${d.toFixed(0)}s` : `${d.toFixed(0)}s`; }

type AccrualProjection = NonNullable<DigestData['capability_adoption_verdicts']>[string]['accrual_projection'];
type CapabilityVerdict = NonNullable<DigestData['capability_adoption_verdicts']>[string];

function fmtAccrual(p: NonNullable<AccrualProjection>): string {
  const days = p.window_days ?? 7;
  const eta = p.days_to_min_n == null ? '—' : `${p.days_to_min_n}d`;
  const holdbackPct = ((p.holdback_fraction ?? 0) * 100).toFixed(0);
  let result = `${p.with_7d ?? 0} with / ${p.without_7d ?? 0} without in last ${days}d · est. ${eta} to threshold at ${holdbackPct}% holdback`;
  if (p.alternative_projections && Object.keys(p.alternative_projections).length > 0) {
    const altParts = Object.entries(p.alternative_projections)
      .sort(([a], [b]) => parseFloat(a) - parseFloat(b))
      .map(([f, d]) => `${(parseFloat(f) * 100).toFixed(0)}%→${d == null ? '—' : `${d}d`}`)
      .join(', ');
    result += ` · alt: ${altParts}`;
  }
  return result;
}

function hasLaneMetrics(v: CapabilityVerdict): boolean {
  return v.n_lane != null || v.n_control != null || v.lane_merge_rate != null || v.control_merge_rate != null;
}

function verdictClass(verdict: CapabilityVerdict['verdict']): string {
  if (verdict === 'adopt' || verdict === 'ADOPT') return 'text-emerald-400';
  if (verdict === 'reject' || verdict === 'KILL') return 'text-red-400';
  if (verdict === 'no_effect') return 'text-yellow-400';
  return 'text-amber-400';
}

function isVerdictDecided(verdict: CapabilityVerdict['verdict']): boolean {
  return verdict === 'adopt' || verdict === 'ADOPT' || verdict === 'reject' || verdict === 'KILL' || verdict === 'no_effect';
}

function laneMetricSummary(v: CapabilityVerdict): string[] {
  const lines = [
    `Lane samples ${v.n_lane ?? 0}`,
    `Control samples ${v.n_control ?? 0}`,
    `Merge rate ${fmtRate(v.lane_merge_rate)} lane / ${fmtRate(v.control_merge_rate)} control`,
    `Cost/merged ${fmtMoney(v.lane_cost_per_merged)} lane / ${fmtMoney(v.control_cost_per_merged)} control`,
    `Checkpoint loss ${fmtRate(v.checkpoint_loss_rate)}`,
    `Gate violations ${v.gate_integrity_violations ?? 0}`,
  ];
  if (v.lane_fix_tasks_sampled != null) {
    lines.push(`Warm resume ${fmtRate(v.warm_resume_fraction)} (${v.lane_fix_tasks_sampled} fix tasks)`);
    lines.push(`Checkpoint receipt ${fmtRate(v.checkpoint_fraction)}`);
  }
  const cmp = v.lane_relay_comparison;
  if (cmp != null) {
    const vnum = cmp.source_version?.version_number;
    const label = vnum != null ? `vs v${vnum}` : 'vs relay';
    lines.push(`Wall-clock ${fmtSeconds(cmp.lane?.wall_clock_seconds)} lane / ${fmtSeconds(cmp.relay?.wall_clock_seconds)} relay (${fmtSecondsDelta(cmp.deltas?.wall_clock_seconds)} ${label})`);
  }
  return lines;
}

type DeclareVerdict = 'adopt' | 'reject' | 'continue-trial';

function DeclareVerdictForm({ projectId, capName, onDone, onSuccess }: { projectId: string; capName: string; onDone: () => void; onSuccess?: () => void }) {
  const [verdict, setVerdict] = useState<DeclareVerdict>('adopt');
  const [rationale, setRationale] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function handleSubmit() {
    setSubmitting(true);
    setError(null);
    try {
      await declareCapabilityVerdict(projectId, capName, verdict, rationale || undefined);
      if (onSuccess) {
        onSuccess();
      }
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Request failed');
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="mt-1 flex flex-col gap-1 border-t border-neutral-700/50 pt-1">
      <div className="flex items-center gap-2">
        <select
          value={verdict}
          onChange={e => setVerdict(e.target.value as DeclareVerdict)}
          className="text-ui-sm bg-workbench-panel border border-neutral-700 rounded px-1 py-0.5 text-neutral-200"
          disabled={submitting}
        >
          <option value="adopt">adopt</option>
          <option value="reject">reject</option>
          <option value="continue-trial">continue-trial</option>
        </select>
        <input
          type="text"
          placeholder="Rationale (optional)"
          value={rationale}
          onChange={e => setRationale(e.target.value)}
          className="text-ui-sm bg-workbench-panel border border-neutral-700 rounded px-1 py-0.5 text-neutral-200 flex-1 min-w-0"
          disabled={submitting}
        />
        {/* eslint-disable-next-line viewport/min-touch-target */}
        <button
          onClick={handleSubmit}
          disabled={submitting}
          className="text-ui-sm px-2 py-0.5 rounded bg-workbench-layer hover:bg-workbench-hover text-neutral-200 disabled:opacity-50 shrink-0"
        >
          {submitting ? '…' : 'Declare'}
        </button>
        {/* eslint-disable-next-line viewport/min-touch-target */}
        <button
          onClick={onDone}
          disabled={submitting}
          className="text-ui-sm px-1.5 py-0.5 rounded text-neutral-500 hover:text-neutral-300"
        >
          ✕
        </button>
      </div>
      {error && <div className="text-ui-sm text-red-400">{error}</div>}
    </div>
  );
}

function DeclareButton({ projectId, capName, onSuccess }: { projectId: string; capName: string; onSuccess?: () => void }) {
  const [open, setOpen] = useState(false);
  if (open) {
    return <DeclareVerdictForm projectId={projectId} capName={capName} onDone={() => setOpen(false)} onSuccess={onSuccess} />;
  }
  return (
    // eslint-disable-next-line viewport/min-touch-target
    <button
      onClick={() => setOpen(true)}
      className="text-ui-sm px-1.5 py-0.5 rounded bg-workbench-panel hover:bg-workbench-hover text-neutral-400 hover:text-neutral-200 mt-0.5"
    >
      Declare verdict
    </button>
  );
}

export function CapabilityTrialsPanel({ digest, projectId, onRefreshDigest }: { digest: DigestData | null; projectId?: string; onRefreshDigest?: () => void }) {
  if (digest === null) return null;
  const verdicts = Object.entries(digest?.capability_adoption_verdicts ?? {});
  const genericVerdicts = verdicts.filter(([, v]) => !hasLaneMetrics(v));
  const totalWith = genericVerdicts.reduce((sum, [, v]) => sum + Math.min(v.n_with ?? 0, v.thresholds?.min_n_per_arm ?? 30), 0);
  const totalWithout = genericVerdicts.reduce((sum, [, v]) => sum + Math.min(v.n_without ?? 0, v.thresholds?.min_n_per_arm ?? 30), 0);
  const genericTarget = genericVerdicts.reduce((sum, [, v]) => sum + (v.thresholds?.min_n_per_arm ?? 30), 0);
  if (verdicts.length === 0) {
    return <section><h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Capability Trials</h2><div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg px-3 py-4 text-ui-md text-neutral-500">No capability trials are currently running.</div></section>;
  }
  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Capability Trials <span className="text-neutral-600 normal-case font-normal">{genericTarget > 0 ? `· ${totalWith}/${genericTarget} with · ${totalWithout}/${genericTarget} without` : `· ${verdicts.length} verdict${verdicts.length === 1 ? '' : 's'}`}</span></h2>
      <div className="bg-workbench-base/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {verdicts.map(([name, v]) => {
            const minN = v.thresholds?.min_n_per_arm ?? 30;
            const isLane = hasLaneMetrics(v);
            return (
              <div key={name} className="px-3 py-2 text-ui-sm">
                <div className="flex items-start justify-between gap-2 mb-1">
                  <span className="font-medium text-neutral-200 break-words">{name}</span>
                  <div className="shrink-0 text-right">
                    <span className={`font-medium ${verdictClass(v.verdict)}`}>{v.verdict}</span>
                    {v.is_stalled && <span className="ml-1 inline-block px-1 py-0.5 rounded text-ui-xs font-medium bg-amber-900/40 text-amber-400 border border-amber-700/40">Stalled</span>}
                    {v.verdict_reason && <div className="text-neutral-500">{v.verdict_reason}</div>}
                  </div>
                </div>
                {isLane ? (
                  <div className="grid grid-cols-1 gap-y-0.5 text-neutral-500 min-[420px]:grid-cols-2 min-[420px]:gap-x-4">
                    <span>Lane samples: <span className="text-neutral-300 tabular-nums">{v.n_lane ?? 0}</span></span>
                    <span>Control samples: <span className="text-neutral-300 tabular-nums">{v.n_control ?? 0}</span></span>
                    <span>Lane merge rate: <span className="text-neutral-300 tabular-nums">{fmtRate(v.lane_merge_rate)}</span></span>
                    <span>Control merge rate: <span className="text-neutral-300 tabular-nums">{fmtRate(v.control_merge_rate)}</span></span>
                    <span>Lane cost/merged: <span className="text-neutral-300 tabular-nums">{fmtMoney(v.lane_cost_per_merged)}</span></span>
                    <span>Control cost/merged: <span className="text-neutral-300 tabular-nums">{fmtMoney(v.control_cost_per_merged)}</span></span>
                    <span>Checkpoint loss: <span className="text-neutral-300 tabular-nums">{fmtRate(v.checkpoint_loss_rate)}</span></span>
                    <span>Gate violations: <span className="text-neutral-300 tabular-nums">{v.gate_integrity_violations ?? 0}</span></span>
                    {v.lane_fix_tasks_sampled != null && <>
                      <span>Warm resume: <span className="text-neutral-300 tabular-nums">{fmtRate(v.warm_resume_fraction)} ({v.lane_fix_tasks_sampled} fix)</span></span>
                      <span>Checkpoint receipt: <span className="text-neutral-300 tabular-nums">{fmtRate(v.checkpoint_fraction)}</span></span>
                    </>}
                    {v.engagement_low && <span className="text-amber-300">Engagement low — treatment not delivered</span>}
                    {v.immediate_alert && <span className="text-red-300">Immediate alert: yes</span>}
                    {v.lane_relay_comparison != null && (() => {
                      const cmp = v.lane_relay_comparison;
                      const vnum = cmp?.source_version?.version_number;
                      const label = vnum != null ? `v${vnum}` : 'relay';
                      return <>
                        <span className="col-span-2 text-neutral-400 font-medium">Wall-clock vs {label} baseline:</span>
                        <span>Lane: <span className="text-neutral-300 tabular-nums">{fmtSeconds(cmp?.lane?.wall_clock_seconds)}</span></span>
                        <span>Relay: <span className="text-neutral-300 tabular-nums">{fmtSeconds(cmp?.relay?.wall_clock_seconds)}</span></span>
                        <span className="col-span-2">Delta: <span className="text-neutral-300 tabular-nums">{fmtSecondsDelta(cmp?.deltas?.wall_clock_seconds)}</span></span>
                      </>;
                    })()}
                    <span>Evaluated: <span className="text-neutral-300">{fmtDate(v.evaluated_at)}</span></span>
                  </div>
                ) : (
                  <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-500">
                    <span>Lift: <span className="text-neutral-300 font-mono tabular-nums">{fmtLift(v.lift ?? null)}</span></span>
                    <span>Samples: <span className="text-neutral-300 tabular-nums">{v.n_with}/{v.n_without}</span></span>
                    <span>With: <span className="text-neutral-300 tabular-nums">{Math.min(v.n_with ?? 0, minN)}/{minN}</span></span>
                    <span>Without: <span className="text-neutral-300 tabular-nums">{Math.min(v.n_without ?? 0, minN)}/{minN}</span></span>
                    <span>Evaluated: <span className="text-neutral-300">{fmtDate(v.evaluated_at)}</span></span>
                  </div>
                )}
                {v.arm_composition && (
                  <div className="mt-0.5 text-neutral-500">
                    Arm: <span className="text-neutral-400">{v.arm_composition.with_assigned ?? 0}+{v.arm_composition.with_receipt ?? 0}w / {v.arm_composition.without_assigned ?? 0}+{v.arm_composition.without_receipt ?? 0}wo</span>
                    {(v.arm_composition.without_contaminated ?? 0) > 0 && <span className="text-amber-400 ml-1">· {v.arm_composition.without_contaminated} holdback contaminated</span>}
                  </div>
                )}
                {v.accrual_projection && (
                  <div className="mt-0.5 text-neutral-500">{fmtAccrual(v.accrual_projection)}</div>
                )}
                {projectId && !isVerdictDecided(v.verdict) && <DeclareButton projectId={projectId} capName={name} onSuccess={onRefreshDigest} />}
              </div>
            );
          })}
        </div>
        <table className="hidden sm:table w-full text-xs">
          <thead><tr className="text-ui-sm text-neutral-500 border-b border-neutral-800/50"><th className="text-left px-3 py-2 font-medium">Capability</th><th className="text-right px-3 py-2 font-medium">Verdict</th><th className="text-right px-3 py-2 font-medium">Metric</th><th className="text-right px-3 py-2 font-medium">Samples</th><th className="text-right px-3 py-2 font-medium">Cost / Guardrails</th><th className="text-right px-3 py-2 font-medium">Evaluated</th>{projectId && <th className="text-right px-3 py-2 font-medium">Action</th>}</tr></thead>
          <tbody className="divide-y divide-neutral-800/30">{verdicts.map(([name, v]) => {
            const minN = v.thresholds?.min_n_per_arm ?? 30;
            const isLane = hasLaneMetrics(v);
            return (
              <tr key={name}>
                <td className="px-3 py-2 text-neutral-200">
                  {name}
                  {isLane && <div className="text-ui-sm text-neutral-500 font-normal">{laneMetricSummary(v).join(' · ')}</div>}
                  {v.accrual_projection && <div className="text-ui-sm text-neutral-500 font-normal">{fmtAccrual(v.accrual_projection)}</div>}
                </td>
                <td className="text-right px-3 py-2">
                  <span className={`font-medium ${verdictClass(v.verdict)}`}>{v.verdict}</span>
                  {v.is_stalled && <span className="ml-1 inline-block px-1 py-0.5 rounded text-ui-xs font-medium bg-amber-900/40 text-amber-400 border border-amber-700/40">Stalled</span>}
                  {v.verdict_reason && <div className="text-ui-sm text-neutral-500">{v.verdict_reason}</div>}
                  {v.immediate_alert && <div className="text-ui-sm text-red-300">Immediate alert</div>}
                  {v.arm_composition && <div className="text-ui-sm text-neutral-500">{v.arm_composition.with_assigned ?? 0}+{v.arm_composition.with_receipt ?? 0}w / {v.arm_composition.without_assigned ?? 0}+{v.arm_composition.without_receipt ?? 0}wo{(v.arm_composition.without_contaminated ?? 0) > 0 ? <span className="text-amber-400 ml-1">· {v.arm_composition.without_contaminated} contaminated</span> : null}</div>}
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 font-mono tabular-nums">{isLane ? <>{fmtRate(v.lane_merge_rate)} / {fmtRate(v.control_merge_rate)}</> : fmtLift(v.lift ?? null)}</td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{isLane ? <>{v.n_lane ?? 0} lane / {v.n_control ?? 0} control</> : <>{v.n_with}/{minN}</>}</td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{isLane ? <>{fmtMoney(v.lane_cost_per_merged)} / {fmtMoney(v.control_cost_per_merged)}<div className="text-ui-sm text-neutral-500">loss {fmtRate(v.checkpoint_loss_rate)} · gates {v.gate_integrity_violations ?? 0}</div>{v.lane_fix_tasks_sampled != null && <div className="text-ui-sm text-neutral-500">warm {fmtRate(v.warm_resume_fraction)} · ckpt {fmtRate(v.checkpoint_fraction)} ({v.lane_fix_tasks_sampled} fix)</div>}{v.engagement_low && <div className="text-ui-sm text-amber-400">engagement low</div>}</> : <>{v.n_without}/{minN}</>}</td>
                <td className="text-right px-3 py-2 text-neutral-300">{fmtDate(v.evaluated_at)}</td>
                {projectId && <td className="text-right px-3 py-2">{!isVerdictDecided(v.verdict) && <DeclareButton projectId={projectId} capName={name} onSuccess={onRefreshDigest} />}</td>}
              </tr>
            );
          })}</tbody>
        </table>
      </div>
    </section>
  );
}
