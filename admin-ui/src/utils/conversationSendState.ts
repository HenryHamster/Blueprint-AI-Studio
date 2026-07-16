import type {
  StrategicMessageWakeupState,
  StrategicSessionMessageCreate,
} from '../strategicStateApi';

export type ComposerSendState = 'sending' | 'accepted' | 'waiting' | 'needs_retry';

export interface PendingConversationSend {
  actor_user_id: string;
  project_id: string;
  key: string;
  content: string;
  created_at: string;
  message_type: string;
  state: ComposerSendState;
  wakeup_state?: StrategicMessageWakeupState | null;
  reply_to_message_id?: string | null;
  reply_to?: { id: string; content: string; from: string; message_type: string; status: string } | null;
  context?: Record<string, unknown>;
  /** Immutable create envelope. Retries must replay this exact payload. */
  payload?: StrategicSessionMessageCreate;
}

export function loadPendingConversationSend(
  storage: Storage,
  key: string,
  expected?: { actorUserId: string; projectId: string },
): PendingConversationSend | null {
  try {
    const raw = storage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<PendingConversationSend>;
    if (typeof parsed.key !== 'string' || typeof parsed.content !== 'string'
      || typeof parsed.created_at !== 'string' || typeof parsed.message_type !== 'string') return null;
    if (expected && (
      parsed.actor_user_id !== expected.actorUserId
      || parsed.project_id !== expected.projectId
    )) return null;
    return { ...parsed, state: parsed.state ?? 'sending' } as PendingConversationSend;
  } catch {
    return null;
  }
}

export function savePendingConversationSend(
  storage: Storage,
  key: string,
  pending: PendingConversationSend | null,
): void {
  try {
    if (pending) {
      const stripTransientUrls = (value: unknown): unknown => Array.isArray(value)
        ? value.map(item => {
            if (!item || typeof item !== 'object' || Array.isArray(item)) return item;
            const durable = { ...(item as Record<string, unknown>) };
            delete durable.url;
            delete durable.thumbnail_url;
            return durable;
          })
        : value;
      const durable = {
        ...pending,
        context: pending.context ? {
          ...pending.context,
          attachments: stripTransientUrls(pending.context.attachments),
        } : pending.context,
        payload: pending.payload ? {
          ...pending.payload,
          attachments: stripTransientUrls(pending.payload.attachments),
        } : pending.payload,
      };
      storage.setItem(key, JSON.stringify(durable));
    }
    else storage.removeItem(key);
  } catch {
    // Storage is a durability aid; quota/privacy failures must not block send.
  }
}
