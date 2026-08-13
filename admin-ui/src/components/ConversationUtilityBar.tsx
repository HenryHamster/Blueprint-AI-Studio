import { Search, ChevronUp } from 'lucide-react';

/**
 * Compact, collapsible search bar shown above the strategist conversation.
 * Attention and operational alerts intentionally stay out of the Chat surface.
 */
export function ConversationUtilityBar({
  expanded,
  onExpand,
  onCollapse,
  searchQuery,
  onSearchChange,
}: {
  expanded: boolean;
  onExpand: () => void;
  onCollapse: () => void;
  searchQuery: string;
  onSearchChange: (value: string) => void;
}) {
  return (
    <div className="shrink-0 border-b border-workbench-border bg-workbench-base px-3 py-1.5 md:px-4">
      <div className="mx-auto flex w-full max-w-[940px] min-w-0 flex-wrap items-center gap-2">
        {expanded ? (
          <input
            aria-label="Search strategist conversation"
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
          </>
        )}
        {expanded && (
          <button
            type="button"
            onClick={onCollapse}
            title="Hide search"
            aria-label="Hide search"
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
