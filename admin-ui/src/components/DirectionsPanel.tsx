import { useState, useMemo, useCallback } from 'react';
import { ChevronDown, Plus, X } from 'lucide-react';
import { useStrategistStatus } from '../hooks/useStrategistStatus';
import { StrategistStatusChip } from './StrategistStatusChip';
import DesignDocViewer from './DesignDocViewer';
import ProblemModelViewer from './ProblemModelViewer';
import RunningUpdatePanel from './RunningUpdatePanel';
import { AutonomousConfigSection } from './AutonomousConfigSection';
import { DirectionsPanelHeader, type DirectionsTab } from './DirectionsPanelHeader';
import { DirectionsAddForm } from './DirectionsAddForm';
import { DirectionsQuickLinks } from './DirectionsQuickLinks';
import { DirectionsList, type DirectionCategoryFilter } from './DirectionsList';
import { useDirectionsData } from '../hooks/useDirectionsData';
import type { Project } from '../types/project';
import type { OutcomeKindBreakdown } from '../utils/outcomeKinds';

export interface ResearchHistoryEntry {
  timestamp: string;
  task_id: string;
  tasks_created: number;
  finding_titles: string[];
  key_finding_descriptions: string[];
  tasks_completed?: number;
  tasks_failed?: number;
  cycle_insights?: string[];
  product_ratio?: number | null;
  type_distribution?: Record<string, number> | null;
  prs_merged?: number | null;
  prs_rejected?: number | null;
  taxonomy_summary?: Record<string, number> | null;
  autonomous_prs_merged?: number | null;
  assisted_prs_merged?: number | null;
}

export interface RemediationHistoryEntry {
  task_id?: string | null;
  result?: 'merged' | 'failed' | 'partial_success' | 'held' | 'pending' | string | null;
  timestamp?: string | null;
  fingerprint?: string | null;
  failure_class?: string | null;
  error_summary?: string | null;
  held_at?: string | null;
  partial_success_at?: string | null;
  [key: string]: unknown;
}

