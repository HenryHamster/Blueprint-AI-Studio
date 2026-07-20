import { Search, ChevronUp } from 'lucide-react';
import type { ReactNode } from 'react';

/**
 * Compact, collapsible utility bar shown above the Resident conversation. Collapsed by
 * default — a search icon plus optional "N need your action" / "N alerts" count pills —
 * so the conversation stays clean. Expanding reveals the full conversation-search input;
 * the caller renders the inline alerts banner and the needs-action strip below, gated on
 * the same `expanded` flag. Extracted from MessagesPanel to keep that file under its cap.
 */
export function ConversationUtilityBar({
  expanded,
  onExpand,
  onCollapse,
  searchQuery,
  onSearchChange,
  needsActionCount,
  alertCount,
  residentHealth,
}: {
  expanded: boolean;
  onExpand: () => void;
  onCollapse: () => void;
  searchQuery: string;
  onSearchChange: (value: string) => void;
  needsActionCount: number;
  alertCount: number;
  residentHealth?: ReactNode;
}) {
  return (
    <div className="shrink-0 border-b border-workbench-border bg-workbench-base px-3 py-1.5 md:px-4">
      <div className="mx-auto flex w-full max-w-[940px] min-w-0 flex-wrap items-center gap-2">
        {expanded ? (
          <input
            aria-label="Search Resident conversation"
            type="search"
            value={searchQuery}
            onChange={event => onSearchChange(event.target.value)}
            placeholder="Search conversation"
            className="min-h-[36px] min-w-0 flex-1 rounded-md border border-neutral-700 bg-neutral-950 px-3 py-1.5 text-xs text-neutral-100 placeholder:text-neutral-500 focus:border-indigo-500 focus:outline-none"
          />
        ) : (
          <>
            <button
              type="button"
              onClick={onExpand}
              title="Search conversation"
              aria-label="Search conversation"
              className="inline-flex min-h-[44px] md:min-h-9 shrink-0 items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-2 text-xs text-neutral-400 transition-colors hover:border-neutral-700 hover:text-neutral-200"
            >
              <Search className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span className="hidden sm:inline">Search</span>
            </button>
            {needsActionCount > 0 && (
              <button
                type="button"
                onClick={onExpand}
                title="Show items that need your action"
                className="inline-flex min-h-[44px] md:min-h-9 shrink-0 items-center gap-1.5 rounded-md border border-amber-800/60 bg-amber-950/25 px-2 text-xs font-medium text-amber-200/90 transition-colors hover:bg-amber-900/35"
              >
                <span aria-hidden="true">⚠</span>
                <span className="truncate">{needsActionCount} need{needsActionCount === 1 ? 's' : ''} your action</span>
              </button>
            )}
            {alertCount > 0 && (
              <button
                type="button"
                onClick={onExpand}
                title="Show alerts"
                className="inline-flex min-h-[44px] md:min-h-9 shrink-0 items-center gap-1.5 rounded-md border border-neutral-800 bg-neutral-950/70 px-2 text-xs font-medium text-neutral-300 transition-colors hover:border-neutral-700 hover:text-neutral-100"
              >
                {alertCount} alert{alertCount === 1 ? '' : 's'}
              </button>
            )}
          </>
        )}
        <div className="ml-auto flex min-w-0 items-center gap-2">{residentHealth}</div>
        {expanded && (
          <button
            type="button"
            onClick={onCollapse}
            title="Hide search, alerts and actions"
            aria-label="Hide search, alerts and actions"
            className="inline-flex min-h-[44px] md:min-h-9 shrink-0 items-center gap-1 rounded-md border border-neutral-800 bg-neutral-950/70 px-2 text-xs text-neutral-400 transition-colors hover:border-neutral-700 hover:text-neutral-200"
          >
            <ChevronUp className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span className="hidden sm:inline">Hide</span>
          </button>
        )}
      </div>
    </div>
  );
}
