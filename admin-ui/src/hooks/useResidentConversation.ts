import { useCallback, useEffect, useRef, useState, type Dispatch, type SetStateAction } from 'react';
import {
  fetchStrategicConversation,
  isStrategicConversationNotModified,
} from '../strategicStateApi';
import type { ProjectMessage } from '../types/messages';
import { parseTimestamp } from '../utils/time';
import { messageHasClientIdentity } from '../utils/conversationMessageIdentity';

export function compareResidentMessages(a: ProjectMessage, b: ProjectMessage): number {
  if (a.sequence != null || b.sequence != null) {
    if (a.sequence == null) return -1;
    if (b.sequence == null) return 1;
    if (a.sequence !== b.sequence) return a.sequence - b.sequence;
  }
  const timeDelta = (a.created_at ? parseTimestamp(a.created_at).getTime() : 0)
    - (b.created_at ? parseTimestamp(b.created_at).getTime() : 0);
  return timeDelta || a.id.localeCompare(b.id);
}

export function mergeResidentConversationPages(
  existing: ProjectMessage[],
  incoming: ProjectMessage[],
): ProjectMessage[] {
  if (incoming.length === 0) return existing;
  const mergedById = new Map(existing.map(message => [message.id, message]));
  for (const message of incoming) {
    const identity = message.client_idempotency_key ?? message.idempotency_key ?? null;
    if (identity) {
      for (const [existingId, existingMessage] of mergedById) {
        if (existingId !== message.id && messageHasClientIdentity(existingMessage, identity)) {
          mergedById.delete(existingId);
        }
      }
    }
    const current = mergedById.get(message.id);
    mergedById.set(message.id, current && residentMessagesEqual(current, message) ? current : message);
  }
  const merged = [...mergedById.values()].sort(compareResidentMessages);
  return merged.length === existing.length && merged.every((message, index) => message === existing[index])
    ? existing
    : merged;
}

export function residentMessagesEqual(a: ProjectMessage, b: ProjectMessage): boolean {
  return a.id === b.id
    && a.project_id === b.project_id
    && a.message_type === b.message_type
    && a.from_type === b.from_type
    && a.from_task_id === b.from_task_id
    && a.content === b.content
    && a.status === b.status
    && a.resolution === b.resolution
    && a.resolved_at === b.resolved_at
    && a.created_at === b.created_at
    && a.updated_at === b.updated_at
    && a.actor_user_id === b.actor_user_id
    && a.sequence === b.sequence
    && a.surface === b.surface
    && a.handling_state === b.handling_state
    && a.handled_by_task_id === b.handled_by_task_id
    && a.reply_to_message_id === b.reply_to_message_id
    && a.client_idempotency_key === b.client_idempotency_key
    && a.idempotency_key === b.idempotency_key
    && JSON.stringify(a.context ?? {}) === JSON.stringify(b.context ?? {});
}

interface UseResidentConversationOptions {
  enabled: boolean;
  projectId: string | null;
  actorUserId: string | null;
  includeMessageId: string | null;
  searchQuery?: string;
  realtimeRefreshSignal?: number;
  realtimeMessage?: ProjectMessage | null;
  realtimeMessageRequiresCanonicalRefetch?: boolean;
  /** Increments when the strategic-state poll completes with new data.
   *  When set, the conversation refresh piggybacks on the strategic-state
   *  poll cadence instead of running its own independent timer. */
  strategicPollEpoch?: number;
  getMessages: () => ProjectMessage[];
  setMessages: Dispatch<SetStateAction<ProjectMessage[]>>;
  onLoadingChange: (loading: boolean) => void;
  onError: (message: string) => void;
  onTargetFetched?: (messageId: string) => void;
}

