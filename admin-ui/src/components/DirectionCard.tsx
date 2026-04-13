import { useEffect, useRef } from 'react';
import type React from 'react';
import { Check, ChevronDown, ChevronRight, ChevronUp, Pencil, Trash2, X, Zap } from 'lucide-react';
import type { Direction } from './DirectionsPanel';
import type { DirectionCategory } from '../hooks/useDirectionForm';
import { formatRelativeTime, formatTimeRemaining, getCooldownStatus, priorityColors, statusColors } from '../utils/directionHelpers';
import { parseTimestamp } from '../utils/time';

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
  setEditingId: React.Dispatch<React.SetStateAction<string | null>>;
  setEditText: React.Dispatch<React.SetStateAction<string>>;
  setEditPriority: React.Dispatch<React.SetStateAction<'high' | 'medium' | 'low'>>;
  setEditStatus: React.Dispatch<React.SetStateAction<'active' | 'completed' | 'paused' | 'proposed' | 'expired'>>;
  setEditCategory: React.Dispatch<React.SetStateAction<DirectionCategory>>;
  setEditPathTags: React.Dispatch<React.SetStateAction<string[]>>;
  setEditPathInput: React.Dispatch<React.SetStateAction<string>>;
  startEdit: (d: Direction) => void;
  // Action handlers
  updateDirection: (id: string) => Promise<void>;
  approveProposal: (id: string) => Promise<void>;
  rejectProposal: (id: string) => Promise<void>;
  reviveProposal: (id: string) => Promise<void>;
  reactivateDirection: (id: string) => Promise<void>;
  markDirectionComplete: (id: string) => Promise<void>;
  quickToggleStatus: (id: string, status: 'active' | 'paused') => Promise<void>;
  deleteDirection: (id: string) => Promise<void>;
  applyDirectionUpdate: (d: Direction) => Promise<void>;
  dismissDirectionUpdate: (id: string) => Promise<void>;
  triggerResearch: (id: string, bypass?: boolean) => Promise<void>;
  reorderDirections: (newOrder: Direction[]) => Promise<void>;
  // State
  taskCounts: Record<string, number>;
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

