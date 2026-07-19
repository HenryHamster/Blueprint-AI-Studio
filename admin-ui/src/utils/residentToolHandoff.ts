import type { ResidentToolArtifactRef } from '../api/projectTools';

export const RESIDENT_TOOL_HANDOFF_EVENT = 'blueprint:resident-tool-handoff';
const QUEUE_PREFIX = 'resident_tool_handoff_entry';
const QUEUE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
let lastQueuedAt = 0;

export interface ResidentToolHandoffDetail {
  projectId: string;
  artifactRef: ResidentToolArtifactRef;
  intent: 'share' | 'ask';
  prompt: string;
  laneOwner?: string;
}

interface StoredResidentToolHandoff {
  version: 1;
  entry_id: string;
  queued_at: number;
  detail: ResidentToolHandoffDetail;
}

export interface ResidentToolHandoffQueueEntry {
  entryId: string;
  identity: string;
  storageKey: string;
  queuedAt: number;
  detail: ResidentToolHandoffDetail;
}

function queueScopePrefix(actorUserId: string, projectId: string): string {
  return `${QUEUE_PREFIX}:${encodeURIComponent(actorUserId)}:${encodeURIComponent(projectId)}:`;
}

function artifactIdentity(detail: ResidentToolHandoffDetail): string {
  return detail.artifactRef.artifact_ref_id || detail.artifactRef.id;
}

function queuedHandoffKey(actorUserId: string, projectId: string, entryId: string): string {
  return `${queueScopePrefix(actorUserId, projectId)}${encodeURIComponent(entryId)}`;
}

function newEntryId(): string {
  return globalThis.crypto?.randomUUID?.()
    ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function newQueuedAt(): number {
  const clock = typeof performance !== 'undefined'
    ? Math.floor((performance.timeOrigin + performance.now()) * 1000)
    : Date.now() * 1000;
  lastQueuedAt = Math.max(clock, lastQueuedAt + 1);
  return lastQueuedAt;
}

function validDetail(
  value: unknown,
  actorUserId?: string,
  projectId?: string,
): value is ResidentToolHandoffDetail {
  if (!value || typeof value !== 'object') return false;
  const detail = value as Partial<ResidentToolHandoffDetail>;
  const ref = detail.artifactRef as Partial<ResidentToolArtifactRef> | undefined;
  return Boolean(
    typeof detail.projectId === 'string'
    && (!projectId || detail.projectId === projectId)
    && typeof detail.prompt === 'string'
    && (detail.intent === 'share' || detail.intent === 'ask')
    && ref
    && typeof ref.id === 'string'
    && typeof ref.project_id === 'string'
    && ref.project_id === detail.projectId
    && typeof ref.author === 'string'
    && ref.author.length > 0
    && (!actorUserId || ref.author === actorUserId),
  );
}

function readStoredEntry(
  storageKey: string,
  actorUserId: string,
  projectId: string,
): ResidentToolHandoffQueueEntry | null {
  try {
    const raw = localStorage.getItem(storageKey);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredResidentToolHandoff>;
    if (
      parsed.version !== 1
      || typeof parsed.entry_id !== 'string'
      || typeof parsed.queued_at !== 'number'
      || parsed.queued_at < (Date.now() - QUEUE_TTL_MS) * 1000
      || !validDetail(parsed.detail, actorUserId, projectId)
    ) return null;
    return {
      entryId: parsed.entry_id,
      identity: artifactIdentity(parsed.detail),
      storageKey,
      queuedAt: parsed.queued_at,
      detail: parsed.detail,
    };
  } catch {
    return null;
  }
}

export function isResidentToolHandoffQueueStorageKey(
  storageKey: string | null,
  actorUserId: string,
  projectId: string,
): boolean {
  return Boolean(storageKey?.startsWith(queueScopePrefix(actorUserId, projectId)));
}

export function loadQueuedResidentToolHandoffEntries(
  actorUserId: string | null | undefined,
  projectId: string | null | undefined,
): ResidentToolHandoffQueueEntry[] {
  if (!actorUserId || !projectId) return [];
  const prefix = queueScopePrefix(actorUserId, projectId);
  const entries: ResidentToolHandoffQueueEntry[] = [];
  const invalidKeys: string[] = [];
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const storageKey = localStorage.key(index);
      if (!storageKey?.startsWith(prefix)) continue;
      const entry = readStoredEntry(storageKey, actorUserId, projectId);
      if (entry) entries.push(entry);
      else invalidKeys.push(storageKey);
    }
  } catch {
    return [];
  }
  for (const storageKey of invalidKeys) {
    try { localStorage.removeItem(storageKey); } catch { /* retry next load */ }
  }
  const sorted = entries.sort((left, right) => (
    left.queuedAt - right.queuedAt || left.entryId.localeCompare(right.entryId)
  ));
  const latestByIdentity = new Map<string, ResidentToolHandoffQueueEntry>();
  for (const entry of sorted) latestByIdentity.set(entry.identity, entry);
  const latest = new Set(latestByIdentity.values());
  for (const entry of sorted) {
    if (latest.has(entry)) continue;
    try { localStorage.removeItem(entry.storageKey); } catch { /* retry next load */ }
  }
  return sorted.filter(entry => latest.has(entry));
}

