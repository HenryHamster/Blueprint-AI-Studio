import { useCallback, useState } from 'react';
import {
  loadPendingConversationSend,
  savePendingConversationSend,
  type ComposerSendState,
  type PendingConversationSend,
} from '../utils/conversationSendState';
import type { StrategicMessageWakeupState } from '../strategicStateApi';

interface ConversationSendEntry {
  pending: PendingConversationSend | null;
  draftIdentity: string | null;
  restored: boolean;
}

const EMPTY_ENTRY: ConversationSendEntry = { pending: null, draftIdentity: null, restored: false };

function storageKey(actorUserId: string, projectId: string): string {
  return `optimisticSend:${encodeURIComponent(actorUserId)}:${projectId}`;
}

function draftIdentityKey(actorUserId: string, projectId: string): string {
  return `conversationDraftSend:${encodeURIComponent(actorUserId)}:${projectId}`;
}

function readEntry(actorUserId: string | null, projectId: string | null): ConversationSendEntry {
  if (!actorUserId || !projectId) return EMPTY_ENTRY;
  const pending = loadPendingConversationSend(
    sessionStorage,
    storageKey(actorUserId, projectId),
    { actorUserId, projectId },
  );
  const draftIdentity = pending?.key ?? sessionStorage.getItem(draftIdentityKey(actorUserId, projectId));
  return { pending, draftIdentity, restored: pending != null };
}

/** Project-keyed durable composer state with exact-key storage writes. */
export function useConversationSend(projectId: string | null, actorUserId: string | null) {
  const [entries, setEntries] = useState<Record<string, ConversationSendEntry>>({});
  const key = actorUserId && projectId ? `${actorUserId}:${projectId}` : '';
  const entry = actorUserId && projectId ? entries[key] ?? readEntry(actorUserId, projectId) : EMPTY_ENTRY;

  const update = useCallback((targetActorUserId: string, targetProjectId: string, updater: (current: ConversationSendEntry) => ConversationSendEntry) => {
    const targetKey = `${targetActorUserId}:${targetProjectId}`;
    setEntries(currentEntries => {
      const current = currentEntries[targetKey] ?? readEntry(targetActorUserId, targetProjectId);
      const next = updater(current);
      savePendingConversationSend(sessionStorage, storageKey(targetActorUserId, targetProjectId), next.pending);
      if (next.draftIdentity) sessionStorage.setItem(draftIdentityKey(targetActorUserId, targetProjectId), next.draftIdentity);
      else sessionStorage.removeItem(draftIdentityKey(targetActorUserId, targetProjectId));
      return { ...currentEntries, [targetKey]: next };
    });
  }, []);

  const ensureIdentity = useCallback((factory: () => string): string => {
    if (!actorUserId || !projectId) return factory();
    const current = entries[key] ?? readEntry(actorUserId, projectId);
    if (current.draftIdentity) return current.draftIdentity;
    const identity = factory();
    update(actorUserId, projectId, value => ({ ...value, draftIdentity: identity }));
    return identity;
  }, [actorUserId, entries, key, projectId, update]);

  const adoptIdentity = useCallback((identity: string | null | undefined) => {
    if (!identity || !actorUserId || !projectId) return;
    update(actorUserId, projectId, current => (
      current.pending || current.draftIdentity
        ? current
        : { ...current, draftIdentity: identity }
    ));
  }, [actorUserId, projectId, update]);

  const begin = useCallback((pending: Omit<PendingConversationSend, 'actor_user_id' | 'project_id'>) => {
    if (actorUserId && projectId) {
      const scopedPending = { ...pending, actor_user_id: actorUserId, project_id: projectId };
      update(actorUserId, projectId, () => ({ pending: scopedPending, draftIdentity: pending.key, restored: false }));
    }
  }, [actorUserId, projectId, update]);

  const replace = useCallback((pending: Omit<PendingConversationSend, 'actor_user_id' | 'project_id'> | PendingConversationSend) => {
    if (actorUserId && projectId) {
      const scopedPending = { ...pending, actor_user_id: actorUserId, project_id: projectId };
      update(actorUserId, projectId, current => ({ ...current, pending: scopedPending, restored: false }));
    }
  }, [actorUserId, projectId, update]);

  const transition = useCallback((state: ComposerSendState, wakeupState?: StrategicMessageWakeupState | null) => {
    if (!actorUserId || !projectId) return;
    update(actorUserId, projectId, current => current.pending ? {
      ...current,
      draftIdentity: state === 'accepted' || state === 'waiting' ? null : current.draftIdentity,
      pending: {
        ...current.pending,
        state,
        wakeup_state: wakeupState === undefined ? current.pending.wakeup_state : wakeupState,
      },
    } : current);
  }, [actorUserId, projectId, update]);

  const clear = useCallback(() => {
    if (actorUserId && projectId) update(actorUserId, projectId, () => EMPTY_ENTRY);
  }, [actorUserId, projectId, update]);

  const clearIdentity = useCallback(() => {
    if (actorUserId && projectId) update(actorUserId, projectId, current => ({ ...current, draftIdentity: null }));
  }, [actorUserId, projectId, update]);

  return {
    pending: entry.pending,
    sendState: entry.pending?.state ?? null,
    wakeupState: entry.pending?.wakeup_state ?? null,
    restored: entry.restored,
    identity: entry.pending && (entry.pending.state === 'sending' || entry.pending.state === 'needs_retry')
      ? entry.pending.key
      : entry.draftIdentity,
    ensureIdentity,
    adoptIdentity,
    begin,
    replace,
    transition,
    clear,
    clearIdentity,
  };
}
