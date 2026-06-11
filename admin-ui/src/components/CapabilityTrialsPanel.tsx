import type { DigestData } from '../types/digest';

function fmtLift(lift: number | null): string { return lift == null ? '—' : lift >= 0 ? `+${lift.toFixed(3)}` : lift.toFixed(3); }
function fmtDate(ts?: string): string { return ts ? new Date(ts).toLocaleString() : '—'; }

export function CapabilityTrialsPanel({ digest }: { digest: DigestData | null }) {
  const verdicts = Object.entries(digest?.capability_adoption_verdicts ?? {});
  const totalWith = verdicts.reduce((sum, [, v]) => sum + Math.min(v.n_with ?? 0, 30), 0);
  const totalWithout = verdicts.reduce((sum, [, v]) => sum + Math.min(v.n_without ?? 0, 30), 0);
  if (verdicts.length === 0 || (totalWith === 0 && totalWithout === 0)) {
    return <section><h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Capability Trials</h2><div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg px-3 py-4 text-[11px] text-neutral-500">No capability trials are currently running.</div></section>;
  }
  return (
    <section>
      <h2 className="text-[11px] font-bold text-neutral-400 uppercase tracking-widest mb-2">Capability Trials <span className="text-neutral-600 normal-case font-normal">· {totalWith}/30 with · {totalWithout}/30 without</span></h2>
      <div className="bg-neutral-900/50 border border-neutral-800/50 rounded-lg overflow-hidden">
        <div className="sm:hidden divide-y divide-neutral-800/30">
          {verdicts.map(([name, v]) => <div key={name} className="px-3 py-2 text-[10px]">
            <div className="flex items-start justify-between gap-2 mb-1"><span className="font-medium text-neutral-200 break-words">{name}</span><span className={`shrink-0 font-medium ${v.verdict === 'adopt' ? 'text-emerald-400' : v.verdict === 'reject' ? 'text-red-400' : 'text-amber-400'}`}>{v.verdict}</span></div>
            <div className="grid grid-cols-2 gap-x-4 gap-y-0.5 text-neutral-500">
              <span>Lift: <span className="text-neutral-300 font-mono tabular-nums">{fmtLift(v.lift)}</span></span>
              <span>Samples: <span className="text-neutral-300 tabular-nums">{v.n_with}/{v.n_without}</span></span>
              <span>With: <span className="text-neutral-300 tabular-nums">{Math.min(v.n_with, 30)}/30</span></span>
              <span>Without: <span className="text-neutral-300 tabular-nums">{Math.min(v.n_without, 30)}/30</span></span>
              <span>Evaluated: <span className="text-neutral-300">{fmtDate(v.evaluated_at)}</span></span>
            </div>
          </div>)}
        </div>
        <table className="hidden sm:table w-full text-xs">
          <thead><tr className="text-[10px] text-neutral-500 border-b border-neutral-800/50"><th className="text-left px-3 py-2 font-medium">Capability</th><th className="text-right px-3 py-2 font-medium">Verdict</th><th className="text-right px-3 py-2 font-medium">Lift</th><th className="text-right px-3 py-2 font-medium">With</th><th className="text-right px-3 py-2 font-medium">Without</th><th className="text-right px-3 py-2 font-medium">Evaluated</th></tr></thead>
          <tbody className="divide-y divide-neutral-800/30">{verdicts.map(([name, v]) => <tr key={name}><td className="px-3 py-2 text-neutral-200">{name}</td><td className={`text-right px-3 py-2 font-medium ${v.verdict === 'adopt' ? 'text-emerald-400' : v.verdict === 'reject' ? 'text-red-400' : 'text-amber-400'}`}>{v.verdict}</td><td className="text-right px-3 py-2 text-neutral-300 font-mono tabular-nums">{fmtLift(v.lift)}</td><td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{v.n_with}/30</td><td className="text-right px-3 py-2 text-neutral-300 tabular-nums">{v.n_without}/30</td><td className="text-right px-3 py-2 text-neutral-300">{fmtDate(v.evaluated_at)}</td></tr>)}</tbody>
        </table>
      </div>
    </section>
  );
}
