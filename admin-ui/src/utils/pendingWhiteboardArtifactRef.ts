import {
  createResidentToolArtifactRef,
  ToolArtifactConflictError,
  ToolArtifactRequestError,
  type ResidentRenderedImageReceipt,
  type ResidentToolArtifactCreateResponse,
  type ResidentToolArtifactRef,
} from '../api/projectTools';

const STORAGE_PREFIX = 'resident_whiteboard_artifact_operation:v1';
const STORAGE_TTL_MS = 7 * 24 * 60 * 60 * 1000;
const DEFAULT_MAX_ATTEMPTS = 2;

export type WhiteboardArtifactOperationState = 'pending' | 'terminal';

export interface PendingWhiteboardArtifactOperation {
  version: 1;
  operation_id: string;
  actor_user_id: string;
  project_id: string;
  tool_id: string;
  tool_title: string;
  revision: number;
  intent: 'share' | 'ask';
  prompt: string;
  receipt_id: string;
  rendered_image: ResidentRenderedImageReceipt;
  state: WhiteboardArtifactOperationState;
  promotion_intent_id?: string;
  retry_after_ms?: number;
  next_retry_at?: number;
  attempt_count: number;
  last_error?: string;
  created_at: number;
  updated_at: number;
}

export type WhiteboardArtifactSettlement =
  | { state: 'ready'; artifactRef: ResidentToolArtifactRef; operation: PendingWhiteboardArtifactOperation }
  | { state: 'waiting' | 'terminal'; operation: PendingWhiteboardArtifactOperation };

interface CreateOperationInput {
  actorUserId: string;
  projectId: string;
  toolId: string;
  toolTitle: string;
  revision: number;
  intent: 'share' | 'ask';
  prompt: string;
  renderedImage: ResidentRenderedImageReceipt;
}

interface SettleOptions {
  maxAttempts?: number;
  request?: (
    projectId: string,
    toolId: string,
    revision: number,
    renderedImage: ResidentRenderedImageReceipt,
  ) => Promise<ResidentToolArtifactCreateResponse>;
  wait?: (delayMs: number) => Promise<void>;
}

const activeSettlements = new Map<string, Promise<WhiteboardArtifactSettlement>>();

function scopePrefix(actorUserId: string, projectId: string, toolId: string): string {
  return `${STORAGE_PREFIX}:${encodeURIComponent(actorUserId)}:${encodeURIComponent(projectId)}:${encodeURIComponent(toolId)}:`;
}

function storageKey(operation: Pick<PendingWhiteboardArtifactOperation,
  'actor_user_id' | 'project_id' | 'tool_id' | 'receipt_id'>): string {
  return `${scopePrefix(operation.actor_user_id, operation.project_id, operation.tool_id)}${encodeURIComponent(operation.receipt_id)}`;
}

function parseOperation(
  raw: string,
  actorUserId: string,
  projectId: string,
  toolId: string,
): PendingWhiteboardArtifactOperation | null {
  try {
    const value = JSON.parse(raw) as Partial<PendingWhiteboardArtifactOperation>;
    if (
      value.version !== 1
      || typeof value.operation_id !== 'string'
      || value.actor_user_id !== actorUserId
      || value.project_id !== projectId
      || value.tool_id !== toolId
      || typeof value.tool_title !== 'string'
      || !Number.isSafeInteger(value.revision)
      || (value.intent !== 'share' && value.intent !== 'ask')
      || typeof value.prompt !== 'string'
      || typeof value.receipt_id !== 'string'
      || !value.rendered_image
      || value.rendered_image.attachment_id !== value.receipt_id
      || (value.state !== 'pending' && value.state !== 'terminal')
      || !Number.isSafeInteger(value.attempt_count)
      || typeof value.created_at !== 'number'
      || typeof value.updated_at !== 'number'
      || value.updated_at < Date.now() - STORAGE_TTL_MS
    ) return null;
    return value as PendingWhiteboardArtifactOperation;
  } catch {
    return null;
  }
}

export function createPendingWhiteboardArtifactOperation(
  input: CreateOperationInput,
): PendingWhiteboardArtifactOperation {
  const now = Date.now();
  return {
    version: 1,
    operation_id: globalThis.crypto?.randomUUID?.()
      ?? `${now}-${Math.random().toString(36).slice(2)}`,
    actor_user_id: input.actorUserId,
    project_id: input.projectId,
    tool_id: input.toolId,
    tool_title: input.toolTitle,
    revision: input.revision,
    intent: input.intent,
    prompt: input.prompt,
    receipt_id: input.renderedImage.attachment_id,
    rendered_image: input.renderedImage,
    state: 'pending',
    attempt_count: 0,
    created_at: now,
    updated_at: now,
  };
}

export function persistPendingWhiteboardArtifactOperation(
  operation: PendingWhiteboardArtifactOperation,
): boolean {
  try {
    localStorage.setItem(storageKey(operation), JSON.stringify(operation));
    return true;
  } catch {
    return false;
  }
}

