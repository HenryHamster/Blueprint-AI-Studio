import { useMemo } from 'react';
import { parseTimestamp } from '../utils/time';
import { type EventPref, effectiveSignal, isCriticalSeverity } from './eventPrefs';
import { type MuteWindow, isMuteWindowActive } from './muteWindow';
import { type TemporaryMutes } from './temporaryMutes';
import { type RoutineRow, groupRoutineMessages, collapseStaleRoutineRows, isFailingGroup } from './messageGrouping';
import { formatFromType } from './MessageBubble';
import type { ProjectMessage } from '../types/messages';

export type MessageSummaryCounts = {
  blockers: number;
  questions: number;
  feedback: number;
  bugReports: number;
  routine: number;
  highSignalNotes: number;
};

export interface UseMessageListInput {
  messages: ProjectMessage[];
  eventPrefs: Record<string, EventPref>;
  tempMutes: TemporaryMutes;
  muteWindow: MuteWindow | null;
  searchQuery: string;
  sortMode: 'importance' | 'recent';
  criticalOnly: boolean;
  showResolved: boolean;
  showOnlyNew: boolean;
  dividerTime: number | null;
}

const RECENT_RESOLVED = 5;

/**
 * Pure derivation of the MessagesPanel list state. Splits open messages into
 * high-signal vs routine, computes pinned blockers/questions, auto-promotes
 * failing routine groups, computes visible-resolved, and applies
 * search/divider filtering. Inputs in, derived state out — no parent mutation.
 */
