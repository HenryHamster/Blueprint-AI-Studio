import { useEfficiencySnapshot, fmtEffSecs, fmtEffRate, fmtEffIter } from '../hooks/useEfficiencySnapshot';
import { WarmStartOriginRow } from './WarmStartOriginRow';

function EffDelta({ delta, lowerIsBetter }: { delta: number | null | undefined; lowerIsBetter: boolean }) {
  if (delta === null || delta === undefined || delta === 0) return null;
  const improving = lowerIsBetter ? delta < 0 : delta > 0;
  return <span className={`ml-0.5 ${improving ? 'text-emerald-400' : 'text-red-400'}`}>{delta > 0 ? '↑' : '↓'}</span>;
}

export function PipelineEfficiencyPanel({ projectId }: { projectId: string | null }) {
  const data = useEfficiencySnapshot(projectId);
  if (!data) return null;

  const w24 = data.window_24h;
  const w7 = data.window_7d;

  return (
    <section>
      <h2 className="text-ui-md font-bold text-neutral-400 uppercase tracking-widest mb-2">Pipeline Efficiency</h2>
      <div className="bg-workbench-base border border-neutral-800 rounded-lg p-3 flex flex-wrap gap-x-4 gap-y-1 text-ui-sm text-neutral-400">
        <span>
          <span className="text-neutral-500">cold-start p50</span>{': '}
          <span className="font-mono text-neutral-300">{fmtEffSecs(w24?.setup_p50_secs)}</span>
          <EffDelta delta={w24?.vs_prior_setup_p50_delta} lowerIsBetter />
        </span>
        <span>
          <span className="text-neutral-500">reuse rate</span>{': '}
          <span className="font-mono text-neutral-300">{fmtEffRate(w24?.warm_reuse_rate)}</span>
          <EffDelta delta={w24?.vs_prior_warm_reuse_rate_delta} lowerIsBetter={false} />
        </span>
        <WarmStartOriginRow breakdown={w24?.warm_start_origin_breakdown} />
        <span>
          <span className="text-neutral-500">review cycles p50</span>{': '}
          <span className="font-mono text-neutral-300">{fmtEffIter(w7?.review_iter_median)}</span>
          <EffDelta delta={w7?.vs_prior_review_iter_median_delta} lowerIsBetter />
        </span>
      </div>
    </section>
  );
}
