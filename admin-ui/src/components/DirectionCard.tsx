import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type React from 'react';
import { AlertTriangle, Check, ChevronDown, ChevronRight, ChevronUp, Loader2, Pencil, Trash2, X, Zap } from 'lucide-react';
import { BarChart, Bar, Cell, ResponsiveContainer, Tooltip as RechartsTooltip } from 'recharts';
import type { Direction } from './DirectionsPanel';
import type { DirectionCategory } from '../hooks/useDirectionForm';
import { formatRelativeTime, formatRemainingMinutes, getStagedWaitingInfo, STAGED_STALE_DAYS, priorityColors, statusColors } from '../utils/directionHelpers';
import { parseTimestamp } from '../utils/time';
import { healthRateColorClass, summarizeOutcomeKinds } from '../utils/outcomeKinds';

const TAXONOMY_ORDER = ['scope_related', 'review_quality', 'infra_related', 'code_defect'] as const;
const TAXONOMY_LABELS: Record<string, string> = {
  scope_related: 'scope',
  review_quality: 'review',
  infra_related: 'infra',
  code_defect: 'code',
};
const TAXONOMY_COLORS: Record<string, string> = {
  scope_related: 'bg-cyan-500/20 text-cyan-400 border-cyan-500/30',
  review_quality: 'bg-purple-500/20 text-purple-400 border-purple-500/30',
  infra_related: 'bg-amber-500/20 text-amber-400 border-amber-500/30',
  code_defect: 'bg-red-500/20 text-red-400 border-red-500/30',
};

type RemediationSummaryRow = {
  key: string;
  resultLabel: string;
  badgeClass: string;
  fingerprintShort: string | null;
  timestamp: string | null;
  fingerprintChange: 'changed' | 'same' | null;
};

function formatAutoRecoveryLine(heldUntil: string | null | undefined): string | null {
  if (!heldUntil) return null;
  const heldUntilDate = parseTimestamp(heldUntil);
  if (Number.isNaN(heldUntilDate.getTime())) return null;

  const remainingMs = heldUntilDate.getTime() - Date.now();
  if (remainingMs <= 0) return 'auto-recovery overdue';

  const totalMinutes = Math.ceil(remainingMs / 60000);
  if (totalMinutes < 60) return `auto-recovers in ${totalMinutes}m`;
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  return minutes > 0 ? `auto-recovers in ${hours}h ${minutes}m` : `auto-recovers in ${hours}h`;
}

function remediationResultMeta(result: string | null | undefined): Pick<RemediationSummaryRow, 'resultLabel' | 'badgeClass'> {
  const normalized = (result || '').toLowerCase();
  if (normalized === 'merged') {
    return { resultLabel: 'merged', badgeClass: 'bg-emerald-950/40 border-emerald-900/50 text-emerald-400' };
  }
  if (normalized === 'failed') {
    return { resultLabel: 'failed', badgeClass: 'bg-red-950/40 border-red-900/50 text-red-400' };
  }
  if (normalized === 'partial_success') {
    return { resultLabel: 'partial', badgeClass: 'bg-amber-950/40 border-amber-900/50 text-amber-400' };
  }
  if (normalized === 'held') {
    return { resultLabel: 'held', badgeClass: 'bg-red-950/30 border-red-900/50 text-red-300' };
  }
  return { resultLabel: normalized || 'pending', badgeClass: 'bg-neutral-900/60 border-neutral-700/60 text-neutral-300' };
}

export interface DirectionCardProps {
  direction: Direction;
  statusKey: string;
  activeItems: Direction[];
  // Edit form state
  editingId: string | null;
  editText: string;
  editPriority: 'high' | 'medium' | 'low';
  editStatus: 'active' | 'completed' | 'paused' | 'proposed' | 'expired' | 'staged';
  editCategory: DirectionCategory;
  editPathTags: string[];
  editPathInput: string;
  editCompletionCriteria: string;
  editSuccessEvidence: string;
  editRetirementCondition: string;
  editCostBudget?: string;
  setEditingId: React.Dispatch<React.SetStateAction<string | null>>;
  setEditText: React.Dispatch<React.SetStateAction<string>>;
  setEditPriority: React.Dispatch<React.SetStateAction<'high' | 'medium' | 'low'>>;
  setEditStatus: React.Dispatch<React.SetStateAction<'active' | 'completed' | 'paused' | 'proposed' | 'expired' | 'staged'>>;
  setEditCategory: React.Dispatch<React.SetStateAction<DirectionCategory>>;
  setEditPathTags: React.Dispatch<React.SetStateAction<string[]>>;
  setEditPathInput: React.Dispatch<React.SetStateAction<string>>;
  setEditCompletionCriteria: React.Dispatch<React.SetStateAction<string>>;
  setEditSuccessEvidence: React.Dispatch<React.SetStateAction<string>>;
  setEditRetirementCondition: React.Dispatch<React.SetStateAction<string>>;
  setEditCostBudget?: React.Dispatch<React.SetStateAction<string>>;
  startEdit: (d: Direction) => void;
  // Action handlers
  updateDirection: (id: string) => Promise<void>;
  approveProposal: (id: string) => Promise<void>;
  rejectProposal: (id: string) => Promise<void>;
  reviveProposal: (id: string) => Promise<void>;
  reactivateDirection: (id: string) => Promise<void>;
  activateStagedDirection: (id: string) => Promise<void>;
  retargetStagedDirection?: (id: string, version: number) => Promise<void>;
  cancelStagedDirection: (id: string) => Promise<void>;
  unholdDirection: (id: string) => Promise<void>;
  markDirectionComplete: (id: string) => Promise<void>;
  quickToggleStatus: (id: string, status: 'active' | 'paused') => Promise<void>;
  deleteDirection: (id: string) => Promise<void>;
  applyDirectionUpdate: (d: Direction) => Promise<void>;
  dismissDirectionUpdate: (id: string) => Promise<void>;
  triggerResearch: (id: string, bypass?: boolean) => Promise<void>;
  reorderDirections: (newOrder: Direction[]) => Promise<void>;
  decideChurnRetirement?: (id: string, decision: 'KEEP' | 'RETIRE', rationale?: string) => Promise<void>;
  // State
  taskCounts: Record<string, number>;
  activeTasks?: Record<string, Array<{ id: string; status: string; title: string; latest_status_message?: string }>>;
  cooldownMinutes: number;
  researchingIds: Record<string, boolean>;
  researchSuccessIds: Record<string, boolean>;
  // Force confirm popover
  forceConfirmId: string | null;
  setForceConfirmId: React.Dispatch<React.SetStateAction<string | null>>;
  // Open release version to stage against, or null when none is open. When null,
  // the edit-form "Staged" status option is disabled (the backend rejects staging
  // with no open version).
  stageTargetVersion?: number | null;
  // Navigation callbacks
  onViewFindings?: (directionId: string) => void;
  onViewKnowledge?: (directionId: string) => void;
  onNavigateToTask?: (taskId: string) => void;
}