export interface Direction {
  id: string;
  text: string;
  priority: 'high' | 'medium' | 'low';
  added_at: string;
  status: 'active' | 'completed' | 'paused' | 'proposed' | 'expired' | 'staged';
  category?: 'product' | 'structural' | 'mixed' | null;
  // When status is 'staged', the version_number this direction is waiting to be
  // promoted past; it auto-activates when that version promotes and the next opens.
  staged_for_version?: number | null;
  // ISO timestamp stamped when the direction entered the 'staged' status (set on
  // every staging path: operator add, status-change-to-staged, agent create/stage).
  staged_at?: string | null;
  completion_criteria?: string | null;
  paths?: string[];
  last_researched_at?: string | null;
  refractory_until?: string | null;
  refractory_reason?: 'standard' | 'saturation' | null;
  refractory_remaining_minutes?: number | null;
  proposed_by?: string;
  proposal_rationale?: string;
  proposed_at?: string;
  research_history?: ResearchHistoryEntry[];
  possibly_saturated?: boolean;
  possibly_complete?: boolean;
  completion_suggestion?: string;
  pending_update?: {
    suggested_text?: string;
    suggested_priority?: string;
    suggested_paths?: string[];
    rationale: string;
    proposed_at: string;
    proposed_by: string;
  };
  notes?: Array<{
    text: string;
    added_at: string;
    added_by_task: string;
  }>;
  paused_reason?: string;
  paused_at?: string;
  auto_paused?: boolean;
  // Hold state: a direction with repeated CODE_ERROR failures is held from
  // autonomous spawning until manually cleared or the 2-hour timeout elapses.
  // Populated by backend GET /directions response. held_reason is null when
  // held=false.
  held?: boolean;
  held_reason?: string | null;
  held_at?: string | null;
  held_until?: string | null;
  // Diagnosis / remediation pipeline state (surfaced from backend direction dict)
  needs_manual_intervention?: boolean;
  diagnosis_in_progress?: boolean;
  diagnosis_summary?: string;
  diagnosis_completed_at?: string;
  remediation_attempts?: number;
  last_remediation_result?: 'merged' | 'failed' | 'pending' | string;
  remediation_monitor_started_at?: string;
  remediation_history?: RemediationHistoryEntry[];
  /**
   * Merged PRs per research cycle (unbounded, can exceed 1).
   * Null when the direction has never been researched.
   */
  merges_per_cycle?: number | null;
  history_aggregate?: {
    total_prs_merged?: number | null;
    total_prs_rejected?: number | null;
    total_tasks_failed?: number | null;
    total_autonomous_prs_merged?: number | null;
    total_assisted_prs_merged?: number | null;
    total_operator_actions?: number | null;
    [key: string]: unknown;
  } | null;
  cost_budget_usd?: number | null;
  estimated_cost_usd?: number | null;
  budget_utilization_pct?: number | null;
  budget_exceeded?: boolean;
  quality_score?: number | null;
  // Generalized cross-task-type success metrics, emitted by GET /directions
  // alongside the legacy PR-merge counters. `outcome_kind_breakdown` is keyed
  // by outcome kind (code_merge, research_quality, audit_findings, …).
  generalized_successes?: number | null;
  generalized_health_rate?: number | null;
  outcome_kind_breakdown?: OutcomeKindBreakdown | null;
  // Count of durable knowledge documents (web_research output) accumulated by
  // this direction, emitted by GET /directions.
  knowledge_document_count?: number | null;
  cost_per_merged_pr?: number | null;
  cost_per_merge_exceeds_threshold?: boolean;
  // Per-(provider, model) cost breakdown for the digest window. Keys are
  // `${provider}|${model}` (e.g. `anthropic|claude-opus-4-7`); values are
  // dollars rounded to 4 places. Null when no tasks have run yet for the
  // direction in the window.
  cost_by_provider_model?: Record<string, number> | null;
  retirement_consideration_evidence?: {
    window_cycles?: number;
    direction_cost_per_merge_usd?: number;
    threshold_usd?: number;
    cost_by_provider_model?: Record<string, { cost_usd: number; share_pct: number }> | null;
  } | null;
  churn_metrics?: {
    window_cycles: number;
    merged: number;
    non_merge_finished: number;
    merge_rate: number;
    threshold: number;
    min_merge_rate: number;
  } | null;
  lifetime_burn_pause_evidence?: {
    lifetime_merged: number;
    lifetime_non_merge_finished: number;
    lifetime_merge_rate: number;
  } | null;
  // Direction context captured at creation: the stated outcome (success_evidence),
  // its retirement condition, and any implementation constraints.
  context?: {
    success_evidence?: string;
    retirement_condition?: string;
    implementation_constraints?: string;
    [key: string]: unknown;
  } | null;
  // Churn-retirement recommendation: set by the zombie-retire ladder when a
  // paused direction has hit the max churn-pause-cycles threshold. The direction
  // is held PAUSED pending an explicit operator Keep/Retire decision. All three
  // fields are cleared once the operator submits a decision.
  churn_retirement_recommended?: boolean;
  churn_retirement_recommended_reason?: string | null;
  churn_retirement_recommended_at?: string | null;
  // Advisory outcome-less-scaffolding flag: set by the backend detector when a
  // long-running direction keeps merging PRs at a healthy rate but shows no
  // progress toward its stated success_evidence. ADVISORY ONLY — never auto-pauses.
  outcome_scaffolding_flagged?: boolean;
  outcome_scaffolding_flagged_at?: string | null;
  outcome_scaffolding_evidence?: {
    total_cycles: number;
    lifetime_merged: number;
    lifetime_merge_rate: number;
    stall_run_cycles: number;
    progress_signal: number;
    outcome_progress_window: number[];
  } | null;
}


const STATUS_GROUPS: Array<{ key: 'proposed' | 'active' | 'staged' | 'paused' | 'completed' | 'expired'; label: string }> = [
  { key: 'proposed', label: 'Proposed by Agent' },
  { key: 'active', label: 'Active' },
  { key: 'staged', label: 'Staged for next version' },
  { key: 'paused', label: 'Paused' },
  { key: 'completed', label: 'Completed' },
  { key: 'expired', label: 'Expired' },
];

const priorityOrder: Record<string, number> = { high: 0, medium: 1, low: 2 };

