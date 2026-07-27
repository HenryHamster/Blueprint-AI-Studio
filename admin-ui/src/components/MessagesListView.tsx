/* eslint-disable react-hooks/refs */
import { type Dispatch, type SetStateAction, type RefObject, type ReactNode, useMemo, useEffect, useRef, useCallback, useImperativeHandle } from 'react';
import { MessageSquare, AlertTriangle, HelpCircle, ChevronDown, ArrowDown, ChevronUp, MoreVertical } from 'lucide-react';
import { List, useDynamicRowHeight, useListRef, type RowComponentProps, type ListImperativeAPI } from 'react-window';
import { ROUTINE_EVENT_LABELS } from './eventPrefs';
import { type RoutineRow, routineSummary, formatDailyDigestSummary, outcomeBreakdown, insertTimeDividers, formatTimestamp, formatSinceTimestamp } from './messageGrouping';
import { MessageBubble, type MessageBubbleProps } from './MessageBubble';
import type { ProjectMessage } from '../types/messages';
import { MessagesSummaryCard } from './MessagesSummaryCard';
import { type MessageSummaryCounts } from './useMessageList';
import { type TemporaryMutes } from './temporaryMutes';
import { preserveScrollAnchorAfterPrepend } from '../utils/conversationScrollAnchor';

type BubbleProps = Omit<MessageBubbleProps, 'msg' | 'faded' | 'readOnly' | 'replyTarget'>;

function replyTargetFor(message: ProjectMessage, messagesById: ReadonlyMap<string, ProjectMessage>): ProjectMessage | null {
  const targetId = message.reply_to_message_id
    ?? (typeof message.context.reply_to === 'string' ? message.context.reply_to : null);
  return targetId ? messagesById.get(targetId) ?? null : null;
}

function TimeDividerRow({ label, className }: { label: string; className?: string }) {
  return (
    <div className={`flex items-center gap-1.5 text-ui-md text-neutral-600 font-semibold uppercase tracking-wide px-1 mt-2${className ? ` ${className}` : ''}`}>
      <div className="flex-1 border-t border-neutral-800/60" />
      <span className="shrink-0">{label}</span>
      <div className="flex-1 border-t border-neutral-800/60" />
    </div>
  );
}

// ─── Virtual-list row item types ─────────────────────────────────────────────

export type TriageItem =
  | { kind: 'banner_new' }
  | { kind: 'banner_caught_up' }
  | { kind: 'loading' }
  | { kind: 'empty'; emptyKind: 'no_messages' | 'no_new' | 'no_match' }
  | { kind: 'time_divider'; label: string }
  | { kind: 'section_sep' }
  | { kind: 'section_head_blockers' }
  | { kind: 'section_head_questions'; count: number }
  | { kind: 'section_head_linked' }
  | { kind: 'new_divider' }
  | { kind: 'summary_card' }
  | { kind: 'message'; msg: ProjectMessage; faded?: boolean; readOnly?: boolean }
  | { kind: 'promo_group'; groupKey: string; label: string; msgs: ProjectMessage[]; isExpanded: boolean }
  | { kind: 'routine_head' }
  | { kind: 'mute_active_banner' }
  | { kind: 'routine_group'; row: RoutineRow<ProjectMessage> & { kind: 'group' }; isExpanded: boolean }
  | { kind: 'routine_digest'; digestKey: string; row: RoutineRow<ProjectMessage> & { kind: 'daily_digest' }; isExpanded: boolean }
  | { kind: 'resolved_toggle'; hiddenCount: number }
  | { kind: 'resolved_controls' };

export interface SectionIndices {
  blockers: number;
  questions: number;
  routine: number;
  newDivider: number;
}

interface TriageRowData {
  items: TriageItem[];
  messagesById: ReadonlyMap<string, ProjectMessage>;
  dividerTime: number | null;
  totalNewCount: number;
  showTempMuteMenu: boolean;
  routineExpanded: boolean;
  tempMutes: TemporaryMutes;
  topNoisyEventTypes: Array<[string, number]>;
  searchQuery: string;
  newMessageCounts: MessageSummaryCounts;
  listRef: RefObject<ListImperativeAPI | null>;
  sectionIndices: SectionIndices;
  bubbleProps: BubbleProps;
  highlightedMessageId?: string | null;
  visibleRoutineCount: number;
  mutedRoutineCount: number;
  routineOpen: ProjectMessage[];
  onDismissDivider: () => void;
  onShowOnlyNew: () => void;
  setExpandedGroupKeys: Dispatch<SetStateAction<Set<string>>>;
  setRoutineExpanded: Dispatch<SetStateAction<boolean>>;
  setShowResolved: Dispatch<SetStateAction<boolean>>;
  setShowTempMuteMenu: Dispatch<SetStateAction<boolean>>;
  addTemporaryMute: (eventType: string, durationMs: number) => void;
  handleBulkDeleteResolved: () => void;
}