export function useResidentConversation({
  enabled,
  projectId,
  actorUserId,
  includeMessageId,
  searchQuery = '',
  realtimeRefreshSignal,
  realtimeMessage,
  realtimeMessageRequiresCanonicalRefetch = false,
  strategicPollEpoch,
  getMessages,
  setMessages,
  onLoadingChange,
  onError,
  onTargetFetched,
}: UseResidentConversationOptions) {
  const [hasMore, setHasMore] = useState(false);
  const [nextBeforeSequence, setNextBeforeSequence] = useState<number | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const etagRef = useRef<string | null>(null);
  const requestSequenceRef = useRef(0);
  const trimmedSearch = searchQuery.trim();
  const normalizedSearch = trimmedSearch.length >= 2 ? trimmedSearch : '';
  const activeScopeRef = useRef(`${actorUserId ?? ''}:${projectId ?? ''}:${normalizedSearch}`);
  const fetchedTargetKeyRef = useRef<string | null>(null);
  const gapContinuationRef = useRef<() => void>(() => {});
  const refreshAbortRef = useRef<AbortController | null>(null);
  const olderAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    activeScopeRef.current = `${actorUserId ?? ''}:${projectId ?? ''}:${normalizedSearch}`;
    fetchedTargetKeyRef.current = null;
    etagRef.current = null;
    requestSequenceRef.current += 1;
    refreshAbortRef.current?.abort();
    olderAbortRef.current?.abort();
    // Cursor state belongs to the selected project and must reset at this boundary.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setHasMore(false);
    setNextBeforeSequence(null);
    setLoadingOlder(false);
  }, [actorUserId, projectId, includeMessageId, normalizedSearch]);

  const refresh = useCallback(async (fillSequenceGap = false, explicitForwardFrom?: number | null) => {
    if (!enabled || !projectId || !actorUserId) return;
    refreshAbortRef.current?.abort();
    const controller = new AbortController();
    refreshAbortRef.current = controller;
    const requestSequence = ++requestSequenceRef.current;
    const requestScope = `${actorUserId}:${projectId}:${normalizedSearch}`;
    const isCurrentRequest = () => (
      !controller.signal.aborted
      && activeScopeRef.current === requestScope
      && requestSequence === requestSequenceRef.current
    );
    const targetKey = includeMessageId ? `${projectId}:${includeMessageId}` : null;
    const requestedTargetId = targetKey && fetchedTargetKeyRef.current !== targetKey
      ? includeMessageId
      : null;
    const existing = getMessages().filter(message => message.project_id === projectId);
    const priorSequences = existing
      .filter(message => message.sequence != null)
      .map(message => message.sequence as number);
    const priorMax = priorSequences.length ? Math.max(...priorSequences) : null;
    const forwardFrom = fillSequenceGap && requestedTargetId == null && !normalizedSearch
      ? (explicitForwardFrom === undefined ? priorMax : explicitForwardFrom)
      : null;
    try {
      const result = await fetchStrategicConversation(
        projectId,
        requestedTargetId
          ? { includeMessageId: requestedTargetId, limit: 100 }
          : normalizedSearch
          ? { search: normalizedSearch, limit: 100 }
          : forwardFrom != null
          ? { afterSequence: forwardFrom, limit: 100 }
          : { includeMessageId: requestedTargetId, limit: 100 },
        controller.signal,
        forwardFrom == null ? etagRef.current : null,
      );
      if (isStrategicConversationNotModified(result)) return;
      if (!isCurrentRequest()) return;
      if (requestedTargetId && targetKey) {
        fetchedTargetKeyRef.current = targetKey;
        onTargetFetched?.(requestedTargetId);
      }
      etagRef.current = result.etag ?? null;
      let page = result.conversation.items ?? [];
      let cursor = result.conversation.next_before_sequence ?? null;
      let pageHasMore = Boolean(result.conversation.has_more);
      let gapPages = 0;
      let forwardCursor = forwardFrom ?? -1;
      while (
        forwardFrom != null
        && pageHasMore
        && gapPages < 10
      ) {
        const lastSequence = page.reduce<number>((maximum, message) => (
          message.sequence == null ? maximum : Math.max(maximum, message.sequence)
        ), forwardCursor);
        if (lastSequence <= forwardCursor) break;
        forwardCursor = lastSequence;
        const gapResult = await fetchStrategicConversation(projectId, {
          afterSequence: lastSequence,
          limit: 100,
        }, controller.signal);
        if (!isCurrentRequest()) return;
        if (isStrategicConversationNotModified(gapResult)) break;
        const gapItems = gapResult.conversation.items ?? [];
        page = mergeResidentConversationPages(page, gapItems);
        cursor = gapResult.conversation.next_before_sequence ?? null;
        pageHasMore = Boolean(gapResult.conversation.has_more);
        gapPages += 1;
      }
      if (!isCurrentRequest()) return;
      const merged = normalizedSearch ? page.slice().sort(compareResidentMessages) : mergeResidentConversationPages(existing, page);
      setMessages(merged);
      if (forwardFrom == null) {
        setHasMore(normalizedSearch ? false : pageHasMore);
        setNextBeforeSequence(normalizedSearch ? null : cursor);
      }
      if (forwardFrom != null && pageHasMore) {
        window.setTimeout(() => gapContinuationRef.current(), 0);
      }
    } catch (error) {
      if (isCurrentRequest() && !(error instanceof DOMException && error.name === 'AbortError')) {
        onError('Failed to load messages');
      }
    } finally {
      if (isCurrentRequest()) onLoadingChange(false);
      if (refreshAbortRef.current === controller) refreshAbortRef.current = null;
    }
  }, [actorUserId, enabled, getMessages, includeMessageId, normalizedSearch, onError, onLoadingChange, onTargetFetched, projectId, setMessages]);

  useEffect(() => {
    gapContinuationRef.current = () => { void refresh(true); };
  }, [refresh]);

  const loadOlder = useCallback(async () => {
    if (!enabled || !projectId || !actorUserId || normalizedSearch || loadingOlder || !hasMore || nextBeforeSequence == null) return;
    olderAbortRef.current?.abort();
    const controller = new AbortController();
    olderAbortRef.current = controller;
    const requestScope = `${actorUserId}:${projectId}:${normalizedSearch}`;
    const isCurrentRequest = () => (
      !controller.signal.aborted
      && activeScopeRef.current === requestScope
    );
    setLoadingOlder(true);
    try {
      const result = await fetchStrategicConversation(projectId, {
        beforeSequence: nextBeforeSequence,
        limit: 100,
      }, controller.signal);
      if (isStrategicConversationNotModified(result) || !isCurrentRequest()) return;
      setMessages(previous => mergeResidentConversationPages(previous, result.conversation.items ?? []));
      setHasMore(Boolean(result.conversation.has_more));
      setNextBeforeSequence(result.conversation.next_before_sequence ?? null);
    } catch (error) {
      if (isCurrentRequest() && !(error instanceof DOMException && error.name === 'AbortError')) {
        onError('Failed to load older messages');
      }
    } finally {
      if (isCurrentRequest()) setLoadingOlder(false);
      if (olderAbortRef.current === controller) olderAbortRef.current = null;
    }
  }, [actorUserId, enabled, hasMore, loadingOlder, nextBeforeSequence, normalizedSearch, onError, projectId, setMessages]);

  const refetchCanonicalMessage = useCallback(async (messageId: string) => {
    if (!enabled || !projectId || !actorUserId) return;
    refreshAbortRef.current?.abort();
    const controller = new AbortController();
    refreshAbortRef.current = controller;
    const requestSequence = ++requestSequenceRef.current;
    const requestScope = `${actorUserId}:${projectId}:${normalizedSearch}`;
    const isCurrentRequest = () => (
      !controller.signal.aborted
      && activeScopeRef.current === requestScope
      && requestSequence === requestSequenceRef.current
    );
    try {
      const result = await fetchStrategicConversation(
        projectId,
        { includeMessageId: messageId, limit: 1 },
        controller.signal,
      );
      if (isStrategicConversationNotModified(result) || !isCurrentRequest()) return;
      const canonical = (result.conversation.items ?? []).find(message => message.id === messageId);
      if (canonical) {
        setMessages(previous => mergeResidentConversationPages(previous, [canonical]));
      }
    } catch (error) {
      if (isCurrentRequest() && !(error instanceof DOMException && error.name === 'AbortError')) {
        onError('Failed to reconcile a realtime message');
      }
    } finally {
      if (refreshAbortRef.current === controller) refreshAbortRef.current = null;
    }
  }, [actorUserId, enabled, normalizedSearch, onError, projectId, setMessages]);

  const previousSearchRef = useRef(normalizedSearch);
  useEffect(() => {
    if (!enabled || previousSearchRef.current === normalizedSearch) return;
    previousSearchRef.current = normalizedSearch;
    const timer = window.setTimeout(() => { void refresh(false); }, 250);
    return () => window.clearTimeout(timer);
  }, [enabled, normalizedSearch, refresh]);

  useEffect(() => {
    if (!enabled || !projectId || !realtimeMessage || realtimeMessage.project_id !== projectId || realtimeMessage.surface !== 'conversation') return;
    if (normalizedSearch) {
      const timer = window.setTimeout(() => { void refresh(false); }, 100);
      return () => window.clearTimeout(timer);
    }
    const existing = getMessages().filter(message => message.project_id === projectId);
    const sequences = existing.flatMap(message => message.sequence == null ? [] : [message.sequence]);
    const priorMax = sequences.length ? Math.max(...sequences) : null;
    if (realtimeMessageRequiresCanonicalRefetch) {
      const timer = window.setTimeout(() => {
        void refetchCanonicalMessage(realtimeMessage.id)
          .then(() => refresh(priorMax != null, priorMax));
      }, 0);
      return () => window.clearTimeout(timer);
    }
    setMessages(previous => mergeResidentConversationPages(previous, [realtimeMessage]));
    const timer = window.setTimeout(() => { void refresh(priorMax != null, priorMax); }, 0);
    return () => window.clearTimeout(timer);
  }, [enabled, getMessages, normalizedSearch, projectId, realtimeMessage, realtimeMessageRequiresCanonicalRefetch, refetchCanonicalMessage, refresh, setMessages]);

  useEffect(() => {
    if (!enabled || !realtimeRefreshSignal) return;
    const timer = window.setTimeout(() => { void refresh(true); }, 100);
    return () => window.clearTimeout(timer);
  }, [enabled, realtimeRefreshSignal, refresh]);

  // When the strategic-state poll delivers new data, piggyback a conversation
  // refresh on it instead of running an independent timer. This eliminates the
  // duplicate strategic-state assembly request that the legacy independent poll
  // would trigger on the backend (the conversation handler re-assembles state).
  useEffect(() => {
    if (!enabled || strategicPollEpoch == null || strategicPollEpoch === 0) return;
    const timer = window.setTimeout(() => { void refresh(true); }, 100);
    return () => window.clearTimeout(timer);
  }, [enabled, strategicPollEpoch, refresh]);

  return { refresh, loadOlder, hasMore, loadingOlder };
}