export function useMessageList({
  messages,
  eventPrefs,
  tempMutes,
  muteWindow,
  searchQuery,
  sortMode,
  criticalOnly,
  showResolved,
  showOnlyNew,
  dividerTime,
}: UseMessageListInput) {
  const byTime = (a: ProjectMessage, b: ProjectMessage) => {
    const ta = a.created_at ? parseTimestamp(a.created_at).getTime() : 0;
    const tb = b.created_at ? parseTimestamp(b.created_at).getTime() : 0;
    return ta - tb;
  };

  const matchesSearch = (msg: ProjectMessage): boolean => {
    if (!searchQuery.trim()) return true;
    const q = searchQuery.toLowerCase();
    if (msg.content.toLowerCase().includes(q)) return true;
    if (msg.resolution?.toLowerCase().includes(q)) return true;
    if (formatFromType(msg).toLowerCase().includes(q)) return true;
    const et = msg.context.event_type as string | undefined;
    if (et?.toLowerCase().includes(q)) return true;
    const at = msg.context.annotation_type as string | undefined;
    if (at?.toLowerCase().includes(q)) return true;
    const src = msg.context.source as string | undefined;
    if (src?.toLowerCase().includes(q)) return true;
    return false;
  };

  const { openMessages, linkedMessages, resolvedMessages, openFeedbackMessages } = useMemo(() => {
    const open = messages.filter(m => m.status === 'open' && (!criticalOnly || isCriticalSeverity(m)));
    const linked = messages.filter(m => m.status === 'linked' && (!criticalOnly || isCriticalSeverity(m)));
    return {
      openMessages: open,
      linkedMessages: linked,
      resolvedMessages: messages.filter(m => m.status === 'resolved'),
      openFeedbackMessages: open.filter(m => m.message_type === 'FEEDBACK' && matchesSearch(m)),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [messages, criticalOnly, searchQuery]);

  // Count new high-signal messages (created after divider time) — for visual divider only
  const newHighSignalCount = useMemo(() => dividerTime
    ? openMessages.filter(m =>
        m.created_at &&
        effectiveSignal(m, eventPrefs, tempMutes) === 'high' &&
        parseTimestamp(m.created_at).getTime() > dividerTime
      ).length
    : 0, [openMessages, dividerTime, eventPrefs, tempMutes]);

  // Count new messages by type for the summary card (respecting mute prefs and criticalOnly)
  const newMessageCounts = useMemo<MessageSummaryCounts>(() => dividerTime
    ? {
        blockers: openMessages.filter(m =>
          m.message_type === 'BLOCKER' &&
          m.created_at &&
          parseTimestamp(m.created_at).getTime() > dividerTime &&
          effectiveSignal(m, eventPrefs, tempMutes) !== 'mute'
        ).length,
        feedback: openMessages.filter(m =>
          m.message_type === 'FEEDBACK' &&
          m.created_at &&
          parseTimestamp(m.created_at).getTime() > dividerTime &&
          effectiveSignal(m, eventPrefs, tempMutes) !== 'mute'
        ).length,
        bugReports: openMessages.filter(m =>
          m.message_type === 'BUG_REPORT' &&
          m.created_at &&
          parseTimestamp(m.created_at).getTime() > dividerTime &&
          effectiveSignal(m, eventPrefs, tempMutes) !== 'mute'
        ).length,
        routine: openMessages.filter(m =>
          effectiveSignal(m, eventPrefs, tempMutes) === 'routine' &&
          m.created_at &&
          parseTimestamp(m.created_at).getTime() > dividerTime
        ).length,
        highSignalNotes: openMessages.filter(m =>
          m.message_type === 'NOTE' &&
          m.created_at &&
          parseTimestamp(m.created_at).getTime() > dividerTime &&
          effectiveSignal(m, eventPrefs, tempMutes) === 'high'
        ).length,
        questions: openMessages.filter(m =>
          m.message_type === 'QUESTION' &&
          m.created_at &&
          parseTimestamp(m.created_at).getTime() > dividerTime &&
          // Match pinnedQuestions predicate (effectiveSignal === 'high'): the summary card
          // scrolls to the pinned-questions section, so it must not announce QUESTIONs that
          // were demoted to 'routine' and live elsewhere in the list.
          effectiveSignal(m, eventPrefs, tempMutes) === 'high'
        ).length,
      }
    : { blockers: 0, feedback: 0, bugReports: 0, routine: 0, highSignalNotes: 0, questions: 0 },
  [openMessages, dividerTime, eventPrefs, tempMutes]);

  // Total new count: each message counted exactly once (high-signal and routine are disjoint)
  const totalNewCount = newHighSignalCount + newMessageCounts.routine;

  // Split open messages by signal level, respecting per-event-type prefs and temp mutes, then filter by search
  const muteActive = isMuteWindowActive(muteWindow);
  const { highSignalOpen, routineOpen } = useMemo(() => {
    const isNew = (m: ProjectMessage): boolean => {
      if (!showOnlyNew || !dividerTime || !m.created_at) return true;
      return parseTimestamp(m.created_at).getTime() > dividerTime;
    };
    const SEVERITY_RANK_INNER: Record<string, number> = { BLOCKER: 0, BUG_REPORT: 1, FEEDBACK: 2, NOTE: 3 };
    const CONTEXT_SEVERITY_RANK_INNER: Record<string, number> = { high: 0, medium: 1, low: 2 };
    const byTimeDescInner = (a: ProjectMessage, b: ProjectMessage) => {
      const ta = a.created_at ? parseTimestamp(a.created_at).getTime() : 0;
      const tb = b.created_at ? parseTimestamp(b.created_at).getTime() : 0;
      return tb - ta;
    };
    const bySeverityInner = (a: ProjectMessage, b: ProjectMessage) => {
      const ra = SEVERITY_RANK_INNER[a.message_type] ?? 3;
      const rb = SEVERITY_RANK_INNER[b.message_type] ?? 3;
      if (ra !== rb) return ra - rb;
      const sa = (a.context.severity as string | undefined) ?? 'undefined';
      const sb = (b.context.severity as string | undefined) ?? 'undefined';
      const sa_rank = CONTEXT_SEVERITY_RANK_INNER[sa] ?? 3;
      const sb_rank = CONTEXT_SEVERITY_RANK_INNER[sb] ?? 3;
      if (sa_rank !== sb_rank) return sa_rank - sb_rank;
      return byTimeDescInner(a, b);
    };
    return {
      highSignalOpen: [...openMessages.filter(m => effectiveSignal(m, eventPrefs, tempMutes) === 'high' && matchesSearch(m) && isNew(m))]
        .sort(sortMode === 'importance' ? bySeverityInner : byTimeDescInner),
      // Always compute routine messages; mute only filters them from the non-promoted routine section
      routineOpen: [...openMessages.filter(m => effectiveSignal(m, eventPrefs, tempMutes) === 'routine' && matchesSearch(m) && isNew(m))].sort(byTimeDescInner),
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [openMessages, eventPrefs, tempMutes, searchQuery, sortMode, showOnlyNew, dividerTime]);

  // In importance mode, pin BLOCKERs and unanswered QUESTIONs at the top with visual dividers
  const pinnedBlockers = sortMode === 'importance' ? highSignalOpen.filter(m => m.message_type === 'BLOCKER') : [];
  const pinnedQuestions = sortMode === 'importance' ? highSignalOpen.filter(m => m.message_type === 'QUESTION') : [];
  const otherHighSignal = sortMode === 'importance' ? highSignalOpen.filter(m => m.message_type !== 'BLOCKER' && m.message_type !== 'QUESTION') : highSignalOpen;

  // Failing routine groups auto-promoted above the routine ops section
  const { promotedGroups, remainingRoutineRows, autoExpandedKeys, visibleRoutineCount, mutedRoutineCount } = useMemo(() => {
    const rows = collapseStaleRoutineRows(groupRoutineMessages(routineOpen));
    const promoted = rows.filter(
      (r): r is RoutineRow<ProjectMessage> & { kind: 'group' } =>
        r.kind === 'group' && isFailingGroup(r.msgs) && eventPrefs[r.event_type] !== 'mute',
    );
    const promotedSet = new Set(promoted.map(r => r.key));
    // When mute is active, filter out routine messages from the remaining section, but keep promoted groups
    const remaining = muteActive
      ? rows.filter(r => promotedSet.has((r as RoutineRow<ProjectMessage> & { key?: string }).key ?? ''))
      : rows.filter(r => r.kind !== 'group' || !promotedSet.has(r.key));
    // Auto-expand groups that have search matches
    const withMatches = new Set(
      rows
        .filter((r): r is RoutineRow<ProjectMessage> & { kind: 'group' } => r.kind === 'group' && r.msgs.length > 0)
        .map(r => r.key)
    );
    // Calculate shown vs muted routine message counts when mute is active
    let visible = routineOpen.length;
    let muted = 0;
    if (muteActive && promoted.length > 0) {
      visible = promoted.reduce((sum, row) => sum + row.msgs.length, 0);
      muted = routineOpen.length - visible;
    }
    return {
      promotedGroups: promoted,
      remainingRoutineRows: remaining,
      autoExpandedKeys: searchQuery.trim() ? withMatches : new Set<string>(),
      visibleRoutineCount: visible,
      mutedRoutineCount: muted,
    };
  }, [routineOpen, eventPrefs, muteActive, searchQuery]);

  // Resolved: show last 5 by default, toggle to show all, filtered by search
  const sortedResolved = [...resolvedMessages.filter(m => matchesSearch(m))].sort(byTime);
  const visibleResolved = showResolved ? sortedResolved : sortedResolved.slice(-RECENT_RESOLVED);
  const hiddenResolvedCount = showResolved ? 0 : Math.max(0, sortedResolved.length - RECENT_RESOLVED);

  return {
    openMessages,
    linkedMessages,
    resolvedMessages,
    openFeedbackMessages,
    newHighSignalCount,
    newMessageCounts,
    totalNewCount,
    muteActive,
    highSignalOpen,
    routineOpen,
    pinnedBlockers,
    pinnedQuestions,
    otherHighSignal,
    promotedGroups,
    remainingRoutineRows,
    autoExpandedKeys,
    visibleRoutineCount,
    mutedRoutineCount,
    sortedResolved,
    visibleResolved,
    hiddenResolvedCount,
  };
}
