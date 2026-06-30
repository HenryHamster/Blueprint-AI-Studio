import { type Dispatch, type SetStateAction, type RefObject } from 'react';
import { MessageSquare, AlertTriangle, HelpCircle, ChevronDown, ArrowDown, ChevronUp, Check, MoreVertical } from 'lucide-react';
import { ROUTINE_EVENT_LABELS } from './eventPrefs';
import { type RoutineRow, type MessageWithDivider, routineSummary, formatDailyDigestSummary, outcomeBreakdown, insertTimeDividers, formatTimestamp, formatSinceTimestamp } from './messageGrouping';
import { MessageBubble, type MessageBubbleProps } from './MessageBubble';
import type { ProjectMessage } from '../types/messages';
import { MessagesSummaryCard } from './MessagesSummaryCard';
import { type MessageSummaryCounts } from './useMessageList';
import { type TemporaryMutes } from './temporaryMutes';

type BubbleProps = Omit<MessageBubbleProps, 'msg' | 'faded' | 'readOnly' | 'allMessages'>;

function TimeDividerRow({ label, className }: { label: string; className?: string }) {
  return (
    <div className={`flex items-center gap-1.5 text-ui-md text-neutral-600 font-semibold uppercase tracking-wide px-1 mt-2${className ? ` ${className}` : ''}`}>
      <div className="flex-1 border-t border-neutral-800/60" />
      <span className="shrink-0">{label}</span>
      <div className="flex-1 border-t border-neutral-800/60" />
    </div>
  );
}

export interface MessagesListViewProps {
  presentation?: 'triage' | 'conversation';
  // Refs
  scrollRef: RefObject<HTMLDivElement | null>;
  blockersRefEl: RefObject<HTMLDivElement | null>;
  questionsRefEl: RefObject<HTMLDivElement | null>;
  routineRefEl: RefObject<HTMLDivElement | null>;
  dividerDismissedRef: RefObject<boolean>;

  // Raw + derived list state
  messages: ProjectMessage[];
  loading: boolean;
  dividerTime: number | null;
  totalNewCount: number;
  newMessageCounts: MessageSummaryCounts;
  highSignalOpen: ProjectMessage[];
  routineOpen: ProjectMessage[];
  linkedMessages: ProjectMessage[];
  pinnedBlockers: ProjectMessage[];
  pinnedQuestions: ProjectMessage[];
  otherHighSignal: ProjectMessage[];
  promotedGroups: (RoutineRow<ProjectMessage> & { kind: 'group' })[];
  remainingRoutineRows: RoutineRow<ProjectMessage>[];
  autoExpandedKeys: Set<string>;
  visibleRoutineCount: number;
  mutedRoutineCount: number;
  visibleResolved: ProjectMessage[];
  hiddenResolvedCount: number;
  resolvedMessages: ProjectMessage[];

  // UI state
  searchQuery: string;
  showOnlyNew: boolean;
  showResolved: boolean;
  routineExpanded: boolean;
  showScrollDown: boolean;
  muteActive: boolean;
  showTempMuteMenu: boolean;
  expandedGroupKeys: Set<string>;
  tempMutes: TemporaryMutes;

  // Shared bubble props
  bubbleProps: BubbleProps;
  highlightedMessageId?: string | null;

  // Callbacks
  onDismissDivider: () => void;
  onShowOnlyNew: () => void;
  setExpandedGroupKeys: Dispatch<SetStateAction<Set<string>>>;
  setRoutineExpanded: Dispatch<SetStateAction<boolean>>;
  setShowResolved: Dispatch<SetStateAction<boolean>>;
  setShowTempMuteMenu: Dispatch<SetStateAction<boolean>>;
  addTemporaryMute: (eventType: string, durationMs: number) => void;
  getTop3NoisyEventTypes: () => Array<[string, number]>;
  handleResolveGroup: (msgs: ProjectMessage[]) => void;
  handleBulkDeleteResolved: () => void;
  scrollToBottom: () => void;
}