export function DirectionsPanel({
  addToast,
  onViewFindings,
  onViewKnowledge,
  onNavigateToTask,
  selectedProjectId,
  projects = [],
  fetchDirectionsRef,
  strategistActive,
  cycleProgress,
  lastCycleCompletion,
  activeTab: controlledActiveTab,
  onActiveTabChange,
  showHeaderTitle = true,
  showCycleControls = true,
  showInternalTabs = true,
  readOnly = true,
  canManageGlobalSettings = false,
}: {
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  onViewFindings?: (directionId: string) => void;
  onViewKnowledge?: (directionId: string) => void;
  onNavigateToTask?: (taskId: string) => void;
  selectedProjectId: string | null;
  projects?: Project[];
  fetchDirectionsRef?: React.MutableRefObject<() => void>;
  strategistActive?: boolean;
  cycleProgress?: import('../hooks/useLogViewer').StrategicCycleProgress | null;
  lastCycleCompletion?: import('../hooks/useLogViewer').StrategicCycleCompletion | null;
  activeTab?: DirectionsTab;
  onActiveTabChange?: (tab: DirectionsTab) => void;
  showHeaderTitle?: boolean;
  showCycleControls?: boolean;
  showInternalTabs?: boolean;
  readOnly?: boolean;
  canManageGlobalSettings?: boolean;
}) {
  // Active tab
  const [uncontrolledActiveTab, setUncontrolledActiveTab] = useState<DirectionsTab>('directions');
  const activeTab = controlledActiveTab ?? uncontrolledActiveTab;
  const setActiveTab = (tab: DirectionsTab) => {
    if (controlledActiveTab === undefined) setUncontrolledActiveTab(tab);
    onActiveTabChange?.(tab);
  };

  // Collapsible sections — expired collapsed by default, others expanded
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({ expired: true });

  // Add-direction form starts collapsed; operator expands via "+ New direction"
  const [formExpanded, setFormExpanded] = useState(false);

  // Category filter
  const [categoryFilter, setCategoryFilter] = useState<DirectionCategoryFilter>('all');

  // All data state + API handlers (+ form / autonomous-config sub-hooks)
  const data = useDirectionsData({
    selectedProjectId,
    addToast,
    fetchDirectionsRef,
    canOperateProject: !readOnly,
    canManageGlobalSettings,
  });
  const { form, config } = data;

  function handleSpawnDirection(gapId: string, gapName: string) {
    form.setNewText(gapName);
    form.setNewLinkedGapId(gapId);
    setActiveTab('directions');
    setFormExpanded(true);
  }

  function toggleSection(key: string) {
    setCollapsed(prev => ({ ...prev, [key]: !prev[key] }));
  }

  const categoryFilterFn = useCallback(
    (d: Direction) => categoryFilter === 'all' || d.category === categoryFilter,
    [categoryFilter]
  );

  const grouped = useMemo(() => STATUS_GROUPS.map(({ key, label }) => ({
    key,
    label,
    items: key === 'active'
      ? data.directions.filter(d => d.status === key).filter(categoryFilterFn)
      : data.directions
          .filter(d => d.status === key)
          .filter(categoryFilterFn)
          .sort((a, b) => priorityOrder[a.priority] - priorityOrder[b.priority]),
  })), [data.directions, categoryFilterFn]);

  const proposedCount = useMemo(
    () => data.directions.filter(d => d.status === 'proposed').length,
    [data.directions]
  );
  const activeItems = useMemo(
    () => (grouped.find(g => g.key === 'active') ?? { items: [] }).items,
    [grouped]
  );

  const strategistStatus = useStrategistStatus({ strategistActive, cycleProgress, lastCycleCompletion });
  const showStrategistChip = strategistStatus.phase !== 'idle' && (strategistActive || lastCycleCompletion);

  return (
      <div className="flex-1 min-h-0 overflow-hidden flex flex-col bg-neutral-950">
        <DirectionsPanelHeader
          selectedProjectId={selectedProjectId}
          projects={projects}
          proposedCount={proposedCount}
          activeTab={activeTab}
          setActiveTab={setActiveTab}
          problemModel={data.problemModel}
          sizeHealth={data.problemModelSizeHealth}
          designDoc={data.designDoc}
          runningUpdate={data.runningUpdate}
          triggerCycle={data.triggerCycle}
          showTitle={showHeaderTitle}
          showCycleControls={showCycleControls && !readOnly}
          showTabs={showInternalTabs}
        />
        {showStrategistChip && (
          <div className="px-5 py-2 border-b border-neutral-800 shrink-0">
            <StrategistStatusChip status={strategistStatus} />
          </div>
        )}
        <div className="flex-1 min-h-0 overflow-hidden flex flex-col">
          {activeTab === 'directions' && (<>
          <div className="px-5 py-3 border-b border-neutral-800 space-y-3 shrink-0">
            {selectedProjectId && (
              <>
                {/* The bare On/Off toggle that used to live here is superseded by
                    the first-class AutonomousStatus control in the header (which
                    also surfaces lifecycle, circuit-breaker and blocking-guard
                    status). Only the autonomy *configuration* remains here. */}
                <p className="text-xs text-neutral-500 leading-relaxed">
                  Active directions guide the research agent. Only Active directions are included in new tasks.
                </p>
                {canManageGlobalSettings && !readOnly && <AutonomousConfigSection
                  configCooldownHours={config.configCooldownHours}
                  setConfigCooldownHours={config.setConfigCooldownHours}
                  configProductiveCooldownMinutes={config.configProductiveCooldownMinutes}
                  setConfigProductiveCooldownMinutes={config.setConfigProductiveCooldownMinutes}
                  configIntervalMinutes={config.configIntervalMinutes}
                  setConfigIntervalMinutes={config.setConfigIntervalMinutes}
                  configMaxParallel={config.configMaxParallel}
                  setConfigMaxParallel={config.setConfigMaxParallel}
                  configMaxTotalResearch={config.configMaxTotalResearch}
                  setConfigMaxTotalResearch={config.setConfigMaxTotalResearch}
                  configMaxTasksPerHour={config.configMaxTasksPerHour}
                  setConfigMaxTasksPerHour={config.setConfigMaxTasksPerHour}
                  configMaxConcurrentAutoRemediations={config.configMaxConcurrentAutoRemediations}
                  setConfigMaxConcurrentAutoRemediations={config.setConfigMaxConcurrentAutoRemediations}
                  configResidualRemediationCooldownHours={config.configResidualRemediationCooldownHours}
                  setConfigResidualRemediationCooldownHours={config.setConfigResidualRemediationCooldownHours}
                  configResidualAuditDeferEscalationThreshold={config.configResidualAuditDeferEscalationThreshold}
                  setConfigResidualAuditDeferEscalationThreshold={config.setConfigResidualAuditDeferEscalationThreshold}
                  configMaxActiveDirections={config.configMaxActiveDirections}
                  setConfigMaxActiveDirections={config.setConfigMaxActiveDirections}
                  configArchitectureReviewFrequency={config.configArchitectureReviewFrequency}
                  setConfigArchitectureReviewFrequency={config.setConfigArchitectureReviewFrequency}
                  configStrategicMaxIntervalMinutes={config.configStrategicMaxIntervalMinutes}
                  setConfigStrategicMaxIntervalMinutes={config.setConfigStrategicMaxIntervalMinutes}
                  configMaxProvisionRetries={config.configMaxProvisionRetries}
                  setConfigMaxProvisionRetries={config.setConfigMaxProvisionRetries}
                  configMaxRollupRetries={config.configMaxRollupRetries}
                  setConfigMaxRollupRetries={config.setConfigMaxRollupRetries}
                  configDriftOverClaimMinClaims={config.configDriftOverClaimMinClaims}
                  setConfigDriftOverClaimMinClaims={config.setConfigDriftOverClaimMinClaims}
                  configDriftConsistencyThreshold={config.configDriftConsistencyThreshold}
                  setConfigDriftConsistencyThreshold={config.setConfigDriftConsistencyThreshold}
                  configDriftStallMinClaims={config.configDriftStallMinClaims}
                  setConfigDriftStallMinClaims={config.setConfigDriftStallMinClaims}
                  autonomousConfigOpen={config.autonomousConfigOpen}
                  setAutonomousConfigOpen={config.setAutonomousConfigOpen}
                  savingConfigField={config.savingConfigField}
                  saveConfigField={config.saveConfigField}
                  clearResidualFingerprints={config.clearResidualFingerprints}
                />}
              </>
            )}
          </div>
          {selectedProjectId && !readOnly && (
            <>
              <div className={`px-5 py-2 shrink-0 ${!formExpanded ? 'border-b border-neutral-800' : ''}`}>
                <button
                  onClick={() => setFormExpanded(v => !v)}
                  className="flex items-center gap-1.5 text-xs text-neutral-400 hover:text-neutral-200 transition-colors min-h-[44px] md:min-h-[36px]"
                >
                  {formExpanded
                    ? <><ChevronDown className="w-3.5 h-3.5" /><span>Cancel</span></>
                    : <><Plus className="w-3.5 h-3.5" /><span>New direction</span></>}
                </button>
              </div>
              {formExpanded && (
                <DirectionsAddForm form={form} addDirection={() => { data.addDirection(); setFormExpanded(false); }} stageTargetVersion={data.openVersionNumber} linkedGapId={form.newLinkedGapId} />
              )}
            </>
          )}
          {data.error && (
            <div className="px-5 py-2 bg-red-950/50 border-b border-red-900/50 flex items-center justify-between shrink-0">
              <span className="text-xs text-red-400">{data.error}</span>
              <button onClick={() => data.setError(null)} className="text-red-400 hover:text-red-300 transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center">
                <X className="w-3 h-3" />
              </button>
            </div>
          )}
          <div className="flex-1 overflow-y-auto custom-scrollbar px-5 py-3">
          <DirectionsQuickLinks
            selectedProjectId={selectedProjectId}
            loading={data.loading}
            problemModel={data.problemModel}
            runningUpdate={data.runningUpdate}
            designDoc={data.designDoc}
            setActiveTab={setActiveTab}
          />
          <DirectionsList
            loading={data.loading}
            selectedProjectId={selectedProjectId}
            directions={data.directions}
            grouped={grouped}
            activeItems={activeItems}
            collapsed={collapsed}
            toggleSection={toggleSection}
            categoryFilter={categoryFilter}
            setCategoryFilter={setCategoryFilter}
            form={form}
            updateDirection={data.updateDirection}
            approveProposal={data.approveProposal}
            rejectProposal={data.rejectProposal}
            reviveProposal={data.reviveProposal}
            reactivateDirection={data.reactivateDirection}
            activateStagedDirection={data.activateStagedDirection}
            retargetStagedDirection={data.retargetStagedDirection}
            cancelStagedDirection={data.cancelStagedDirection}
            unholdDirection={data.unholdDirection}
            markDirectionComplete={data.markDirectionComplete}
            quickToggleStatus={data.quickToggleStatus}
            deleteDirection={data.deleteDirection}
            applyDirectionUpdate={data.applyDirectionUpdate}
            dismissDirectionUpdate={data.dismissDirectionUpdate}
            triggerResearch={data.triggerResearch}
            reorderDirections={data.reorderDirections}
            decideChurnRetirement={data.decideChurnRetirement}
            taskCounts={data.taskCounts}
            activeTasks={data.activeTasks}
            cooldownMinutes={config.cooldownMinutes}
            researchingIds={data.researchingIds}
            researchSuccessIds={data.researchSuccessIds}
            forceConfirmId={data.forceConfirmId}
            setForceConfirmId={data.setForceConfirmId}
            stageTargetVersion={data.openVersionNumber}
            onViewFindings={onViewFindings}
            onViewKnowledge={onViewKnowledge}
            onNavigateToTask={onNavigateToTask}
            readOnly={readOnly}
          />
          </div>
          </>)}
          {activeTab === 'problem-model' && selectedProjectId && (
            <div className="flex-1 min-h-0 overflow-hidden">
              <ProblemModelViewer
                projectId={selectedProjectId}
                problemModel={data.problemModel}
                selfReflectionLog={data.selfReflectionLog}
                currentVersion={data.currentVersion}
                addToast={addToast}
                onUpdated={data.setProblemModel}
                onSpawnDirection={readOnly ? undefined : handleSpawnDirection}
                onLinkDirection={readOnly ? undefined : data.linkDirectionToGap}
                readOnly={readOnly}
                directions={data.directions
                  .filter(d => d.status === 'active')
                  .map(d => ({ id: d.id, text: d.text }))}
              />
            </div>
          )}
          {activeTab === 'running-update' && selectedProjectId && (
            <div className="flex-1 min-h-0 overflow-hidden">
              <RunningUpdatePanel
                projectId={selectedProjectId}
                document={data.runningUpdate}
                acknowledgedAt={data.runningUpdateAckAt}
                reconciliation={data.runningUpdateReconciliation}
                addToast={addToast}
                onAcknowledged={() => { data.setRunningUpdateAckAt(new Date().toISOString()); }}
                onEdited={(newBody) => { data.setRunningUpdate(newBody); }}
                readOnly={readOnly}
              />
            </div>
          )}
          {activeTab === 'design-doc' && (
            <div className="flex-1 min-h-0 overflow-hidden">
              <DesignDocViewer designDoc={data.designDoc} />
            </div>
          )}
        </div>
      </div>
    );
}
