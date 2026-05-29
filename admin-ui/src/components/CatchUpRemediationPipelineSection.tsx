import { useState } from 'react';
import type { ReactNode } from 'react';
import { ChevronDown, ChevronUp, RotateCcw } from 'lucide-react';
import { timeAgo } from '../utils/time';

interface CiAutoRemediation {
  spawned: number;
  merged: number;
  failed: number;
  by_ci_failure_type?: Record<string, { spawned: number; merged: number; failed: number }>;
}

export interface RemediationFunnelSlice {
  investigations_spawned: number;
  investigations_completed: number;
  investigations_with_diagnosis: number;
  remediations_spawned: number;
  remediations_merged: number;
  remediations_failed: number;
  fixes_held?: number;
  avg_time_to_remediation_hours: number | null;
  active_investigations: number;
  investigations_by_confidence?: Record<string, number>;
  remediation_success_by_confidence?: Record<string, { merged: number; failed: number }>;
  ci_auto_remediation?: CiAutoRemediation;
  recent_investigations?: Array<{
    task_id: string;
    direction_id?: string | null;
    summary?: string | null;
    confidence?: string | null;
    severity?: string | null;
    completed_at?: string | null;
  }>;
  active_investigations_detail?: Array<{
    task_id: string;
    direction_id?: string | null;
    elapsed_hours?: number | null;
  }>;
}

interface CatchUpRemediationPipelineSectionProps {
  remediationFunnel: RemediationFunnelSlice;
  frbcEntries: [string, number][];
  mrbcEntries: [string, number][];
  cmbcEntries: [string, number][];
  carTypeEntries: [string, { spawned: number; merged: number; failed: number }][];
  dropOffEntries: [string, number][];
}

