import { memo, useEffect, useMemo, useRef, useState } from 'react';
import type React from 'react';
import { AlertTriangle, Check, ChevronDown, ChevronRight, ChevronUp, Loader2, Pencil, Trash2, X, Zap } from 'lucide-react';
import { BarChart, Bar, Cell, ResponsiveContainer, Tooltip as RechartsTooltip } from 'recharts';
import type { Direction } from './DirectionsPanel';
import type { DirectionCategory } from '../hooks/useDirectionForm';
import { formatRelativeTime, formatTimeRemaining, getCooldownStatus, priorityColors, statusColors } from '../utils/directionHelpers';
import { parseTimestamp } from '../utils/time';

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
  editStatus: 'active' | 'completed' | 'paused' | 'proposed' | 'expired';
  editCategory: DirectionCategory;
  editPathTags: string[];
  editPathInput: string;
  editCompletionCriteria: string;
  editCostBudget?: string;
  setEditingId: React.Dispatch<React.SetStateAction<string | null>>;
  setEditText: React.Dispatch<React.SetStateAction<string>>;
  setEditPriority: React.Dispatch<React.SetStateAction<'high' | 'medium' | 'low'>>;
  setEditStatus: React.Dispatch<React.SetStateAction<'active' | 'completed' | 'paused' | 'proposed' | 'expired'>>;
  setEditCategory: React.Dispatch<React.SetStateAction<DirectionCategory>>;
  setEditPathTags: React.Dispatch<React.SetStateAction<string[]>>;
  setEditPathInput: React.Dispatch<React.SetStateAction<string>>;
  setEditCompletionCriteria: React.Dispatch<React.SetStateAction<string>>;
  setEditCostBudget?: React.Dispatch<React.SetStateAction<string>>;
  startEdit: (d: Direction) => void;
  // Action handlers
  updateDirection: (id: string) => Promise<void>;
  approveProposal: (id: string) => Promise<void>;
  rejectProposal: (id: string) => Promise<void>;
  reviveProposal: (id: string) => Promise<void>;
  reactivateDirection: (id: string) => Promise<void>;
  unholdDirection: (id: string) => Promise<void>;
  markDirectionComplete: (id: string) => Promise<void>;
  quickToggleStatus: (id: string, status: 'active' | 'paused') => Promise<void>;
  deleteDirection: (id: string) => Promise<void>;
  applyDirectionUpdate: (d: Direction) => Promise<void>;
  dismissDirectionUpdate: (id: string) => Promise<void>;
  triggerResearch: (id: string, bypass?: boolean) => Promise<void>;
  reorderDirections: (newOrder: Direction[]) => Promise<void>;
  // State
  taskCounts: Record<string, number>;
  activeTasks?: Record<string, Array<{ id: string; status: string; title: string; latest_status_message?: string }>>;
  cooldownMinutes: number;
  researchingIds: Record<string, boolean>;
  researchSuccessIds: Record<string, boolean>;
  // Force confirm popover
  forceConfirmId: string | null;
  setForceConfirmId: React.Dispatch<React.SetStateAction<string | null>>;
  // Research history
  historyOpenIds: Record<string, boolean>;
  setHistoryOpenIds: React.Dispatch<React.SetStateAction<Record<string, boolean>>>;
  // Navigation callbacks
  onViewFindings?: (directionId: string) => void;
  onNavigateToTask?: (taskId: string) => void;
}

