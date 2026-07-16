import type { ConversationDraftReplyTarget } from './conversationDraftState';
import { saveComposerDraft } from './conversationDraftState';

export interface ConversationComposerIntent {
  type: string;
  content: string;
  reply_to: ConversationDraftReplyTarget | null;
  source_refs: Array<Record<string, unknown>>;
  artifact_refs: Array<Record<string, unknown>>;
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
): void {
  saveComposerDraft(actorUserId, projectId, intent);
  if (!intent.content.trim() && !intent.reply_to) clearIdentity();
}