export function CatchUpRemediationPipelineSection({
  remediationFunnel: rf,
  frbcEntries,
  mrbcEntries,
  cmbcEntries,
  carTypeEntries,
  dropOffEntries,
}: CatchUpRemediationPipelineSectionProps) {
  const [remediationExpanded, setRemediationExpanded] = useState(false);
  const [investigationsExpanded, setInvestigationsExpanded] = useState(false);

  const successRate = rf.remediations_spawned > 0
    ? rf.remediations_merged / rf.remediations_spawned
    : null;
  const stages: { label: string; value: number }[] = [
    { label: 'Spawned', value: rf.investigations_spawned },
    { label: 'Completed', value: rf.investigations_completed },
    { label: 'Diagnosed', value: rf.investigations_with_diagnosis },
    { label: 'Remediations', value: rf.remediations_spawned },
    { label: 'Merged', value: rf.remediations_merged },
    ...(rf.fixes_held != null ? [{ label: 'Held', value: rf.fixes_held }] : []),
  ];
  const maxVal = Math.max(1, ...stages.map(s => s.value));

  return (
    <div>
      <button
        onClick={() => setRemediationExpanded(v => !v)}
        aria-expanded={remediationExpanded}
        className="flex items-center gap-1.5 text-[11px] font-bold text-neutral-400 uppercase tracking-widest hover:text-neutral-300 transition-colors mb-1.5"
      >
        {remediationExpanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        <RotateCcw className="w-3 h-3 text-cyan-500" />
        Remediation pipeline ({rf.investigations_spawned})
      </button>
      {remediationExpanded && (
        <div className="flex flex-col gap-2">
          <div className="flex items-center gap-3 text-[10px] text-neutral-400">
            {successRate != null && (
              <span>
                <span className="text-neutral-500">success:</span>{' '}
                <span className="text-emerald-400 font-mono">{(successRate * 100).toFixed(0)}%</span>
                <span className="text-neutral-600"> ({rf.remediations_merged}/{rf.remediations_spawned})</span>
              </span>
            )}
            {rf.avg_time_to_remediation_hours != null && (
              <span>
                <span className="text-neutral-500">avg time:</span>{' '}
                <span className="text-neutral-300 font-mono">{rf.avg_time_to_remediation_hours.toFixed(1)}h</span>
              </span>
            )}
            {rf.active_investigations > 0 && (
              <span>
                <span className="text-neutral-500">active:</span>{' '}
                <span className="text-cyan-400 font-mono">{rf.active_investigations}</span>
              </span>
            )}
            {rf.remediations_failed > 0 && (
              <span>
                <span className="text-neutral-500">failed:</span>{' '}
                <span className="text-red-400 font-mono">{rf.remediations_failed}</span>
              </span>
            )}
            {rf.fixes_held != null && rf.fixes_held > 0 && (
              <span>
                <span className="text-neutral-500">held:</span>{' '}
                <span className="text-emerald-400 font-mono">{rf.fixes_held}</span>
              </span>
            )}
          </div>
          <div className="flex flex-col gap-1">
            {stages.map((s, i) => {
              const pct = (s.value / maxVal) * 100;
              return (
                <div key={s.label} className="flex items-center gap-2 text-[10px]">
                  <span className="w-24 text-neutral-500 shrink-0">
                    {i > 0 && <span className="text-neutral-700">↓ </span>}
                    {s.label}
                  </span>
                  <div className="flex-1 bg-neutral-900 rounded h-4 relative overflow-hidden">
                    <div
                      className="absolute inset-y-0 left-0 bg-cyan-500/30 border-r border-cyan-500/50"
                      style={{ width: `${pct}%` }}
                    />
                    <span className="absolute inset-0 flex items-center px-1.5 font-mono text-neutral-300">
                      {s.value}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>
          {(() => {
            const confOrder = ['high', 'medium', 'low'];
            const confColor: Record<string, string> = { high: 'text-emerald-400', medium: 'text-amber-400', low: 'text-neutral-400' };
            const ibc = rf.investigations_by_confidence ?? {};
            const rsbc = rf.remediation_success_by_confidence ?? {};
            const car = rf.ci_auto_remediation;
            const ibcEntries = confOrder.filter(k => (ibc[k] ?? 0) > 0);
            const rsbcEntries = confOrder.filter(k => { const v = rsbc[k]; return !!v && (v.merged + v.failed) > 0; });
            const hasCar = !!car && (car.spawned + car.merged + car.failed) > 0;
            const dropOffTotal = dropOffEntries.reduce((acc, [, v]) => acc + v, 0);
            const dropOffDominant = dropOffEntries[0]?.[0];
            type Row = { key: string; label: string; nodes: ReactNode };
            const rows: Row[] = [];
            if (ibcEntries.length) rows.push({ key: 'ibc', label: 'confidence:', nodes: ibcEntries.map(k => (
              <span key={k} className="inline-flex items-baseline gap-1"><span className={confColor[k]}>{k}</span><span className="font-mono text-neutral-300">{ibc[k]}</span></span>
            )) });
            if (rsbcEntries.length) rows.push({ key: 'rsbc', label: 'success/conf:', nodes: rsbcEntries.map(k => {
              const v = rsbc[k]; if (!v) return null;
              const total = v.merged + v.failed; const pct = total > 0 ? Math.round((v.merged / total) * 100) : 0;
              return <span key={k} className="inline-flex items-baseline gap-1"><span className={confColor[k]}>{k}</span><span className="font-mono text-neutral-300">{pct}%</span><span className="text-neutral-600">({v.merged}/{total})</span></span>;
            }) });
            if (frbcEntries.length) rows.push({ key: 'frbc', label: 'failed class:', nodes: frbcEntries.map(([cls, count]) => (
              <span key={cls} className="inline-flex items-baseline gap-1"><span className="text-red-400">{cls}</span><span className="font-mono text-neutral-300">{count}</span></span>
            )) });
            if (mrbcEntries.length) rows.push({ key: 'mrbc', label: 'merged class:', nodes: mrbcEntries.map(([cls, count]) => (
              <span key={cls} className="inline-flex items-baseline gap-1"><span className="text-emerald-400">{cls}</span><span className="font-mono text-neutral-300">{count}</span></span>
            )) });
            if (cmbcEntries.length) rows.push({ key: 'cmbc', label: 'all-time merged by class:', nodes: cmbcEntries.map(([cls, count]) => (
              <span key={cls} className="inline-flex items-baseline gap-1"><span className={count >= 3 ? 'text-emerald-400' : 'text-amber-400'}>{cls}</span><span className="font-mono text-neutral-300">{count}</span>{count >= 3 && <span className="text-emerald-600 text-[9px]">✓</span>}</span>
            )) });
            if (hasCar && car) rows.push({ key: 'car', label: 'ci auto-remediation:', nodes: (['spawned', 'merged', 'failed'] as const).map(k => (
              <span key={k} className="inline-flex items-baseline gap-1"><span className="text-neutral-500">{k}</span><span className={`font-mono ${k === 'merged' ? 'text-emerald-400' : k === 'failed' ? 'text-red-400' : 'text-neutral-300'}`}>{car[k]}</span></span>
            )) });
            if (dropOffEntries.length) rows.push({ key: 'drop', label: 'diagnosis drop-off:', nodes: dropOffEntries.map(([reason, count]) => (
              <span key={reason} className="inline-flex items-baseline gap-1"><span className={reason === dropOffDominant ? 'text-amber-400' : 'text-neutral-400'}>{reason}</span><span className="font-mono text-neutral-300">{count}</span>{dropOffTotal > 0 && <span className="text-neutral-600">({Math.round((count / dropOffTotal) * 100)}%)</span>}</span>
            )) });
            if (carTypeEntries.length) rows.push({ key: 'carT', label: 'ci by failure type:', nodes: carTypeEntries.map(([cls, v]) => (
              <span key={cls} className="inline-flex items-baseline gap-1"><span className="text-cyan-400">{cls}</span><span className="font-mono text-neutral-300">{v.spawned}</span><span className="text-neutral-600">({v.merged}✓/{v.failed}✗)</span></span>
            )) });
            if (!rows.length) return null;
            return (
              <div className="flex flex-col gap-1 text-[10px] text-neutral-400 border-t border-neutral-800 pt-1.5">
                {rows.map(r => (
                  <div key={r.key} className="flex items-center gap-2 flex-wrap">
                    <span className="text-neutral-500 w-24 shrink-0">{r.label}</span>
                    {r.nodes}
                  </div>
                ))}
              </div>
            );
          })()}
          {(() => {
            const recent = rf.recent_investigations ?? [];
            const active = rf.active_investigations_detail ?? [];
            if (!recent.length && !active.length) return null;
            const visibleRecent = investigationsExpanded || recent.length <= 2
              ? recent
              : recent.slice(0, 2);
            const confBadge: Record<string, string> = {
              high: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
              medium: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
              low: 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30',
            };
            const sevBadge: Record<string, string> = {
              high: 'bg-red-500/20 text-red-400 border-red-500/30',
              medium: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
              low: 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30',
            };
            return (
              <div className="flex flex-col gap-1.5 text-[10px] border-t border-neutral-800 pt-1.5">
                {recent.length > 0 && (
                  <div className="flex flex-col gap-1">
                    <div className="text-neutral-500">recent diagnoses:</div>
                    {visibleRecent.map(inv => (
                      <div key={inv.task_id} className="flex items-start gap-2">
                        <span className="flex-1 text-neutral-300 truncate">
                          {inv.summary || <span className="text-neutral-600 italic">(no summary)</span>}
                        </span>
                        {inv.confidence && (
                          <span className={`${confBadge[inv.confidence] ?? confBadge.low} border px-1 rounded uppercase text-[9px] font-semibold shrink-0`}>
                            {inv.confidence}
                          </span>
                        )}
                        {inv.severity && (
                          <span className={`${sevBadge[inv.severity] ?? sevBadge.low} border px-1 rounded uppercase text-[9px] font-semibold shrink-0`}>
                            {inv.severity}
                          </span>
                        )}
                        {inv.completed_at && <span className="text-neutral-600 font-mono shrink-0">{timeAgo(inv.completed_at)}</span>}
                      </div>
                    ))}
                    {recent.length > 2 && (
                      <button
                        onClick={() => setInvestigationsExpanded(v => !v)}
                        className="text-[9px] text-neutral-500 hover:text-neutral-300 self-start"
                      >
                        {investigationsExpanded ? 'show less' : `show ${recent.length - 2} more`}
                      </button>
                    )}
                  </div>
                )}
                {active.length > 0 && (
                  <div className="flex flex-col gap-1">
                    <div className="text-neutral-500">active investigations:</div>
                    {active.map(inv => {
                      const wedged = (inv.elapsed_hours ?? 0) > 4;
                      return (
                        <div key={inv.task_id} className={`flex items-center gap-2 ${wedged ? 'text-amber-400' : 'text-neutral-400'}`}>
                          <span className="font-mono shrink-0">
                            {inv.elapsed_hours != null ? `${inv.elapsed_hours.toFixed(1)}h` : '—'}
                          </span>
                          <span className="truncate text-neutral-500">
                            dir: {inv.direction_id ?? '—'}
                          </span>
                          {wedged && <span className="text-[9px] uppercase font-semibold shrink-0">wedged?</span>}
                        </div>
                      );
                    })}
                  </div>
                )}
              </div>
            );
          })()}
        </div>
      )}
    </div>
  );
}
