import { ChevronRight } from 'lucide-react';
import { formatRelativeTime } from '../utils/directionHelpers';
import type { ProblemModel } from '../types';
import type { DirectionsTab } from './DirectionsPanelHeader';

export interface DirectionsQuickLinksProps {
  selectedProjectId: string | null;
  loading: boolean;
  problemModel: ProblemModel;
  runningUpdate: string | null;
  designDoc: Record<string, unknown>;
  setActiveTab: (tab: DirectionsTab) => void;
}

/**
 * The three collapsible quick-link summaries shown at the top of the Directions
 * list: Problem Model, Running Update, and Architecture. Each links to its tab.
 * Extracted verbatim from DirectionsPanel.
 */
export function DirectionsQuickLinks({
  selectedProjectId,
  loading,
  problemModel,
  runningUpdate,
  designDoc,
  setActiveTab,
}: DirectionsQuickLinksProps) {
  return (
    <>
      {/* Problem Model quick-link */}
      {selectedProjectId && !loading && (() => {
        const capabilities = problemModel.capabilities ?? [];
        const gaps = problemModel.gaps ?? [];
        const hasPurpose = Boolean(problemModel.intent?.purpose);
        const pending = problemModel.pending_user_confirmation ?? [];
        const statusCounts = capabilities.reduce<Record<string, number>>((acc, c) => {
          const st = c.status || 'planned';
          acc[st] = (acc[st] || 0) + 1;
          return acc;
        }, {});
        const capBarColors: Record<string, string> = {
          working: 'bg-emerald-500', partial: 'bg-amber-500', planned: 'bg-workbench-card',
        };
        const modelEmpty = !hasPurpose && capabilities.length === 0 && gaps.length === 0;
        if (modelEmpty) return null;
        return (
        <div className="mb-5">
          <button
            onClick={() => setActiveTab('problem-model')}
            className="flex items-center gap-2 w-full text-left group cursor-pointer hover:bg-workbench-hover/50 rounded px-1.5 -mx-1.5 py-1.5 transition-colors min-h-[44px] md:min-h-auto"
          >
            <ChevronRight className="w-3.5 h-3.5 text-cyan-500/60 group-hover:text-cyan-400 transition-colors" />
            <span className="text-xs font-bold text-neutral-300 tracking-wide group-hover:text-neutral-100 transition-colors">
              Problem Model
            </span>
            {problemModel.last_updated && (
              <span className="text-ui-xs text-neutral-600 font-mono ml-1">
                {formatRelativeTime(problemModel.last_updated)}
              </span>
            )}
            <span className="text-ui-xs text-neutral-600 ml-1">
              {gaps.length} gap{gaps.length === 1 ? '' : 's'}
            </span>
            {pending.length > 0 && (
              <span className="text-ui-xs text-amber-400 ml-1 font-semibold">
                {pending.length} needs attention
              </span>
            )}
          </button>
          {capabilities.length > 0 && (
            <div className="px-1.5 mt-0.5">
              <div className="flex h-1.5 rounded-full overflow-hidden bg-workbench-panel mb-1">
                {(['working', 'partial', 'planned'] as const).map(status => {
                  const count = statusCounts[status] || 0;
                  if (count === 0) return null;
                  const pct = (count / capabilities.length) * 100;
                  return <div key={status} className={`${capBarColors[status]} transition-all`} style={{ width: `${pct}%` }} />;
                })}
              </div>
            </div>
          )}
        </div>
        );
      })()}

      {/* Running Update quick-link */}
      {selectedProjectId && !loading && runningUpdate && runningUpdate.trim() && (
        <div className="mb-5">
          <button
            onClick={() => setActiveTab('running-update')}
            className="flex items-center gap-2 w-full text-left group cursor-pointer hover:bg-workbench-hover/50 rounded px-1.5 -mx-1.5 py-1.5 transition-colors min-h-[44px] md:min-h-auto"
          >
            <ChevronRight className="w-3.5 h-3.5 text-emerald-500/60 group-hover:text-emerald-400 transition-colors" />
            <span className="text-xs font-bold text-neutral-300 tracking-wide group-hover:text-neutral-100 transition-colors">
              Running Update
            </span>
            <span className="inline-block w-1.5 h-1.5 rounded-full bg-emerald-400 ml-1" aria-label="has content" />
          </button>
        </div>
      )}

      {/* Architecture quick-link */}
      {(() => {
        const pages = Object.entries(designDoc).filter(([k]) => !k.startsWith('_'));
        const meta = (designDoc._meta || {}) as Record<string, unknown>;
        const capPages = pages.filter(([k]) => k.startsWith('capabilities/'))
          .map(([k, v]) => {
            const obj = typeof v === 'object' && v ? v as Record<string, unknown> : {};
            return { key: k, name: k.replace('capabilities/', ''), status: String(obj.status || 'missing') };
          });
        const capStatusColors: Record<string, string> = {
          mature: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
          functional: 'bg-emerald-500/20 text-emerald-400 border-emerald-500/30',
          partial: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
          missing: 'bg-red-500/20 text-red-400 border-red-500/30',
        };
        if (pages.length === 0) return null;
        return (
        <div className="mb-5">
          <button
            onClick={() => setActiveTab('design-doc')}
            className="flex items-center gap-2 w-full text-left group cursor-pointer hover:bg-workbench-hover/50 rounded px-1.5 -mx-1.5 py-1.5 transition-colors min-h-[44px] md:min-h-auto"
          >
            <ChevronRight className="w-3.5 h-3.5 text-purple-500/60 group-hover:text-purple-400 transition-colors" />
            <span className="text-xs font-bold text-neutral-300 tracking-wide group-hover:text-neutral-100 transition-colors">
              Architecture
            </span>
            {typeof meta.last_updated === 'string' && (
              <span className="text-ui-xs text-neutral-600 font-mono ml-1">
                {formatRelativeTime(meta.last_updated as string)}
              </span>
            )}
            <span className="text-ui-xs text-neutral-600 ml-1">{pages.length} pages</span>
          </button>
          {capPages.length > 0 && (
            <div className="px-1.5 mt-0.5">
              <div className="flex flex-wrap gap-1">
                {capPages.slice(0, 8).map(cap => (
                  <span key={cap.key} className={`text-ui-2xs px-1.5 py-0.5 rounded border ${capStatusColors[cap.status] || capStatusColors.missing}`}>
                    {cap.name}
                  </span>
                ))}
              </div>
            </div>
          )}
        </div>
        );
      })()}
    </>
  );
}
