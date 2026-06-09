import { useState } from 'react';
import { timeAgo } from '../utils/time';
import { StrategicChallengeChip } from './StrategicChallengeChip';
import type { DigestData } from '../types/digest';
import type { DirectionProgress } from './DirectionProgressSection';

export interface CatchUpLatestCycleSectionProps {
  latestCycle: NonNullable<DigestData<DirectionProgress>['latest_cycle']>;
}

/**
 * "Latest strategic cycle" card: outcome, completion time, directions delta,
 * vision/memo flags, and a collapsible summary_text. The summary expand/collapse
 * state lives here (previously CatchUpPanel's `cycleSummaryExpanded`) since
 * nothing else reads it.
 */
export function CatchUpLatestCycleSection({ latestCycle }: CatchUpLatestCycleSectionProps) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="flex flex-col gap-1 p-2 rounded border border-neutral-800 bg-neutral-900/40">
      <div className="flex items-center gap-2 text-[11px]">
        <span className="font-semibold text-neutral-400">Latest strategic cycle</span>
        {latestCycle.outcome && (
          <span className="px-1.5 py-0.5 rounded border text-[10px] bg-neutral-800/60 text-neutral-300 border-neutral-700">
            {latestCycle.outcome}
          </span>
        )}
        {latestCycle.completed_at && (
          <span className="text-[10px] text-neutral-600 font-mono">{timeAgo(latestCycle.completed_at)}</span>
        )}
        <span className="text-[10px] text-neutral-500">
          +{latestCycle.directions_created ?? 0} / −{latestCycle.directions_retired ?? 0} directions
        </span>
        {latestCycle.vision_updated && (
          <span className="text-[10px] text-cyan-400">vision updated</span>
        )}
        {(latestCycle.memos_available != null || latestCycle.memos_cited != null) && (
          <span className="text-[10px] text-violet-400" title="memos available / cited in this cycle">
            {latestCycle.memos_available ?? 0}m avail, {latestCycle.memos_cited ?? 0} cited
          </span>
        )}
        {latestCycle.strategic_judgment_challenge && (
          <StrategicChallengeChip summary={latestCycle.strategic_judgment_challenge} />
        )}
      </div>
      {latestCycle.summary_text && (
        <div className="text-[11px] text-neutral-400 leading-relaxed">
          {expanded || latestCycle.summary_text.length <= 200
            ? latestCycle.summary_text
            : latestCycle.summary_text.slice(0, 200) + '…'}
          {latestCycle.summary_text.length > 200 && (
            <button
              type="button"
              onClick={() => setExpanded(v => !v)}
              className="ml-1 text-[10px] text-cyan-500 hover:text-cyan-400"
            >
              {expanded ? 'less' : 'more'}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
