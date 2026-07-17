export interface ConversationDraftReplyTarget {
  id: string;
  content: string;
  from: string;
  message_type: string;
  status: string;
}

export interface ComposerDraftEnvelope {
  version: 4;
  write_id: string;
  written_at: number;
  actor_user_id: string;
  project_id: string;
  type: string;
  content: string;
  reply_to: ConversationDraftReplyTarget | null;
  source_refs: Array<Record<string, unknown>>;
  artifact_refs: Array<Record<string, unknown>>;
  operator_intent?: Record<string, unknown>;
  attachment_send_key: string | null;
}

export type ComposerDraftSaveResult = string | null | false;

const LEGACY_PREFIX = 'messages_composer_draft';
const ENTRY_PREFIX = 'messages_composer_draft_entry';
const ENTRY_TTL_MS = 14 * 24 * 60 * 60 * 1000;
const MAX_SCOPED_HISTORY = 8;
let lastWrittenAt = 0;

function normalizedActor(actorUserId?: string | null): string {
  return actorUserId || 'anonymous';
}

function normalizedProject(projectId?: string | null): string {
  return projectId || 'unknown';
}

function legacyKey(actorUserId?: string | null, projectId?: string | null): string {
  return `${LEGACY_PREFIX}:${normalizedActor(actorUserId)}:${normalizedProject(projectId)}`;
}

function scopePrefix(actorUserId?: string | null, projectId?: string | null): string {
  return `${ENTRY_PREFIX}:${encodeURIComponent(normalizedActor(actorUserId))}:${encodeURIComponent(normalizedProject(projectId))}:`;
}

function entryKey(actorUserId: string | null | undefined, projectId: string | null | undefined, writeId: string): string {
  return `${scopePrefix(actorUserId, projectId)}${encodeURIComponent(writeId)}`;
}

