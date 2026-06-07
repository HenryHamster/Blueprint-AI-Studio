import type { RefObject } from 'react';
import { AlertTriangle, Check, RotateCcw } from 'lucide-react';
import { isSafeUrl, FAILURE_CLASS_CONFIG } from './TaskCard';
import { WaitingBadge } from './catchupRows';
import { formatSnoozeRemaining } from './catchupHelpers';
import type { DismissedState } from './catchupHelpers';

export interface NeedsAttentionItem {
  task_id: string;
  title: string;
  reason: 'failed' | 'approval_required';
  error_snippet: string | null;
  pr_url: string | null;
  waiting_since?: string;
  waiting_hours?: number;
  approval_reason?: string;
  touched_critical?: string[];
  failure_class?: string | null;
  scope?: string | null;
  direction_id?: string | null;
  direction_text?: string | null;
  merge_retries_saved?: number | null;
  task_type?: string;
  spend_cap_usd?: number | null;
  deadline_seconds?: number | null;
}

function formatDeadline(seconds: number): string {
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  const hours = seconds / 3600;
  return Number.isInteger(hours) ? `${hours}h` : `${hours.toFixed(1)}h`;
}

export interface CatchUpNeedsAttentionSectionProps {
  items: NeedsAttentionItem[];
  visibleItems: NeedsAttentionItem[];
  approvalItems: NeedsAttentionItem[];
  failedItems: NeedsAttentionItem[];
  dismissed: DismissedState;
  expandedSnippets: Set<string>;
  openDropdown: string | null;
  showDismissed: boolean;
  dropdownRef: RefObject<HTMLDivElement | null>;
  isHidden: (taskId: string) => boolean;
  onSelectTask: (task: { id: string }) => void;
  onApprove?: (taskId: string) => void;
  onRetry?: (taskId: string) => void;
  onBulkApprove?: (taskIds: string[]) => void;
  onBulkRetry?: (taskIds: string[]) => void;
  handleApprove: (taskId: string) => void;
  handleRetry: (taskId: string) => void;
  handleBulkApproveAll: () => void;
  handleBulkRetryAll: () => void;
  setOpenDropdown: (v: string | null) => void;
  toggleSnippet: (id: string) => void;
  dismissItem: (taskId: string) => void;
  snoozeItem: (taskId: string, hours: number) => void;
  restoreItem: (taskId: string) => void;
  handleToggleShowDismissed: () => void;
}

