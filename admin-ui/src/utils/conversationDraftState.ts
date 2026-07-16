export interface ConversationDraftReplyTarget {
  id: string;
  content: string;
  from: string;
  message_type: string;
  status: string;
}

export interface ComposerDraftEnvelope {
  version: 2;
  actor_user_id: string;
  project_id: string;
  type: string;
  content: string;
  reply_to: ConversationDraftReplyTarget | null;
  source_refs: Array<Record<string, unknown>>;
  artifact_refs: Array<Record<string, unknown>>;
  attachment_send_key: string | null;
}

function key(actorUserId?: string | null, projectId?: string | null): string {
  return `messages_composer_draft:${actorUserId || 'anonymous'}:${projectId || 'unknown'}`;
}

export function loadComposerDraft(actorUserId?: string | null, projectId?: string | null): ComposerDraftEnvelope | null {
  try {
    const raw = localStorage.getItem(key(actorUserId, projectId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<ComposerDraftEnvelope>;
    if (typeof parsed.type !== 'string' || typeof parsed.content !== 'string') return null;
    if (parsed.actor_user_id && parsed.actor_user_id !== actorUserId) return null;
    if (parsed.project_id && parsed.project_id !== projectId) return null;
    return {
      version: 2,
      actor_user_id: actorUserId ?? 'anonymous',
      project_id: projectId ?? 'unknown',
      type: parsed.type,
      content: parsed.content,
      reply_to: parsed.reply_to ?? null,
      source_refs: Array.isArray(parsed.source_refs) ? parsed.source_refs : [],
      artifact_refs: Array.isArray(parsed.artifact_refs) ? parsed.artifact_refs : [],
      attachment_send_key: typeof parsed.attachment_send_key === 'string' ? parsed.attachment_send_key : null,
    };
  } catch { return null; }
}

export function saveComposerDraft(actorUserId: string | null | undefined, projectId: string | null | undefined, draft: Omit<ComposerDraftEnvelope, 'version' | 'actor_user_id' | 'project_id'>): void {
  if (!draft.content && !draft.reply_to && !draft.attachment_send_key) {
    clearComposerDraft(actorUserId, projectId);
    return;
  }
  try {
    localStorage.setItem(key(actorUserId, projectId), JSON.stringify({
      ...draft,
      version: 2,
      actor_user_id: actorUserId ?? 'anonymous',
      project_id: projectId ?? 'unknown',
    } satisfies ComposerDraftEnvelope));
  } catch { /* storage is a durability aid */ }
}

export function clearComposerDraft(actorUserId: string | null | undefined, projectId: string | null | undefined): void {
  try { localStorage.removeItem(key(actorUserId, projectId)); } catch { /* ignore */ }
}
