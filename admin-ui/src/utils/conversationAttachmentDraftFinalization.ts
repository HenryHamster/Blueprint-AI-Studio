import type { ConversationDraftReplyTarget } from './conversationDraftState';
import { saveComposerDraft, type ComposerDraftSaveResult } from './conversationDraftState';

export interface ConversationComposerIntent {
  type: string;
  content: string;
  reply_to: ConversationDraftReplyTarget | null;
  source_refs: Array<Record<string, unknown>>;
  artifact_refs: Array<Record<string, unknown>>;
  operator_intent?: Record<string, unknown>;
  attachment_send_key: string | null;
}

export function updateConversationComposerIntent(
  current: ConversationComposerIntent,
  patch: Partial<ConversationComposerIntent>,
): ConversationComposerIntent {
  return { ...current, ...patch };
}

export function finalizeLastAttachmentRemoval(
  actorUserId: string,
  projectId: string,
  intent: ConversationComposerIntent,
  clearIdentity: () => void,
  previousWriteId?: string | null,
): ComposerDraftSaveResult {
  const result = saveComposerDraft(actorUserId, projectId, intent, undefined, previousWriteId);
  if (!intent.content.trim() && !intent.reply_to) clearIdentity();
  return result;
}