/**
 * Presentational scroll area for MessagesPanel: banners, caught-up/empty/no-match/loading
 * states, pinned sections, the NEW divider, summary card, other-high-signal, promoted
 * groups, the routine collapse/expand block, the resolved section + toggles, and the
 * scroll-down button. Renders entirely from props — owns no list-derivation logic.
 */
export function MessagesListView({
  presentation = 'triage',
  scrollRef,
  blockersRefEl,
  questionsRefEl,
  routineRefEl,
  dividerDismissedRef,
  messages,
  loading,
  dividerTime,
  totalNewCount,
  newMessageCounts,
  highSignalOpen,
  routineOpen,
  linkedMessages,
  pinnedBlockers,
  pinnedQuestions,
  otherHighSignal,
  promotedGroups,
  remainingRoutineRows,
  autoExpandedKeys,
  visibleRoutineCount,
  mutedRoutineCount,
  visibleResolved,
  hiddenResolvedCount,
  resolvedMessages,
  searchQuery,
  showOnlyNew,
  showResolved,
  routineExpanded,
  showScrollDown,
  muteActive,
  showTempMuteMenu,
  expandedGroupKeys,
  tempMutes,
  bubbleProps,
  highlightedMessageId,
  onDismissDivider,
  onShowOnlyNew,
  setExpandedGroupKeys,
  setRoutineExpanded,
  setShowResolved,
  setShowTempMuteMenu,
  addTemporaryMute,
  getTop3NoisyEventTypes,
  handleResolveGroup,
  handleBulkDeleteResolved,
  scrollToBottom,
}: MessagesListViewProps) {
  if (presentation === 'conversation') {
    return (
      <div ref={scrollRef} className="relative flex-1 overflow-y-auto bg-[#05080c] px-2.5 py-3 custom-scrollbar md:px-3">
        <div data-testid="strategic-conversation-column" className="mx-auto flex w-full max-w-[940px] flex-col gap-3 lg:mx-0">
          {loading ? (
            <p className="py-8 text-center text-xs text-neutral-500">Loading...</p>
          ) : messages.length === 0 ? (
            <div className="flex flex-col items-center justify-center py-12 text-neutral-500">
              <MessageSquare className="mb-3 h-8 w-8 opacity-40" />
              <p className="text-sm">No messages yet</p>
            </div>
          ) : (
            insertTimeDividers(messages).map((item, idx) =>
              item.kind === 'divider' ? (
                <TimeDividerRow key={`divider-conversation-${idx}`} label={item.label} className="my-1" />
              ) : (
                <MessageBubble
                  key={item.msg.id}
                  msg={item.msg}
                  faded={item.msg.status === 'resolved'}
                  allMessages={messages}
                  presentation="chat"
                  highlighted={item.msg.id === highlightedMessageId}
                  {...bubbleProps}
                />
              ),
            )
          )}
        </div>
        {showScrollDown && (
          <button
            onClick={scrollToBottom}
            className="absolute bottom-24 left-1/2 flex min-h-11 min-w-11 -translate-x-1/2 items-center justify-center rounded-full border border-neutral-700 bg-neutral-900 p-2 text-neutral-400 shadow-lg transition-colors hover:bg-neutral-800 hover:text-neutral-200"
            aria-label="Scroll to latest message"
          >
            <ArrowDown className="h-4 w-4" />
          </button>
        )}
      </div>
    );
  }

  return (
    <div ref={scrollRef} className="relative flex-1 space-y-2.5 overflow-y-auto bg-[#05080c] px-3 py-3 custom-scrollbar md:px-4">
      {/* Prominent banner: X new since {time} with action buttons */}
      {dividerTime && totalNewCount > 0 && !dividerDismissedRef.current && (
        <div className="mb-2.5 flex flex-col gap-3 rounded-md border border-blue-500/40 bg-[#10151c] p-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold text-blue-300 truncate">
              {totalNewCount} new since {formatSinceTimestamp(dividerTime)}
            </div>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <button
              onClick={onShowOnlyNew}
              className="min-h-[44px] shrink-0 rounded-md bg-blue-600 px-3 py-2 text-xs font-medium text-white transition-colors hover:bg-blue-500 md:min-h-8"
            >
              Show only new
            </button>
            <button
              onClick={onDismissDivider}
              className="min-h-[44px] shrink-0 rounded-md border border-blue-500/50 px-3 py-2 text-xs font-medium text-blue-300 transition-colors hover:bg-blue-500/10 md:min-h-8"
            >
              Mark all read
            </button>
          </div>
        </div>
      )}

      {/* Caught-up indicator: when divider is set, not dismissed, and zero new messages of any kind */}
      {dividerTime && !dividerDismissedRef.current && totalNewCount === 0 && !showOnlyNew && (
        <div className="mb-2.5 flex flex-col gap-3 rounded-md border border-emerald-500/40 bg-[#10151c] p-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold text-emerald-300 truncate">
              No new since {formatSinceTimestamp(dividerTime)}
            </div>
          </div>
          <div className="flex items-center gap-2 flex-wrap">
            <button
              onClick={onDismissDivider}
              className="min-h-[44px] shrink-0 rounded-md border border-emerald-500/50 px-3 py-2 text-xs font-medium text-emerald-300 transition-colors hover:bg-emerald-500/10 md:min-h-8"
            >
              Reset baseline
            </button>
          </div>
        </div>
      )}

      {loading ? (
        <p className="text-xs text-neutral-500 text-center py-8">Loading...</p>
      ) : messages.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-neutral-500">
          <MessageSquare className="w-8 h-8 mb-3 opacity-40" />
          <p className="text-sm">No messages yet</p>
          <p className="text-xs mt-1 opacity-70">Send feedback, report bugs, or ask questions</p>
        </div>
      ) : showOnlyNew && dividerTime && highSignalOpen.length === 0 && routineOpen.length === 0 && linkedMessages.length === 0 && visibleResolved.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-neutral-500">
          <MessageSquare className="w-8 h-8 mb-3 opacity-40" />
          <p className="text-sm">No new messages since {formatTimestamp(dividerTime)}</p>
          <p className="text-xs mt-1 opacity-70">You're all caught up!</p>
        </div>
      ) : searchQuery.trim() && highSignalOpen.length === 0 && routineOpen.length === 0 && linkedMessages.length === 0 && visibleResolved.length === 0 ? (
        <div className="flex flex-col items-center justify-center py-12 text-neutral-500">
          <MessageSquare className="w-8 h-8 mb-3 opacity-40" />
          <p className="text-sm">No matches for "{searchQuery}"</p>
          <p className="text-xs mt-1 opacity-70">Try a different search term</p>
        </div>
      ) : (
        <>
          {/* Pinned BLOCKERs (importance mode only) */}
          {pinnedBlockers.length > 0 && (
            <>
              <div className="flex items-center gap-1.5 text-ui-md text-amber-400/70 font-semibold uppercase tracking-wide px-1">
                <AlertTriangle className="w-3 h-3" />
                <span>Pinned · Blockers</span>
              </div>
              {insertTimeDividers(pinnedBlockers).map((item, idx) =>
                item.kind === 'divider' ? (
                  <TimeDividerRow key={`divider-pinned-${idx}`} label={item.label} />
                ) : (
                  <MessageBubble key={item.msg.id} msg={item.msg} allMessages={messages} {...bubbleProps} />
                )
              )}
              {(pinnedQuestions.length > 0 || otherHighSignal.length > 0) && <div className="border-t border-neutral-800/60" />}
            </>
          )}

          {/* Pinned QUESTIONs (importance mode only) — awaiting operator reply */}
          <div ref={questionsRefEl}>
            {pinnedQuestions.length > 0 && (
              <>
                <div className="flex items-center gap-1.5 text-ui-md text-blue-400/70 font-semibold uppercase tracking-wide px-1">
                  <HelpCircle className="w-3 h-3" />
                  <span>Awaiting reply · Questions</span>
                  <span className="ml-auto bg-blue-500/20 text-blue-300 border border-blue-500/40 rounded-full px-1.5 py-0.5 text-ui-sm">{pinnedQuestions.length} waiting</span>
                </div>
                {insertTimeDividers(pinnedQuestions).map((item, idx) =>
                  item.kind === 'divider' ? (
                    <TimeDividerRow key={`divider-questions-${idx}`} label={item.label} />
                  ) : (
                    <MessageBubble key={item.msg.id} msg={item.msg} allMessages={messages} {...bubbleProps} />
                  )
                )}
                {otherHighSignal.length > 0 && <div className="border-t border-neutral-800/60" />}
              </>
            )}
          </div>

          {/* Divider: show before first new message if divider time is set and not dismissed */}
          {dividerTime && totalNewCount > 0 && !dividerDismissedRef.current && (
            <div data-divider-marker className="flex items-center gap-2 px-2 py-3 text-ui-md text-blue-300/70">
              <div className="flex-1 border-t border-blue-500/40" />
              <div className="flex items-center gap-2 whitespace-nowrap font-medium">
                <span className="flex items-center gap-1">
                  <ChevronUp className="w-3 h-3" />
                  NEW since {formatTimestamp(dividerTime)}
                </span>
                <button
                  onClick={onDismissDivider}
                  className="px-2 py-0.5 text-xs bg-blue-600/50 hover:bg-blue-600/70 rounded border border-blue-500/60 text-blue-200 transition-colors min-h-[44px] md:min-h-[28px] flex items-center"
                  title="Mark all caught up and dismiss this divider"
                >
                  All caught up
                </button>
              </div>
              <div className="flex-1 border-t border-blue-500/40" />
            </div>
          )}

          {/* Summary card: show counts of new messages if divider is active */}
          {dividerTime && !dividerDismissedRef.current && (newMessageCounts.blockers > 0 || newMessageCounts.questions > 0 || newMessageCounts.feedback > 0 || newMessageCounts.bugReports > 0 || newMessageCounts.routine > 0 || newMessageCounts.highSignalNotes > 0) && (
            <MessagesSummaryCard
              newMessageCounts={newMessageCounts}
              onScrollToBlockers={() => {
                blockersRefEl.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
              }}
              onScrollToQuestions={() => {
                questionsRefEl.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
              }}
              onScrollToRoutine={() => {
                routineRefEl.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
              }}
            />
          )}

          {/* Other high-signal open messages */}
          <div ref={blockersRefEl}>
            {insertTimeDividers(otherHighSignal).map((item, idx) =>
              item.kind === 'divider' ? (
                <TimeDividerRow key={`divider-high-${idx}`} label={item.label} />
              ) : (
                <MessageBubble key={item.msg.id} msg={item.msg} allMessages={messages} {...bubbleProps} />
              )
            )}
          </div>

          {/* Auto-promoted failing routine groups */}
          {promotedGroups.map(row => {
            const bd = outcomeBreakdown(row.msgs);
            const isGroupExpanded = expandedGroupKeys.has(row.key) || autoExpandedKeys.has(row.key);
            return (
              <div key={row.key} className="overflow-hidden rounded-md border border-red-800/50 bg-red-950/20">
                <div className="flex items-center gap-2 px-4 py-2.5 bg-red-950/30 min-h-[44px]">
                  <button
                    onClick={() => setExpandedGroupKeys(prev => { const next = new Set(prev); if (next.has(row.key)) next.delete(row.key); else next.add(row.key); return next; })}
                    className="flex-1 flex items-center gap-2 text-left"
                  >
                    <AlertTriangle className="w-3.5 h-3.5 text-red-400 shrink-0" />
                    <span className="text-xs text-red-300 font-medium">{row.msgs.length} {row.label}</span>
                    {bd && <span className="text-xs text-red-400">{bd}</span>}
                  </button>
                  <button onClick={() => handleResolveGroup(row.msgs)} title="Resolve all" className="text-emerald-500/70 hover:text-emerald-400 p-1 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center">
                    <Check className="w-3 h-3" />
                  </button>
                </div>
                {isGroupExpanded && (
                  <div className="space-y-2 p-2 border-t border-red-900/40">
                    {row.msgs.map(msg => <MessageBubble key={msg.id} msg={msg} readOnly allMessages={messages} {...bubbleProps} />)}
                  </div>
                )}
              </div>
            );
          })}

          {/* Routine ops group — collapsed by default */}
          {routineOpen.length > 0 && (
            <div ref={routineRefEl} className="overflow-hidden rounded-md border border-neutral-800 bg-[#10151c]">
              <div className="flex min-h-[44px] items-center bg-[#10151c] transition-colors hover:bg-neutral-900/70">
                <button
                  onClick={() => setRoutineExpanded(e => !e)}
                  className="flex-1 flex items-center gap-2 px-4 py-2.5 text-left"
                >
                  <ChevronDown className={`w-3.5 h-3.5 text-neutral-600 shrink-0 transition-transform ${routineExpanded ? 'rotate-180' : ''}`} />
                  <span className="text-xs text-neutral-500 font-medium shrink-0">
                    {visibleRoutineCount} routine ops
                    {mutedRoutineCount > 0 && <span className="text-neutral-600 ml-1">({mutedRoutineCount} muted)</span>}
                  </span>
                  <span className="text-ui-md text-neutral-600 truncate">{routineSummary(routineOpen)}</span>
                </button>
                <div className="relative shrink-0 px-2">
                  <button
                    onClick={() => setShowTempMuteMenu(v => !v)}
                    className="text-neutral-500 hover:text-neutral-300 p-1.5 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                    title="Temporarily mute event types"
                  >
                    <MoreVertical className="w-4 h-4" />
                  </button>
                  {showTempMuteMenu && (
                    <>
                      <div className="fixed inset-0 z-40" onClick={() => setShowTempMuteMenu(false)} />
                      <div className="absolute right-0 top-full z-50 mt-1 min-w-48 space-y-1 rounded-md border border-neutral-700 bg-[#10151c] p-2 shadow-xl">
                        {getTop3NoisyEventTypes().length > 0 ? (
                          <>
                            <p className="text-ui-sm text-neutral-500 px-2 py-1">Mute for</p>
                            {getTop3NoisyEventTypes().map(([eventType, count]) => {
                              const label = ROUTINE_EVENT_LABELS[eventType] ?? eventType.replace(/_/g, ' ');
                              // eslint-disable-next-line react-hooks/purity -- render-time freshness check, unchanged from the original inline MessagesPanel render
                              const isActive = tempMutes[eventType] && tempMutes[eventType] > Date.now();
                              return (
                                <div key={eventType} className="text-xs px-2 py-1 text-neutral-400">
                                  <div className="font-medium mb-1">{label} ({count})</div>
                                  <div className="flex gap-1 flex-wrap">
                                    {(
                                      [
                                        ['30m', 1800000, 'Mute 30m'] as const,
                                        ['1h', 3600000, 'Mute 1h'] as const,
                                        ['Tomorrow', 86400000, 'Mute until tomorrow'] as const,
                                      ]
                                    ).map(([shortLabel, ms, title]) => (
                                      <button
                                        key={shortLabel}
                                        onClick={() => addTemporaryMute(eventType, ms)}
                                        title={title}
                                        className={`text-ui-md px-2 py-1 rounded transition-colors min-h-[44px] md:min-h-auto flex items-center ${isActive ? 'bg-amber-500/20 text-amber-400' : 'bg-neutral-800 text-neutral-300 hover:bg-neutral-700'}`}
                                      >
                                        {shortLabel}
                                      </button>
                                    ))}
                                  </div>
                                </div>
                              );
                            })}
                          </>
                        ) : (
                          <p className="text-ui-sm text-neutral-500 px-2 py-2">No recent events to mute</p>
                        )}
                      </div>
                    </>
                  )}
                </div>
              </div>
              {muteActive && (
                <div className="px-4 py-2 bg-amber-950/30 border-t border-amber-800/30 text-ui-md text-amber-400">
                  🔕 Mute active, but failing groups still surface
                </div>
              )}
              {routineExpanded && (
                <div className="space-y-3 p-3 border-t border-neutral-800/40">
                  {(() => {
                    // Get first message date for each row (for single: the message, for group: first in group)
                    const rowsWithDate = remainingRoutineRows.map(row => {
                      const msg = row.kind === 'single' ? row.msg : row.msgs[0];
                      return { row, firstMsg: msg };
                    });

                    // Create divider list for all messages in order
                    const allMsgs = rowsWithDate.map(r => r.firstMsg);
                    const msgWithDividers = insertTimeDividers(allMsgs);

                    // Build a set of message IDs that have a divider before them
                    const msgsWithDividerBefore = new Set<string>();
                    msgWithDividers.forEach((item, idx) => {
                      if (item.kind === 'message' && idx > 0 && msgWithDividers[idx - 1]?.kind === 'divider') {
                        msgsWithDividerBefore.add(item.msg.id);
                      }
                    });

                    // Also build a map of message ID to divider label
                    const dividerLabels = new Map<string, string>();
                    msgWithDividers.forEach((item, idx) => {
                      if (item.kind === 'divider' && idx + 1 < msgWithDividers.length && msgWithDividers[idx + 1]?.kind === 'message') {
                        dividerLabels.set((msgWithDividers[idx + 1] as MessageWithDivider<ProjectMessage> & { kind: 'message' }).msg.id, item.label);
                      }
                    });

                    return rowsWithDate.map(({ row, firstMsg }) => {
                      const hasDividerBefore = msgsWithDividerBefore.has(firstMsg.id);
                      const dividerLabel = dividerLabels.get(firstMsg.id);

                      if (row.kind === 'group') {
                        const isGroupExpanded = expandedGroupKeys.has(row.key) || autoExpandedKeys.has(row.key);
                        const bd = outcomeBreakdown(row.msgs);
                        return (
                          <div key={row.key}>
                            {hasDividerBefore && dividerLabel && (
                              <TimeDividerRow label={dividerLabel} className="mb-2" />
                            )}
                            <div className="overflow-hidden rounded-md border border-neutral-800/60 bg-[#10151c]">
                              <div className="flex items-center gap-2 bg-neutral-900/30 px-3 py-2">
                                <button
                                  onClick={() => setExpandedGroupKeys(prev => {
                                    const next = new Set(prev);
                                    if (next.has(row.key)) next.delete(row.key); else next.add(row.key);
                                    return next;
                                  })}
                                  className="flex-1 flex items-center gap-2 text-left"
                                >
                                  <ChevronDown className={`w-3 h-3 text-neutral-600 shrink-0 transition-transform ${isGroupExpanded ? 'rotate-180' : ''}`} />
                                  <span className="text-xs text-neutral-500 font-medium">{row.msgs.length} {row.label}{bd ? ` ${bd}` : ''}</span>
                                </button>
                                <button
                                  onClick={() => handleResolveGroup(row.msgs)}
                                  title="Resolve all"
                                  className="text-emerald-500/70 hover:text-emerald-400 p-1 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                                >
                                  <Check className="w-3 h-3" />
                                </button>
                              </div>
                              {isGroupExpanded && (
                                <div className="space-y-2 p-2 border-t border-neutral-800/40">
                                  {row.msgs.map(msg => <MessageBubble key={msg.id} msg={msg} readOnly allMessages={messages} {...bubbleProps} />)}
                                </div>
                              )}
                            </div>
                          </div>
                        );
                      }

                      if (row.kind === 'daily_digest') {
                        const digestKey = `digest-${row.day}`;
                        const isExpanded = expandedGroupKeys.has(digestKey);
                        const summary = formatDailyDigestSummary(row.breakdown);
                        return (
                          <div key={digestKey}>
                            {hasDividerBefore && dividerLabel && (
                              <TimeDividerRow label={dividerLabel} className="mb-2" />
                            )}
                            <div className="overflow-hidden rounded-md border border-neutral-800/60 bg-[#10151c] opacity-60">
                              <button
                                onClick={() => setExpandedGroupKeys(prev => { const next = new Set(prev); if (next.has(digestKey)) next.delete(digestKey); else next.add(digestKey); return next; })}
                                className="w-full flex items-center gap-2 px-3 py-2 bg-neutral-900/30 text-left"
                              >
                                <ChevronDown className={`w-3 h-3 text-neutral-600 shrink-0 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />
                                <span className="text-xs text-neutral-500 font-medium">{row.count} stale routine{summary ? ` · ${summary}` : ''}</span>
                              </button>
                              {isExpanded && (
                                <div className="space-y-2 p-2 border-t border-neutral-800/40">
                                  {row.msgs.map(msg => <MessageBubble key={msg.id} msg={msg} readOnly allMessages={messages} {...bubbleProps} />)}
                                </div>
                              )}
                            </div>
                          </div>
                        );
                      }

                      // Single message
                      return (
                        <div key={`single-${row.msg.id}`}>
                          {hasDividerBefore && dividerLabel && (
                            <TimeDividerRow label={dividerLabel} className="mb-2" />
                          )}
                          <MessageBubble key={row.msg.id} msg={row.msg} allMessages={messages} {...bubbleProps} />
                        </div>
                      );
                    });
                  })()}
                </div>
              )}
            </div>
          )}

          {/* Linked messages (being addressed) */}
          {linkedMessages.length > 0 && (
            <>
              <div className="flex items-center gap-1.5 text-ui-md text-indigo-400/70 font-semibold uppercase tracking-wide px-1">
                <span>Being addressed · Linked</span>
              </div>
              {insertTimeDividers(linkedMessages).map((item, idx) =>
                item.kind === 'divider' ? (
                  <TimeDividerRow key={`divider-linked-${idx}`} label={item.label} />
                ) : (
                  <MessageBubble key={item.msg.id} msg={item.msg} readOnly allMessages={messages} {...bubbleProps} />
                )
              )}
              {visibleResolved.length > 0 && <div className="border-t border-neutral-800/60" />}
            </>
          )}

          {/* Resolved messages (faded) */}
          {insertTimeDividers(visibleResolved).map((item, idx) =>
            item.kind === 'divider' ? (
              <TimeDividerRow key={`divider-resolved-${idx}`} label={item.label} />
            ) : (
              <MessageBubble key={item.msg.id} msg={item.msg} faded allMessages={messages} {...bubbleProps} />
            )
          )}

          {/* Resolved toggle + clear */}
          {hiddenResolvedCount > 0 && (
            <div className="flex justify-center gap-3 pt-1">
              <button
                onClick={() => setShowResolved(true)}
                className="text-ui-md text-neutral-600 hover:text-neutral-400 transition-colors px-2 py-1 min-h-[44px] md:min-h-auto flex items-center"
              >
                Show {hiddenResolvedCount} older resolved
              </button>
            </div>
          )}
          {showResolved && resolvedMessages.length > 0 && (
            <div className="flex justify-center gap-3 pt-1">
              <button
                onClick={() => setShowResolved(false)}
                className="text-ui-md text-neutral-600 hover:text-neutral-400 transition-colors px-2 py-1 min-h-[44px] md:min-h-auto flex items-center"
              >
                Hide older resolved
              </button>
              <button
                onClick={handleBulkDeleteResolved}
                className="text-ui-md text-neutral-600 hover:text-red-400 transition-colors px-2 py-1 min-h-[44px] md:min-h-auto flex items-center"
              >
                Clear all resolved
              </button>
            </div>
          )}
        </>
      )}

      {/* Scroll to bottom button */}
      {showScrollDown && (
        <button
          onClick={scrollToBottom}
          className="sticky bottom-2 left-1/2 -translate-x-1/2 bg-neutral-800 border border-neutral-700 text-neutral-400 hover:text-neutral-200 p-2 rounded-full shadow-lg transition-colors z-10 min-h-[44px] min-w-[44px] flex items-center justify-center"
        >
          <ArrowDown className="w-4 h-4" />
        </button>
      )}
    </div>
  );
}
