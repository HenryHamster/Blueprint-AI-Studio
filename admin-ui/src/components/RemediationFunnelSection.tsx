interface RemediationFunnelData {
  investigations_spawned?: number;
  investigations_completed?: number;
  investigations_with_diagnosis?: number;
  remediations_spawned?: number;
  remediations_merged?: number;
  remediations_failed?: number;
  avg_time_to_remediation_hours?: number | null;
  active_investigations?: number;
  investigations_by_confidence?: Record<string, number> | null;
  failed_remediations_by_class?: Record<string, number> | null;
  merged_remediations_by_trigger_class?: Record<string, number> | null;
  investigations_spawned_by_class?: Record<string, number> | null;
  investigations_completed_by_class?: Record<string, number> | null;
  remediations_spawned_by_class?: Record<string, number> | null;
  remediation_success_by_confidence?: Record<string, { merged: number; failed: number }> | null;
  diagnosis_drop_off_by_reason?: Record<string, number> | null;
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

export interface RemediationFunnelSectionProps {
  funnel?: RemediationFunnelData | null;
  conversion?: InvestigationConversion | null;
  loading?: boolean;
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
  return FAILURE_CLASS_COLORS[cls] ?? 'bg-neutral-500/20 text-neutral-400 border-neutral-500/30';
}

export function RemediationFunnelSection({ funnel, conversion, loading }: RemediationFunnelSectionProps) {
  if (loading) {
    return (
      <section>
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Remediation Funnel</h2>
        <div className="animate-pulse bg-neutral-800/60 rounded h-24 w-full" />
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
  const active = funnel.active_investigations ?? 0;
  const avgHours = funnel.avg_time_to_remediation_hours ?? null;

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

  const structuralFilterPassRate = conversion && (conversion.parseable ?? 0) > 0
    ? pct((conversion.parseable ?? 0) - (conversion.structural ?? 0), conversion.parseable ?? 0)
    : null;

  return (
    <section>
      <div className="flex items-center gap-2 mb-2">
        <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest">Remediation Funnel</h2>
        {active > 0 && (
          <span className="text-[10px] text-amber-400" title="Investigations currently in flight">
            {active} active
          </span>
        )}
        {avgHours != null && (
          <span className="text-[10px] text-neutral-500" title="Average time from investigation spawn to remediation merge">
            ~{avgHours.toFixed(1)}h avg time to remediation
          </span>
        )}
        {remFailed > 0 && (
          <span className="text-[10px] text-red-400" title="Remediations that failed">
            {remFailed} failed
          </span>
        )}
      </div>

      {/* Horizontal funnel */}
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 mb-2">
        <div className="flex items-stretch gap-1 overflow-x-auto">
          {stages.map((s, i) => {
            const conv = s.prev != null && s.prev > 0 ? pct(s.count, s.prev) : null;
            return (
              <div key={s.key} className="flex items-center gap-1 shrink-0">
                {i > 0 && (
                  <span className="text-[10px] font-mono text-neutral-500" title={`${s.count} / ${s.prev} = ${conv}%`}>
                    &rarr; {conv}%
                  </span>
                )}
                <div className="min-w-[86px] px-2 py-1.5 rounded border border-neutral-700 bg-neutral-800/40 text-center">
                  <div className="text-[9px] uppercase tracking-wide text-neutral-500">{s.label}</div>
                  <div className="text-sm font-bold text-neutral-100">{s.count}</div>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Per-failure-class breakdown */}
      {classKeys.length > 0 && (
        <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3 mb-2">
          <div className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1.5">By failure class</div>
          <div className="space-y-1">
            <div className="grid grid-cols-[minmax(110px,1fr)_repeat(5,minmax(48px,1fr))] gap-2 text-[9px] uppercase tracking-wide text-neutral-600">
              <span>Class</span>
              <span className="text-right">Spawned</span>
              <span className="text-right">Completed</span>
              <span className="text-right">Remediated</span>
              <span className="text-right">Merged</span>
              <span className="text-right">Failed</span>
            </div>
            {classKeys.map(cls => {
              const s = funnel.investigations_spawned_by_class?.[cls] ?? 0;
              const c = funnel.investigations_completed_by_class?.[cls] ?? 0;
              const r = funnel.remediations_spawned_by_class?.[cls] ?? 0;
              const m = funnel.merged_remediations_by_trigger_class?.[cls] ?? 0;
              const f = funnel.failed_remediations_by_class?.[cls] ?? 0;
              return (
                <div key={cls} className="grid grid-cols-[minmax(110px,1fr)_repeat(5,minmax(48px,1fr))] gap-2 text-[11px] items-center">
                  <span className={`inline-flex justify-center text-[9px] font-semibold px-1.5 py-0.5 rounded border ${classBadge(cls)}`}>
                    {cls}
                  </span>
                  <span className="text-right font-mono text-neutral-300">{s}</span>
                  <span className="text-right font-mono text-neutral-300">{c}</span>
                  <span className="text-right font-mono text-neutral-300">{r}</span>
                  <span className="text-right font-mono text-emerald-400">{m}</span>
                  <span className="text-right font-mono text-red-400">{f}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Drop-off + confidence */}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
        {(dropOff.length > 0 || structuralFilterPassRate != null) && (
          <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3">
            <div className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1.5">Diagnosis drop-off</div>
            {structuralFilterPassRate != null && (
              <div className="flex justify-between text-[11px] text-neutral-400 mb-1">
                <span>structural filter pass</span>
                <span className="font-mono text-neutral-200">{structuralFilterPassRate}%</span>
              </div>
            )}
            <div className="space-y-0.5">
              {dropOff.map(([reason, count]) => (
                <div key={reason} className="flex justify-between text-[11px] text-neutral-400">
                  <span>{DROP_OFF_LABELS[reason] ?? reason}</span>
                  <span className="font-mono text-neutral-300">{count}</span>
                </div>
              ))}
            </div>
          </div>
        )}

        {confDist.length > 0 && (
          <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3">
            <div className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1.5">Confidence distribution</div>
            <div className="space-y-0.5">
              {confDist
                .sort((a, b) => {
                  const ai = CONFIDENCE_ORDER.indexOf(a[0] as typeof CONFIDENCE_ORDER[number]);
                  const bi = CONFIDENCE_ORDER.indexOf(b[0] as typeof CONFIDENCE_ORDER[number]);
                  return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
                })
                .map(([conf, count]) => (
                  <div key={conf} className="flex justify-between text-[11px] text-neutral-400">
                    <span>{conf}</span>
                    <span className="font-mono text-neutral-300">{count}</span>
                  </div>
                ))}
            </div>
          </div>
        )}

        {successByConf.length > 0 && (
          <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg p-3">
            <div className="text-[10px] text-neutral-500 uppercase tracking-wide mb-1.5">Success by confidence</div>
            <div className="space-y-0.5">
              {successByConf.map(([conf, { merged, failed }]) => {
                const total = merged + failed;
                const rate = pct(merged, total);
                const rateColor = rate >= 60 ? 'text-emerald-400' : rate >= 30 ? 'text-amber-400' : 'text-red-400';
                return (
                  <div key={conf} className="flex justify-between text-[11px] text-neutral-400">
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
    </section>
  );
}
