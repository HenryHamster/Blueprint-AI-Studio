import { useMemo } from 'react';
import type { DigestData } from '../types/digest';
import type { DirectionProgress } from './DirectionProgressSection';

const FILTER_KEYS: { key: string; label: string }[] = [
  { key: 'hash_dedup', label: 'hash dedup' },
  { key: 'word_overlap', label: 'word overlap' },
  { key: 'file_overlap', label: 'file overlap' },
  { key: 'type_filtered', label: 'type filter' },
  { key: 'empty_prompt', label: 'empty prompt' },
  { key: 'batch_cap', label: 'batch cap' },
];

export interface CatchUpResearchYieldSectionProps {
  pipelineFilterFunnel: DigestData<DirectionProgress>['pipeline_filter_funnel'];
}

/**
 * "Research yield" row: findings-produced → tasks-created conversion plus the
 * top filter drop reasons. Renders nothing unless the funnel produced findings
 * (matching the inline guard it was extracted from). `drops` (filter-key
 * tallies) is derived here instead of being threaded through props.
 */
export function CatchUpResearchYieldSection({ pipelineFilterFunnel }: CatchUpResearchYieldSectionProps) {
  const drops = useMemo(() => {
    const ff = pipelineFilterFunnel;
    if (!ff) return [];
    return FILTER_KEYS
      .map(f => ({ ...f, v: ff[f.key] ?? 0 }))
      .filter(f => f.v > 0)
      .sort((a, b) => b.v - a.v);
  }, [pipelineFilterFunnel]);

  if (!pipelineFilterFunnel || (pipelineFilterFunnel.findings_produced ?? 0) <= 0) return null;

  const ff = pipelineFilterFunnel;
  const produced = ff.findings_produced ?? 0;
  const created = ff.tasks_created ?? 0;
  const pct = produced > 0 ? Math.round((created / produced) * 100) : 0;

  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-neutral-400">
      <span className="text-[10px] text-neutral-500 uppercase tracking-wide">Research yield</span>
      <span>
        <span className="font-semibold text-neutral-200">{created}</span>
        <span className="text-neutral-500"> / {produced} findings &rarr; tasks </span>
        <span className="font-semibold text-neutral-200">({pct}%)</span>
      </span>
      {drops.slice(0, 3).map(d => (
        <span key={d.key} className="text-neutral-500" title={d.label}>
          {d.label} <span className="font-mono text-neutral-400">-{d.v}</span>
        </span>
      ))}
    </div>
  );
}
