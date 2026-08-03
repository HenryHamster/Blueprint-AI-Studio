import type React from 'react';
import { ChevronDown, ChevronRight } from 'lucide-react';
import { DirectionCard } from './DirectionCard';
import { ErrorBoundary } from './ErrorBoundary';
import type { Direction } from './DirectionsPanel';
import type { useDirectionForm } from '../hooks/useDirectionForm';

export type DirectionCategoryFilter = 'all' | 'product' | 'structural' | 'mixed';

export interface DirectionGroup {
  key: 'proposed' | 'active' | 'staged' | 'paused' | 'completed' | 'expired';
  label: string;
  items: Direction[];
}

export interface DirectionsListProps {
  readOnly?: boolean;
  loading: boolean;
  selectedProjectId: string | null;
  directions: Direction[];
  grouped: DirectionGroup[];
  activeItems: Direction[];
  collapsed: Record<string, boolean>;
  toggleSection: (key: string) => void;
  categoryFilter: DirectionCategoryFilter;
  setCategoryFilter: (cat: DirectionCategoryFilter) => void;
  form: ReturnType<typeof useDirectionForm>;
  // Handlers
  updateDirection: (id: string) => Promise<void>;
  approveProposal: (id: string) => Promise<void>;
  rejectProposal: (id: string) => Promise<void>;
  reviveProposal: (id: string) => Promise<void>;
  reactivateDirection: (id: string) => Promise<void>;
  activateStagedDirection: (id: string) => Promise<void>;
  retargetStagedDirection: (id: string, version: number) => Promise<void>;
  cancelStagedDirection: (id: string) => Promise<void>;
  unholdDirection: (id: string) => Promise<void>;
  markDirectionComplete: (id: string) => Promise<void>;
  quickToggleStatus: (id: string, currentStatus: 'active' | 'paused') => Promise<void>;
  deleteDirection: (id: string) => Promise<void>;
  applyDirectionUpdate: (d: Direction) => Promise<void>;
  dismissDirectionUpdate: (id: string) => Promise<void>;
  triggerResearch: (id: string, bypass?: boolean) => Promise<void>;
  reorderDirections: (newOrder: Direction[]) => Promise<void>;
  decideChurnRetirement?: (id: string, decision: 'KEEP' | 'RETIRE', rationale?: string) => Promise<void>;
  // Per-direction display state
  taskCounts: Record<string, number>;
  activeTasks: Record<string, Array<{ id: string; status: string; title: string; latest_status_message?: string }>>;
  cooldownMinutes: number;
  researchingIds: Record<string, boolean>;
  researchSuccessIds: Record<string, boolean>;
  forceConfirmId: string | null;
  setForceConfirmId: React.Dispatch<React.SetStateAction<string | null>>;
  // Open release version number staging targets, or null when no version is open
  // (staging is then disabled — the backend rejects staging with no open version).
  stageTargetVersion: number | null;
  onViewFindings?: (directionId: string) => void;
  onViewKnowledge?: (directionId: string) => void;
  onNavigateToTask?: (taskId: string) => void;
}

/**
 * Category filter row + the grouped, collapsible list of DirectionCards.
 * Handles the loading / no-project / empty placeholder states. Extracted
 * verbatim from DirectionsPanel.
 */
