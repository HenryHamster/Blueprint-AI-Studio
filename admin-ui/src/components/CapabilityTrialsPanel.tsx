import type { DigestData } from '../types/digest';

function fmtLift(lift: number | null): string { return lift == null ? '—' : lift >= 0 ? `+${lift.toFixed(3)}` : lift.toFixed(3); }
function fmtDate(ts?: string): string { return ts ? new Date(ts).toLocaleString() : '—'; }

type AccrualProjection = NonNullable<DigestData['capability_adoption_verdicts']>[string]['accrual_projection'];

function fmtAccrual(p: NonNullable<AccrualProjection>): string {
  const days = p.window_days ?? 7;
  const eta = p.days_to_min_n == null ? '—' : `${p.days_to_min_n}d`;
  const holdbackPct = ((p.holdback_fraction ?? 0) * 100).toFixed(0);
  return `${p.with_7d ?? 0} with / ${p.without_7d ?? 0} without in last ${days}d · est. ${eta} to threshold at ${holdbackPct}% holdback`;
}

export function CapabilityTrialsPanel({ digest }: { digest: DigestData | null }) {
  const verdicts = Object.entries(digest?.capability_adoption_verdicts ?? {});
  const totalWith = verdicts.reduce((sum, [, v]) => sum + Math.min(v.n_with ?? 0, v.thresholds?.min_n_per_arm ?? 30), 0);
  const totalWithout = verdicts.reduce((sum, [, v]) => sum + Math.min(v.n_without ?? 0, v.thresholds?.min_n_per_arm ?? 30), 0);
  const totalTarget = verdicts.reduce((sum, [, v]) => sum + (v.thresholds?.min_n_per_arm ?? 30), 0);
  if (verdicts.length === 0 || (totalWith === 0 && totalWithout === 0)) {
    return <section><h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Capability Trials</h2><div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-4 text-[11px] text-neutral-500">No capability trials are currently running.</div></section>;
  }
  return (
    <section>
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Capability Trials <span className="text-neutral-600 normal-case font-normal">· {totalWith}/{totalTarget} with · {totalWithout}/{totalTarget} without</span></h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {verdicts.map(([name, v]) => {
            const minN = v.thresholds?.min_n_per_arm ?? 30;
            return (
              <div key={name} className="px-3 py-2 text-[10px]">
                <div className="flex items-start justify-between gap-2 mb-1">
                  <span className="font-medium text-neutral-200 break-words">{name}</span>
                  <div className="shrink-0 text-right">
                    <span className={`font-medium ${v.verdict === 'adopt' ? 'text-emerald-400' : v.verdict === 'reject' ? 'text-red-400' : 'text-amber-400'}`}>{v.verdict}</span>
                    {v.verdict_reason && <div className="text-neutral-500">{v.verdict_reason}</div>}
                  </div>
                </div>
                <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-500">
                  <span>Lift: <span className="text-neutral-300 font-mono tabular-nums">{fmtLift(v.lift)}</span></span>
                  <span>Samples: <span className="text-neutral-300 tabular-nums">{v.n_with}/{v.n_without}</span></span>
                  <span>With: <span className="text-neutral-300 tabular-nums">{Math.min(v.n_with, minN)}/{minN}</span></span>
                  <span>Without: <span className="text-neutral-300 tabular-nums">{Math.min(v.n_without, minN)}/{minN}</span></span>
                  <span>Evaluated: <span className="text-neutral-300">{fmtDate(v.evaluated_at)}</span></span>
                </div>
                {v.arm_composition && (
                  <div className="mt-0.5 text-neutral-500">
                    Arm: <span className="text-neutral-400">{v.arm_composition.with_assigned ?? 0}+{v.arm_composition.with_receipt ?? 0}w / {v.arm_composition.without_assigned ?? 0}+{v.arm_composition.without_receipt ?? 0}wo</span>
                  </div>
                )}
                {v.accrual_projection && (
                  <div className="mt-0.5 text-neutral-500">{fmtAccrual(v.accrual_projection)}</div>
                )}
              </div>
            );
          })}
        </div>
        <table className="hidden sm:table w-full text-xs">
          <thead><tr className="text-[10px] text-neutral-500 border-b border-neutral-800/50"><th className="text-left px-3 py-2 font-medium">Capability</th><th className="text-right px-3 py-2 font-medium">Verdict</th><th className="text-right px-3 py-2 font-medium">Lift</th><th className="text-right px-3 py-2 font-medium">With</th><th className="text-right px-3 py-2 font-medium">Without</th><th className="text-right px-3 py-2 font-medium">Evaluated</th></tr></thead>
          <tbody className="divide-y divide-neutral-800/30">{verdicts.map(([name, v]) => {
            const minN = v.thresholds?.min_n_per_arm ?? 30;
            return (
              <tr key={name}>
                <td className="px-3 py-2 text-neutral-200">
                  {name}
                  {v.accrual_projection && <div className="text-[10px] text-neutral-500 font-normal">{fmtAccrual(v.accrual_projection)}</div>}
                </td>
                <td className="text-right px-3 py-2">
                  <span className={`font-medium ${v.verdict === 'adopt' ? 'text-emerald-400' : v.verdict === 'reject' ? 'text-red-400' : 'text-amber-400'}`}>{v.verdict}</span>
                  {v.verdict_reason && <div className="text-[10px] text-neutral-500">{v.verdict_reason}</div>}
                  {v.arm_composition && <div className="text-[10px] text-neutral-500">{v.arm_composition.with_assigned ?? 0}+{v.arm_composition.with_receipt ?? 0}w / {v.arm_composition.without_assigned ?? 0}+{v.arm_composition.without_receipt ?? 0}wo</div>}
                </td>
                <td className="text-right px-3 py-2 text-neutral-300 font-mono tabular-nums">{fmtLift(v.lift)}</td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{v.n_with}/{minN}</td>
                <td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{v.n_without}/{minN}</td>
                <td className="text-right px-3 py-2 text-neutral-300">{fmtDate(v.evaluated_at)}</td>
              </tr>
            );
          })}</tbody>
        </table>
      </div>
    </section>
  );
}
