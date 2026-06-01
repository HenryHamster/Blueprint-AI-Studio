import type { DigestData } from './DashboardHome';

const FILTER_KEYS: { key: string; label: string }[] = [
  { key: 'hash_dedup', label: 'hash dedup' },
  { key: 'word_overlap', label: 'word overlap' },
  { key: 'file_overlap', label: 'file overlap' },
  { key: 'type_filtered', label: 'type filter' },
  { key: 'empty_prompt', label: 'empty prompt' },
  { key: 'batch_cap', label: 'batch cap' },
];

export function ResearchYieldFunnel({ digest }: { digest: DigestData | null }) {
  const ff = digest?.pipeline_filter_funnel;
  if (!ff || (ff.findings_produced ?? 0) <= 0) return null;
  const produced = ff.findings_produced ?? 0;
  const created = ff.tasks_created ?? 0;
  const pct = produced > 0 ? Math.round((created / produced) * 100) : 0;
  const drops = FILTER_KEYS
    .map(f => ({ ...f, v: ff[f.key] ?? 0 }))
    .filter(f => f.v > 0)
    .sort((a, b) => b.v - a.v);
  const topDrop = drops[0];
  return (
    <section>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-neutral-400">
        <span className="text-[10px] text-neutral-500 uppercase tracking-wide">Research yield</span>
        <span>
          <span className="font-semibold text-neutral-200">{created}</span>
          <span className="text-neutral-500"> / {produced} findings &rarr; tasks </span>
          <span className="font-semibold text-neutral-200">({pct}%)</span>
        </span>
        {topDrop && (
          <span className="text-neutral-500">
            top drop: <span className="text-neutral-300">{topDrop.label}</span> <span className="font-mono text-neutral-400">-{topDrop.v}</span>
          </span>
        )}
        {drops.length > 1 && (
          <span className="flex flex-wrap gap-1">
            {drops.slice(1).map(d => (
              <span key={d.key} className="text-[10px] text-neutral-500" title={d.label}>
                {d.label} <span className="font-mono text-neutral-400">-{d.v}</span>
              </span>
            ))}
          </span>
        )}
      </div>
    </section>
  );
}