export function DirectionCard({
  direction: d,
  statusKey: key,
  activeItems,
  editingId, editText, editPriority, editStatus, editCategory, editPathTags, editPathInput,
  setEditingId, setEditText, setEditPriority, setEditStatus, setEditCategory, setEditPathTags, setEditPathInput,
  startEdit,
  updateDirection, approveProposal, rejectProposal, reviveProposal,
  reactivateDirection, markDirectionComplete, quickToggleStatus, deleteDirection,
  applyDirectionUpdate, dismissDirectionUpdate, triggerResearch, reorderDirections,
  taskCounts, cooldownMinutes, researchingIds, researchSuccessIds,
  forceConfirmId, setForceConfirmId,
  historyOpenIds, setHistoryOpenIds,
  onViewFindings, onNavigateToTask,
}: DirectionCardProps) {
  const forceConfirmRef = useRef<HTMLDivElement | null>(null);

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
          {/* Auto-pause reason subtitle */}
          {key === 'paused' && d.paused_reason && (
            <p className="text-[10px] text-amber-500/80 mt-0.5 leading-relaxed">
              {d.paused_reason}
              {d.paused_at && <span className="text-neutral-600"> · paused {formatRelativeTime(d.paused_at)}</span>}
            </p>
          )}
          {/* Positive completion suggestion */}
          {d.possibly_complete && d.completion_suggestion && (
            <p className="text-[10px] text-emerald-500/80 mt-0.5 leading-relaxed">
              {d.completion_suggestion}
            </p>
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
            {/* Merge rate badge */}
            {(() => {
              const hasHistory = d.research_history && d.research_history.length >= 3;
              if (!hasHistory || d.merge_rate == null) return null;
              const rate = d.merge_rate as number;
              const color = rate >= 0.5 ? 'emerald' : rate >= 0.2 ? 'amber' : 'red';
              return (
                <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}>
                  {rate.toFixed(2)} PRs/cycle
                </span>
              );
            })()}
            {/* Lifetime metrics badges */}
            {(() => {
              const history = d.research_history;
              if (!history || history.length === 0) return null;
              const totalPrsMerged = history.reduce((sum, e) => sum + (e.prs_merged ?? 0), 0);
              const totalTasksCreated = history.reduce((sum, e) => sum + (e.tasks_created ?? 0), 0);
              const successRate = totalTasksCreated > 0 ? totalPrsMerged / totalTasksCreated : 0;
              const totalCycles = history.length;
              return (
                <>
                  {totalPrsMerged > 0 && (
                    <span className="text-[10px] px-1.5 py-0.5 rounded border font-medium bg-blue-950/40 border-blue-900/50 text-blue-400">
                      {totalPrsMerged} PRs merged
                    </span>
                  )}
                  {totalCycles >= 2 && (
                    <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium bg-${successRate >= 0.5 ? 'emerald' : successRate >= 0.2 ? 'amber' : 'red'}-950/40 border-${successRate >= 0.5 ? 'emerald' : successRate >= 0.2 ? 'amber' : 'red'}-900/50 text-${successRate >= 0.5 ? 'emerald' : successRate >= 0.2 ? 'amber' : 'red'}-400`}>
                      {Math.round(successRate * 100)}% success
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
            {(taskCounts[d.id] ?? 0) > 0 && (
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
              {d.notes.slice(-3).map((n, ni) => (
                <div key={ni} className="text-neutral-500 leading-relaxed">• {n.text.slice(0, 120)}</div>
              ))}
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
          {d.research_history && d.research_history.length > 0 && (() => {
            const historyMerged = d.research_history.reduce((sum, e) => sum + (e.prs_merged ?? 0), 0);
            const historyFailed = d.research_history.reduce((sum, e) => sum + (e.tasks_failed ?? 0), 0);
            const aggMerged = d.history_aggregate?.total_prs_merged ?? 0;
            const aggFailed = d.history_aggregate?.total_tasks_failed ?? 0;
            const totalMerged = historyMerged + (aggMerged as number);
            const totalFailed = historyFailed + (aggFailed as number);
            const totalTasks = totalMerged + totalFailed;
            const successRate = totalTasks >= 3 ? Math.round((totalMerged / totalTasks) * 100) : null;

            // Find most recent entry with prs_merged > 0 and finding_titles
            const latestShipped = [...d.research_history].reverse().find(e => (e.prs_merged ?? 0) > 0 && e.finding_titles.length > 0);
            const latestTitle = latestShipped?.finding_titles[0];
            const truncatedTitle = latestTitle && latestTitle.length > 60 ? latestTitle.slice(0, 60) + '…' : latestTitle;

            return (
              <div className="mt-1.5 space-y-0.5">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium ${totalMerged > 0 ? 'bg-emerald-950/40 border-emerald-900/50 text-emerald-400' : 'bg-neutral-800/60 border-neutral-700/60 text-neutral-500'}`}>
                    {totalMerged} PRs shipped
                  </span>
                  {successRate !== null && (() => {
                    const color = successRate >= 70 ? 'emerald' : successRate >= 40 ? 'amber' : 'red';
                    return (
                      <span className={`text-[10px] px-1.5 py-0.5 rounded border font-medium bg-${color}-950/40 border-${color}-900/50 text-${color}-400`}>
                        {successRate}% success
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
                {latestShipped && truncatedTitle && (
                  <p className="text-[10px] text-neutral-600 italic leading-relaxed">
                    Latest shipped: {truncatedTitle} <span className="not-italic">({formatRelativeTime(latestShipped.timestamp)})</span>
                  </p>
                )}
              </div>
            );
          })()}
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
                      {(entry.product_ratio != null || entry.type_distribution) && (
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