function DirectionCardImpl({
  direction: d,
  statusKey: key,
  activeItems,
  editingId, editText, editPriority, editStatus, editCategory, editPathTags, editPathInput, editCompletionCriteria, editCostBudget,
  setEditingId, setEditText, setEditPriority, setEditStatus, setEditCategory, setEditPathTags, setEditPathInput, setEditCompletionCriteria, setEditCostBudget,
  startEdit,
  updateDirection, approveProposal, rejectProposal, reviveProposal,
  reactivateDirection, unholdDirection, markDirectionComplete, quickToggleStatus, deleteDirection,
  applyDirectionUpdate, dismissDirectionUpdate, triggerResearch, reorderDirections,
  taskCounts, activeTasks, cooldownMinutes, researchingIds, researchSuccessIds,
  forceConfirmId, setForceConfirmId,
  historyOpenIds, setHistoryOpenIds,
  onViewFindings, onNavigateToTask,
}: DirectionCardProps) {
  const forceConfirmRef = useRef<HTMLDivElement | null>(null);
  const [expandedNotes, setExpandedNotes] = useState<Set<number>>(new Set());
  const [expandedRemediationIds, setExpandedRemediationIds] = useState<Record<string, boolean>>({});
  const showAllRemediation = Boolean(expandedRemediationIds[d.id]);

  const autoRecoveryLine = useMemo(
    () => formatAutoRecoveryLine(d.held_until),
    [d.held_until],
  );

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

  return (
    <div className={`border rounded-lg p-3 ${d.status === 'proposed' ? 'bg-purple-950/20 border-purple-900/40' : d.status === 'expired' ? 'bg-neutral-900/30 border-neutral-800/40 opacity-70' : 'bg-neutral-900/50 border-neutral-800/50'}`}>
      {d.status === 'proposed' ? (
        /* Proposed direction — approve or reject */
        <div>
          <div className="flex items-start gap-2">
            <p className="text-sm text-neutral-200 flex-1">{d.text}</p>
            <div className="flex items-center gap-1 shrink-0">
              <button
                onClick={() => approveProposal(d.id)}
                title="Approve — activate this direction"
                className="text-[10px] px-2 py-0.5 rounded border font-medium transition-colors bg-emerald-950/60 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/50"
              >
                Approve
              </button>
              <button
                onClick={() => rejectProposal(d.id)}
                title="Reject — remove this proposal"
                className="text-[10px] px-2 py-0.5 rounded border font-medium transition-colors bg-red-950/60 border-red-900/50 text-red-400 hover:bg-red-900/50"
              >
                Reject
              </button>
            </div>
          </div>
          {d.proposal_rationale && (
            <blockquote className="mt-2 border-l-2 border-purple-800/50 pl-2 text-[11px] text-purple-300/80 italic leading-relaxed">
              {d.proposal_rationale}
            </blockquote>
          )}
          {d.completion_criteria && (
            <p className="mt-1.5 text-[11px] text-neutral-500 leading-relaxed">
              <span className="font-medium text-neutral-600">Done when: </span>
              <span className="italic">{d.completion_criteria}</span>
            </p>
          )}
          <div className="flex items-center gap-2 mt-2">
            <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${priorityColors[d.priority]}`}>
              {d.priority}
            </span>
            <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${statusColors.proposed}`}>
              Proposed by agent
            </span>
            {d.proposed_at && (
              <span className="text-[10px] text-neutral-600 ml-auto">
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
              <button
                onClick={() => reviveProposal(d.id)}
                title="Revive — move back to proposed"
                className="text-[10px] px-2 py-0.5 rounded border font-medium transition-colors bg-purple-950/60 border-purple-900/50 text-purple-400 hover:bg-purple-900/50"
              >
                Revive
              </button>
              <button
                onClick={() => approveProposal(d.id)}
                title="Approve — activate this direction"
                className="text-[10px] px-2 py-0.5 rounded border font-medium transition-colors bg-emerald-950/60 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/50"
              >
                Approve
              </button>
            </div>
          </div>
          {d.proposal_rationale && (
            <blockquote className="mt-2 border-l-2 border-neutral-700/50 pl-2 text-[11px] text-neutral-500 italic leading-relaxed">
              {d.proposal_rationale}
            </blockquote>
          )}
          {d.completion_criteria && (
            <p className="mt-1.5 text-[11px] text-neutral-500 leading-relaxed">
              <span className="font-medium text-neutral-600">Done when: </span>
              <span className="italic">{d.completion_criteria}</span>
            </p>
          )}
          <div className="flex items-center gap-2 mt-2">
            <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${priorityColors[d.priority]}`}>
              {d.priority}
            </span>
            <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${statusColors.expired}`}>
              Expired
            </span>
            {d.proposed_at && (
              <span className="text-[10px] text-neutral-600 ml-auto">
                proposed {formatRelativeTime(d.proposed_at)}
              </span>
            )}
          </div>
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
          <div className="bg-neutral-950 border border-neutral-700 rounded px-2 py-1.5 flex flex-wrap gap-1 items-center focus-within:ring-1 focus-within:ring-neutral-600">
            {editPathTags.map(tag => (
              <span key={tag} className="bg-neutral-800 border border-neutral-700 text-neutral-300 text-[10px] font-mono px-1.5 py-0.5 rounded flex items-center gap-1">
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
          <div className="flex items-center gap-2">
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
              onChange={e => setEditStatus(e.target.value as 'active' | 'completed' | 'paused')}
              className="bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
            >
              <option value="active">Active</option>
              <option value="completed">Completed</option>
              <option value="paused">Paused</option>
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
            <button onClick={() => updateDirection(d.id)} className="text-emerald-400 hover:text-emerald-300 transition-colors">
              <Check className="w-3.5 h-3.5" />
            </button>
            <button onClick={() => setEditingId(null)} className="text-neutral-500 hover:text-neutral-300 transition-colors">
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
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
                    <button
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
                    <button
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
                const cooldownStatus = getCooldownStatus(d, cooldownMinutes);
                const inCooldown = cooldownStatus === 'cooling';
                return (
                  <div className="relative flex items-center gap-1">
                    <button
                      onClick={() => !inCooldown && triggerResearch(d.id)}
                      disabled={researchingIds[d.id] || inCooldown}
                      title={inCooldown ? 'Direction is in cooldown — use force to bypass' : 'Trigger research for this direction'}
                      className="text-[10px] px-1.5 py-0.5 rounded border font-medium transition-colors disabled:opacity-40 bg-neutral-800/60 border-neutral-700/60 text-neutral-400 hover:enabled:text-neutral-200 hover:enabled:bg-neutral-700/60"
                    >
                      {researchingIds[d.id] ? '…' : researchSuccessIds[d.id] ? 'Queued' : 'Research'}
                    </button>
                    {inCooldown && (
                      <div className="relative">
                        <button
                          onClick={() => setForceConfirmId(forceConfirmId === d.id ? null : d.id)}
                          disabled={researchingIds[d.id]}
                          title="Force research (bypass cooldown)"
                          className="text-[10px] px-1 py-0.5 rounded border font-medium transition-colors disabled:opacity-40 bg-amber-950/40 border-amber-900/50 text-amber-400 hover:bg-amber-900/40"
                        >
                          <Zap className="w-2.5 h-2.5" />
                        </button>
                        {forceConfirmId === d.id && (
                          <div
                            ref={forceConfirmRef}
                            className="absolute right-0 top-6 z-10 w-52 bg-neutral-900 border border-amber-900/60 rounded-lg p-3 shadow-xl"
                          >
                            <p className="text-[11px] text-amber-300 font-medium mb-1">Force research?</p>
                            <p className="text-[10px] text-neutral-400 mb-2 leading-relaxed">Bypasses the cooldown and queues a research task immediately.</p>
                            <div className="flex gap-2">
                              <button
                                onClick={() => triggerResearch(d.id, true)}
                                className="flex-1 text-[10px] px-2 py-1 rounded border bg-amber-950/60 border-amber-900/50 text-amber-300 hover:bg-amber-900/50 font-medium transition-colors"
                              >
                                Force
                              </button>
                              <button
                                onClick={() => setForceConfirmId(null)}
                                className="flex-1 text-[10px] px-2 py-1 rounded border bg-neutral-800 border-neutral-700 text-neutral-400 hover:text-neutral-200 font-medium transition-colors"
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
              <button onClick={() => startEdit(d)} className="text-neutral-400 hover:text-neutral-300 transition-colors p-0.5">
                <Pencil className="w-3 h-3" />
              </button>
              <button onClick={() => deleteDirection(d.id)} className="text-neutral-400 hover:text-red-400 transition-colors p-0.5">
                <Trash2 className="w-3 h-3" />
              </button>
            </div>
          </div>
          {d.completion_criteria && (
            <p className="mt-1.5 text-[11px] text-neutral-500 leading-relaxed">
              <span className="font-medium text-neutral-600">Done when: </span>
              <span className="italic">{d.completion_criteria}</span>
            </p>
          )}
          {/* Last researched / cooldown subtitle */}
          {key === 'active' && (() => {
            const cooldownStatus = getCooldownStatus(d, cooldownMinutes);
            if (cooldownStatus === 'never') {
              return <p className="text-[10px] text-neutral-600 mt-0.5">Never researched</p>;
            }
            if (cooldownStatus === 'cooling') {
              return <p className="text-[10px] text-amber-500/80 mt-0.5">Last researched {formatRelativeTime(d.last_researched_at ?? '')}</p>;
            }
            return <p className="text-[10px] text-neutral-600 mt-0.5">Last researched {formatRelativeTime(d.last_researched_at ?? '')}</p>;
          })()}
          {/* Auto-pause reason subtitle — red for budget-exceeded pauses, amber for yield/saturation */}
          {key === 'paused' && d.paused_reason && (
            <p className={`text-[10px] mt-0.5 leading-relaxed ${d.budget_exceeded ? 'text-red-400/80' : 'text-amber-500/80'}`}>
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
                <p className="text-[11px] text-red-300 font-medium leading-tight">On hold</p>
                <p className="text-[10px] text-red-400/80 leading-relaxed mt-0.5">
                  {d.held_reason || 'Held after repeated failures.'}
                </p>
                {autoRecoveryLine && (
                  <p className="text-[10px] text-red-300/90 leading-relaxed mt-0.5">
                    {autoRecoveryLine}
                  </p>
                )}
              </div>
              <button
                onClick={(e) => { e.stopPropagation(); void unholdDirection(d.id); }}
                className="text-[10px] px-2 py-0.5 rounded border border-red-700 text-red-200 hover:bg-red-900/40 shrink-0"
                title="Clear the hold so this direction can spawn autonomous tasks again"
              >
                unhold
              </button>
            </div>
          )}
          {/* Positive completion suggestion */}
          {d.possibly_complete && d.completion_suggestion && (
            <p className="text-[10px] text-emerald-500/80 mt-0.5 leading-relaxed">
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
                    <p className="text-[11px] text-red-300 font-medium leading-tight">Manual intervention needed</p>
                    <p className="text-[10px] text-red-400/80 leading-relaxed mt-0.5">
                      Auto-remediation escalated after {d.remediation_attempts ?? 0} failed attempt{(d.remediation_attempts ?? 0) !== 1 ? 's' : ''}.
                      {d.diagnosis_summary && <span className="block mt-0.5 text-neutral-400 italic">{d.diagnosis_summary}</span>}
                    </p>
                  </div>
                </div>
              ) : (
                <>
                  {d.diagnosis_in_progress && (
                    <div className="flex items-center gap-1.5 text-[10px] text-amber-400">
                      <Loader2 className="w-3 h-3 animate-spin shrink-0" />
                      <span>Diagnosing failure pattern…</span>
                    </div>
                  )}
                  {d.diagnosis_summary && !d.diagnosis_in_progress && (
                    <p className="text-[10px] text-neutral-400 leading-relaxed">
                      <span className="text-neutral-500 font-medium">Diagnosis: </span>
                      {d.diagnosis_summary}
                      {d.diagnosis_completed_at && (
                        <span className="text-neutral-600 ml-1">· {formatRelativeTime(d.diagnosis_completed_at)}</span>
                      )}
                    </p>
                  )}
                  {(d.remediation_attempts ?? 0) > 0 && (() => {
                    const attempts = d.remediation_attempts ?? 0;
                    const result = d.last_remediation_result;
                    const color = result === 'merged' ? 'emerald' : result === 'failed' ? 'red' : 'amber';
                    const label = result === 'merged' ? 'merged' : result === 'failed' ? 'failed' : 'pending';
                    return (
                      <span className={`inline-block text-[10px] px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}>
                        Remediation {attempts}× · {label}
                      </span>
                    );
                  })()}
                </>
              )}
            </div>
          )}
          {remediationSummaryRows.length > 0 && (
            <div className="mt-2">
              <p className="text-[10px] font-medium uppercase tracking-wider text-neutral-500">
                Recent remediation attempts
              </p>
              <div className="mt-1 space-y-1">
                {remediationSummaryRows.slice(0, showAllRemediation ? 3 : 2).map((entry) => (
                  <div key={entry.key} className="text-[10px] text-neutral-400 flex items-center gap-1.5 leading-relaxed">
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
                <button
                  onClick={() => setExpandedRemediationIds(prev => ({ ...prev, [d.id]: !prev[d.id] }))}
                  className="mt-1 text-[10px] text-neutral-500 hover:text-neutral-300 transition-colors"
                >
                  {showAllRemediation ? 'show less' : 'show more'}
                </button>
              )}
            </div>
          )}
          <div className="flex items-center gap-2 mt-2">
            <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${priorityColors[d.priority]}`}>
              {d.priority}
            </span>
            {d.status === 'active' || d.status === 'paused' ? (
              <button
                onClick={() => quickToggleStatus(d.id, d.status as 'active' | 'paused')}
                title={d.status === 'active' ? 'Click to pause' : 'Click to activate'}
                className={`text-[10px] px-1.5 py-0.5 rounded border font-medium transition-opacity hover:opacity-70 cursor-pointer ${statusColors[d.status]}`}
              >
                {d.status}
              </button>
            ) : (
              <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${statusColors[d.status]}`}>
                {d.status}
              </span>
            )}
            {d.category === 'product' && (
              <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-emerald-950/40 border-emerald-900/50 text-emerald-400">
                Product
              </span>
            )}
            {d.category === 'structural' && (
              <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-blue-950/40 border-blue-900/50 text-blue-400">
                Structural
              </span>
            )}
            {d.category === 'mixed' && (
              <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-purple-950/40 border-purple-900/50 text-purple-400">
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
                  className={`text-[10px] px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}
                >
                  {Math.round(score * 100)}% quality
                </span>
              );
            })()}
            {/* Aggregate product ratio badge from latest research history entry */}
            {(() => {
              const entries = d.research_history?.filter(e => e.product_ratio != null);
              if (!entries || entries.length === 0) return null;
              const latest = [...entries].reverse()[0];
              const ratio = latest.product_ratio as number;
              const pct = Math.round(ratio * 100);
              const color = ratio > 0.3 ? 'emerald' : ratio >= 0.1 ? 'amber' : 'red';
              return (
                <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}>
                  {pct}% product
                </span>
              );
            })()}
            {/* Productivity score badge — (merged - 0.5*rejected) / cycles; quality-weighted, can go negative */}
            {(() => {
              const hasHistory = d.research_history && d.research_history.length >= 3;
              if (!hasHistory || d.merge_rate == null) return null;
              const score = d.merge_rate as number;
              const color = score >= 0.5 ? 'emerald' : score >= 0.2 ? 'amber' : score >= 0 ? 'red' : 'red';
              return (
                <span
                  title="Productivity score: (merged − 0.5 × rejected) / cycles. Negative values indicate rejections outweighed merges."
                  className={`text-[10px] px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}
                >
                  {score.toFixed(2)} productivity
                </span>
              );
            })()}
            {/* Acceptance rate badge — merged / (merged + rejected) */}
            {(() => {
              const history = d.research_history;
              if (!history || history.length === 0) return null;
              const historyMerged = history.reduce((sum, e) => sum + (e.prs_merged ?? 0), 0);
              const historyRejected = history.reduce((sum, e) => sum + (e.prs_rejected ?? 0), 0);
              const aggMerged = (d.history_aggregate?.total_prs_merged as number | null) ?? 0;
              const aggRejected = (d.history_aggregate?.total_prs_rejected as number | null) ?? 0;
              const totalMerged = historyMerged + aggMerged;
              const totalRejected = historyRejected + aggRejected;
              const totalDecided = totalMerged + totalRejected;
              if (totalRejected === 0 || totalDecided === 0) return null;
              const rate = totalMerged / totalDecided;
              const pct = Math.round(rate * 100);
              const color = rate >= 0.6 ? 'emerald' : rate >= 0.3 ? 'amber' : 'red';
              return (
                <span
                  title={`${totalMerged} merged / ${totalDecided} decided (${totalRejected} rejected)`}
                  className={`text-[10px] px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}
                >
                  {pct}% acceptance
                </span>
              );
            })()}
            {/* Lifetime metrics badges */}
            {(() => {
              const history = d.research_history;
              if (!history || history.length === 0) return null;
              const totalPrsMerged = history.reduce((sum, e) => sum + (e.prs_merged ?? 0), 0);
              const totalTasksCreated = history.reduce((sum, e) => sum + (e.tasks_created ?? 0), 0);
              const rawRate = totalTasksCreated > 0 ? totalPrsMerged / totalTasksCreated : 0;
              // tasks_created is frozen at cycle-complete time, but prs_merged keeps
              // updating as children (including later auto-remediation spawns under
              // the same parent) finish merging — so the raw ratio can exceed 1.0.
              // Clamp for display; surface the raw numbers in the tooltip.
              const displayRate = Math.min(rawRate, 1);
              const totalCycles = history.length;
              const colorBucket = displayRate >= 0.5 ? 'emerald' : displayRate >= 0.2 ? 'amber' : 'red';
              return (
                <>
                  {totalPrsMerged > 0 && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-blue-950/40 border-blue-900/50 text-blue-400">
                      {totalPrsMerged} PRs merged
                    </span>
                  )}
                  {totalCycles >= 2 && (
                    <span
                      title={`${totalPrsMerged} merged across ${totalTasksCreated} spawned tasks${rawRate > 1 ? ' (capped at 100%; later auto-remediation children inflated the ratio)' : ''}`}
                      className={`text-[10px] px-1.5 py-0.5 rounded border font-medium bg-${colorBucket}-950/40 border-${colorBucket}-900/50 text-${colorBucket}-400`}
                    >
                      {Math.round(displayRate * 100)}% success
                    </span>
                  )}
                </>
              );
            })()}
            {/* Reactivate button for auto-paused directions */}
            {d.auto_paused && key === 'paused' && (
              <button
                onClick={() => reactivateDirection(d.id)}
                className="text-[10px] px-1.5 py-0.5 rounded border font-medium transition-colors bg-emerald-950/40 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/40"
              >
                Reactivate
              </button>
            )}
            {/* Mark Complete button for possibly-complete directions */}
            {d.possibly_complete && key === 'active' && (
              <button
                onClick={() => markDirectionComplete(d.id)}
                className="text-[10px] px-1.5 py-0.5 rounded border font-medium transition-colors bg-emerald-950/40 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/40"
              >
                Mark Complete
              </button>
            )}
            {/* Budget utilization indicator */}
            {(() => {
              const spend = d.estimated_cost_usd;
              const budget = d.cost_budget_usd;
              if (spend == null && (budget == null || budget === 0)) return null;
              const utilPct = d.budget_utilization_pct != null
                ? d.budget_utilization_pct
                : (budget != null && budget > 0 && spend != null ? (spend / budget) * 100 : null);
              const exceeded = d.budget_exceeded ?? (utilPct != null && utilPct > 100);
              if (budget == null || budget === 0) {
                return (
                  <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-neutral-800/60 border-neutral-700/60 text-neutral-500">
                    ${(spend ?? 0).toFixed(2)} spent
                  </span>
                );
              }
              if (spend == null) {
                return (
                  <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-neutral-800/60 border-neutral-700/60 text-neutral-500">
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
                  className={`text-[10px] px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}
                >
                  {label}
                </span>
              );
            })()}
            {/* Cost-per-merge retirement warning */}
            {d.cost_per_merge_exceeds_threshold && (() => {
              // Prefer the rolling-window split from retirement_consideration_evidence (per-(provider,model)
              // cost summed across the same window the threshold uses); fall back to the digest's
              // window-level cost_by_provider_model so the badge still shows context for legacy entries.
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
                    className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-amber-950/40 border-amber-900/50 text-amber-400"
                  >
                    High cost / autonomous merge
                  </span>
                  {top.length > 0 && (
                    <span
                      title={tooltip}
                      className="text-[10px] text-amber-400/80 font-mono"
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
            })()}
            {/* Cooldown badge */}
            {key === 'active' && (() => {
              const cooldownStatus = getCooldownStatus(d, cooldownMinutes);
              if (cooldownStatus === 'cooling') {
                return (
                  <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-amber-950/40 text-amber-400 border-amber-900/50">
                    Cooling down · {formatTimeRemaining(d.last_researched_at ?? '', cooldownMinutes)}
                  </span>
                );
              }
              return (
                <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-emerald-950/40 text-emerald-400 border-emerald-900/50">
                  Ready
                </span>
              );
            })()}
            <span className="text-[10px] text-neutral-600 ml-auto">
              {d.added_at ? parseTimestamp(d.added_at).toLocaleDateString() : ''}
            </span>
            {/* Active research tasks for this direction — clickable badges when we
                have the per-task list, otherwise fall back to a plain count. */}
            {(() => {
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
                      key={t.id}
                      type="button"
                      onClick={() => onNavigateToTask?.(t.id)}
                      title={t.title || t.latest_status_message || t.status}
                      className={`text-[10px] px-1.5 py-0.5 rounded border font-medium transition-opacity hover:opacity-80 ${statusColor(t.status)}`}
                    >
                      {t.status.toLowerCase()} · {t.id.slice(0, 8)}
                    </button>
                  ))}
                </div>
              );
            })()}
            {(!activeTasks?.[d.id]) && (taskCounts[d.id] ?? 0) > 0 && (
              <span className="text-[10px] text-neutral-500">
                {taskCounts[d.id]} {taskCounts[d.id] === 1 ? 'task' : 'tasks'}
              </span>
            )}
          </div>
          {d.paths && d.paths.length > 0 && (
            <div className="flex flex-wrap gap-1 mt-1.5">
              {d.paths.map(p => (
                <span key={p} className="text-[10px] px-1.5 py-0.5 rounded bg-neutral-800/60 border border-neutral-700/60 text-neutral-400 font-mono">
                  {p}
                </span>
              ))}
            </div>
          )}
          {d.notes && d.notes.length > 0 && (
            <div className="mt-2 p-1.5 bg-neutral-800/30 border border-neutral-700/40 rounded text-xs">
              <div className="text-neutral-500 font-medium mb-1">Agent notes ({d.notes.length}):</div>
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
                      <span className="ml-1 text-neutral-600 text-[10px]">
                        {expanded ? '(click to collapse)' : '(click to expand)'}
                      </span>
                    )}
                  </div>
                );
              })}
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
                <button onClick={() => applyDirectionUpdate(d)} className="text-xs px-2 py-0.5 rounded border bg-emerald-950/40 border-emerald-900/50 text-emerald-400 hover:bg-emerald-900/40 transition-colors">Apply</button>
                <button onClick={() => dismissDirectionUpdate(d.id)} className="text-xs px-2 py-0.5 rounded border bg-neutral-800/60 border-neutral-700/60 text-neutral-400 hover:text-neutral-300 transition-colors">Dismiss</button>
              </div>
            </div>
          )}
          {/* Lifetime outcome metrics */}
          {lifetimeMetrics && (
            <div className="mt-1.5 space-y-0.5">
              <div className="flex flex-wrap items-center gap-1.5">
                {lifetimeMetrics.totalAutonomous != null || lifetimeMetrics.totalAssisted != null ? (
                  <>
                    {(lifetimeMetrics.totalAutonomous ?? 0) > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-emerald-950/40 border-emerald-900/50 text-emerald-400">
                        {lifetimeMetrics.totalAutonomous} auto
                      </span>
                    )}
                    {(lifetimeMetrics.totalAssisted ?? 0) > 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-blue-950/40 border-blue-900/50 text-blue-400">
                        {lifetimeMetrics.totalAssisted} assisted
                      </span>
                    )}
                    {(lifetimeMetrics.totalAutonomous ?? 0) === 0 && (lifetimeMetrics.totalAssisted ?? 0) === 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-neutral-800/60 border-neutral-700/60 text-neutral-500">
                        0 PRs shipped
                      </span>
                    )}
                    {(lifetimeMetrics.totalAutonomous ?? 0) >= 10 && lifetimeMetrics.totalOperatorActions === 0 && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-emerald-900/30 border-emerald-700/50 text-emerald-300" title="Direction ships autonomously — eligible for retirement review">
                        ✓ self-sufficient
                      </span>
                    )}
                  </>
                ) : (
                  <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${lifetimeMetrics.totalMerged > 0 ? 'bg-emerald-950/40 border-emerald-900/50 text-emerald-400' : 'bg-neutral-800/60 border-neutral-700/60 text-neutral-500'}`}>
                    {lifetimeMetrics.totalMerged} PRs shipped
                  </span>
                )}
                {lifetimeMetrics.successRate !== null && (() => {
                  const rate = lifetimeMetrics.successRate;
                  const color = rate >= 70 ? 'emerald' : rate >= 40 ? 'amber' : 'red';
                  return (
                    <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}>
                      {rate}% success
                    </span>
                  );
                })()}
                {onViewFindings && (
                  <button
                    onClick={() => onViewFindings(d.id)}
                    className="text-[10px] text-blue-400 hover:text-blue-300 cursor-pointer transition-colors"
                  >
                    View findings →
                  </button>
                )}
              </div>
              {lifetimeMetrics.latestShipped && lifetimeMetrics.truncatedTitle && (
                <p className="text-[10px] text-neutral-600 italic leading-relaxed">
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
                            <div className="text-[10px] bg-neutral-900 border border-neutral-700 rounded px-2 py-1 max-w-[180px] shadow-lg">
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
                <p className="text-[10px] text-neutral-600">{lifetimeMetrics.lastCycleLabel}</p>
                {lifetimeMetrics.deltaPrs != null ? (
                  <span
                    title={`Latest cycle: ${lifetimeMetrics.latestCyclePrs} PRs merged`}
                    className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${
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
                  <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-neutral-800/60 border-neutral-700/60 text-neutral-500">
                    {lifetimeMetrics.latestCyclePrs} this cycle
                  </span>
                )}
                {lifetimeMetrics.rollingAvg3 != null && (
                  <span className="text-[10px] text-neutral-600">
                    avg {lifetimeMetrics.rollingAvg3 % 1 === 0 ? lifetimeMetrics.rollingAvg3.toFixed(0) : lifetimeMetrics.rollingAvg3.toFixed(1)}/cycle
                  </span>
                )}
              </div>
            </div>
          )}
          {d.research_history && d.research_history.length > 0 && (
            <div className="mt-2">
              <button
                onClick={() => setHistoryOpenIds(prev => ({ ...prev, [d.id]: !prev[d.id] }))}
                className="flex items-center gap-1.5 w-full text-left group"
              >
                {historyOpenIds[d.id]
                  ? <ChevronDown className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                  : <ChevronRight className="w-3 h-3 text-neutral-600 group-hover:text-neutral-400 transition-colors" />
                }
                <span className="text-[10px] font-medium text-neutral-500 uppercase tracking-wider group-hover:text-neutral-400 transition-colors flex-1">
                  Research History ({d.research_history.length})
                </span>
                {d.possibly_saturated && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-amber-950/40 text-amber-400 border-amber-900/50">
                    possibly saturated
                  </span>
                )}
                {d.possibly_complete && (
                  <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-emerald-950/40 text-emerald-400 border-emerald-900/50">
                    possibly complete
                  </span>
                )}
              </button>
              {historyOpenIds[d.id] && (
                <div className="mt-1.5 space-y-2">
                  {[...d.research_history].reverse().slice(0, 5).map((entry, i) => (
                    <div key={entry.task_id ?? i} className="p-2 bg-neutral-900/60 border border-neutral-800/60 rounded text-[10px]">
                      <div className="flex items-center justify-between mb-1">
                        <div className="flex items-center gap-1.5">
                          <span className="text-neutral-400 font-medium">{formatRelativeTime(entry.timestamp)}</span>
                          {entry.task_id && onNavigateToTask && (
                            <button
                              onClick={() => onNavigateToTask(entry.task_id)}
                              className="font-mono text-[9px] text-blue-400 hover:text-blue-300 transition-colors"
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
                              <span className={`text-[10px] px-1 py-0.5 rounded border bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}>
                                {pct}% product
                              </span>
                            );
                          })()}
                          {entry.type_distribution && Object.keys(entry.type_distribution).length > 0 && (
                            <span className="text-[10px] text-neutral-600">
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
                                className={`text-[10px] px-1 py-0.5 rounded border ${TAXONOMY_COLORS[cat]}`}
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
              )}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export const DirectionCard = memo(DirectionCardImpl);