function DirectionCardImpl({
  direction: d,
  statusKey: key,
  activeItems,
  editingId, editText, editPriority, editStatus, editCategory, editPathTags, editPathInput, editCompletionCriteria, editSuccessEvidence, editRetirementCondition, editCostBudget,
  setEditingId, setEditText, setEditPriority, setEditStatus, setEditCategory, setEditPathTags, setEditPathInput, setEditCompletionCriteria, setEditSuccessEvidence, setEditRetirementCondition, setEditCostBudget,
  startEdit,
  updateDirection, approveProposal, rejectProposal, reviveProposal,
  reactivateDirection, activateStagedDirection, retargetStagedDirection, cancelStagedDirection, unholdDirection, markDirectionComplete, quickToggleStatus, deleteDirection,
  applyDirectionUpdate, dismissDirectionUpdate, triggerResearch, reorderDirections, decideChurnRetirement,
  taskCounts, activeTasks, researchingIds, researchSuccessIds,
  forceConfirmId, setForceConfirmId,
  stageTargetVersion,
  onViewFindings, onViewKnowledge, onNavigateToTask,
}: DirectionCardProps) {
  const forceConfirmRef = useRef<HTMLDivElement | null>(null);
  const retireConfirmRef = useRef<HTMLDivElement | null>(null);
  const [retireConfirmOpen, setRetireConfirmOpen] = useState(false);
  const [expandedNotes, setExpandedNotes] = useState<Set<number>>(new Set());
  const [notesOpen, setNotesOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [expandedRemediationIds, setExpandedRemediationIds] = useState<Record<string, boolean>>({});
  const showAllRemediation = Boolean(expandedRemediationIds[d.id]);
  // Pending retarget version for a staged direction (the version input draft).
  const [retargetVersion, setRetargetVersion] = useState<string>('');

  const autoRecoveryLine = useMemo(
    () => formatAutoRecoveryLine(d.held_until),
    [d.held_until],
  );

  // Generalized cross-task-type outcome lanes. Only surfaced when a non-code-
  // merge lane has terminal outcomes — otherwise the count equals merged PRs
  // and the existing PR badges already represent it (honest legacy degrade).
  const outcomeRows = useMemo(
    () => summarizeOutcomeKinds(d.outcome_kind_breakdown),
    [d.outcome_kind_breakdown],
  );
  const showGeneralized = outcomeRows.some(row => row.kind !== 'code_merge');

  const remediationSummaryRows = useMemo<RemediationSummaryRow[]>(() => {
    const history = Array.isArray(d.remediation_history) ? d.remediation_history : [];
    if (history.length === 0) return [];

    const sorted = [...history].sort((a, b) => {
      const aTs = parseTimestamp(a.timestamp ?? '').getTime();
      const bTs = parseTimestamp(b.timestamp ?? '').getTime();
      return bTs - aTs;
    });

    return sorted.slice(0, 3).map((entry, idx) => {
      const resultMeta = remediationResultMeta(typeof entry.result === 'string' ? entry.result : null);
      const currentFp = typeof entry.fingerprint === 'string' ? entry.fingerprint : null;
      const previousFp = typeof sorted[idx + 1]?.fingerprint === 'string' ? sorted[idx + 1]?.fingerprint : null;
      const fingerprintChange = currentFp && previousFp ? (currentFp === previousFp ? 'same' : 'changed') : null;
      return {
        key: entry.task_id || `${entry.timestamp || 'unknown'}-${idx}`,
        resultLabel: resultMeta.resultLabel,
        badgeClass: resultMeta.badgeClass,
        fingerprintShort: currentFp ? currentFp.slice(0, 8) : null,
        timestamp: typeof entry.timestamp === 'string' ? entry.timestamp : null,
        fingerprintChange,
      };
    });
  }, [d.remediation_history]);

  const lifetimeMetrics = useMemo(() => {
    const history = d.research_history;
    if (!history || history.length === 0) return null;

    const historyMerged = history.reduce((sum, e) => sum + (e.prs_merged ?? 0), 0);
    const historyFailed = history.reduce((sum, e) => sum + (e.tasks_failed ?? 0), 0);
    const aggMerged = d.history_aggregate?.total_prs_merged ?? 0;
    const aggFailed = d.history_aggregate?.total_tasks_failed ?? 0;
    const totalMerged = historyMerged + (aggMerged as number);
    const totalFailed = historyFailed + (aggFailed as number);

    // Attribution split — only present when sibling cluster has landed
    const hasAttribution = history.some(e => e.autonomous_prs_merged != null || e.assisted_prs_merged != null);
    const totalAutonomous = hasAttribution
      ? history.reduce((sum, e) => sum + (e.autonomous_prs_merged ?? 0), 0) + ((d.history_aggregate?.total_autonomous_prs_merged as number | null) ?? 0)
      : null;
    const totalAssisted = hasAttribution
      ? history.reduce((sum, e) => sum + (e.assisted_prs_merged ?? 0), 0) + ((d.history_aggregate?.total_assisted_prs_merged as number | null) ?? 0)
      : null;
    const totalOperatorActions = ((d.history_aggregate?.total_operator_actions as number | null) ?? null);
    const totalTasks = totalMerged + totalFailed;
    const successRate = totalTasks >= 3 ? Math.round((totalMerged / totalTasks) * 100) : null;

    const latestShipped = [...history].reverse().find(e => (e.prs_merged ?? 0) > 0 && e.finding_titles.length > 0);
    const latestTitle = latestShipped?.finding_titles[0];
    const truncatedTitle = latestTitle && latestTitle.length > 60 ? latestTitle.slice(0, 60) + '…' : latestTitle;

    const sparkData = history.map((e, i) => ({
      cycle: i,
      prs: e.prs_merged ?? 0,
      timestamp: e.timestamp,
      findings: e.finding_titles,
    }));
    const lastEntry = history[history.length - 1];
    const lastPrs = lastEntry.prs_merged ?? 0;
    let recentNoMergeCount = 0;
    for (let i = history.length - 1; i >= 0; i--) {
      if ((history[i].prs_merged ?? 0) > 0) break;
      recentNoMergeCount++;
    }
    const lastCycleLabel = lastPrs > 0
      ? `Last cycle: ${lastPrs} PR${lastPrs !== 1 ? 's' : ''}`
      : `No merges in last ${recentNoMergeCount} cycle${recentNoMergeCount !== 1 ? 's' : ''}`;

    const prevEntry = history.length >= 2 ? history[history.length - 2] : null;
    const deltaPrs = prevEntry != null ? lastPrs - (prevEntry.prs_merged ?? 0) : null;
    const rollingAvg3 = history.length >= 3
      ? history.slice(-3).reduce((s, e) => s + (e.prs_merged ?? 0), 0) / 3
      : null;

    return {
      totalMerged,
      successRate,
      latestShipped,
      truncatedTitle,
      sparkData,
      lastCycleLabel,
      latestCyclePrs: lastPrs,
      deltaPrs,
      rollingAvg3,
      totalAutonomous,
      totalAssisted,
      totalOperatorActions,
    };
  }, [d.research_history, d.history_aggregate]);

  // Stable callback references for action prop handlers — avoids re-creating
  // inline arrow functions on every render (finding 5: 36 inline callbacks
  // prevented React.memo from short-circuiting re-renders).
  const handleApprove = useCallback(() => approveProposal(d.id), [approveProposal, d.id]);
  const handleReject = useCallback(() => rejectProposal(d.id), [rejectProposal, d.id]);
  const handleEdit = useCallback(() => startEdit(d), [startEdit, d]);
  const handleDelete = useCallback(() => deleteDirection(d.id), [deleteDirection, d.id]);
  const handleTriggerResearch = useCallback(() => triggerResearch(d.id), [triggerResearch, d.id]);
  const handleForceResearch = useCallback(() => triggerResearch(d.id, true), [triggerResearch, d.id]);
  const handleUnhold = useCallback((e: React.MouseEvent) => { e.stopPropagation(); void unholdDirection(d.id); }, [unholdDirection, d.id]);
  const handleMarkComplete = useCallback(() => markDirectionComplete(d.id), [markDirectionComplete, d.id]);
  const handleRevive = useCallback(() => reviveProposal(d.id), [reviveProposal, d.id]);
  const handleActivateStaged = useCallback(() => activateStagedDirection?.(d.id), [activateStagedDirection, d.id]);
  const handleCancelStaged = useCallback(() => cancelStagedDirection?.(d.id), [cancelStagedDirection, d.id]);
  const handleForceConfirmToggle = useCallback(() => setForceConfirmId(forceConfirmId === d.id ? null : d.id), [forceConfirmId, d.id, setForceConfirmId]);
  const handleDismissForceConfirm = useCallback(() => setForceConfirmId(null), [setForceConfirmId]);
  const handleKeepDirection = useCallback(() => decideChurnRetirement?.(d.id, 'KEEP'), [decideChurnRetirement, d.id]);
  const handleRetireDirection = useCallback(() => { setRetireConfirmOpen(false); decideChurnRetirement?.(d.id, 'RETIRE'); }, [decideChurnRetirement, d.id]);

  // Memoize the budget utilization badge (was IIFE at ~1051) so it does
  // not recompute cost rankings and color classes on every render.
  const budgetUtilizationBadge = useMemo<React.ReactNode>(() => {
    const spend = d.estimated_cost_usd;
    const budget = d.cost_budget_usd;
    if (spend == null && (budget == null || budget === 0)) return null;
    const utilPct = d.budget_utilization_pct != null
      ? d.budget_utilization_pct
      : (budget != null && budget > 0 && spend != null ? (spend / budget) * 100 : null);
    const exceeded = d.budget_exceeded ?? (utilPct != null && utilPct > 100);
    if (budget == null || budget === 0) {
      return (
        <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-neutral-800/60 border-neutral-700/60 text-neutral-500">
          ${(spend ?? 0).toFixed(2)} spent
        </span>
      );
    }
    if (spend == null) {
      return (
        <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-neutral-800/60 border-neutral-700/60 text-neutral-500">
          Budget ${budget.toFixed(2)}
        </span>
      );
    }
    const color = exceeded ? 'red' : utilPct != null && utilPct > 80 ? 'amber' : 'emerald';
    const label = exceeded
      ? `$${spend.toFixed(2)} · over budget`
      : utilPct != null
        ? `$${spend.toFixed(2)} / $${budget.toFixed(2)} · ${Math.round(utilPct)}%`
        : `$${spend.toFixed(2)} / $${budget.toFixed(2)}`;
    return (
      <span
        title={`$${spend.toFixed(2)} spent of $${budget.toFixed(2)} budget`}
        className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}
      >
        {label}
      </span>
    );
  }, [d.estimated_cost_usd, d.cost_budget_usd, d.budget_utilization_pct, d.budget_exceeded]);

  // Memoize the cost-per-merge warning badges (was IIFE at ~1089) so it
  // does not recompute provider/model cost rankings each render.
  const costPerMergeWarning = useMemo<React.ReactNode>(() => {
    if (!d.cost_per_merge_exceeds_threshold) return null;
    const evidenceSplit = d.retirement_consideration_evidence?.cost_by_provider_model;
    const ranked: Array<{ key: string; cost: number; share: number | null }> = [];
    if (evidenceSplit && Object.keys(evidenceSplit).length > 0) {
      for (const [k, v] of Object.entries(evidenceSplit)) {
        ranked.push({ key: k, cost: v.cost_usd, share: v.share_pct });
      }
    } else if (d.cost_by_provider_model && Object.keys(d.cost_by_provider_model).length > 0) {
      const total = Object.values(d.cost_by_provider_model).reduce((a, b) => a + b, 0);
      for (const [k, v] of Object.entries(d.cost_by_provider_model)) {
        ranked.push({ key: k, cost: v, share: total > 0 ? v / total : null });
      }
    }
    ranked.sort((a, b) => (b.share ?? 0) - (a.share ?? 0) || b.cost - a.cost);
    const top = ranked.slice(0, 3);
    const tooltip = top.length > 0
      ? `Cost per autonomous merge: $${(d.cost_per_merged_pr ?? 0).toFixed(2)} — autonomous merges only; operator-assisted merges are excluded from the denominator.\n` +
        top.map(r => `${r.key}: $${r.cost.toFixed(2)}${r.share != null ? ` (${Math.round(r.share * 100)}%)` : ''}`).join('\n')
      : `Cost per autonomous merge: $${(d.cost_per_merged_pr ?? 0).toFixed(2)} — autonomous merges only; operator-assisted merges are excluded from the denominator.`;
    return (
      <>
        <span
          title={tooltip}
          className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-amber-950/40 border-amber-900/50 text-amber-400"
        >
          High cost / autonomous merge
        </span>
        {top.length > 0 && (
          <span
            title={tooltip}
            className="text-ui-sm text-amber-400/80 font-mono"
          >
            {top.map(r => {
              const label = r.key.split('|').pop() || r.key;
              return r.share != null
                ? `${label} ${Math.round(r.share * 100)}%`
                : `${label} $${r.cost.toFixed(2)}`;
            }).join(' · ')}
          </span>
        )}
      </>
    );
  }, [d.cost_per_merge_exceeds_threshold, d.retirement_consideration_evidence, d.cost_by_provider_model, d.cost_per_merged_pr]);

  // Memoize active-task badges (was IIFE at ~1196) so the status-color map and
  // task badge list are not rebuilt each render.
  const activeTasksBadges = useMemo<React.ReactNode>(() => {
    const tasks = activeTasks?.[d.id] ?? [];
    if (tasks.length === 0) return null;
    const statusColor = (s: string) => {
      if (s === 'IN_PROGRESS') return 'bg-yellow-950/40 border-yellow-900/50 text-yellow-400';
      if (s === 'PROVISIONING') return 'bg-sky-950/40 border-sky-900/60 text-sky-400';
      if (s === 'EVALUATING') return 'bg-indigo-950/40 border-indigo-900/60 text-indigo-400';
      return 'bg-neutral-800/60 border-neutral-700/60 text-neutral-400';
    };
    return (
      <div className="flex flex-wrap gap-1">
        {tasks.map(t => (
          <button
            data-viewport-allow-small-target
            key={t.id}
            type="button"
            onClick={() => onNavigateToTask?.(t.id)}
            title={t.title || t.latest_status_message || t.status}
            className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium transition-opacity hover:opacity-80 ${statusColor(t.status)}`}
          >
            {t.status.toLowerCase()} · {t.id.slice(0, 8)}
          </button>
        ))}
      </div>
    );
  }, [activeTasks, d.id, onNavigateToTask]);

  // Close force-confirm popover on outside click
  useEffect(() => {
    if (forceConfirmId !== d.id) return;
    function handleClick(e: MouseEvent) {
      if (forceConfirmRef.current && !forceConfirmRef.current.contains(e.target as Node)) {
        setForceConfirmId(null);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [forceConfirmId, d.id, setForceConfirmId]);

  // Close retire-confirm popover on outside click
  useEffect(() => {
    if (!retireConfirmOpen) return;
    function handleClick(e: MouseEvent) {
      if (retireConfirmRef.current && !retireConfirmRef.current.contains(e.target as Node)) {
        setRetireConfirmOpen(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [retireConfirmOpen]);

  return (
    <div className={`border rounded-lg p-2 ${d.status === 'proposed' ? 'bg-purple-950/20 border-purple-900/40' : d.status === 'expired' ? 'bg-neutral-900/30 border-neutral-800/40 opacity-70' : d.status === 'staged' ? 'bg-sky-950/20 border-sky-900/40' : 'bg-neutral-900/50 border-neutral-800/50'}`}>
      {d.status === 'proposed' ? (
        /* Proposed direction — approve or reject */
        <div>
          <div className="flex items-start gap-2">
            <p className="text-sm text-neutral-200 flex-1">{d.text}</p>
            <div className="flex items-center gap-1 shrink-0">
              {/* reason: inline direction action button in dense card; text-ui-sm py-0.5 matches card density */}
              <button
                data-viewport-allow-small-target
                onClick={handleApprove}
                title="Approve — activate this direction"
                className="text-ui-sm px-2 py-0.5 rounded border font-medium transition-colors bg-emerald-950/60 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/50"
              >
                Approve
              </button>
              {/* reason: inline direction action button in dense card; text-ui-sm py-0.5 matches card density */}
              <button
                data-viewport-allow-small-target
                onClick={handleReject}
                title="Reject — remove this proposal"
                className="text-ui-sm px-2 py-0.5 rounded border font-medium transition-colors bg-red-950/60 border-red-900/50 text-red-400 hover:bg-red-900/50"
              >
                Reject
              </button>
            </div>
          </div>
          {d.proposal_rationale && (
            <blockquote className="mt-2 border-l-2 border-purple-800/50 pl-2 text-ui-md text-purple-300/80 italic leading-relaxed">
              {d.proposal_rationale}
            </blockquote>
          )}
          {d.completion_criteria && (
            <p className="mt-1.5 text-ui-md text-neutral-500 leading-relaxed">
              <span className="font-medium text-neutral-600">Done when: </span>
              <span className="italic">{d.completion_criteria}</span>
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2 mt-2 min-w-0">
            <span className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium ${priorityColors[d.priority]}`}>
              {d.priority}
            </span>
            <span className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium ${statusColors.proposed}`}>
              Proposed by agent
            </span>
            {d.proposed_at && (
              <span className="text-ui-sm text-neutral-600 ml-auto">
                proposed {formatRelativeTime(d.proposed_at)}
              </span>
            )}
          </div>
        </div>
      ) : d.status === 'expired' ? (
        /* Expired direction — revive or approve */
        <div>
          <div className="flex items-start gap-2">
            <p className="text-sm text-neutral-400 flex-1">{d.text}</p>
            <div className="flex items-center gap-1 shrink-0">
              {/* reason: inline direction action button in dense card; text-ui-sm py-0.5 matches card density */}
              <button
                data-viewport-allow-small-target
                onClick={handleRevive}
                title="Revive — move back to proposed"
                className="text-ui-sm px-2 py-0.5 rounded border font-medium transition-colors bg-purple-950/60 border-purple-900/50 text-purple-400 hover:bg-purple-900/50"
              >
                Revive
              </button>
              {/* reason: inline direction action button in dense card; text-ui-sm py-0.5 matches card density */}
              <button
                data-viewport-allow-small-target
                onClick={handleApprove}
                title="Approve — activate this direction"
                className="text-ui-sm px-2 py-0.5 rounded border font-medium transition-colors bg-emerald-950/60 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/50"
              >
                Approve
              </button>
            </div>
          </div>
          {d.proposal_rationale && (
            <blockquote className="mt-2 border-l-2 border-neutral-700/50 pl-2 text-ui-md text-neutral-500 italic leading-relaxed">
              {d.proposal_rationale}
            </blockquote>
          )}
          {d.completion_criteria && (
            <p className="mt-1.5 text-ui-md text-neutral-500 leading-relaxed">
              <span className="font-medium text-neutral-600">Done when: </span>
              <span className="italic">{d.completion_criteria}</span>
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2 mt-2 min-w-0">
            <span className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium ${priorityColors[d.priority]}`}>
              {d.priority}
            </span>
            <span className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium ${statusColors.expired}`}>
              Expired
            </span>
            {d.proposed_at && (
              <span className="text-ui-sm text-neutral-600 ml-auto">
                proposed {formatRelativeTime(d.proposed_at)}
              </span>
            )}
          </div>
        </div>
      ) : d.status === 'staged' ? (
        /* Staged direction — waiting for the next version; activate now or cancel */
        <div>
          <div className="flex items-start gap-2">
            <p className="text-sm text-neutral-200 flex-1">{d.text}</p>
            <div className="flex items-center gap-1 shrink-0">
              {/* reason: inline direction action button in dense card; text-ui-sm py-0.5 matches card density */}
              <button
                data-viewport-allow-small-target
                onClick={handleActivateStaged}
                title="Activate now — start this direction immediately instead of waiting for the next version"
                className="text-ui-sm px-2 py-0.5 rounded border font-medium transition-colors bg-emerald-950/60 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/50"
              >
                Activate now
              </button>
              {/* reason: inline direction action button in dense card; text-ui-sm py-0.5 matches card density */}
              <button
                data-viewport-allow-small-target
                onClick={handleCancelStaged}
                title="Cancel — remove this staged direction so it will not auto-activate"
                className="text-ui-sm px-2 py-0.5 rounded border font-medium transition-colors bg-red-950/60 border-red-900/50 text-red-400 hover:bg-red-900/50"
              >
                Cancel
              </button>
            </div>
          </div>
          {d.completion_criteria && (
            <p className="mt-1.5 text-ui-md text-neutral-500 leading-relaxed">
              <span className="font-medium text-neutral-600">Done when: </span>
              <span className="italic">{d.completion_criteria}</span>
            </p>
          )}
          <div className="flex flex-wrap items-center gap-2 mt-2 min-w-0">
            <span className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium ${priorityColors[d.priority]}`}>
              {d.priority}
            </span>
            <span className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium ${statusColors.staged}`}>
              Staged
            </span>
            {d.staged_for_version != null && (
              <span
                className="text-ui-sm text-sky-400/80"
                title={`Auto-activates when version ${d.staged_for_version} promotes and the next version opens`}
              >
                Waiting for v{d.staged_for_version} to promote
              </span>
            )}
            {(() => {
              const waiting = getStagedWaitingInfo(d);
              if (!waiting) return null;
              return (
                <span
                  className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium ${
                    waiting.isStale
                      ? 'bg-red-950/40 text-red-400 border-red-900/50'
                      : 'bg-neutral-800/40 text-neutral-400 border-neutral-700/50'
                  }`}
                  title={
                    waiting.isStale
                      ? `Stalling — waiting ${waiting.days}d for promotion, past the ${STAGED_STALE_DAYS}d threshold (operator alert fires ~21d)`
                      : `Waiting ${waiting.days}d since staged`
                  }
                >
                  {waiting.label}
                </span>
              );
            })()}
          </div>
          {/* Retarget — bind this staged direction to a different (current-or-
              later) version. Defaults the input to the currently-open version. */}
          {retargetStagedDirection && (
            <div className="flex items-center gap-1.5 mt-2">
              <span className="text-ui-sm text-neutral-500">Retarget to v</span>
              <input
                type="number"
                min={stageTargetVersion ?? 1}
                value={retargetVersion}
                placeholder={stageTargetVersion != null ? String(stageTargetVersion) : ''}
                onChange={e => setRetargetVersion(e.target.value)}
                title="Bind this staged direction to a different version (current open or later)"
                className="w-16 bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-0.5 text-ui-sm focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600"
              />
              <button
                data-viewport-allow-small-target
                disabled={!retargetVersion.trim() || Number.isNaN(Number(retargetVersion))}
                onClick={() => {
                  const v = Number(retargetVersion);
                  if (Number.isInteger(v)) retargetStagedDirection(d.id, v);
                  setRetargetVersion('');
                }}
                className="text-ui-sm px-2 py-0.5 rounded border font-medium transition-colors bg-sky-950/60 border-sky-900/50 text-sky-400 hover:bg-sky-900/50 disabled:opacity-40 disabled:cursor-not-allowed"
              >
                Retarget
              </button>
            </div>
          )}
        </div>
      ) : editingId === d.id ? (
        /* Edit mode */
        <div className="space-y-2">
          <input
            value={editText}
            onChange={e => setEditText(e.target.value)}
            className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
          />
          <textarea
            value={editCompletionCriteria}
            onChange={e => setEditCompletionCriteria(e.target.value)}
            placeholder="Done when..."
            rows={2}
            className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600 resize-y"
          />
          {/* Acceptance-evidence contract editable inline; 500-char limit
              mirrors the backend create/update endpoint. */}
          <textarea
            value={editSuccessEvidence}
            onChange={e => setEditSuccessEvidence(e.target.value)}
            placeholder="Success evidence..."
            rows={2}
            maxLength={500}
            className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600 resize-y"
          />
          <textarea
            value={editRetirementCondition}
            onChange={e => setEditRetirementCondition(e.target.value)}
            placeholder="Retirement condition..."
            rows={2}
            maxLength={500}
            className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-3 py-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600 resize-y"
          />
          <div className="bg-neutral-950 border border-neutral-700 rounded px-2 py-1.5 flex flex-wrap gap-1 items-center focus-within:ring-1 focus-within:ring-neutral-600">
            {editPathTags.map(tag => (
              <span key={tag} className="bg-neutral-800 border border-neutral-700 text-neutral-300 text-ui-sm font-mono px-1.5 py-0.5 rounded flex items-center gap-1">
                {tag}
                <button
                  type="button"
                  onClick={() => setEditPathTags(prev => prev.filter(t => t !== tag))}
                  className="text-neutral-500 hover:text-neutral-300 transition-colors leading-none"
                >
                  <X className="w-2.5 h-2.5" />
                </button>
              </span>
            ))}
            <input
              value={editPathInput}
              onChange={e => setEditPathInput(e.target.value)}
              onKeyDown={e => {
                if (e.key === 'Enter' || e.key === ',') {
                  e.preventDefault();
                  const val = editPathInput.trim();
                  if (val && !editPathTags.includes(val)) setEditPathTags(prev => [...prev, val]);
                  setEditPathInput('');
                } else if (e.key === 'Backspace' && !editPathInput && editPathTags.length > 0) {
                  setEditPathTags(prev => prev.slice(0, -1));
                }
              }}
              placeholder={editPathTags.length === 0 ? 'Path scope (e.g. backend/)' : ''}
              className="flex-1 min-w-[120px] bg-transparent text-neutral-100 text-xs focus:outline-none placeholder-neutral-600"
            />
          </div>
          <div className="flex flex-wrap items-center gap-2">
            <select
              value={editPriority}
              onChange={e => setEditPriority(e.target.value as 'high' | 'medium' | 'low')}
              className="bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
            >
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
            <select
              value={editStatus}
              onChange={e => setEditStatus(e.target.value as 'active' | 'completed' | 'paused' | 'staged')}
              className="bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
            >
              <option value="active">Active</option>
              <option value="completed">Completed</option>
              <option value="paused">Paused</option>
              {/* Stage for the next version — disabled when no version is open to
                  stage against, since the backend rejects that case. The selected
                  target version is surfaced in the hint below the row. */}
              <option
                value="staged"
                disabled={stageTargetVersion == null}
                title={stageTargetVersion == null
                  ? 'No open release version to stage against'
                  : `Stage to begin when v${stageTargetVersion} promotes and the next version opens`}
              >
                Staged
              </option>
            </select>
            <select
              value={editCategory ?? ''}
              onChange={e => setEditCategory((e.target.value || null) as DirectionCategory)}
              className="bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
            >
              <option value="">Type…</option>
              <option value="product">Product</option>
              <option value="structural">Structural</option>
              <option value="mixed">Mixed</option>
            </select>
            <div className="flex-1" />
            {setEditCostBudget && (
              <input
                type="number"
                min="0"
                step="0.01"
                value={editCostBudget ?? ''}
                onChange={e => setEditCostBudget(e.target.value)}
                placeholder="Budget $"
                title="Cost budget (USD) — alerts fire when spend exceeds 80% of this"
                className="w-24 bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600"
              />
            )}
            {/* reason: inline icon-only confirm/cancel in edit row; w-3.5 h-3.5 icon matches edit form density */}
            <button data-viewport-allow-small-target onClick={() => updateDirection(d.id)} className="text-emerald-400 hover:text-emerald-300 transition-colors">
              <Check className="w-3.5 h-3.5" />
            </button>
            {/* reason: inline icon-only cancel in edit row; w-3.5 h-3.5 icon matches edit form density */}
            <button data-viewport-allow-small-target onClick={() => setEditingId(null)} className="text-neutral-500 hover:text-neutral-300 transition-colors">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
          {stageTargetVersion == null ? (
            <p className="text-ui-sm text-neutral-500 leading-relaxed">
              No open release version to stage against.
            </p>
          ) : editStatus === 'staged' && (
            <p className="text-ui-sm text-sky-400/80 leading-relaxed">
              Will stage to begin after v{stageTargetVersion} promotes and the next version opens.
            </p>
          )}
        </div>
      ) : (
        /* View mode */
        <div>
          <div className="flex items-start gap-2">
            <p className="text-sm text-neutral-200 flex-1">{d.text}</p>
            <div className="flex items-center gap-1 shrink-0">
              {key === 'active' && (() => {
                const idx = activeItems.findIndex(x => x.id === d.id);
                return (<>
                  {idx > 0 && (
                    // reason: inline icon-only reorder button in dense direction row; p-0.5 w-3 h-3 icon
                    <button
                      data-viewport-allow-small-target
                      onClick={() => {
                        const next = [...activeItems];
                        [next[idx - 1], next[idx]] = [next[idx], next[idx - 1]];
                        reorderDirections(next);
                      }}
                      className="text-neutral-400 hover:text-neutral-300 transition-colors p-0.5"
                      title="Move up"
                    >
                      <ChevronUp className="w-3 h-3" />
                    </button>
                  )}
                  {idx < activeItems.length - 1 && (
                    // reason: inline icon-only reorder button in dense direction row; p-0.5 w-3 h-3 icon
                    <button
                      data-viewport-allow-small-target
                      onClick={() => {
                        const next = [...activeItems];
                        [next[idx], next[idx + 1]] = [next[idx + 1], next[idx]];
                        reorderDirections(next);
                      }}
                      className="text-neutral-400 hover:text-neutral-300 transition-colors p-0.5"
                      title="Move down"
                    >
                      <ChevronDown className="w-3 h-3" />
                    </button>
                  )}
                </>);
              })()}
              {key === 'active' && (() => {
                const inCooldown = d.refractory_remaining_minutes != null;
                return (
                  <div className="relative flex items-center gap-1">
                    {/* reason: inline research trigger chip in dense direction row; text-ui-sm py-0.5 */}
                    <button
                      data-viewport-allow-small-target
                      onClick={handleTriggerResearch}
                      disabled={researchingIds[d.id] || inCooldown}
                      title={inCooldown ? 'Direction is in cooldown — use force to bypass' : 'Trigger research for this direction'}
                      className="text-ui-sm px-1.5 py-0.5 rounded border font-medium transition-colors disabled:opacity-40 bg-neutral-800/60 border-neutral-700/60 text-neutral-400 hover:enabled:text-neutral-200 hover:enabled:bg-neutral-700/60"
                    >
                      {researchingIds[d.id] ? '…' : researchSuccessIds[d.id] ? 'Queued' : 'Research'}
                    </button>
                    {inCooldown && (
                      <div className="relative">
                        {/* reason: inline force-research icon chip in dense direction row; text-ui-sm py-0.5 */}
                        <button
                          data-viewport-allow-small-target
                          onClick={handleForceConfirmToggle}
                          disabled={researchingIds[d.id]}
                          title="Force research (bypass cooldown)"
                          className="text-ui-sm px-1 py-0.5 rounded border font-medium transition-colors disabled:opacity-40 bg-amber-950/40 border-amber-900/50 text-amber-400 hover:bg-amber-900/40"
                        >
                          <Zap className="w-2.5 h-2.5" />
                        </button>
                        {forceConfirmId === d.id && (
                          <div
                            ref={forceConfirmRef}
                            className="absolute right-0 top-6 z-10 w-52 bg-neutral-900 border border-amber-900/60 rounded-lg p-3 shadow-xl"
                          >
                            <p className="text-ui-md text-amber-300 font-medium mb-1">Force research?</p>
                            <p className="text-ui-sm text-neutral-400 mb-2 leading-relaxed">Bypasses the cooldown and queues a research task immediately.</p>
                            <div className="flex gap-2">
                              <button
                                onClick={handleForceResearch}
                                className="flex-1 text-ui-sm px-2 py-1 min-h-[44px] rounded border bg-amber-950/60 border-amber-900/50 text-amber-300 hover:bg-amber-900/50 font-medium transition-colors"
                              >
                                Force
                              </button>
                              <button
                                onClick={handleDismissForceConfirm}
                                className="flex-1 text-ui-sm px-2 py-1 min-h-[44px] rounded border bg-neutral-800 border-neutral-700 text-neutral-400 hover:text-neutral-200 font-medium transition-colors"
                              >
                                Cancel
                              </button>
                            </div>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                );
              })()}
              {/* reason: inline icon-only edit button in dense direction row; p-0.5 w-3 h-3 icon */}
              <button data-viewport-allow-small-target onClick={handleEdit} className="text-neutral-400 hover:text-neutral-300 transition-colors p-0.5">
                <Pencil className="w-3 h-3" />
              </button>
              {/* reason: inline icon-only delete button in dense direction row; p-0.5 w-3 h-3 icon */}
              <button data-viewport-allow-small-target onClick={handleDelete} className="text-neutral-400 hover:text-red-400 transition-colors p-0.5">
                <Trash2 className="w-3 h-3" />
              </button>
            </div>
          </div>
          {d.completion_criteria && (
            <p className="mt-1.5 text-ui-md text-neutral-500 leading-relaxed">
              <span className="font-medium text-neutral-600">Done when: </span>
              <span className="italic">{d.completion_criteria}</span>
            </p>
          )}
          {d.context?.success_evidence && (
            <p className="mt-1.5 text-ui-md text-neutral-500 leading-relaxed">
              <span className="font-medium text-neutral-600">Success evidence: </span>
              <span className="italic">{d.context.success_evidence}</span>
            </p>
          )}
          {d.context?.retirement_condition && (
            <p className="mt-1.5 text-ui-md text-neutral-500 leading-relaxed">
              <span className="font-medium text-neutral-600">Retirement condition: </span>
              <span className="italic">{d.context.retirement_condition}</span>
            </p>
          )}
          {/* Last researched / cooldown subtitle */}
          {key === 'active' && (() => {
            if (!d.last_researched_at) {
              return <p className="text-ui-sm text-neutral-600 mt-0.5">Never researched</p>;
            }
            return <p className="text-ui-sm text-neutral-600 mt-0.5">Last researched {formatRelativeTime(d.last_researched_at ?? '')}</p>;
          })()}
          {/* Auto-pause reason subtitle — red for budget-exceeded pauses, amber for yield/saturation */}
          {key === 'paused' && d.paused_reason && (
            <p className={`text-ui-sm mt-0.5 leading-relaxed ${d.budget_exceeded ? 'text-red-400/80' : 'text-amber-500/80'}`}>
              {d.paused_reason}
              {d.paused_at && <span className="text-neutral-600"> · paused {formatRelativeTime(d.paused_at)}</span>}
            </p>
          )}
          {/* Hold banner — direction paused from autonomous spawning after a
              CODE_ERROR streak. Clears on next success or via manual unhold. */}
          {d.held && (
            <div className="mt-1.5 flex items-start gap-1.5 p-1.5 bg-red-950/30 border border-red-900/50 rounded">
              <AlertTriangle className="w-3 h-3 text-red-400 shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <p className="text-ui-md text-red-300 font-medium leading-tight">On hold</p>
                <p className="text-ui-sm text-red-400/80 leading-relaxed mt-0.5">
                  {d.held_reason || 'Held after repeated failures.'}
                </p>
                {autoRecoveryLine && (
                  <p className="text-ui-sm text-red-300/90 leading-relaxed mt-0.5">
                    {autoRecoveryLine}
                  </p>
                )}
              </div>
              {/* reason: inline unhold action button in dense hold-banner; text-ui-sm py-0.5 matches card density */}
              <button
                data-viewport-allow-small-target
                onClick={handleUnhold}
                className="text-ui-sm px-2 py-0.5 rounded border border-red-700 text-red-200 hover:bg-red-900/40 shrink-0"
                title="Clear the hold so this direction can spawn autonomous tasks again"
              >
                unhold
              </button>
            </div>
          )}
          {/* Churn-retirement recommendation banner — operator must explicitly
              decide Keep or Retire; the direction is held PAUSED until decided. */}
          {d.churn_retirement_recommended && (
            <div className="mt-1.5 flex items-start gap-1.5 p-1.5 bg-amber-950/30 border border-amber-900/50 rounded">
              <AlertTriangle className="w-3 h-3 text-amber-400 shrink-0 mt-0.5" />
              <div className="flex-1 min-w-0">
                <p className="text-ui-md text-amber-300 font-medium leading-tight">Retirement recommended</p>
                {d.churn_retirement_recommended_reason && (
                  <p className="text-ui-sm text-amber-400/80 leading-relaxed mt-0.5">
                    {d.churn_retirement_recommended_reason}
                  </p>
                )}
                {d.churn_retirement_recommended_at && (
                  <p className="text-ui-sm text-neutral-600 mt-0.5">
                    flagged {formatRelativeTime(d.churn_retirement_recommended_at)}
                  </p>
                )}
              </div>
              <div className="flex items-center gap-1 shrink-0">
                {/* reason: inline direction action button in dense card; text-ui-sm py-0.5 matches card density */}
                <button
                  data-viewport-allow-small-target
                  onClick={handleKeepDirection}
                  title="Keep — clear the retirement recommendation and keep this direction active"
                  className="text-ui-sm px-2 py-0.5 rounded border font-medium transition-colors bg-emerald-950/60 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/50"
                >
                  Keep
                </button>
                <div className="relative">
                  {/* reason: inline direction action button in dense card; text-ui-sm py-0.5 matches card density */}
                  <button
                    data-viewport-allow-small-target
                    onClick={() => setRetireConfirmOpen(v => !v)}
                    title="Retire — permanently retire this direction"
                    className="text-ui-sm px-2 py-0.5 rounded border font-medium transition-colors bg-red-950/60 border-red-900/50 text-red-400 hover:bg-red-900/50"
                  >
                    Retire
                  </button>
                  {retireConfirmOpen && (
                    <div
                      ref={retireConfirmRef}
                      className="absolute right-0 top-6 z-10 w-52 bg-neutral-900 border border-red-900/60 rounded-lg p-3 shadow-xl"
                    >
                      <p className="text-ui-md text-red-300 font-medium mb-1">Retire this direction?</p>
                      <p className="text-ui-sm text-neutral-400 mb-2 leading-relaxed">This will permanently retire the direction. This action cannot be undone.</p>
                      <div className="flex gap-2">
                        <button
                          onClick={handleRetireDirection}
                          className="flex-1 text-ui-sm px-2 py-1 min-h-[44px] rounded border bg-red-950/60 border-red-900/50 text-red-300 hover:bg-red-900/50 font-medium transition-colors"
                        >
                          Retire
                        </button>
                        <button
                          onClick={() => setRetireConfirmOpen(false)}
                          className="flex-1 text-ui-sm px-2 py-1 min-h-[44px] rounded border bg-neutral-800 border-neutral-700 text-neutral-400 hover:text-neutral-200 font-medium transition-colors"
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  )}
                </div>
              </div>
            </div>
          )}
          {/* Positive completion suggestion */}
          {d.possibly_complete && d.completion_suggestion && (
            <p className="text-ui-sm text-emerald-500/80 mt-0.5 leading-relaxed">
              {d.completion_suggestion}
            </p>
          )}
          {/* Diagnosis / remediation pipeline status */}
          {(d.needs_manual_intervention || d.diagnosis_in_progress || d.diagnosis_summary || (d.remediation_attempts ?? 0) > 0) && (
            <div className="mt-2 space-y-1">
              {d.needs_manual_intervention ? (
                <div className="flex items-start gap-1.5 p-1.5 bg-red-950/40 border border-red-900/50 rounded">
                  <AlertTriangle className="w-3 h-3 text-red-400 shrink-0 mt-0.5" />
                  <div className="flex-1 min-w-0">
                    <p className="text-ui-md text-red-300 font-medium leading-tight">Manual intervention needed</p>
                    {d.paused_reason?.startsWith('Lifetime burn backstop:') && d.lifetime_burn_pause_evidence ? (
                      <p className="text-ui-sm text-red-400/80 leading-relaxed mt-0.5">
                        Direction has accumulated {d.lifetime_burn_pause_evidence.lifetime_non_merge_finished} non-merging tasks with only {d.lifetime_burn_pause_evidence.lifetime_merged} merged ({Math.round(d.lifetime_burn_pause_evidence.lifetime_merge_rate * 100)}% lifetime rate).
                        <span className="block mt-0.5">Operator decision required: retire this direction or reset its accumulated history.</span>
                      </p>
                    ) : (
                      <p className="text-ui-sm text-red-400/80 leading-relaxed mt-0.5">
                        Auto-remediation escalated after {d.remediation_attempts ?? 0} failed attempt{(d.remediation_attempts ?? 0) !== 1 ? 's' : ''}.
                        {d.diagnosis_summary && <span className="block mt-0.5 text-neutral-400 italic break-all">{d.diagnosis_summary}</span>}
                      </p>
                    )}
                  </div>
                </div>
              ) : (
                <>
                  {d.diagnosis_in_progress && (
                    <div className="flex items-center gap-1.5 text-ui-sm text-amber-400">
                      <Loader2 className="w-3 h-3 animate-spin shrink-0" />
                      <span>Diagnosing failure pattern…</span>
                    </div>
                  )}
                  {d.diagnosis_summary && !d.diagnosis_in_progress && (
                    <p className="text-ui-sm text-neutral-400 leading-relaxed break-all">
                      <span className="text-neutral-500 font-medium">Diagnosis: </span>
                      {d.diagnosis_summary}
                      {d.diagnosis_completed_at && (
                        <span className="text-neutral-600 ml-1">· {formatRelativeTime(d.diagnosis_completed_at)}</span>
                      )}
                    </p>
                  )}
                  {(d.remediation_attempts ?? 0) > 0 && (() => {
                    const attempts = d.remediation_attempts ?? 0;
                    const resultMeta = remediationResultMeta(d.last_remediation_result);
                    return (
                      <span className={`inline-block text-ui-sm px-1.5 py-0.5 rounded border font-medium ${resultMeta.badgeClass}`}>
                        Remediation {attempts}× · {resultMeta.resultLabel}
                      </span>
                    );
                  })()}
                </>
              )}
            </div>
          )}
          {remediationSummaryRows.length > 0 && (
            <div className="mt-2">
              <p className="text-ui-sm font-medium uppercase tracking-wider text-neutral-500">
                Recent remediation attempts
              </p>
              <div className="mt-1 space-y-1">
                {remediationSummaryRows.slice(0, showAllRemediation ? 3 : 2).map((entry) => (
                  <div key={entry.key} className="text-ui-sm text-neutral-400 flex items-center gap-1.5 leading-relaxed">
                    <span className={`px-1.5 py-0.5 rounded border font-medium ${entry.badgeClass}`}>
                      {entry.resultLabel}
                    </span>
                    {entry.fingerprintShort && (
                      <span className="font-mono text-neutral-300">
                        {entry.fingerprintShort}
                      </span>
                    )}
                    {entry.timestamp && (
                      <span className="text-neutral-500">
                        {formatRelativeTime(entry.timestamp)}
                      </span>
                    )}
                    {entry.fingerprintChange && (
                      <span className="text-neutral-600">
                        fp {entry.fingerprintChange}
                      </span>
                    )}
                  </div>
                ))}
              </div>
              {remediationSummaryRows.length > 2 && (
                // reason: inline expand-more link in remediation section; text-ui-sm single-line
                <button
                  data-viewport-allow-small-target
                  onClick={() => setExpandedRemediationIds(prev => ({ ...prev, [d.id]: !prev[d.id] }))}
                  className="mt-1 text-ui-sm text-neutral-500 hover:text-neutral-300 transition-colors"
                >
                  {showAllRemediation ? 'show less' : 'show more'}
                </button>
              )}
            </div>
          )}
          <div className="flex flex-wrap items-center gap-2 mt-2 min-w-0">
            <span className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium ${priorityColors[d.priority]}`}>
              {d.priority}
            </span>
            {d.status === 'active' || d.status === 'paused' ? (
              // reason: inline status toggle chip in dense direction row; text-ui-sm py-0.5
              <button
                data-viewport-allow-small-target
                onClick={() => quickToggleStatus(d.id, d.status as 'active' | 'paused')}
                title={d.status === 'active' ? 'Click to pause' : 'Click to activate'}
                className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium transition-opacity hover:opacity-70 cursor-pointer ${statusColors[d.status]}`}
              >
                {d.status}
              </button>
            ) : (
              <span className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium ${statusColors[d.status]}`}>
                {d.status}
              </span>
            )}
            {d.category === 'product' && (
              <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-emerald-950/40 border-emerald-900/50 text-emerald-400">
                Product
              </span>
            )}
            {d.category === 'structural' && (
              <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-blue-950/40 border-blue-900/50 text-blue-400">
                Structural
              </span>
            )}
            {d.category === 'mixed' && (
              <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-purple-950/40 border-purple-900/50 text-purple-400">
                Mixed
              </span>
            )}
            {/* Quality score badge — taxonomy-based structural health */}
            {typeof d.quality_score === 'number' && (() => {
              const score = d.quality_score;
              const color = score >= 0.6 ? 'emerald' : score >= 0.3 ? 'amber' : 'red';
              return (
                <span
                  title="Direction quality score: taxonomy-based structural health (0–1)"
                  className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}
                >
                  {Math.round(score * 100)}% quality
                </span>
              );
            })()}
            {/* Generalized cross-task-type success — distinct from the PR-merge
                count; shown only when a non-code-merge lane has outcomes. */}
            {showGeneralized && (
              <span
                title="Generalized success across task types (code merges + research/audit/memo/verification), distinct from the PR-merge count"
                className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-neutral-800/60 border-neutral-700/60 ${healthRateColorClass(d.generalized_health_rate)}`}
              >
                {d.generalized_successes ?? 0} eval{typeof d.generalized_health_rate === 'number' ? ` · ${Math.round(d.generalized_health_rate * 100)}%` : ''}
              </span>
            )}
            {/* Accumulated knowledge documents (web_research output) for this direction */}
            {typeof d.knowledge_document_count === 'number' && d.knowledge_document_count > 0 && (
              onViewKnowledge ? (
                <button
                  data-viewport-allow-small-target
                  onClick={() => onViewKnowledge(d.id)}
                  title="Open Knowledge library filtered to this direction's documents"
                  className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-blue-950/40 border-blue-900/50 text-blue-300 hover:bg-blue-900/40 transition-colors cursor-pointer"
                >
                  {d.knowledge_document_count} doc{d.knowledge_document_count !== 1 ? 's' : ''}
                </button>
              ) : (
                <span
                  title="Durable knowledge documents accumulated by this direction's web_research tasks"
                  className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-blue-950/40 border-blue-900/50 text-blue-300"
                >
                  {d.knowledge_document_count} doc{d.knowledge_document_count !== 1 ? 's' : ''}
                </span>
              )
            )}
            {/* Reactivate button for auto-paused directions */}
            {d.auto_paused && key === 'paused' && (
              // reason: inline direction action chip; text-ui-sm py-0.5 matches card density
              <button
                data-viewport-allow-small-target
                onClick={() => reactivateDirection(d.id)}
                className="text-ui-sm px-1.5 py-0.5 rounded border font-medium transition-colors bg-emerald-950/40 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/40"
              >
                Reactivate
              </button>
            )}
            {/* Mark Complete button for possibly-complete directions */}
            {d.possibly_complete && key === 'active' && (
              // reason: inline mark-complete action chip; text-ui-sm py-0.5 matches card density
              <button
                data-viewport-allow-small-target
                onClick={handleMarkComplete}
                className="text-ui-sm px-1.5 py-0.5 rounded border font-medium transition-colors bg-emerald-950/40 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/40"
              >
                Mark Complete
              </button>
            )}
            {/* Budget utilization indicator */}
            {budgetUtilizationBadge}
            {costPerMergeWarning}
            {/* Cooldown badge — suppressed when direction is held */}
            {key === 'active' && !d.held && (() => {
              if (d.refractory_remaining_minutes != null) {
                return (
                  <span
                    title={
                      d.refractory_reason === 'saturation'
                        ? 'Saturation hold: the direction was recently unproductive and is waiting longer before it can be researched again.'
                        : 'Standard hold: the direction is waiting out its research cooldown.'
                    }
                    className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-amber-950/40 text-amber-400 border-amber-900/50"
                  >
                    {d.refractory_reason === 'saturation' ? 'Saturation hold' : 'Cooling down'} · {formatRemainingMinutes(d.refractory_remaining_minutes)}
                  </span>
                );
              }
              return (
                <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-emerald-950/40 text-emerald-400 border-emerald-900/50">
                  Ready
                </span>
              );
            })()}
            {/* Churn warning badge — non-merge volume approaching or at auto-pause threshold */}
            {d.churn_metrics && (() => {
              const { non_merge_finished, merged, merge_rate, threshold, min_merge_rate, window_cycles } = d.churn_metrics;
              const atThreshold = non_merge_finished >= threshold && merge_rate < min_merge_rate;
              const approaching = non_merge_finished >= Math.floor(threshold * 0.5) && merge_rate < min_merge_rate * 2;
              if (!approaching && !atThreshold) return null;
              const color = atThreshold ? 'red' : 'amber';
              return (
                <span
                  title={`Non-merge churn: ${non_merge_finished} tasks finished without merging, ${merged} merged in last ${window_cycles} cycles — ${Math.round(merge_rate * 100)}% merge rate (pause threshold: ${threshold} tasks at <${Math.round(min_merge_rate * 100)}% rate)`}
                  className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}
                >
                  {non_merge_finished}/{threshold} non-merge · {Math.round(merge_rate * 100)}%
                </span>
              );
            })()}
            {/* Outcome-less scaffolding warning — advisory: long-running direction
                keeps merging PRs at a healthy rate but shows no progress toward its
                stated success_evidence. A consideration to surface, not an enforced pause. */}
            {d.outcome_scaffolding_flagged && (() => {
              const ev = d.outcome_scaffolding_evidence;
              const tooltip = ev
                ? `Possible outcome-less scaffolding: ${ev.total_cycles} cycles, ${ev.lifetime_merged} merges at ${Math.round(ev.lifetime_merge_rate * 100)}% rate, but progress toward success_evidence has been flat for the last ${ev.stall_run_cycles} cycles. Advisory only — consider whether this direction is still advancing its goal.`
                : 'Possible outcome-less scaffolding: keeps merging PRs but shows no progress toward its stated success_evidence. Advisory only.';
              return (
                <span
                  title={tooltip}
                  className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-amber-950/40 border-amber-900/50 text-amber-400"
                >
                  Outcome-less scaffolding?
                  {ev ? ` · ${ev.stall_run_cycles}-cycle stall` : ''}
                </span>
              );
            })()}
            <span className="text-ui-sm text-neutral-600 ml-auto">
              {d.added_at ? parseTimestamp(d.added_at).toLocaleDateString() : ''}
            </span>
            {/* Active research tasks for this direction — clickable badges when we
                have the per-task list, otherwise fall back to a plain count. */}
            {activeTasksBadges}
            {(!activeTasks?.[d.id]) && (taskCounts[d.id] ?? 0) > 0 && (
              <span className="text-ui-sm text-neutral-500">
                {taskCounts[d.id]} {taskCounts[d.id] === 1 ? 'task' : 'tasks'}
              </span>
            )}
          </div>
          {showGeneralized && (
            <div className="flex flex-wrap gap-1 mt-1.5">
              {outcomeRows.map(row => {
                const rate = row.terminal > 0 ? row.success / row.terminal : 0;
                const color = rate >= 0.7
                  ? 'bg-emerald-950/40 border-emerald-900/50 text-emerald-400'
                  : rate >= 0.4
                    ? 'bg-amber-950/40 border-amber-900/50 text-amber-400'
                    : 'bg-neutral-800/60 border-neutral-700/60 text-neutral-400';
                return (
                  <span
                    key={row.kind}
                    title={`${row.label}: ${row.success} success / ${row.neutral} neutral / ${row.failure} failure (${Math.round(rate * 100)}%)`}
                    className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium ${color}`}
                  >
                    {row.label} {row.success}/{row.terminal}
                  </span>
                );
              })}
            </div>
          )}
          {d.paths && d.paths.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1.5">
              {d.paths.map(p => (
                <span key={p} className="text-ui-sm px-1.5 py-0.5 rounded bg-neutral-800/60 border border-neutral-700/60 text-neutral-400 font-mono">
                  {p}
                </span>
              ))}
            </div>
          )}
          {d.notes && d.notes.length > 0 && (
            <div className="mt-1.5">
              <button
                onClick={() => setNotesOpen(o => !o)}
                className="flex items-center gap-1 text-ui-sm text-neutral-500 hover:text-neutral-400 transition-colors"
              >
                {notesOpen
                  ? <ChevronDown className="w-3 h-3" />
                  : <ChevronRight className="w-3 h-3" />
                }
                <span className="font-medium">Agent notes ({d.notes.length})</span>
              </button>
              {notesOpen && (
                <div className="mt-0.5 p-1.5 bg-neutral-800/30 border border-neutral-700/40 rounded text-xs">
                  {d.notes.slice(-3).map((n, ni) => {
                    const expanded = expandedNotes.has(ni);
                    const truncated = n.text.length > 200;
                    const body = expanded || !truncated ? n.text : `${n.text.slice(0, 200)}…`;
                    return (
                      <div
                        key={ni}
                        className={`text-neutral-500 leading-relaxed break-words ${truncated ? 'cursor-pointer hover:text-neutral-400' : ''}`}
                        onClick={() => {
                          if (!truncated) return;
                          setExpandedNotes((prev) => {
                            const next = new Set(prev);
                            if (next.has(ni)) next.delete(ni);
                            else next.add(ni);
                            return next;
                          });
                        }}
                        role={truncated ? 'button' : undefined}
                        tabIndex={truncated ? 0 : undefined}
                      >
                        • {body}
                        {truncated && (
                          <span className="ml-1 text-neutral-600 text-ui-sm">
                            {expanded ? '(click to collapse)' : '(click to expand)'}
                          </span>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
          {d.pending_update && (
            <div className="mt-2 p-2 bg-amber-950/20 border border-amber-900/30 rounded text-xs">
              <div className="text-amber-400 font-medium mb-1">Agent suggests update:</div>
              {d.pending_update.suggested_text && d.pending_update.suggested_text !== d.text && (
                <div className="text-neutral-300">Text: &ldquo;{d.pending_update.suggested_text.slice(0, 150)}&rdquo;</div>
              )}
              {d.pending_update.suggested_priority && d.pending_update.suggested_priority !== d.priority && (
                <div className="text-neutral-400">Priority: {d.priority} → {d.pending_update.suggested_priority}</div>
              )}
              {d.pending_update.rationale && (
                <div className="text-neutral-500 italic mt-1">{d.pending_update.rationale}</div>
              )}
              <div className="flex gap-2 mt-2">
                <button onClick={() => applyDirectionUpdate(d)} className="text-xs px-2 py-0.5 min-h-[44px] rounded border bg-emerald-950/40 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/40 transition-colors">Apply</button>
                <button onClick={() => dismissDirectionUpdate(d.id)} className="text-xs px-2 py-0.5 min-h-[44px] rounded border bg-neutral-800/60 border-neutral-700/60 text-neutral-400 hover:text-neutral-300 transition-colors">Dismiss</button>
              </div>
            </div>
          )}
          {d.research_history && d.research_history.length > 0 && (
            <div className="mt-1.5">
              {/* reason: research history section collapse toggle; single-row text-ui-sm header */}
              <button
                data-viewport-allow-small-target
                onClick={() => setHistoryOpen(o => !o)}
                className="flex items-center gap-1.5 w-full text-left group"
              >
                {historyOpen
                  ? <ChevronDown className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                  : <ChevronRight className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                }
                <span className="text-ui-sm font-medium text-neutral-500 uppercase tracking-wider group-hover:text-neutral-400 transition-colors flex-1">
                  Research History ({d.research_history.length})
                </span>
                {!historyOpen && lifetimeMetrics && (
                  <span className="text-ui-sm text-neutral-600 font-mono">
                    {lifetimeMetrics.totalMerged} merged
                    {lifetimeMetrics.successRate !== null ? ` · ${lifetimeMetrics.successRate}%` : ''}
                  </span>
                )}
                {d.possibly_saturated && (
                  <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-amber-950/40 text-amber-400 border-amber-900/50">
                    possibly saturated
                  </span>
                )}
                {d.possibly_complete && (
                  <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-emerald-950/40 text-emerald-400 border-emerald-900/50">
                    possibly complete
                  </span>
                )}
              </button>
              {historyOpen && (
                <div>
                  {lifetimeMetrics && (
                    <div className="mt-1 mb-2 space-y-0.5">
                      <div className="flex flex-wrap items-center gap-1.5">
                        {lifetimeMetrics.totalAutonomous != null || lifetimeMetrics.totalAssisted != null ? (
                          <>
                            {(lifetimeMetrics.totalAutonomous ?? 0) > 0 && (
                              <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-emerald-950/40 border-emerald-900/50 text-emerald-400">
                                {lifetimeMetrics.totalAutonomous} auto
                              </span>
                            )}
                            {(lifetimeMetrics.totalAssisted ?? 0) > 0 && (
                              <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-blue-950/40 border-blue-900/50 text-blue-400">
                                {lifetimeMetrics.totalAssisted} assisted
                              </span>
                            )}
                            {(lifetimeMetrics.totalAutonomous ?? 0) === 0 && (lifetimeMetrics.totalAssisted ?? 0) === 0 && (
                              <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-neutral-800/60 border-neutral-700/60 text-neutral-500">
                                0 PRs shipped
                              </span>
                            )}
                            {(lifetimeMetrics.totalAutonomous ?? 0) >= 10 && lifetimeMetrics.totalOperatorActions === 0 && (
                              <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-emerald-900/30 border-emerald-700/50 text-emerald-300" title="Direction ships autonomously — eligible for retirement review">
                                ✓ self-sufficient
                              </span>
                            )}
                          </>
                        ) : (
                          <span className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium ${lifetimeMetrics.totalMerged > 0 ? 'bg-emerald-950/40 border-emerald-900/50 text-emerald-400' : 'bg-neutral-800/60 border-neutral-700/60 text-neutral-500'}`}>
                            {lifetimeMetrics.totalMerged} PRs shipped
                          </span>
                        )}
                        {lifetimeMetrics.successRate !== null && (() => {
                          const rate = lifetimeMetrics.successRate;
                          const color = rate >= 70 ? 'emerald' : rate >= 40 ? 'amber' : 'red';
                          return (
                            <span className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}>
                              {rate}% success
                            </span>
                          );
                        })()}
                        {onViewFindings && (
                          // reason: inline link button in compact direction metrics row; text-ui-sm single-line
                          <button
                            data-viewport-allow-small-target
                            onClick={() => onViewFindings(d.id)}
                            className="text-ui-sm text-blue-400 hover:text-blue-300 cursor-pointer transition-colors"
                          >
                            View findings →
                          </button>
                        )}
                      </div>
                      {lifetimeMetrics.latestShipped && lifetimeMetrics.truncatedTitle && (
                        <p className="text-ui-sm text-neutral-600 italic leading-relaxed">
                          Latest shipped: {lifetimeMetrics.truncatedTitle} <span className="not-italic">({formatRelativeTime(lifetimeMetrics.latestShipped.timestamp)})</span>
                        </p>
                      )}
                      {lifetimeMetrics.sparkData.length >= 3 && (
                        <div className="mt-1">
                          <ResponsiveContainer width="100%" height={30}>
                            <BarChart data={lifetimeMetrics.sparkData} barCategoryGap="15%" margin={{ top: 0, right: 0, bottom: 0, left: 0 }}>
                              <Bar dataKey="prs" isAnimationActive={false}>
                                {lifetimeMetrics.sparkData.map((entry, index) => (
                                  <Cell
                                    key={`cell-${index}`}
                                    fill={entry.prs > 0 ? '#10b981' : '#404040'}
                                  />
                                ))}
                              </Bar>
                              <RechartsTooltip
                                cursor={{ fill: 'rgba(255,255,255,0.05)' }}
                                content={({ active, payload }) => {
                                  if (!active || !payload?.length) return null;
                                  const pt = payload[0].payload as { prs: number; timestamp: string; findings: string[] };
                                  return (
                                    <div className="text-ui-sm bg-neutral-900 border border-neutral-700 rounded px-2 py-1 max-w-[180px] shadow-lg">
                                      <div className="text-neutral-400">{formatRelativeTime(pt.timestamp)}</div>
                                      <div className="text-emerald-400 font-medium">{pt.prs} PR{pt.prs !== 1 ? 's' : ''} merged</div>
                                      {pt.findings.slice(0, 2).map((f, fi) => (
                                        <div key={fi} className="text-neutral-500 truncate">• {f.slice(0, 45)}</div>
                                      ))}
                                    </div>
                                  );
                                }}
                              />
                            </BarChart>
                          </ResponsiveContainer>
                        </div>
                      )}
                      <div className="flex items-center gap-1.5 mt-0.5 flex-wrap">
                        <p className="text-ui-sm text-neutral-600">{lifetimeMetrics.lastCycleLabel}</p>
                        {lifetimeMetrics.deltaPrs != null ? (
                          <span
                            title={`Latest cycle: ${lifetimeMetrics.latestCyclePrs} PRs merged`}
                            className={`text-ui-sm px-1.5 py-0.5 rounded border font-medium ${
                              lifetimeMetrics.deltaPrs > 0
                                ? 'bg-emerald-950/40 border-emerald-900/50 text-emerald-400'
                                : lifetimeMetrics.deltaPrs < 0
                                  ? 'bg-red-950/40 border-red-900/50 text-red-400'
                                  : 'bg-neutral-800/60 border-neutral-700/60 text-neutral-500'
                            }`}
                          >
                            {lifetimeMetrics.deltaPrs > 0 ? '+' : ''}{lifetimeMetrics.deltaPrs} vs prior
                          </span>
                        ) : (
                          <span className="text-ui-sm px-1.5 py-0.5 rounded border font-medium bg-neutral-800/60 border-neutral-700/60 text-neutral-500">
                            {lifetimeMetrics.latestCyclePrs} this cycle
                          </span>
                        )}
                        {lifetimeMetrics.rollingAvg3 != null && (
                          <span className="text-ui-sm text-neutral-600">
                            avg {lifetimeMetrics.rollingAvg3 % 1 === 0 ? lifetimeMetrics.rollingAvg3.toFixed(0) : lifetimeMetrics.rollingAvg3.toFixed(1)}/cycle
                          </span>
                        )}
                      </div>
                    </div>
                  )}
                  <div className="space-y-2">
                  {[...d.research_history].reverse().slice(0, 5).map((entry, i) => (
                    <div key={entry.task_id ?? i} className="p-2 bg-neutral-900/60 border border-neutral-800/60 rounded text-ui-sm">
                      <div className="flex items-center justify-between mb-1">
                        <div className="flex items-center gap-1.5">
                          <span className="text-neutral-400 font-medium">{formatRelativeTime(entry.timestamp)}</span>
                          {entry.task_id && onNavigateToTask && (
                            // reason: inline task-id link in compact history row; text-ui-xs
                            <button
                              data-viewport-allow-small-target
                              onClick={() => onNavigateToTask(entry.task_id)}
                              className="font-mono text-ui-xs text-blue-400 hover:text-blue-300 transition-colors"
                              title="Navigate to research task"
                            >
                              {entry.task_id.slice(0, 8)}
                            </button>
                          )}
                        </div>
                        <div className="flex items-center gap-1.5 text-neutral-600">
                          <span>{entry.tasks_created} created</span>
                          {entry.tasks_completed != null && (
                            <span className="text-emerald-600">{entry.tasks_completed} done</span>
                          )}
                          {entry.tasks_failed != null && entry.tasks_failed > 0 && (
                            <span className="text-red-600">{entry.tasks_failed} failed</span>
                          )}
                          {entry.prs_merged != null && (
                            <span className="text-blue-500">{entry.prs_merged} merged</span>
                          )}
                        </div>
                      </div>
                      {entry.finding_titles.length > 0 && (
                        <p className="text-neutral-500 leading-relaxed">
                          {entry.finding_titles.join(', ')}
                        </p>
                      )}
                      {entry.key_finding_descriptions.length > 0 && (
                        <div className="mt-1 space-y-0.5">
                          {entry.key_finding_descriptions.map((desc, j) => (
                            <p key={j} className="text-neutral-600 leading-relaxed">
                              {desc.length > 100 ? desc.slice(0, 100) + '…' : desc}
                            </p>
                          ))}
                        </div>
                      )}
                      {(entry.product_ratio != null || entry.type_distribution || entry.taxonomy_summary) && (
                        <div className="mt-1 flex flex-wrap items-center gap-1.5 border-t border-neutral-800/60 pt-1">
                          {entry.product_ratio != null && (() => {
                            const pct = Math.round((entry.product_ratio as number) * 100);
                            const color = (entry.product_ratio as number) > 0.3 ? 'emerald' : (entry.product_ratio as number) >= 0.1 ? 'amber' : 'red';
                            return (
                              <span className={`text-ui-sm px-1 py-0.5 rounded border bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}>
                                {pct}% product
                              </span>
                            );
                          })()}
                          {entry.type_distribution && Object.keys(entry.type_distribution).length > 0 && (
                            <span className="text-ui-sm text-neutral-600">
                              {Object.entries(entry.type_distribution).map(([k, v]) => `${k}:${v}`).join(' ')}
                            </span>
                          )}
                          {entry.taxonomy_summary && TAXONOMY_ORDER.map(cat => {
                            const count = entry.taxonomy_summary?.[cat] ?? 0;
                            if (count <= 0) return null;
                            return (
                              <span
                                key={cat}
                                title={`${TAXONOMY_LABELS[cat]}: ${count} failure${count !== 1 ? 's' : ''}`}
                                className={`text-ui-sm px-1 py-0.5 rounded border ${TAXONOMY_COLORS[cat]}`}
                              >
                                {TAXONOMY_LABELS[cat]} {count}
                              </span>
                            );
                          })}
                        </div>
                      )}
                      {entry.cycle_insights && entry.cycle_insights.length > 0 && (
                        <div className="mt-1 space-y-0.5 border-t border-neutral-800/60 pt-1">
                          {entry.cycle_insights.map((insight, j) => (
                            <p key={j} className="text-blue-500/70 leading-relaxed italic">
                              {insight}
                            </p>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                  </div>
                </div>
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export const DirectionCard = memo(DirectionCardImpl);