export function CatchUpNeedsAttentionSection({
  items,
  visibleItems,
  approvalItems,
  failedItems,
  dismissed,
  expandedSnippets,
  openDropdown,
  showDismissed,
  dropdownRef,
  isHidden,
  onSelectTask,
  onApprove,
  onRetry,
  onBulkApprove,
  onBulkRetry,
  handleApprove,
  handleRetry,
  handleBulkApproveAll,
  handleBulkRetryAll,
  setOpenDropdown,
  toggleSnippet,
  dismissItem,
  snoozeItem,
  restoreItem,
  handleToggleShowDismissed,
}: CatchUpNeedsAttentionSectionProps) {
  if (items.length === 0) return null;
  const hiddenItems = items.filter(item => isHidden(item.task_id));
  const countLabel = hiddenItems.length > 0
    ? `${visibleItems.length} visible / ${items.length} total`
    : String(items.length);
  return (
    <div className="border border-amber-500/30 rounded-lg p-3 flex flex-col gap-2">
      <div className="flex items-center gap-1.5 text-[11px] font-bold text-amber-400 uppercase tracking-widest">
        <AlertTriangle className="w-3 h-3" />
        <span className="flex-1">Needs attention ({countLabel})</span>
        {onBulkApprove && approvalItems.length >= 2 && (
          <button
            className="text-[10px] font-semibold px-2 py-4 rounded border flex items-center gap-1 bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/20 border-emerald-500/20 normal-case tracking-normal"
            onClick={handleBulkApproveAll}
          >
            <Check className="w-3 h-3" />
            Approve all ({approvalItems.length})
          </button>
        )}
        {onBulkRetry && failedItems.length >= 2 && (
          <button
            className="text-[10px] font-semibold px-2 py-4 rounded border flex items-center gap-1 bg-blue-500/10 text-blue-500 hover:bg-blue-500/20 border-blue-500/20 normal-case tracking-normal"
            onClick={handleBulkRetryAll}
          >
            <RotateCcw className="w-3 h-3" />
            Retry all ({failedItems.length})
          </button>
        )}
      </div>
      <div className="flex flex-col gap-1.5">
        {visibleItems.map(item => (
          <div key={item.task_id} className="flex flex-col gap-1">
            <div
              role="button"
              tabIndex={0}
              className="flex flex-wrap items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/80 px-2 py-1.5 rounded cursor-pointer hover:bg-neutral-800/50 min-h-[44px]"
              onClick={() => onSelectTask({ id: item.task_id })}
              onKeyDown={e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelectTask({ id: item.task_id }); } }}
            >
              <span className="min-w-0 flex-1 truncate text-neutral-300">{item.title}</span>
              {item.direction_text && (
                <span
                  className="shrink-0 text-[9px] px-1.5 py-0.5 rounded bg-neutral-700/30 text-neutral-400 border border-neutral-700/40 truncate max-w-[12rem]"
                  title={item.direction_text}
                >
                  {item.direction_text.length > 40 ? item.direction_text.slice(0, 40) + '…' : item.direction_text}
                </span>
              )}
              {item.scope && (
                <span
                  className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-indigo-500/10 text-indigo-400 border border-indigo-500/20"
                  title={`scope: ${item.scope}`}
                >
                  {item.scope}
                </span>
              )}
              <span className={`shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded ${
                item.reason === 'failed'
                  ? 'bg-red-500/10 text-red-400'
                  : 'bg-amber-500/10 text-amber-400'
              }`}>
                {item.reason === 'failed' ? 'Failed' : 'Approval required'}
              </span>
              {item.reason === 'approval_required' && item.task_type === 'compute_experiment' && (
                <span
                  className="shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-400 border border-purple-500/20"
                  title="A compute_experiment launch held for operator approval — approve, adjust the cap, or reject"
                >
                  Held compute launch
                </span>
              )}
              {item.reason === 'failed' && item.failure_class && FAILURE_CLASS_CONFIG[item.failure_class] && (
                <span
                  className={`shrink-0 text-[9px] uppercase tracking-wider font-semibold px-1.5 py-0.5 rounded ${FAILURE_CLASS_CONFIG[item.failure_class].color}`}
                  title={FAILURE_CLASS_CONFIG[item.failure_class].hint}
                >
                  {FAILURE_CLASS_CONFIG[item.failure_class].label}
                </span>
              )}
              {item.reason === 'failed' && typeof item.merge_retries_saved === 'number' && item.merge_retries_saved > 0 && (
                <span
                  className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-emerald-500/10 text-emerald-400 border border-emerald-500/20"
                  title="Merge failed with identical error repeatedly — early exit saved these retry attempts"
                >
                  saved {item.merge_retries_saved} {item.merge_retries_saved === 1 ? 'retry' : 'retries'}
                </span>
              )}
              {item.reason === 'failed' && item.pr_url && isSafeUrl(item.pr_url) && (
                <a
                  href={item.pr_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={e => e.stopPropagation()}
                  className="shrink-0 text-[9px] text-emerald-400 hover:text-emerald-300 font-medium"
                >
                  {item.pr_url.match(/\/(\d+)\/?$/)?.[1] ? `PR #${item.pr_url.match(/\/(\d+)\/?$/)?.[1]}` : 'PR'}
                </a>
              )}
              <WaitingBadge hours={item.waiting_hours} />
              {item.reason === 'approval_required' && onApprove && (
                <button
                  className="text-[9px] font-semibold px-2 py-4 rounded border flex items-center gap-0.5 bg-emerald-500/10 text-emerald-500 hover:bg-emerald-500/20 border-emerald-500/20"
                  onClick={e => { e.stopPropagation(); handleApprove(item.task_id); }}
                >
                  <Check className="w-2.5 h-2.5" />
                  Approve
                </button>
              )}
              {item.reason === 'failed' && onRetry && (
                <button
                  className="text-[9px] font-semibold px-2 py-4 rounded border flex items-center gap-0.5 bg-blue-500/10 text-blue-500 hover:bg-blue-500/20 border-blue-500/20"
                  onClick={e => { e.stopPropagation(); handleRetry(item.task_id); }}
                >
                  <RotateCcw className="w-2.5 h-2.5" />
                  Retry
                </button>
              )}
              {/* Dismiss/snooze button */}
              <div className="relative shrink-0" onClick={e => e.stopPropagation()}>
                <button
                  className="text-neutral-600 hover:text-neutral-400 transition-colors min-h-[44px] min-w-[44px] flex items-center justify-center leading-none text-sm"
                  title="Dismiss or snooze"
                  aria-label="Dismiss or snooze this item"
                  onClick={() => setOpenDropdown(openDropdown === item.task_id ? null : item.task_id)}
                  onKeyDown={e => { if (e.key === 'Escape') setOpenDropdown(null); }}
                >
                  ×
                </button>
                {openDropdown === item.task_id && (
                  <div
                    ref={dropdownRef}
                    role="menu"
                    className="absolute right-0 top-full mt-0.5 z-10 bg-neutral-900 border border-neutral-700 rounded shadow-lg py-0.5 min-w-[110px]"
                    onKeyDown={e => { if (e.key === 'Escape') setOpenDropdown(null); }}
                  >
                    {(['Dismiss', 'Snooze 4h', 'Snooze 12h', 'Snooze 24h'] as const).map(opt => (
                      <button
                        key={opt}
                        role="menuitem"
                        className="block w-full text-left text-[11px] px-3 py-1 text-neutral-300 hover:bg-neutral-800 hover:text-neutral-100 transition-colors"
                        onClick={() => {
                          if (opt === 'Dismiss') dismissItem(item.task_id);
                          else if (opt === 'Snooze 4h') snoozeItem(item.task_id, 4);
                          else if (opt === 'Snooze 12h') snoozeItem(item.task_id, 12);
                          else snoozeItem(item.task_id, 24);
                        }}
                      >
                        {opt}
                      </button>
                    ))}
                  </div>
                )}
              </div>
            </div>
            {item.reason === 'approval_required' && item.task_type === 'compute_experiment'
              && (typeof item.spend_cap_usd === 'number' || typeof item.deadline_seconds === 'number') && (
              <div className="flex items-center gap-1.5 px-2">
                {typeof item.spend_cap_usd === 'number' && (
                  <span
                    className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-300 border border-purple-500/20"
                    title="Proposed spend cap for this held compute launch"
                  >
                    spend ${item.spend_cap_usd.toLocaleString(undefined, { maximumFractionDigits: 2 })}
                  </span>
                )}
                {typeof item.deadline_seconds === 'number' && (
                  <span
                    className="shrink-0 text-[9px] font-semibold px-1.5 py-0.5 rounded bg-purple-500/10 text-purple-300 border border-purple-500/20"
                    title="Proposed wall-clock deadline for this held compute launch"
                  >
                    deadline {formatDeadline(item.deadline_seconds)}
                  </span>
                )}
              </div>
            )}
            {item.reason === 'approval_required' && item.approval_reason && (
              <div className="flex items-center gap-1.5 px-2">
                <span className="text-[10px] text-neutral-500 italic truncate">
                  {item.approval_reason.length > 100 ? item.approval_reason.slice(0, 100) + '…' : item.approval_reason}
                </span>
                {item.touched_critical && item.touched_critical.length > 0 && (
                  <span className="shrink-0 text-[9px] px-1.5 py-0.5 rounded bg-red-500/10 text-red-400 border border-red-500/20">
                    critical files
                  </span>
                )}
              </div>
            )}
            {item.error_snippet && (
              <div className="px-2">
                <button
                  onClick={() => toggleSnippet(item.task_id)}
                  aria-expanded={expandedSnippets.has(item.task_id)}
                  aria-label={expandedSnippets.has(item.task_id) ? 'Hide error details' : 'Show error details'}
                  className="text-[10px] text-neutral-600 hover:text-neutral-400 transition-colors"
                >
                  {expandedSnippets.has(item.task_id) ? 'hide error ▲' : 'show error ▼'}
                </button>
                {expandedSnippets.has(item.task_id) && (
                  <pre className="mt-1 text-[10px] text-red-400/80 bg-red-950/20 border border-red-900/30 rounded p-2 overflow-x-auto whitespace-pre-wrap break-all">
                    {item.error_snippet}
                  </pre>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
      {hiddenItems.length > 0 && (
        <div className="mt-1">
          <button
            className="text-[10px] text-neutral-600 hover:text-neutral-400 transition-colors"
            onClick={handleToggleShowDismissed}
          >
            {showDismissed ? 'Hide dismissed' : `Show ${hiddenItems.length} dismissed`}
          </button>
          {showDismissed && (
            <div className="flex flex-col gap-1.5 mt-1.5">
              {hiddenItems.map(item => {
                const entry = dismissed[item.task_id];
                return (
                  <div key={item.task_id} className="flex items-center gap-2 text-xs bg-neutral-950 border border-neutral-800/50 px-2 py-1.5 rounded opacity-50">
                    <span className="min-w-0 flex-1 truncate text-neutral-400">{item.title}</span>
                    {entry?.type === 'snooze' && (
                      <span className="shrink-0 text-[9px] font-mono text-neutral-500">
                        {formatSnoozeRemaining((entry as { type: 'snooze'; until: number }).until)}
                      </span>
                    )}
                    <button
                      className="shrink-0 text-[9px] font-semibold px-1.5 min-h-[44px] flex items-center rounded border bg-neutral-700/30 text-neutral-400 hover:bg-neutral-700/60 border-neutral-700/40 transition-colors"
                      aria-label="Restore this item"
                      onClick={() => restoreItem(item.task_id)}
                    >
                      Restore
                    </button>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
