const STAGE_ORDER = ['provision', 'clone', 'test', 'lint', 'build', 'pr_creation', 'merge', 'review'] as const;
const STAGE_COLORS: Record<string, string> = {
  provision: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  clone: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  test: 'bg-red-500/20 text-red-400 border-red-500/30',
  lint: 'bg-red-500/20 text-red-400 border-red-500/30',
  build: 'bg-red-500/20 text-red-400 border-red-500/30',
  pr_creation: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  merge: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  review: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
};

export interface FailureStageDistribution {
  stages: Record<string, number>;
  total_classified: number;
  dominant_stage: string | null;
}

interface Props {
  distribution: FailureStageDistribution | null | undefined;
}

export function FailureStageWidget({ distribution }: Props) {
  if (!distribution || distribution.total_classified === 0) return null;
  return (
    <section>
      <div className="flex flex-wrap gap-1.5 items-center">
        <span className="text-ui-sm text-neutral-500 uppercase tracking-wide mr-1">Stage</span>
        {STAGE_ORDER.map(stage => {
          const count = distribution.stages[stage];
          if (!count) return null;
          const isDominant = distribution.dominant_stage === stage;
          const base = STAGE_COLORS[stage] ?? 'bg-workbench-control/20 text-neutral-400 border-neutral-500/30';
          const cls = isDominant ? `${base} ring-1 ring-current` : base;
          return (
            <span
              key={stage}
              className={`inline-flex items-center gap-1 text-ui-sm font-semibold px-2 py-0.5 rounded border uppercase ${cls}`}
              title={`${stage}: ${count}${isDominant ? ' — dominant stage' : ''}`}
            >
              {stage}
              <span className="font-bold">{count}</span>
            </span>
          );
        })}
      </div>
    </section>
  );
}
