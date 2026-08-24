import { Search, ChevronUp, CircleAlert, LoaderCircle, PanelRightOpen, PanelRightClose } from 'lucide-react';
import {
  WORKDAY_STATE_PRESENTATION,
  type ConversationWorkdayStatus,
} from '../utils/conversationWorkdayStatus';
import type { StrategicChatLiveState } from '../utils/strategicSessionLive';
import { timeAgo } from '../utils/time';

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
  chatLiveState,
  interactionMode,
  detailRevealed,
  onToggleDetail,
}: {
  expanded: boolean;
  onExpand: () => void;
  onCollapse: () => void;
  searchQuery: string;
  onSearchChange: (value: string) => void;
  workdayStatus?: ConversationWorkdayStatus | null;
  chatLiveState?: StrategicChatLiveState | null;
  /** Current strategist interaction mode. In 'continuous' (Workday) mode the
   *  Workday readout owns this slot outright. */
  interactionMode?: string | null;
  detailRevealed?: boolean;
  onToggleDetail?: (value: boolean) => void;
}) {
  // The chat reply chip and the Workday readout share one slot. In Workday
  // mode `computeStrategicChatLiveState` is non-null for exactly the lifecycles
  // the Resident runs under, so an ungated chip would replace the Workday
  // readout — dropping data-workday-state, the compact/full label, the detail
  // and the age stamp — precisely while Workday is running. This component owns
  // that invariant so no caller can bypass it by passing a raw live state.
  const chatChip = interactionMode === 'continuous' ? null : (chatLiveState ?? null);
  const workdayPresentation = workdayStatus
    ? WORKDAY_STATE_PRESENTATION[workdayStatus.state]
    : null;
  const workdayAge = workdayStatus?.lastActivityMs != null
    ? timeAgo(workdayStatus.lastActivityMs)
    : null;
  const chatAge = chatChip?.lastActivityMs != null
    ? timeAgo(chatChip.lastActivityMs)
    : null;
  const chatIsActive = chatChip?.phase === 'streaming' || chatChip?.phase === 'working';
  const chatNeedsAttention = chatChip?.phase === 'stuck' || chatChip?.phase === 'stale';
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
        {onToggleDetail && (
          <button
            type="button"
            onClick={() => onToggleDetail(!detailRevealed)}
            title={detailRevealed ? 'Hide details' : 'Show details'}
            aria-label={detailRevealed ? 'Hide details' : 'Show details'}
            aria-pressed={Boolean(detailRevealed)}
            data-testid="conversation-detail-toggle"
            className={`inline-flex min-h-[44px] md:min-h-9 shrink-0 items-center gap-1.5 rounded-md border px-2 text-xs transition-colors ${
              detailRevealed
                ? 'border-neutral-700 bg-workbench-shell text-neutral-200'
                : 'border-neutral-800 bg-workbench-shell/70 text-neutral-400 hover:border-neutral-700 hover:text-neutral-200'
            }`}
          >
            {detailRevealed ? (
              <PanelRightClose className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            ) : (
              <PanelRightOpen className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
            )}
            <span className="hidden sm:inline">{detailRevealed ? 'Hide details' : 'Details'}</span>
          </button>
        )}
        {(chatChip || (workdayStatus && workdayPresentation)) && (
          <div
            role="status"
            aria-live="polite"
            data-testid="conversation-workday-status"
            {...(workdayStatus && !chatChip ? { 'data-workday-state': workdayStatus.state } : {})}
            {...(chatChip ? { 'data-chat-live-phase': chatChip.phase } : {})}
            aria-label={chatChip
              ? (chatNeedsAttention ? 'Strategic status' : 'Strategic reply status')
              : undefined}
            title={chatChip
              ? [chatChip.label, chatChip.detail].filter(Boolean).join(' · ')
              : workdayStatus?.title}
            className="ml-auto flex min-w-0 max-w-full flex-1 items-center justify-end gap-1.5 whitespace-nowrap rounded-md border border-neutral-800/80 bg-workbench-shell/45 px-2 py-1 text-ui-md sm:flex-initial"
          >
            {chatNeedsAttention ? (
              <CircleAlert
                className="h-3.5 w-3.5 shrink-0 text-amber-300"
                aria-hidden="true"
              />
            ) : chatIsActive ? (
              <LoaderCircle
                className="h-3.5 w-3.5 shrink-0 animate-spin text-emerald-300"
                aria-hidden="true"
              />
            ) : chatChip ? (
              <span
                aria-hidden="true"
                className="h-1.5 w-1.5 shrink-0 rounded-full bg-workbench-control"
              />
            ) : (
              <span
                aria-hidden="true"
                className={`h-1.5 w-1.5 shrink-0 rounded-full ${workdayPresentation?.dot}`}
              />
            )}
            <span className={`min-w-0 truncate font-medium ${chatChip
              ? (chatNeedsAttention ? 'text-amber-300' : chatIsActive ? 'text-emerald-300' : 'text-neutral-300')
              : workdayPresentation?.text}`}>
              {chatChip ? chatChip.label : workdayStatus?.compactLabel ? (
                <>
                  <span className="sm:hidden">{workdayStatus.compactLabel}</span>
                  <span className="hidden sm:inline">{workdayStatus.label}</span>
                </>
              ) : workdayStatus?.label}
            </span>
            {(chatChip?.detail || workdayStatus?.detail) && (
              <>
                <span aria-hidden="true" className="shrink-0 text-neutral-700">·</span>
                <span className="min-w-0 truncate text-neutral-500">
                  {chatChip ? chatChip.detail : (
                    <>
                      <span className="block max-w-full truncate sm:hidden">
                        {workdayStatus?.compactDetail}
                      </span>
                      <span className="hidden max-w-full truncate sm:inline">
                        {workdayStatus?.detail}
                      </span>
                    </>
                  )}
                </span>
              </>
            )}
            {chatChip && chatAge && (
              <>
                <span aria-hidden="true" className="shrink-0 text-neutral-700">·</span>
                <span
                  data-testid="conversation-chat-age"
                  className="shrink-0 text-neutral-600"
                >
                  {chatAge}
                </span>
              </>
            )}
            {!chatChip && workdayAge && (
              <>
                <span aria-hidden="true" className="shrink-0 text-neutral-700">·</span>
                <span
                  data-testid="conversation-workday-age"
                  className="shrink-0 text-neutral-600"
                >
                  {workdayAge}
                </span>
              </>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