export function DirectionsList(props: DirectionsListProps) {
  const {
    loading,
    selectedProjectId,
    directions,
    grouped,
    activeItems,
    collapsed,
    toggleSection,
    categoryFilter,
    setCategoryFilter,
    form,
    taskCounts,
    activeTasks,
    cooldownMinutes,
    researchingIds,
    researchSuccessIds,
    forceConfirmId,
    setForceConfirmId,
    stageTargetVersion,
    onViewFindings,
    onViewKnowledge,
    onNavigateToTask,
    readOnly = true,
  } = props;

  return (
    <>
      {/* Category filter */}
      {!loading && directions.length > 0 && (
        <div className="flex items-center gap-1 mb-3">
          {(['all', 'product', 'structural', 'mixed'] as const).map(cat => (
            <button
              key={cat}
              onClick={() => setCategoryFilter(cat)}
              className={`text-ui-sm px-2 py-0.5 rounded border font-medium transition-colors capitalize min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto ${
                categoryFilter === cat
                  ? cat === 'product' ? 'bg-emerald-950/60 border-emerald-900/50 text-emerald-400'
                    : cat === 'structural' ? 'bg-blue-950/60 border-blue-900/50 text-blue-400'
                    : cat === 'mixed' ? 'bg-purple-950/60 border-purple-900/50 text-purple-400'
                    : 'bg-neutral-700/60 border-neutral-600/50 text-neutral-300'
                  : 'bg-neutral-900/40 border-neutral-700/40 text-neutral-500 hover:text-neutral-300'
              }`}
            >
              {cat}
            </button>
          ))}
        </div>
      )}

      {loading ? (
        <p className="text-xs text-neutral-500 text-center py-8">Loading...</p>
      ) : !selectedProjectId ? (
        <p className="text-xs text-neutral-500 text-center py-8">Select a project</p>
      ) : directions.length === 0 ? (
        <p className="text-xs text-neutral-500 text-center py-8">No directions yet — use the form above to add one</p>
      ) : (
        <div className="space-y-4">
          {grouped.map(({ key, label, items }) => {
            if (items.length === 0) return null;
            const isCollapsed = collapsed[key] ?? false;
            return (
              <div key={key}>
                <button
                  onClick={() => toggleSection(key)}
                  className="flex items-center gap-1.5 w-full text-left mb-2 group min-h-[44px] md:min-h-auto"
                >
                  {isCollapsed
                    ? <ChevronRight className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                    : <ChevronDown className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                  }
                  <span className="text-ui-md font-semibold text-neutral-400 uppercase tracking-wider group-hover:text-neutral-300 transition-colors">
                    {label} ({items.length})
                  </span>
                  {key === 'staged' && stageTargetVersion != null && (
                    <span className="text-ui-sm text-sky-400/70 normal-case">
                      current open: v{stageTargetVersion}
                    </span>
                  )}
                </button>
                {!isCollapsed && (
                  <div className="space-y-2">
                    {items.map(d => (
                      <ErrorBoundary key={d.id} level="widget">
                      <DirectionCard
                        readOnly={readOnly}
                        direction={d}
                        statusKey={key}
                        activeItems={activeItems}
                        editingId={form.editingId}
                        editText={form.editText}
                        editPriority={form.editPriority}
                        editStatus={form.editStatus}
                        editCategory={form.editCategory}
                        editPathTags={form.editPathTags}
                        editPathInput={form.editPathInput}
                        editCompletionCriteria={form.editCompletionCriteria}
                        editSuccessEvidence={form.editSuccessEvidence}
                        editRetirementCondition={form.editRetirementCondition}
                        editCostBudget={form.editCostBudget}
                        setEditCostBudget={form.setEditCostBudget}
                        setEditingId={form.setEditingId}
                        setEditText={form.setEditText}
                        setEditPriority={form.setEditPriority}
                        setEditStatus={form.setEditStatus}
                        setEditCategory={form.setEditCategory}
                        setEditPathTags={form.setEditPathTags}
                        setEditPathInput={form.setEditPathInput}
                        setEditCompletionCriteria={form.setEditCompletionCriteria}
                        setEditSuccessEvidence={form.setEditSuccessEvidence}
                        setEditRetirementCondition={form.setEditRetirementCondition}
                        startEdit={form.startEdit}
                        updateDirection={props.updateDirection}
                        approveProposal={props.approveProposal}
                        rejectProposal={props.rejectProposal}
                        reviveProposal={props.reviveProposal}
                        reactivateDirection={props.reactivateDirection}
                        activateStagedDirection={props.activateStagedDirection}
                        retargetStagedDirection={props.retargetStagedDirection}
                        cancelStagedDirection={props.cancelStagedDirection}
                        unholdDirection={props.unholdDirection}
                        markDirectionComplete={props.markDirectionComplete}
                        quickToggleStatus={props.quickToggleStatus}
                        deleteDirection={props.deleteDirection}
                        applyDirectionUpdate={props.applyDirectionUpdate}
                        dismissDirectionUpdate={props.dismissDirectionUpdate}
                        triggerResearch={props.triggerResearch}
                        reorderDirections={props.reorderDirections}
                        decideChurnRetirement={props.decideChurnRetirement}
                        taskCounts={taskCounts}
                        activeTasks={activeTasks}
                        cooldownMinutes={cooldownMinutes}
                        researchingIds={researchingIds}
                        researchSuccessIds={researchSuccessIds}
                        forceConfirmId={forceConfirmId}
                        setForceConfirmId={setForceConfirmId}
                        stageTargetVersion={stageTargetVersion}
                        onViewFindings={onViewFindings}
                        onViewKnowledge={onViewKnowledge}
                        onNavigateToTask={onNavigateToTask}
                      />
                      </ErrorBoundary>
                    ))}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}
    </>
  );
}
