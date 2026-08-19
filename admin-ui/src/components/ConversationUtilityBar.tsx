import { Search, ChevronUp } from 'lucide-react';
import type {
  ConversationWorkdayState,
  ConversationWorkdayStatus,
} from '../utils/conversationWorkdayStatus';

const WORKDAY_STATE_PRESENTATION: Record<
  ConversationWorkdayState,
  { dot: string; text: string }
> = {
  off: { dot: 'bg-workbench-card', text: 'text-neutral-500' },
  ready: { dot: 'bg-sky-700', text: 'text-sky-400' },
  starting: { dot: 'bg-amber-400 animate-pulse', text: 'text-amber-300' },
  working: { dot: 'bg-emerald-400 animate-pulse', text: 'text-emerald-300' },
  waiting: { dot: 'bg-sky-700', text: 'text-sky-400' },
  consultation_queued: { dot: 'bg-violet-400 animate-pulse', text: 'text-violet-300' },
  consultation_running: { dot: 'bg-violet-400 animate-pulse', text: 'text-violet-300' },
  consultation_delivering: { dot: 'bg-indigo-400 animate-pulse', text: 'text-indigo-300' },
  paused: { dot: 'bg-amber-600', text: 'text-amber-400' },
  retrying: { dot: 'bg-amber-400 animate-pulse', text: 'text-amber-300' },
  stopping: { dot: 'bg-amber-600', text: 'text-amber-400' },
  unavailable: { dot: 'bg-red-700', text: 'text-red-400' },
  needs_restart: { dot: 'bg-red-400', text: 'text-red-300' },
};

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
  workdayStatus,
}: {
  expanded: boolean;
  onExpand: () => void;
  onCollapse: () => void;
  searchQuery: string;
  onSearchChange: (value: string) => void;
  workdayStatus?: ConversationWorkdayStatus | null;
}) {
  const workdayPresentation = workdayStatus
    ? WORKDAY_STATE_PRESENTATION[workdayStatus.state]
    : null;
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
            className="min-h-[36px] min-w-0 flex-1 rounded-md border border-neutral-700 bg-workbench-shell px-3 py-1.5 text-xs text-neutral-100 placeholder:text-neutral-500 workbench-focus"
          />
        ) : (
          <>
            <button
              type="button"
              onClick={onExpand}
              title="Search conversation"
              aria-label="Search conversation"
              className="inline-flex min-h-[44px] md:min-h-9 shrink-0 items-center gap-1.5 rounded-md border border-neutral-800 bg-workbench-shell/70 px-2 text-xs text-neutral-400 transition-colors hover:border-neutral-700 hover:text-neutral-200"
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
            className="inline-flex min-h-[44px] md:min-h-9 shrink-0 items-center gap-1 rounded-md border border-neutral-800 bg-workbench-shell/70 px-2 text-xs text-neutral-400 transition-colors hover:border-neutral-700 hover:text-neutral-200"
          >
            <ChevronUp className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            <span className="hidden sm:inline">Hide</span>
          </button>
        )}
        {workdayStatus && workdayPresentation && (
          <div
            role="status"
            aria-live="polite"
            data-testid="conversation-workday-status"
            data-workday-state={workdayStatus.state}
            title={workdayStatus.title}
            className="ml-auto flex min-w-0 max-w-full flex-1 items-center justify-end gap-1.5 whitespace-nowrap rounded-md border border-neutral-800/80 bg-workbench-shell/45 px-2 py-1 text-ui-md sm:flex-initial"
          >
            <span
              aria-hidden="true"
              className={`h-1.5 w-1.5 shrink-0 rounded-full ${workdayPresentation.dot}`}
            />
            <span className={`shrink-0 font-medium ${workdayPresentation.text}`}>
              {workdayStatus.compactLabel ? (
                <>
                  <span className="sm:hidden">{workdayStatus.compactLabel}</span>
                  <span className="hidden sm:inline">{workdayStatus.label}</span>
                </>
              ) : workdayStatus.label}
            </span>
            {workdayStatus.detail && (
              <>
                <span aria-hidden="true" className="shrink-0 text-neutral-700">·</span>
                <span className="min-w-0 truncate text-neutral-500">
                  <span className="block max-w-full truncate sm:hidden">
                    {workdayStatus.compactDetail}
                  </span>
                  <span className="hidden max-w-full truncate sm:inline">
                    {workdayStatus.detail}
                  </span>
                </span>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