export function createComposerDraftWriteId(): string {
  return globalThis.crypto?.randomUUID?.()
    ?? `draft-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function nextWrittenAt(): number {
  const clock = typeof performance !== 'undefined'
    ? Math.floor((performance.timeOrigin + performance.now()) * 1000)
    : Date.now() * 1000;
  lastWrittenAt = Math.max(clock, lastWrittenAt + 1);
  return lastWrittenAt;
}

function legacyWriteId(raw: string): string {
  let hash = 2166136261;
  for (let index = 0; index < raw.length; index += 1) {
    hash ^= raw.charCodeAt(index);
    hash = Math.imul(hash, 16777619);
  }
  return `legacy-${raw.length}-${(hash >>> 0).toString(16)}`;
}

function parseEnvelope(
  raw: string,
  actorUserId: string | null | undefined,
  projectId: string | null | undefined,
  immutableEntry: boolean,
): ComposerDraftEnvelope | null {
  try {
    const parsed = JSON.parse(raw) as Partial<ComposerDraftEnvelope>;
    if (typeof parsed.type !== 'string' || typeof parsed.content !== 'string') return null;
    const actor = normalizedActor(actorUserId), project = normalizedProject(projectId);
    if (parsed.actor_user_id && parsed.actor_user_id !== actor) return null;
    if (parsed.project_id && parsed.project_id !== project) return null;
    if (immutableEntry && (
      parsed.version !== 4
      || typeof parsed.write_id !== 'string'
      || !parsed.write_id
      || typeof parsed.written_at !== 'number'
      || !Number.isFinite(parsed.written_at)
    )) return null;
    return {
      version: 4,
      write_id: typeof parsed.write_id === 'string' && parsed.write_id
        ? parsed.write_id
        : legacyWriteId(raw),
      written_at: typeof parsed.written_at === 'number' && Number.isFinite(parsed.written_at)
        ? parsed.written_at
        : 0,
      actor_user_id: actor,
      project_id: project,
      type: parsed.type,
      content: parsed.content,
      reply_to: parsed.reply_to ?? null,
      source_refs: Array.isArray(parsed.source_refs) ? parsed.source_refs : [],
      artifact_refs: Array.isArray(parsed.artifact_refs) ? parsed.artifact_refs : [],
      operator_intent: parsed.operator_intent && typeof parsed.operator_intent === 'object'
        ? parsed.operator_intent
        : undefined,
      attachment_send_key: typeof parsed.attachment_send_key === 'string' ? parsed.attachment_send_key : null,
    };
  } catch { return null; }
}

export function loadComposerDraft(actorUserId?: string | null, projectId?: string | null): ComposerDraftEnvelope | null {
  const prefix = scopePrefix(actorUserId, projectId);
  const entries: Array<{ key: string; envelope: ComposerDraftEnvelope }> = [];
  const removeKeys: string[] = [];
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const storageKey = localStorage.key(index);
      if (!storageKey?.startsWith(prefix)) continue;
      const raw = localStorage.getItem(storageKey);
      const envelope = raw ? parseEnvelope(raw, actorUserId, projectId, true) : null;
      if (!envelope || envelope.written_at < (Date.now() - ENTRY_TTL_MS) * 1000) {
        removeKeys.push(storageKey);
      } else {
        entries.push({ key: storageKey, envelope });
      }
    }
  } catch { return null; }
  entries.sort((left, right) => (
    right.envelope.written_at - left.envelope.written_at
    || right.envelope.write_id.localeCompare(left.envelope.write_id)
  ));
  for (const stale of entries.slice(MAX_SCOPED_HISTORY)) removeKeys.push(stale.key);
  for (const storageKey of removeKeys) {
    try { localStorage.removeItem(storageKey); } catch { /* retry on a later load */ }
  }
  if (entries.length > 0) return entries[0].envelope;
  try {
    const raw = localStorage.getItem(legacyKey(actorUserId, projectId));
    return raw ? parseEnvelope(raw, actorUserId, projectId, false) : null;
  } catch { return null; }
}

export function saveComposerDraft(
  actorUserId: string | null | undefined,
  projectId: string | null | undefined,
  draft: Omit<ComposerDraftEnvelope, 'version' | 'write_id' | 'written_at' | 'actor_user_id' | 'project_id'>,
  writeId = createComposerDraftWriteId(),
  previousWriteId?: string | null,
): ComposerDraftSaveResult {
  if (
    !draft.content
    && !draft.reply_to
    && !draft.attachment_send_key
    && draft.source_refs.length === 0
    && draft.artifact_refs.length === 0
    && !draft.operator_intent
  ) {
    if (previousWriteId) clearComposerDraftIfCurrent(actorUserId, projectId, previousWriteId);
    return null;
  }
  const envelope: ComposerDraftEnvelope = {
    ...draft,
    version: 4,
    write_id: writeId,
    written_at: nextWrittenAt(),
    actor_user_id: normalizedActor(actorUserId),
    project_id: normalizedProject(projectId),
  };
  try {
    // Persist the new immutable entry first. Deleting the exact previous entry
    // cannot consume a concurrent tab's distinct write key.
    localStorage.setItem(entryKey(actorUserId, projectId, writeId), JSON.stringify(envelope));
    if (previousWriteId && previousWriteId !== writeId) {
      clearComposerDraftIfCurrent(actorUserId, projectId, previousWriteId);
    }
    return writeId;
  } catch { return false; }
}

export function clearComposerDraft(actorUserId: string | null | undefined, projectId: string | null | undefined): boolean {
  const prefix = scopePrefix(actorUserId, projectId);
  try {
    const keys: string[] = [];
    for (let index = 0; index < localStorage.length; index += 1) {
      const storageKey = localStorage.key(index);
      if (storageKey?.startsWith(prefix)) keys.push(storageKey);
    }
    for (const storageKey of keys) localStorage.removeItem(storageKey);
    localStorage.removeItem(legacyKey(actorUserId, projectId));
    return true;
  } catch { return false; }
}

export function clearComposerDraftIfCurrent(
  actorUserId: string | null | undefined,
  projectId: string | null | undefined,
  expectedWriteId: string | null | undefined,
): boolean {
  if (!expectedWriteId) return false;
  const storageKey = entryKey(actorUserId, projectId, expectedWriteId);
  try {
    if (localStorage.getItem(storageKey) !== null) {
      localStorage.removeItem(storageKey);
      return true;
    }
    // Compatibility for single-key v2/v3 drafts during rollout. New writes
    // never share this key, so an immutable concurrent entry remains safe.
    const oldKey = legacyKey(actorUserId, projectId);
    const raw = localStorage.getItem(oldKey);
    const legacy = raw ? parseEnvelope(raw, actorUserId, projectId, false) : null;
    if (legacy?.write_id !== expectedWriteId) return false;
    localStorage.removeItem(oldKey);
    return true;
  } catch { return false; }
}
