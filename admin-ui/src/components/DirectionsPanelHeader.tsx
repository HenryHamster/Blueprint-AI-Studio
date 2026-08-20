import { Zap } from 'lucide-react';
import type { ProblemModel, ProblemModelSizeHealth } from '../types';
import type { Project } from '../types/project';

export type DirectionsTab = 'directions' | 'problem-model' | 'running-update' | 'design-doc';

export interface DirectionsPanelHeaderProps {
  selectedProjectId: string | null;
  projects: Project[];
  proposedCount: number;
  activeTab: DirectionsTab;
  setActiveTab: (tab: DirectionsTab) => void;
  problemModel: ProblemModel;
  sizeHealth?: ProblemModelSizeHealth | null;
  designDoc: Record<string, unknown>;
  runningUpdate: string | null;
  triggerCycle: (phase: 'design_doc' | 'strategic_review' | 'full') => void;
  showTitle?: boolean;
  showCycleControls?: boolean;
  showTabs?: boolean;
}

/**
 * Panel header: title + project badge + pending count, spawn-trigger buttons,
 * and the tab bar. Extracted verbatim from DirectionsPanel.
 */
export function DirectionsPanelHeader({
  selectedProjectId,
  projects,
  proposedCount,
  activeTab,
  setActiveTab,
  problemModel,
  sizeHealth,
  designDoc,
  runningUpdate,
  triggerCycle,
  showTitle = true,
  showCycleControls = true,
  showTabs = true,
}: DirectionsPanelHeaderProps) {
  const getStatusColor = (status?: string) => {
    switch (status) {
      case 'healthy':
        return 'bg-green-950/60 text-green-400 border-green-900/50';
      case 'growing':
        return 'bg-yellow-950/60 text-yellow-400 border-yellow-900/50';
      case 'large':
        return 'bg-orange-950/60 text-orange-400 border-orange-900/50';
      case 'oversized':
        return 'bg-red-950/60 text-red-400 border-red-900/50';
      default:
        return 'bg-workbench-shell/60 text-neutral-400 border-neutral-900/50';
    }
  };
  if (!showTitle && !showCycleControls && !showTabs) return null;
  return (
    <>
      {/* Header */}
      {showTitle && (
      <div className="flex items-center justify-between px-5 py-3 border-b border-neutral-800 shrink-0">
        <div className="flex items-center gap-2">
          <h2 className="text-sm font-semibold text-neutral-100 uppercase tracking-wider">Strategic</h2>
          {selectedProjectId && projects.length > 0 && (
            <span className="text-xs text-neutral-500 truncate max-w-[140px]">{projects.find(p => p.id === selectedProjectId)?.name || selectedProjectId}</span>
          )}
          {proposedCount > 0 && activeTab === 'directions' && (
            <span className="text-ui-sm px-1.5 py-0.5 rounded-full font-semibold bg-purple-950/60 text-purple-400 border border-purple-900/50">
              {proposedCount} pending
            </span>
          )}
        </div>
      </div>
      )}

      {/* Spawn triggers */}
      {showCycleControls && (
      <div className="px-5 py-2 border-b border-neutral-800 shrink-0 flex items-center gap-2">
        <div className="shrink-0 flex items-center gap-1">
          <button
            type="button"
            onClick={() => triggerCycle('design_doc')}
            title="Run architecture review (Phase 1 only)"
            className="inline-flex items-center gap-1 text-xs px-2 py-1.5 rounded border border-purple-900/50 text-purple-400 hover:bg-purple-900/20 transition-colors min-h-[44px] md:min-h-auto"
          >
            <Zap className="w-3 h-3" /> Arch
          </button>
          <button
            type="button"
            onClick={() => triggerCycle('strategic_review')}
            title="Run strategic review (Phase 2 only, skip design doc update)"
            className="inline-flex items-center gap-1 text-xs px-2 py-1.5 rounded border border-cyan-900/50 text-cyan-400 hover:bg-cyan-900/20 transition-colors min-h-[44px] md:min-h-auto"
          >
            <Zap className="w-3 h-3" /> Strategic
          </button>
          <button
            type="button"
            onClick={() => triggerCycle('full')}
            title="Run full cycle (Phase 1 then chains to Phase 2)"
            className="inline-flex items-center gap-1 text-xs px-2 py-1.5 rounded border border-neutral-700 text-neutral-300 hover:bg-workbench-hover/60 transition-colors min-h-[44px] md:min-h-auto"
          >
            <Zap className="w-3 h-3" /> Full cycle
          </button>
        </div>
      </div>
      )}

      {/* Tabs — horizontal-scroll strip so the dense tab row never forces the
          panel wide on narrow viewports: the strip's box stays within the
          viewport and its buttons scroll instead of overflowing. Opt the scroll
          container out of the overflow/truncation invariants because its buttons
          legitimately extend past the fold inside the scroll region (same pattern
          as HeaderBar's primary-tab nav). Each button is shrink-0 + whitespace-
          nowrap so it keeps its label and scrolls rather than squishing. */}
      {showTabs && (
      <div
        data-viewport-allow-overflow
        data-viewport-allow-truncation
        className="flex items-center border-b border-neutral-800 shrink-0 overflow-x-auto min-w-0"
      >
        <button
          onClick={() => setActiveTab('directions')}
          className={`px-4 py-2.5 text-sm font-medium transition-colors min-h-[44px] md:min-h-auto shrink-0 whitespace-nowrap ${
            activeTab === 'directions'
              ? 'text-neutral-100 border-b-2 border-blue-500'
              : 'text-neutral-500 hover:text-neutral-300'
          }`}
        >
          Directions
        </button>
        <button
          onClick={() => setActiveTab('problem-model')}
          className={`px-4 py-2.5 text-sm font-medium transition-colors min-h-[44px] md:min-h-auto shrink-0 whitespace-nowrap ${
            activeTab === 'problem-model'
              ? 'text-neutral-100 border-b-2 border-cyan-500'
              : 'text-neutral-500 hover:text-neutral-300'
          }`}
        >
          Problem Model
          {(() => {
            const gapCount = (problemModel.gaps ?? []).length;
            return gapCount > 0 ? <span className="ml-1.5 text-xs text-neutral-500">({gapCount})</span> : null;
          })()}
          {sizeHealth && (
            <span className={`ml-1.5 align-middle text-ui-sm px-1.5 py-0.5 rounded border font-semibold whitespace-nowrap ${getStatusColor(sizeHealth.model_size_status)}`}>
              {sizeHealth.model_size_status} · {Math.round(sizeHealth.serialized_bytes / 1024)}KB
            </span>
          )}
        </button>
        <button
          onClick={() => setActiveTab('running-update')}
          className={`px-4 py-2.5 text-sm font-medium transition-colors min-h-[44px] md:min-h-auto shrink-0 whitespace-nowrap ${
            activeTab === 'running-update'
              ? 'text-neutral-100 border-b-2 border-emerald-500'
              : 'text-neutral-500 hover:text-neutral-300'
          }`}
        >
          Running Update
          {runningUpdate && runningUpdate.trim() && (
            <span className="ml-1.5 inline-block w-1.5 h-1.5 rounded-full bg-emerald-400" aria-label="has content" />
          )}
        </button>
        <button
          onClick={() => setActiveTab('design-doc')}
          className={`px-4 py-2.5 text-sm font-medium transition-colors min-h-[44px] md:min-h-auto shrink-0 whitespace-nowrap ${
            activeTab === 'design-doc'
              ? 'text-neutral-100 border-b-2 border-purple-500'
              : 'text-neutral-500 hover:text-neutral-300'
          }`}
        >
          Design Doc
          {(() => {
            const pc = Object.keys(designDoc).filter(k => !k.startsWith('_')).length;
            return pc > 0 ? <span className="ml-1.5 text-xs text-neutral-500">({pc})</span> : null;
          })()}
        </button>
      </div>
      )}
    </>
  );
}