export function removePendingWhiteboardArtifactOperation(
  operation: PendingWhiteboardArtifactOperation,
): void {
  try {
    const key = storageKey(operation);
    const raw = localStorage.getItem(key);
    const current = raw
      ? parseOperation(raw, operation.actor_user_id, operation.project_id, operation.tool_id)
      : null;
    if (current?.operation_id === operation.operation_id) localStorage.removeItem(key);
  } catch { /* retry on a later mount */ }
}

export function loadPendingWhiteboardArtifactOperations(
  actorUserId: string,
  projectId: string,
  toolId: string,
): PendingWhiteboardArtifactOperation[] {
  const prefix = scopePrefix(actorUserId, projectId, toolId);
  const operations: PendingWhiteboardArtifactOperation[] = [];
  try {
    for (let index = localStorage.length - 1; index >= 0; index -= 1) {
      const key = localStorage.key(index);
      if (!key?.startsWith(prefix)) continue;
      const raw = localStorage.getItem(key);
      const operation = raw ? parseOperation(raw, actorUserId, projectId, toolId) : null;
      if (operation) operations.push(operation);
      else localStorage.removeItem(key);
    }
  } catch {
    return [];
  }
  return operations.sort((left, right) => left.created_at - right.created_at);
}

function isRetryable(cause: unknown): boolean {
  if (cause instanceof ToolArtifactConflictError) return false;
  if (cause instanceof ToolArtifactRequestError) return cause.retryable;
  return true;
}

async function settle(
  initial: PendingWhiteboardArtifactOperation,
  options: SettleOptions,
): Promise<WhiteboardArtifactSettlement> {
  const request = options.request ?? createResidentToolArtifactRef;
  const wait = options.wait ?? (delayMs => new Promise(resolve => setTimeout(resolve, delayMs)));
  const maxAttempts = Math.max(1, Math.min(10, options.maxAttempts ?? DEFAULT_MAX_ATTEMPTS));
  let operation = initial;
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    const remainingDelayMs = Math.max(0, (operation.next_retry_at ?? 0) - Date.now());
    if (remainingDelayMs > 0) await wait(remainingDelayMs);
    operation = {
      ...operation,
      state: 'pending',
      next_retry_at: undefined,
      attempt_count: operation.attempt_count + 1,
      updated_at: Date.now(),
    };
    persistPendingWhiteboardArtifactOperation(operation);
    try {
      const response = await request(
        operation.project_id,
        operation.tool_id,
        operation.revision,
        operation.rendered_image,
      );
      if (response.state === 'ready') {
        return { state: 'ready', artifactRef: response.artifact_ref, operation };
      }
      if (response.receipt_id !== operation.receipt_id) {
        operation = { ...operation, state: 'terminal', last_error: 'Server returned a different Whiteboard upload receipt.', updated_at: Date.now() };
        persistPendingWhiteboardArtifactOperation(operation);
        return { state: 'terminal', operation };
      }
      const retryDelayMs = Math.max(
        2_500,
        response.retry_after_ms,
        Math.min(2_500 * (2 ** Math.max(0, operation.attempt_count - 1)), 30_000),
      );
      operation = {
        ...operation,
        promotion_intent_id: response.promotion_intent_id,
        retry_after_ms: response.retry_after_ms,
        next_retry_at: Date.now() + retryDelayMs,
        last_error: undefined,
        updated_at: Date.now(),
      };
      persistPendingWhiteboardArtifactOperation(operation);
    } catch (cause) {
      const retryDelayMs = Math.max(
        2_500,
        operation.retry_after_ms ?? 0,
        Math.min(2_500 * (2 ** Math.max(0, operation.attempt_count - 1)), 30_000),
      );
      operation = {
        ...operation,
        last_error: cause instanceof Error ? cause.message : 'Whiteboard preparation failed.',
        next_retry_at: Date.now() + retryDelayMs,
        updated_at: Date.now(),
      };
      if (!isRetryable(cause)) {
        operation = { ...operation, state: 'terminal' };
        persistPendingWhiteboardArtifactOperation(operation);
        return { state: 'terminal', operation };
      }
      persistPendingWhiteboardArtifactOperation(operation);
    }
  }
  return { state: 'waiting', operation };
}

export function settlePendingWhiteboardArtifactOperation(
  operation: PendingWhiteboardArtifactOperation,
  options: SettleOptions = {},
): Promise<WhiteboardArtifactSettlement> {
  const active = activeSettlements.get(operation.operation_id);
  if (active) return active;
  const promise = settle(operation, options).finally(() => {
    if (activeSettlements.get(operation.operation_id) === promise) activeSettlements.delete(operation.operation_id);
  });
  activeSettlements.set(operation.operation_id, promise);
  return promise;
}

export function isPendingWhiteboardArtifactStorageKey(
  key: string | null,
  actorUserId: string,
  projectId: string,
  toolId: string,
): boolean {
  return Boolean(key?.startsWith(scopePrefix(actorUserId, projectId, toolId)));
}
