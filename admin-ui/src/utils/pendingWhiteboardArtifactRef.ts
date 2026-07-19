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
const MAX_SCOPED_OPERATIONS = 20;
// The server already performs a bounded reconciliation wait before returning
// 202. One immediate replay covers the common completion/lost-response path
// without leaving the composer-looking control busy for several minutes.
const DEFAULT_MAX_ATTEMPTS = 2;
const MIN_RETRY_MS = 250;
const MAX_RETRY_MS = 30_000;

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
  lane_owner?: string;
  prompt: string;
  receipt_id: string;
  rendered_image: ResidentRenderedImageReceipt;
  state: WhiteboardArtifactOperationState;
  promotion_intent_id?: string;
  retry_after_ms?: number;
  attempt_count: number;
  last_error?: string;
  created_at: number;
  updated_at: number;
}

export type WhiteboardArtifactSettlement =
  | {
      state: 'ready';
      artifactRef: ResidentToolArtifactRef;
      operation: PendingWhiteboardArtifactOperation;
    }
  | {
      state: 'waiting' | 'terminal';
      operation: PendingWhiteboardArtifactOperation;
    };

interface CreateOperationInput {
  actorUserId: string;
  projectId: string;
  toolId: string;
  toolTitle: string;
  revision: number;
  intent: 'share' | 'ask';
  laneOwner?: string;
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

function encode(value: string): string {
  return encodeURIComponent(value);
}

function scopePrefix(actorUserId: string, projectId: string, toolId: string): string {
  return `${STORAGE_PREFIX}:${encode(actorUserId)}:${encode(projectId)}:${encode(toolId)}:`;
}

function storageKey(operation: Pick<PendingWhiteboardArtifactOperation,
  'actor_user_id' | 'project_id' | 'tool_id' | 'receipt_id'>): string {
  return `${scopePrefix(operation.actor_user_id, operation.project_id, operation.tool_id)}${encode(operation.receipt_id)}`;
}

function newOperationId(): string {
  return globalThis.crypto?.randomUUID?.()
    ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function isRenderedImage(value: unknown): value is ResidentRenderedImageReceipt {
  if (!value || typeof value !== 'object') return false;
  const image = value as Partial<ResidentRenderedImageReceipt>;
  return typeof image.attachment_id === 'string'
    && image.attachment_id.length > 0
    && typeof image.storage_uri === 'string'
    && image.storage_uri.length > 0
    && typeof image.checksum === 'string'
    && image.checksum.length > 0;
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
      || typeof value.revision !== 'number'
      || !Number.isSafeInteger(value.revision)
      || (value.intent !== 'share' && value.intent !== 'ask')
      || typeof value.prompt !== 'string'
      || typeof value.receipt_id !== 'string'
      || !isRenderedImage(value.rendered_image)
      || value.rendered_image.attachment_id !== value.receipt_id
      || (value.state !== 'pending' && value.state !== 'terminal')
      || !Number.isSafeInteger(value.attempt_count)
      || (value.attempt_count ?? -1) < 0
      || typeof value.created_at !== 'number'
      || !Number.isFinite(value.created_at)
      || typeof value.updated_at !== 'number'
      || !Number.isFinite(value.updated_at)
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
    operation_id: newOperationId(),
    actor_user_id: input.actorUserId,
    project_id: input.projectId,
    tool_id: input.toolId,
    tool_title: input.toolTitle,
    revision: input.revision,
    intent: input.intent,
    ...(input.laneOwner ? { lane_owner: input.laneOwner } : {}),
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
  } catch {
    // A later mount can replay a ready response and acknowledge the handoff.
  }
}

export function loadPendingWhiteboardArtifactOperations(
  actorUserId: string,
  projectId: string,
  toolId: string,
): PendingWhiteboardArtifactOperation[] {
  const prefix = scopePrefix(actorUserId, projectId, toolId);
  const operations: PendingWhiteboardArtifactOperation[] = [];
  const invalidKeys: string[] = [];
  try {
    for (let index = 0; index < localStorage.length; index += 1) {
      const key = localStorage.key(index);
      if (!key?.startsWith(prefix)) continue;
      const raw = localStorage.getItem(key);
      const operation = raw ? parseOperation(raw, actorUserId, projectId, toolId) : null;
      if (operation) operations.push(operation);
      else invalidKeys.push(key);
    }
    for (const key of invalidKeys) localStorage.removeItem(key);
  } catch {
    return [];
  }
  operations.sort((left, right) => left.created_at - right.created_at
    || left.operation_id.localeCompare(right.operation_id));
  const excess = operations.slice(0, Math.max(0, operations.length - MAX_SCOPED_OPERATIONS));
  for (const operation of excess) removePendingWhiteboardArtifactOperation(operation);
  return operations.slice(-MAX_SCOPED_OPERATIONS);
}

function retryDelay(responseDelay: number | undefined, attempt: number): number {
  const exponential = 500 * (2 ** Math.max(0, attempt - 1));
  return Math.min(MAX_RETRY_MS, Math.max(MIN_RETRY_MS, responseDelay ?? exponential));
}

function errorMessage(cause: unknown): string {
  return cause instanceof Error ? cause.message : 'Whiteboard preparation failed.';
}

function isRetryable(cause: unknown): boolean {
  if (cause instanceof ToolArtifactConflictError) return false;
  if (cause instanceof ToolArtifactRequestError) return cause.retryable;
  // A fetch rejection has no reliable effect boundary. Retrying the exact
  // receipt and body is the only safe way to reconcile a lost 201 response.
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
    operation = {
      ...operation,
      state: 'pending',
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
        operation = {
          ...operation,
          state: 'terminal',
          last_error: 'Server returned a different Whiteboard upload receipt.',
          updated_at: Date.now(),
        };
        persistPendingWhiteboardArtifactOperation(operation);
        return { state: 'terminal', operation };
      }
      operation = {
        ...operation,
        promotion_intent_id: response.promotion_intent_id,
        retry_after_ms: response.retry_after_ms,
        last_error: undefined,
        updated_at: Date.now(),
      };
      persistPendingWhiteboardArtifactOperation(operation);
      if (attempt < maxAttempts) await wait(retryDelay(response.retry_after_ms, attempt));
    } catch (cause) {
      operation = {
        ...operation,
        last_error: errorMessage(cause),
        updated_at: Date.now(),
      };
      if (!isRetryable(cause)) {
        operation = { ...operation, state: 'terminal' };
        persistPendingWhiteboardArtifactOperation(operation);
        return { state: 'terminal', operation };
      }
      persistPendingWhiteboardArtifactOperation(operation);
      if (attempt < maxAttempts) await wait(retryDelay(operation.retry_after_ms, attempt));
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
    if (activeSettlements.get(operation.operation_id) === promise) {
      activeSettlements.delete(operation.operation_id);
    }
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