export function loadQueuedResidentToolHandoffs(
  actorUserId: string | null | undefined,
  projectId: string | null | undefined,
): ResidentToolHandoffDetail[] {
  return loadQueuedResidentToolHandoffEntries(actorUserId, projectId).map(entry => entry.detail);
}

export function enqueueResidentToolHandoff(
  actorUserId: string,
  projectId: string,
  detail: ResidentToolHandoffDetail,
): boolean {
  if (!actorUserId || !projectId) return false;
  if (!validDetail(detail, actorUserId, projectId)) return false;
  const identity = artifactIdentity(detail);
  if (!identity) return false;
  const stored: StoredResidentToolHandoff = {
    version: 1,
    entry_id: newEntryId(),
    queued_at: newQueuedAt(),
    detail,
  };
  try {
    // Immutable per-event keys avoid read-modify-write and replacement races
    // between tabs. Load-time projection selects the latest intent per ref.
    localStorage.setItem(
      queuedHandoffKey(actorUserId, projectId, stored.entry_id),
      JSON.stringify(stored),
    );
    return true;
  } catch {
    return false;
  }
}

export function acknowledgeQueuedResidentToolHandoffs(
  actorUserId: string,
  projectId: string,
  entries: ResidentToolHandoffQueueEntry[],
): void {
  for (const entry of entries) {
    if (!isResidentToolHandoffQueueStorageKey(entry.storageKey, actorUserId, projectId)) continue;
    // Compare the version token before removing. A second tab may have replaced
    // this ArtifactRef entry after our snapshot was applied to the draft.
    const current = readStoredEntry(entry.storageKey, actorUserId, projectId);
    if (current?.entryId !== entry.entryId) continue;
    try { localStorage.removeItem(entry.storageKey); } catch { /* retry on next mount */ }
  }
}

export function dispatchResidentToolHandoff(detail: ResidentToolHandoffDetail): boolean {
  // Persist before notifying. Project tools and the conversation are separate
  // routed surfaces, so the MessagesPanel listener is normally unmounted while
  // Share/Ask is clicked.
  if (!enqueueResidentToolHandoff(detail.artifactRef.author, detail.projectId, detail)) {
    return false;
  }
  window.dispatchEvent(new CustomEvent<ResidentToolHandoffDetail>(RESIDENT_TOOL_HANDOFF_EVENT, { detail }));
  return true;
}

export function isResidentToolHandoffEvent(event: Event): event is CustomEvent<ResidentToolHandoffDetail> {
  if (!(event instanceof CustomEvent)) return false;
  return validDetail(event.detail);
}