function TriageRow({
  index,
  style,
  ariaAttributes,
  items,
  messagesById,
  dividerTime,
  totalNewCount,
  showTempMuteMenu,
  routineExpanded,
  tempMutes,
  topNoisyEventTypes,
  searchQuery,
  newMessageCounts,
  listRef,
  sectionIndices,
  bubbleProps,
  highlightedMessageId,
  visibleRoutineCount,
  mutedRoutineCount,
  routineOpen,
  onDismissDivider,
  onShowOnlyNew,
  setExpandedGroupKeys,
  setRoutineExpanded,
  setShowResolved,
  setShowTempMuteMenu,
  addTemporaryMute,
  handleBulkDeleteResolved,
}: RowComponentProps<TriageRowData>) {
  const item = items[index];
  if (!item) return null;

  if (item.kind === 'banner_new') {
    if (!dividerTime) return null;
    return (
      <div style={style} {...ariaAttributes}>
        <div className="mb-2.5 mx-3 md:mx-4 flex flex-col gap-3 rounded-md border border-blue-500/40 bg-workbench-layer p-3 sm:flex-row sm:items-center sm:justify-between">
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
      </div>
    );
  }

  if (item.kind === 'banner_caught_up') {
    if (!dividerTime) return null;
    return (
      <div style={style} {...ariaAttributes}>
        <div className="mb-2.5 mx-3 md:mx-4 flex flex-col gap-3 rounded-md border border-emerald-500/40 bg-workbench-layer p-3 sm:flex-row sm:items-center sm:justify-between">
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
      </div>
    );
  }

  if (item.kind === 'loading') {
    return (
      <div style={style} {...ariaAttributes}>
        <p className="text-xs text-neutral-500 text-center py-8">Loading...</p>
      </div>
    );
  }

  if (item.kind === 'empty') {
    if (item.emptyKind === 'no_messages') {
      return (
        <div style={style} {...ariaAttributes}>
          <div className="flex flex-col items-center justify-center py-12 text-neutral-500">
            <MessageSquare className="w-8 h-8 mb-3 opacity-40" />
            <p className="text-sm">No messages yet</p>
            <p className="text-xs mt-1 opacity-70">Send feedback, report bugs, or ask questions</p>
          </div>
        </div>
      );
    }
    if (item.emptyKind === 'no_new') {
      if (!dividerTime) return null;
      return (
        <div style={style} {...ariaAttributes}>
          <div className="flex flex-col items-center justify-center py-12 text-neutral-500">
            <MessageSquare className="w-8 h-8 mb-3 opacity-40" />
            <p className="text-sm">No new messages since {formatTimestamp(dividerTime)}</p>
            <p className="text-xs mt-1 opacity-70">You&apos;re all caught up!</p>
          </div>
        </div>
      );
    }
    // no_match
    return (
      <div style={style} {...ariaAttributes}>
        <div className="flex flex-col items-center justify-center py-12 text-neutral-500">
          <MessageSquare className="w-8 h-8 mb-3 opacity-40" />
          <p className="text-sm">No matches for &quot;{searchQuery}&quot;</p>
          <p className="text-xs mt-1 opacity-70">Try a different search term</p>
        </div>
      </div>
    );
  }

  if (item.kind === 'time_divider') {
    return (
      <div style={style} {...ariaAttributes}>
        <div className="px-3 md:px-4">
          <TimeDividerRow label={item.label} />
        </div>
      </div>
    );
  }

  if (item.kind === 'section_sep') {
    return (
      <div style={style} {...ariaAttributes}>
        <div className="mx-3 md:mx-4 border-t border-neutral-800/60" />
      </div>
    );
  }

  if (item.kind === 'section_head_blockers') {
    return (
      <div style={style} {...ariaAttributes}>
        <div className="flex items-center gap-1.5 text-ui-md text-amber-400/70 font-semibold uppercase tracking-wide px-4 md:px-5 py-1">
          <AlertTriangle className="w-3 h-3" />
          <span>Pinned · Blockers</span>
        </div>
      </div>
    );
  }

  if (item.kind === 'section_head_questions') {
    return (
      <div style={style} {...ariaAttributes}>
        <div className="flex items-center gap-1.5 text-ui-md text-blue-400/70 font-semibold uppercase tracking-wide px-4 md:px-5 py-1">
          <HelpCircle className="w-3 h-3" />
          <span>Awaiting reply · Questions</span>
          <span className="ml-auto bg-blue-500/20 text-blue-300 border border-blue-500/40 rounded-full px-1.5 py-0.5 text-ui-sm">{item.count} waiting</span>
        </div>
      </div>
    );
  }

  if (item.kind === 'section_head_linked') {
    return (
      <div style={style} {...ariaAttributes}>
        <div className="flex items-center gap-1.5 text-ui-md text-indigo-400/70 font-semibold uppercase tracking-wide px-4 md:px-5 py-1">
          <span>Being addressed · Linked</span>
        </div>
      </div>
    );
  }

  if (item.kind === 'new_divider') {
    if (!dividerTime) return null;
    return (
      <div style={style} {...ariaAttributes} data-divider-marker>
        <div className="flex items-center gap-2 px-2 py-3 text-ui-md text-blue-300/70">
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
      </div>
    );
  }

  if (item.kind === 'summary_card') {
    return (
      <div style={style} {...ariaAttributes}>
        <div className="px-3 md:px-4 pb-2.5">
          <MessagesSummaryCard
            newMessageCounts={newMessageCounts}
            onScrollToBlockers={() => {
              if (sectionIndices.blockers >= 0) {
                listRef.current?.scrollToRow({ index: sectionIndices.blockers, align: 'start', behavior: 'smooth' });
              }
            }}
            onScrollToQuestions={() => {
              if (sectionIndices.questions >= 0) {
                listRef.current?.scrollToRow({ index: sectionIndices.questions, align: 'start', behavior: 'smooth' });
              }
            }}
            onScrollToRoutine={() => {
              if (sectionIndices.routine >= 0) {
                listRef.current?.scrollToRow({ index: sectionIndices.routine, align: 'start', behavior: 'smooth' });
              }
            }}
          />
        </div>
      </div>
    );
  }

  if (item.kind === 'message') {
    return (
      <div style={style} {...ariaAttributes}>
        <div className="px-3 md:px-4 pb-2.5">
          <MessageBubble
            msg={item.msg}
            faded={item.faded}
            readOnly={item.readOnly}
            replyTarget={replyTargetFor(item.msg, messagesById)}
            highlighted={item.msg.id === highlightedMessageId}
            {...bubbleProps}
          />
        </div>
      </div>
    );
  }

  if (item.kind === 'promo_group') {
    const { groupKey, label, msgs, isExpanded } = item;
    const bd = outcomeBreakdown(msgs);
    return (
      <div style={style} {...ariaAttributes}>
        <div className="mx-3 md:mx-4 pb-2.5 overflow-hidden rounded-md border border-red-800/50 bg-red-950/20">
          <div className="flex items-center gap-2 px-4 py-2.5 bg-red-950/30 min-h-[44px]">
            <button
              onClick={() => setExpandedGroupKeys(prev => {
                const next = new Set(prev);
                if (next.has(groupKey)) next.delete(groupKey); else next.add(groupKey);
                return next;
              })}
              className="flex-1 flex items-center gap-2 text-left"
            >
              <AlertTriangle className="w-3.5 h-3.5 text-red-400 shrink-0" />
              <span className="text-xs text-red-300 font-medium">
                {msgs.length} {label}
              </span>
              {bd && <span className="text-xs text-red-400">{bd}</span>}
            </button>
          </div>
          {isExpanded && (
            <div className="space-y-2 p-2 border-t border-red-900/40">
              {msgs.map(msg => <MessageBubble key={msg.id} msg={msg} readOnly replyTarget={replyTargetFor(msg, messagesById)} {...bubbleProps} />)}
            </div>
          )}
        </div>
      </div>
    );
  }

  if (item.kind === 'routine_head') {
    return (
      <div style={style} {...ariaAttributes}>
        <div className="mx-3 md:mx-4 overflow-hidden rounded-md border border-neutral-800 bg-workbench-layer">
          <div className="flex min-h-[44px] items-center bg-workbench-layer transition-colors hover:bg-neutral-900/70">
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
                  <div className="absolute right-0 top-full z-50 mt-1 min-w-48 space-y-1 rounded-md border border-neutral-700 bg-workbench-layer p-2 shadow-xl">
                    {topNoisyEventTypes.length > 0 ? (
                      <>
                        <p className="text-ui-sm text-neutral-500 px-2 py-1">Mute for</p>
                        {topNoisyEventTypes.map(([eventType, count]) => {
                          const label = ROUTINE_EVENT_LABELS[eventType] ?? eventType.replace(/_/g, ' ');
                          // eslint-disable-next-line react-hooks/purity -- render-time freshness check, unchanged from original
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
        </div>
      </div>
    );
  }

  if (item.kind === 'mute_active_banner') {
    return (
      <div style={style} {...ariaAttributes}>
        <div className="mx-3 md:mx-4 px-4 py-2 bg-amber-950/30 border border-amber-800/30 rounded text-ui-md text-amber-400">
          🔕 Mute active, but failing groups still surface
        </div>
      </div>
    );
  }

  if (item.kind === 'routine_group') {
    const { row, isExpanded } = item;
    const bd = outcomeBreakdown(row.msgs);
    return (
      <div style={style} {...ariaAttributes}>
        <div className="mx-3 md:mx-4 pb-2 overflow-hidden rounded-md border border-neutral-800/60 bg-workbench-layer">
          <div className="flex items-center gap-2 bg-neutral-900/30 px-3 py-2">
            <button
              onClick={() => setExpandedGroupKeys(prev => {
                const next = new Set(prev);
                if (next.has(row.key)) next.delete(row.key); else next.add(row.key);
                return next;
              })}
              className="flex-1 flex items-center gap-2 text-left"
            >
              <ChevronDown className={`w-3 h-3 text-neutral-600 shrink-0 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />
              <span className="text-xs text-neutral-500 font-medium">{row.msgs.length} {row.label}{bd ? ` ${bd}` : ''}</span>
            </button>
          </div>
          {isExpanded && (
            <div className="space-y-2 p-2 border-t border-neutral-800/40">
              {row.msgs.map(msg => <MessageBubble key={msg.id} msg={msg} readOnly replyTarget={replyTargetFor(msg, messagesById)} {...bubbleProps} />)}
            </div>
          )}
        </div>
      </div>
    );
  }

  if (item.kind === 'routine_digest') {
    const { digestKey, row, isExpanded } = item;
    const summary = formatDailyDigestSummary(row.breakdown);
    return (
      <div style={style} {...ariaAttributes}>
        <div className="mx-3 md:mx-4 pb-2 overflow-hidden rounded-md border border-neutral-800/60 bg-workbench-layer opacity-60">
          <button
            onClick={() => setExpandedGroupKeys(prev => {
              const next = new Set(prev);
              if (next.has(digestKey)) next.delete(digestKey); else next.add(digestKey);
              return next;
            })}
            className="w-full flex items-center gap-2 px-3 py-2 bg-neutral-900/30 text-left"
          >
            <ChevronDown className={`w-3 h-3 text-neutral-600 shrink-0 transition-transform ${isExpanded ? 'rotate-180' : ''}`} />
            <span className="text-xs text-neutral-500 font-medium">{row.count} stale routine{summary ? ` · ${summary}` : ''}</span>
          </button>
          {isExpanded && (
            <div className="space-y-2 p-2 border-t border-neutral-800/40">
              {row.msgs.map(msg => <MessageBubble key={msg.id} msg={msg} readOnly replyTarget={replyTargetFor(msg, messagesById)} {...bubbleProps} />)}
            </div>
          )}
        </div>
      </div>
    );
  }

  if (item.kind === 'resolved_toggle') {
    return (
      <div style={style} {...ariaAttributes}>
        <div className="flex justify-center gap-3 pt-1 pb-2">
          <button
            onClick={() => setShowResolved(true)}
            className="text-ui-md text-neutral-600 hover:text-neutral-400 transition-colors px-2 py-1 min-h-[44px] md:min-h-auto flex items-center"
          >
            Show {item.hiddenCount} older resolved
          </button>
        </div>
      </div>
    );
  }

  if (item.kind === 'resolved_controls') {
    return (
      <div style={style} {...ariaAttributes}>
        <div className="flex justify-center gap-3 pt-1 pb-2">
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
      </div>
    );
  }

  return null;
}

// ─── Item list builder ────────────────────────────────────────────────────────

export interface BuildParams {
  loading: boolean;
  messages: ProjectMessage[];
  dividerTime: number | null;
  totalNewCount: number;
  dividerDismissedCurrent: boolean;
  showOnlyNew: boolean;
  showResolved: boolean;
  routineExpanded: boolean;
  muteActive: boolean;
  searchQuery: string;
  pinnedBlockers: ProjectMessage[];
  pinnedQuestions: ProjectMessage[];
  otherHighSignal: ProjectMessage[];
  promotedGroups: (RoutineRow<ProjectMessage> & { kind: 'group' })[];
  remainingRoutineRows: RoutineRow<ProjectMessage>[];
  autoExpandedKeys: Set<string>;
  expandedGroupKeys: Set<string>;
  routineOpen: ProjectMessage[];
  linkedMessages: ProjectMessage[];
  visibleResolved: ProjectMessage[];
  hiddenResolvedCount: number;
  resolvedMessages: ProjectMessage[];
  newMessageCounts: MessageSummaryCounts;
}

// eslint-disable-next-line react-refresh/only-export-components -- exported for unit testing; not a component
export function buildTriageItems(p: BuildParams): { items: TriageItem[]; sectionIndices: SectionIndices } {
  const items: TriageItem[] = [];
  const sec: SectionIndices = { blockers: -1, questions: -1, routine: -1, newDivider: -1 };

  // Banners
  if (p.dividerTime && p.totalNewCount > 0 && !p.dividerDismissedCurrent) {
    items.push({ kind: 'banner_new' });
  }
  if (p.dividerTime && !p.dividerDismissedCurrent && p.totalNewCount === 0 && !p.showOnlyNew) {
    items.push({ kind: 'banner_caught_up' });
  }

  if (p.loading) {
    items.push({ kind: 'loading' });
    return { items, sectionIndices: sec };
  }

  if (p.messages.length === 0) {
    items.push({ kind: 'empty', emptyKind: 'no_messages' });
    return { items, sectionIndices: sec };
  }

  const allEmpty =
    p.pinnedBlockers.length === 0 && p.pinnedQuestions.length === 0 &&
    p.otherHighSignal.length === 0 && p.promotedGroups.length === 0 &&
    p.routineOpen.length === 0 && p.linkedMessages.length === 0 &&
    p.visibleResolved.length === 0;

  if (p.showOnlyNew && p.dividerTime && allEmpty) {
    items.push({ kind: 'empty', emptyKind: 'no_new' });
    return { items, sectionIndices: sec };
  }

  if (p.searchQuery.trim() && allEmpty) {
    items.push({ kind: 'empty', emptyKind: 'no_match' });
    return { items, sectionIndices: sec };
  }

  // Pinned blockers
  if (p.pinnedBlockers.length > 0) {
    sec.blockers = items.length;
    items.push({ kind: 'section_head_blockers' });
    for (const it of insertTimeDividers(p.pinnedBlockers)) {
      if (it.kind === 'divider') items.push({ kind: 'time_divider', label: it.label });
      else items.push({ kind: 'message', msg: it.msg });
    }
    if (p.pinnedQuestions.length > 0 || p.otherHighSignal.length > 0) {
      items.push({ kind: 'section_sep' });
    }
  }

  // Pinned questions
  if (p.pinnedQuestions.length > 0) {
    sec.questions = items.length;
    items.push({ kind: 'section_head_questions', count: p.pinnedQuestions.length });
    for (const it of insertTimeDividers(p.pinnedQuestions)) {
      if (it.kind === 'divider') items.push({ kind: 'time_divider', label: it.label });
      else items.push({ kind: 'message', msg: it.msg });
    }
    if (p.otherHighSignal.length > 0) items.push({ kind: 'section_sep' });
  }

  // NEW divider
  if (p.dividerTime && p.totalNewCount > 0 && !p.dividerDismissedCurrent) {
    sec.newDivider = items.length;
    items.push({ kind: 'new_divider' });
  }

  // Summary card
  const { blockers: nb, questions: nq, feedback: nf, bugReports: nbr, routine: nr, highSignalNotes: nhs } = p.newMessageCounts;
  if (p.dividerTime && !p.dividerDismissedCurrent && (nb > 0 || nq > 0 || nf > 0 || nbr > 0 || nr > 0 || nhs > 0)) {
    items.push({ kind: 'summary_card' });
  }

  // Other high-signal
  for (const it of insertTimeDividers(p.otherHighSignal)) {
    if (it.kind === 'divider') items.push({ kind: 'time_divider', label: it.label });
    else items.push({ kind: 'message', msg: it.msg });
  }

  // Auto-promoted failing groups
  for (const row of p.promotedGroups) {
    const isExpanded = p.expandedGroupKeys.has(row.key) || p.autoExpandedKeys.has(row.key);
    items.push({ kind: 'promo_group', groupKey: row.key, label: row.label, msgs: row.msgs, isExpanded });
  }

  // Routine section
  if (p.routineOpen.length > 0) {
    sec.routine = items.length;
    items.push({ kind: 'routine_head' });
    if (p.muteActive) items.push({ kind: 'mute_active_banner' });
    if (p.routineExpanded) {
      // Interleave time dividers with routine rows
      const firstMsgs = p.remainingRoutineRows.map(row =>
        row.kind === 'single' ? row.msg : row.msgs[0]
      );
      const withDividers = insertTimeDividers(firstMsgs);
      const dividerBeforeId = new Map<string, string>();
      for (let i = 0; i < withDividers.length; i++) {
        const cur = withDividers[i];
        const next = withDividers[i + 1];
        if (cur?.kind === 'divider' && next?.kind === 'message') {
          dividerBeforeId.set(next.msg.id, cur.label);
        }
      }

      for (const row of p.remainingRoutineRows) {
        const firstMsg = row.kind === 'single' ? row.msg : row.msgs[0];
        const divLabel = dividerBeforeId.get(firstMsg.id);
        if (divLabel) items.push({ kind: 'time_divider', label: divLabel });

        if (row.kind === 'group') {
          const isExpanded = p.expandedGroupKeys.has(row.key) || p.autoExpandedKeys.has(row.key);
          items.push({ kind: 'routine_group', row, isExpanded });
        } else if (row.kind === 'daily_digest') {
          const digestKey = `digest-${row.day}`;
          const isExpanded = p.expandedGroupKeys.has(digestKey);
          items.push({ kind: 'routine_digest', digestKey, row, isExpanded });
        } else {
          items.push({ kind: 'message', msg: row.msg });
        }
      }
    }
  }

  // Linked messages
  if (p.linkedMessages.length > 0) {
    items.push({ kind: 'section_head_linked' });
    for (const it of insertTimeDividers(p.linkedMessages)) {
      if (it.kind === 'divider') items.push({ kind: 'time_divider', label: it.label });
      else items.push({ kind: 'message', msg: it.msg, readOnly: true });
    }
    if (p.visibleResolved.length > 0) items.push({ kind: 'section_sep' });
  }

  // Resolved messages
  for (const it of insertTimeDividers(p.visibleResolved)) {
    if (it.kind === 'divider') items.push({ kind: 'time_divider', label: it.label });
    else items.push({ kind: 'message', msg: it.msg, faded: true });
  }

  // Resolved toggle / controls
  if (p.hiddenResolvedCount > 0) {
    items.push({ kind: 'resolved_toggle', hiddenCount: p.hiddenResolvedCount });
  }
  if (p.showResolved && p.resolvedMessages.length > 0) {
    items.push({ kind: 'resolved_controls' });
  }

  return { items, sectionIndices: sec };
}

// ─── Public interface ─────────────────────────────────────────────────────────

export interface MessagesListViewProps {
  presentation?: 'triage' | 'conversation';
  /**
   * Live strategic-session streaming preview, rendered at the tail of the
   * conversation transcript (conversation presentation only). Reconciled away by
   * the caller once the durable strategic ProjectMessage lands, so it never
   * duplicates a persisted message.
   */
  streamingBubble?: ReactNode;
  /** Calm operation status rendered at the conversation tail; details stay in Activity. */
  operationItem?: ReactNode;
  hasOlderConversationMessages?: boolean;
  loadingOlderConversationMessages?: boolean;
  onLoadOlderConversationMessages?: () => void | Promise<void>;
  /** Triage mode: whether there are older messages beyond the initial page. */
  hasOlderTriageMessages?: boolean;
  loadingOlderTriageMessages?: boolean;
  onLoadOlderTriageMessages?: () => void;
  conversationListRef?: RefObject<ConversationListImperativeApi | null>;
  // Refs
  scrollRef: RefObject<HTMLDivElement | null>;
  dividerDismissedRef: RefObject<boolean>;

  // Raw + derived list state
  messages: ProjectMessage[];
  loading: boolean;
  dividerTime: number | null;
  totalNewCount: number;
  newMessageCounts: MessageSummaryCounts;
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
  handleBulkDeleteResolved: () => void;
  scrollToBottom: () => void;
}

/**
 * Presentational scroll area for MessagesPanel.
 *
 * The triage presentation uses react-window to virtualize the flat item list,
 * keeping initial render fast even with 100-200 markdown bubbles.
 */
export function MessagesListView(props: MessagesListViewProps) {
  const {
    presentation = 'triage',
    streamingBubble,
    operationItem,
    hasOlderConversationMessages,
    loadingOlderConversationMessages,
    onLoadOlderConversationMessages,
    conversationListRef,
    ...triageProps
  } = props;

  if (presentation === 'conversation') {
    return (
      <ConversationVirtualList
        {...triageProps}
        streamingBubble={streamingBubble}
        operationItem={operationItem}
        hasOlderConversationMessages={hasOlderConversationMessages}
        loadingOlderConversationMessages={loadingOlderConversationMessages}
        onLoadOlderConversationMessages={onLoadOlderConversationMessages}
        conversationListRef={conversationListRef}
      />
    );
  }

  return <TriageVirtualList {...triageProps} />;
}

export interface ConversationListImperativeApi {
  scrollToMessage: (messageId: string) => Promise<boolean>;
  scrollToBottom: () => void;
}

type ConversationRow =
  | { kind: 'load_older' }
  | { kind: 'loading' }
  | { kind: 'empty' }
  | { kind: 'divider'; label: string }
  | { kind: 'message'; message: ProjectMessage }
  | { kind: 'node'; key: 'streaming' | 'operation'; node: ReactNode };

interface ConversationRowData {
  rows: ConversationRow[];
  messagesById: ReadonlyMap<string, ProjectMessage>;
  bubbleProps: BubbleProps;
  highlightedMessageId?: string | null;
  loadingOlderConversationMessages?: boolean;
  onLoadOlder: () => void;
}

function ConversationRowView({
  index,
  style,
  ariaAttributes,
  rows,
  messagesById,
  bubbleProps,
  highlightedMessageId,
  loadingOlderConversationMessages,
  onLoadOlder,
}: RowComponentProps<ConversationRowData>) {
  const row = rows[index];
  return (
    <div style={style} {...ariaAttributes} className="px-2 md:px-2.5">
      <div className="mx-auto w-full max-w-[940px] py-1.5">
        {row.kind === 'load_older' && (
          <div className="flex justify-center">
            <button
              type="button"
              onClick={onLoadOlder}
              disabled={loadingOlderConversationMessages}
              className="min-h-11 rounded-md border border-neutral-800 bg-neutral-950/60 px-3 py-2 text-xs text-neutral-400 transition-colors hover:border-neutral-700 hover:text-neutral-200 disabled:opacity-50"
            >
              {loadingOlderConversationMessages ? 'Loading older messages…' : 'Load older messages'}
            </button>
          </div>
        )}
        {row.kind === 'loading' && <p className="py-8 text-center text-xs text-neutral-500">Loading...</p>}
        {row.kind === 'empty' && (
          <div className="flex flex-col items-center justify-center py-12 text-neutral-500">
            <MessageSquare className="mb-3 h-8 w-8 opacity-40" />
            <p className="text-sm">No messages yet</p>
          </div>
        )}
        {row.kind === 'divider' && <TimeDividerRow label={row.label} className="my-1" />}
        {row.kind === 'message' && (
          <MessageBubble
            msg={row.message}
            faded={row.message.status === 'resolved'}
            replyTarget={replyTargetFor(row.message, messagesById)}
            presentation="chat"
            highlighted={row.message.id === highlightedMessageId}
            {...bubbleProps}
          />
        )}
        {row.kind === 'node' && row.node}
      </div>
    </div>
  );
}

function ConversationVirtualList({
  scrollRef,
  loading,
  messages,
  bubbleProps,
  highlightedMessageId,
  showScrollDown,
  scrollToBottom,
  streamingBubble,
  operationItem,
  hasOlderConversationMessages,
  loadingOlderConversationMessages,
  onLoadOlderConversationMessages,
  conversationListRef,
}: MessagesListViewProps) {
  const listRef = useListRef(null);
  const rowHeight = useDynamicRowHeight({ defaultRowHeight: 96 });
  const renderedRangeRef = useRef({ startIndex: -1, stopIndex: -1 });
  const olderHistoryIntentRef = useRef(false);
  const initialBottomAnchorRef = useRef<number | null>(null);
  const initialBottomAnchorFrameRef = useRef<number | null>(null);
  const initialBottomAnchoredRef = useRef(false);
  const autoLoadingOlderRef = useRef(false);
  const pendingNavigationRef = useRef<{
    index: number;
    attempts: number;
    timeout: number;
    resolve: (rendered: boolean) => void;
  } | null>(null);
  const messagesById = useMemo(
    () => new Map(messages.map(message => [message.id, message])),
    [messages],
  );
  const rows = useMemo<ConversationRow[]>(() => {
    const next: ConversationRow[] = [];
    if (hasOlderConversationMessages) next.push({ kind: 'load_older' });
    if (loading) next.push({ kind: 'loading' });
    else if (messages.length === 0 && !streamingBubble && !operationItem) next.push({ kind: 'empty' });
    else {
      for (const item of insertTimeDividers(messages)) {
        if (item.kind === 'divider') next.push({ kind: 'divider', label: item.label });
        else next.push({ kind: 'message', message: item.msg });
      }
      if (streamingBubble) next.push({ kind: 'node', key: 'streaming', node: streamingBubble });
      if (operationItem) next.push({ kind: 'node', key: 'operation', node: operationItem });
    }
    return next;
  }, [hasOlderConversationMessages, loading, messages, operationItem, streamingBubble]);

  useImperativeHandle(conversationListRef, () => ({
    scrollToMessage: (messageId: string) => {
      initialBottomAnchorRef.current = null;
      if (initialBottomAnchorFrameRef.current != null) {
        window.cancelAnimationFrame(initialBottomAnchorFrameRef.current);
        initialBottomAnchorFrameRef.current = null;
      }
      const index = rows.findIndex(row => row.kind === 'message' && row.message.id === messageId);
      if (index < 0 || !listRef.current) return Promise.resolve(false);
      const rendered = renderedRangeRef.current;
      if (index >= rendered.startIndex && index <= rendered.stopIndex) return Promise.resolve(true);
      if (pendingNavigationRef.current) {
        window.clearTimeout(pendingNavigationRef.current.timeout);
        pendingNavigationRef.current.resolve(false);
      }
      return new Promise<boolean>(resolve => {
        const timeout = window.setTimeout(() => {
          if (pendingNavigationRef.current?.index !== index) return;
          pendingNavigationRef.current = null;
          resolve(false);
        }, 1500);
        pendingNavigationRef.current = { index, attempts: 1, timeout, resolve };
        listRef.current?.scrollToRow({ index, align: 'center', behavior: 'instant' });
      });
    },
    scrollToBottom: () => {
      const lastIndex = rows.length - 1;
      if (lastIndex < 0 || !listRef.current) return;
      listRef.current?.scrollToRow({ index: lastIndex, align: 'end', behavior: 'instant' });
    },
  }), [conversationListRef, listRef, rows]);

  const handleLoadOlder = useCallback(() => {
    const element = listRef.current?.element;
    if (element && onLoadOlderConversationMessages) {
      void preserveScrollAnchorAfterPrepend(element, onLoadOlderConversationMessages);
    }
  }, [listRef, onLoadOlderConversationMessages]);

  const handleConversationRowsRendered = useCallback((visible: { startIndex: number; stopIndex: number }) => {
    renderedRangeRef.current = visible;
    const pending = pendingNavigationRef.current;
    if (pending && pending.index >= visible.startIndex && pending.index <= visible.stopIndex) {
      pendingNavigationRef.current = null;
      window.clearTimeout(pending.timeout);
      pending.resolve(true);
    } else if (pending) {
      if (pending.attempts < 12) {
        pending.attempts += 1;
        listRef.current?.scrollToRow({ index: pending.index, align: 'center', behavior: 'instant' });
      }
    }
    // Match native chat behavior on touch/wheel scrolling: reaching the top
    // retrieves the next bounded history page. The explicit top button stays
    // available for keyboard and as a fallback if scrolling is interrupted.
    if (
      olderHistoryIntentRef.current
      && visible.startIndex === 0
      && rows[0]?.kind === 'load_older'
      && !loadingOlderConversationMessages
      && !autoLoadingOlderRef.current
    ) {
      olderHistoryIntentRef.current = false;
      autoLoadingOlderRef.current = true;
      handleLoadOlder();
    }
  }, [handleLoadOlder, listRef, loadingOlderConversationMessages, rows]);

  useEffect(() => () => {
    if (initialBottomAnchorFrameRef.current != null) {
      window.cancelAnimationFrame(initialBottomAnchorFrameRef.current);
    }
    if (!pendingNavigationRef.current) return;
    window.clearTimeout(pendingNavigationRef.current.timeout);
    pendingNavigationRef.current.resolve(false);
    pendingNavigationRef.current = null;
  }, []);

  useEffect(() => {
    scrollRef.current = listRef.current?.element ?? null;
    return () => { scrollRef.current = null; };
  });

  useEffect(() => {
    const element = listRef.current?.element;
    if (!element) return;
    let touchY: number | null = null;
    const stopInitialAnchor = () => {
      initialBottomAnchorRef.current = null;
      if (initialBottomAnchorFrameRef.current != null) {
        window.cancelAnimationFrame(initialBottomAnchorFrameRef.current);
        initialBottomAnchorFrameRef.current = null;
      }
    };
    const onWheel = (event: WheelEvent) => {
      stopInitialAnchor();
      olderHistoryIntentRef.current = event.deltaY < 0;
    };
    const onTouchStart = (event: TouchEvent) => {
      stopInitialAnchor();
      touchY = event.touches[0]?.clientY ?? null;
    };
    const onTouchMove = (event: TouchEvent) => {
      const nextY = event.touches[0]?.clientY ?? null;
      if (touchY != null && nextY != null) olderHistoryIntentRef.current = nextY > touchY;
      touchY = nextY;
    };
    const onKeyDown = (event: KeyboardEvent) => {
      stopInitialAnchor();
      olderHistoryIntentRef.current = event.key === 'ArrowUp'
        || event.key === 'PageUp'
        || event.key === 'Home';
    };
    element.addEventListener('touchstart', onTouchStart, { passive: true });
    element.addEventListener('touchmove', onTouchMove, { passive: true });
    element.addEventListener('wheel', onWheel, { passive: true });
    element.addEventListener('keydown', onKeyDown);
    return () => {
      element.removeEventListener('touchstart', onTouchStart);
      element.removeEventListener('touchmove', onTouchMove);
      element.removeEventListener('wheel', onWheel);
      element.removeEventListener('keydown', onKeyDown);
    };
  }, [listRef]);

  // Dynamic markdown rows are measured after their first paint. A single
  // scroll-to-last call can therefore use stale estimates and leave a newly
  // opened conversation in the middle. Keep the initial viewport pinned to the
  // latest row briefly while measurements settle, stopping immediately on any
  // operator scroll intent.
  useEffect(() => {
    if (messages.length === 0) {
      initialBottomAnchorRef.current = null;
      initialBottomAnchoredRef.current = false;
      return;
    }
    if (initialBottomAnchorRef.current != null || initialBottomAnchoredRef.current) return;
    initialBottomAnchoredRef.current = true;
    initialBottomAnchorRef.current = performance.now() + 500;
    const pinToLatest = () => {
      const until = initialBottomAnchorRef.current;
      if (until == null || performance.now() >= until) {
        initialBottomAnchorRef.current = null;
        initialBottomAnchorFrameRef.current = null;
        return;
      }
      const lastIndex = rows.length - 1;
      if (lastIndex >= 0) {
        listRef.current?.scrollToRow({ index: lastIndex, align: 'end', behavior: 'instant' });
      }
      initialBottomAnchorFrameRef.current = window.requestAnimationFrame(pinToLatest);
    };
    initialBottomAnchorFrameRef.current = window.requestAnimationFrame(pinToLatest);
  }, [listRef, messages.length, rows.length]);

  useEffect(() => {
    if (!loadingOlderConversationMessages) autoLoadingOlderRef.current = false;
  }, [loadingOlderConversationMessages, hasOlderConversationMessages]);

  const rowProps: ConversationRowData = useMemo(() => ({
    rows,
    messagesById,
    bubbleProps,
    highlightedMessageId,
    loadingOlderConversationMessages,
    onLoadOlder: handleLoadOlder,
  }), [
    bubbleProps,
    handleLoadOlder,
    highlightedMessageId,
    loadingOlderConversationMessages,
    messagesById,
    rows,
  ]);

  return (
    <div className="relative mx-auto w-full max-w-[940px] flex-1 overflow-hidden bg-workbench-base" data-testid="strategic-conversation-column">
      <List
        rowCount={rows.length}
        rowComponent={ConversationRowView}
        rowProps={rowProps}
        rowHeight={rowHeight}
        overscanCount={6}
        listRef={listRef}
        defaultHeight={600}
        className="h-full custom-scrollbar overflow-y-auto"
        onRowsRendered={handleConversationRowsRendered}
      />
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

// ─── Triage virtual list ──────────────────────────────────────────────────────

type TriageVirtualListProps = Omit<MessagesListViewProps, 'presentation' | 'streamingBubble' | 'operationItem'>;

function TriageVirtualList(props: TriageVirtualListProps) {
  const {
    messages,
    loading,
    dividerTime,
    totalNewCount,
    newMessageCounts,
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
    dividerDismissedRef,
    scrollRef,
    onDismissDivider,
    onShowOnlyNew,
    setExpandedGroupKeys,
    setRoutineExpanded,
    setShowResolved,
    setShowTempMuteMenu,
    addTemporaryMute,
    getTop3NoisyEventTypes,
    handleBulkDeleteResolved,
    hasOlderTriageMessages,
    loadingOlderTriageMessages,
    onLoadOlderTriageMessages,
  } = props;
  const listRef = useListRef(null);
  const messagesById = useMemo(
    () => new Map(messages.map(message => [message.id, message])),
    [messages],
  );

  // Sync the List's outer DOM element to scrollRef so MessagesPanel's scroll
  // behaviors (auto-scroll, near-bottom detection, event listeners) work unchanged.
  useEffect(() => {
    scrollRef.current = listRef.current?.element ?? null;
    return () => { scrollRef.current = null; };
  });

  const topNoisyEventTypes = getTop3NoisyEventTypes();

  const { items, sectionIndices } = useMemo(() => buildTriageItems({
    loading,
    messages,
    dividerTime,
    totalNewCount,
    dividerDismissedCurrent: dividerDismissedRef.current,
    showOnlyNew,
    showResolved,
    routineExpanded,
    muteActive,
    searchQuery,
    pinnedBlockers,
    pinnedQuestions,
    otherHighSignal,
    promotedGroups,
    remainingRoutineRows,
    autoExpandedKeys,
    expandedGroupKeys,
    routineOpen,
    linkedMessages,
    visibleResolved,
    hiddenResolvedCount,
    resolvedMessages,
    newMessageCounts,
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }), [
    loading, messages, dividerTime, totalNewCount, dividerDismissedRef.current,
    showOnlyNew, showResolved, routineExpanded, muteActive, searchQuery,
    pinnedBlockers, pinnedQuestions, otherHighSignal, promotedGroups,
    remainingRoutineRows, autoExpandedKeys, expandedGroupKeys, routineOpen,
    linkedMessages, visibleResolved, hiddenResolvedCount, resolvedMessages,
    newMessageCounts,
  ]);

  const newDividerIndex = sectionIndices.newDivider;
  const handleRowsRenderedRef = useRef<(visible: { startIndex: number }) => void>(() => { /* placeholder */ });
  handleRowsRenderedRef.current = (visible: { startIndex: number }) => {
    if (!dividerDismissedRef.current && newDividerIndex >= 0 && visible.startIndex > newDividerIndex) {
      onDismissDivider();
    }
  };

  const rowHeight = useDynamicRowHeight({ defaultRowHeight: 120 });

  const rowProps: TriageRowData = useMemo(() => ({
    items,
    messagesById,
    dividerTime,
    totalNewCount,
    showTempMuteMenu,
    routineExpanded,
    tempMutes,
    topNoisyEventTypes,
    searchQuery,
    newMessageCounts,
    listRef,
    sectionIndices,
    bubbleProps,
    highlightedMessageId,
    visibleRoutineCount,
    mutedRoutineCount,
    routineOpen,
    onDismissDivider,
    onShowOnlyNew,
    setExpandedGroupKeys,
    setRoutineExpanded,
    setShowResolved,
    setShowTempMuteMenu,
    addTemporaryMute,
    handleBulkDeleteResolved,
  }), [
    items, messagesById, dividerTime, totalNewCount,
    showTempMuteMenu, routineExpanded,
    tempMutes, topNoisyEventTypes, searchQuery, newMessageCounts, listRef,
    sectionIndices, bubbleProps, highlightedMessageId,
    visibleRoutineCount, mutedRoutineCount, routineOpen,
    onDismissDivider, onShowOnlyNew, setExpandedGroupKeys, setRoutineExpanded,
    setShowResolved, setShowTempMuteMenu, addTemporaryMute,
    handleBulkDeleteResolved,
  ]);

  const handleTriageScrollToBottom = useCallback(() => {
    const el = listRef.current?.element;
    if (!el) return;
    // Use scrollHeight directly rather than scrollToRow so the scroll lands at
    // the real bottom regardless of dynamic row-height estimates (audit 71833e6b).
    el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' });
  }, [listRef]);

  return (
    <div className="relative flex-1 overflow-hidden bg-workbench-surface">
      {!!hasOlderTriageMessages && (
        <div className="flex justify-center py-2 border-b border-neutral-800/60">
          <button
            type="button"
            onClick={() => onLoadOlderTriageMessages?.()}
            disabled={!!loadingOlderTriageMessages}
            className="min-h-11 rounded-md border border-neutral-800 bg-neutral-950/60 px-3 py-2 text-xs text-neutral-400 transition-colors hover:border-neutral-700 hover:text-neutral-200 disabled:opacity-50"
          >
            {loadingOlderTriageMessages ? 'Loading older messages…' : 'Load older messages'}
          </button>
        </div>
      )}
      <List
        rowCount={items.length}
        rowComponent={TriageRow}
        rowProps={rowProps}
        rowHeight={rowHeight}
        overscanCount={6}
        listRef={listRef}
        defaultHeight={600}
        className="h-full custom-scrollbar overflow-y-auto"
        onRowsRendered={(visible) => handleRowsRenderedRef.current(visible)}
      />
      {showScrollDown && (
        <button
          onClick={handleTriageScrollToBottom}
          className="absolute bottom-2 left-1/2 -translate-x-1/2 bg-neutral-800 border border-neutral-700 text-neutral-400 hover:text-neutral-200 p-2 rounded-full shadow-lg transition-colors z-10 min-h-[44px] min-w-[44px] flex items-center justify-center"
          aria-label="Scroll to latest message"
        >
          <ArrowDown className="w-4 h-4" />
        </button>
      )}
    </div>
  );
}
