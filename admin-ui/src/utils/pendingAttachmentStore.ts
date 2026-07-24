export interface StoredPendingAttachment {
  id: string;
  file: File;
  mime_type: string;
  name: string;
  size_bytes: number;
}

interface PendingAttachmentRecord extends StoredPendingAttachment {
  key: string;
  actor_user_id: string;
  project_id: string;
  send_key: string;
  updated_at: number;
  cleanup_requested?: boolean;
}

const DB_NAME = 'blueprint-composer';
const STORE_NAME = 'pending-attachments';
const DB_VERSION = 1;
const memoryFallback = new Map<string, PendingAttachmentRecord>();
let databasePromise: Promise<IDBDatabase> | null = null;

function scopePrefix(actorUserId: string, projectId: string, sendKey: string): string {
  return `${encodeURIComponent(actorUserId)}:${encodeURIComponent(projectId)}:${encodeURIComponent(sendKey)}:`;
}

function recordKey(actorUserId: string, projectId: string, sendKey: string, attachmentId: string): string {
  return `${scopePrefix(actorUserId, projectId, sendKey)}${encodeURIComponent(attachmentId)}`;
}

function openDatabase(): Promise<IDBDatabase> {
  if (databasePromise) return databasePromise;
  databasePromise = new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE_NAME)) {
        request.result.createObjectStore(STORE_NAME, { keyPath: 'key' });
      }
    };
    request.onsuccess = () => {
      request.result.onversionchange = () => {
        request.result.close();
        databasePromise = null;
      };
      resolve(request.result);
    };
    request.onerror = () => {
      databasePromise = null;
      reject(request.error ?? new Error('Could not open pending attachment store'));
    };
  });
  return databasePromise;
}

async function withStore<T>(
  mode: IDBTransactionMode,
  operation: (store: IDBObjectStore, setResult: (value: T) => void, fail: (error: unknown) => void) => void,
): Promise<T> {
  const database = await openDatabase();
  return new Promise<T>((resolve, reject) => {
    let result: T;
    let operationError: unknown = null;
    let transaction: IDBTransaction;
    try {
      transaction = database.transaction(STORE_NAME, mode);
      const fail = (error: unknown) => {
        operationError = error;
        try { transaction.abort(); } catch { /* already inactive */ }
      };
      operation(transaction.objectStore(STORE_NAME), value => { result = value; }, fail);
    } catch (error) {
      reject(error);
      return;
    }
    transaction.oncomplete = () => resolve(result as T);
    transaction.onerror = () => {
      operationError ??= transaction.error ?? new Error('Pending attachment transaction failed');
    };
    transaction.onabort = () => reject(operationError ?? transaction.error ?? new Error('Pending attachment transaction aborted'));
  });
}

export async function putPendingAttachment(
  actorUserId: string,
  projectId: string,
  sendKey: string,
  attachment: StoredPendingAttachment,
): Promise<void> {
  const record: PendingAttachmentRecord = {
    ...attachment,
    key: recordKey(actorUserId, projectId, sendKey, attachment.id),
    actor_user_id: actorUserId,
    project_id: projectId,
    send_key: sendKey,
    updated_at: Date.now(),
    cleanup_requested: false,
  };
  if (typeof indexedDB === 'undefined') {
    memoryFallback.set(record.key, record);
    return;
  }
  await withStore<undefined>('readwrite', (store, resolve, reject) => {
    const request = store.put(record);
    request.onsuccess = () => resolve(undefined);
    request.onerror = () => reject(request.error);
  });
}

export async function listPendingAttachments(
  actorUserId: string,
  projectId: string,
  sendKey: string,
): Promise<StoredPendingAttachment[]> {
  const prefix = scopePrefix(actorUserId, projectId, sendKey);
  if (typeof indexedDB === 'undefined') {
    return [...memoryFallback.values()]
      .filter(record => record.key.startsWith(prefix))
      .sort((a, b) => a.updated_at - b.updated_at)
      .map(({ id, file, mime_type, name, size_bytes }) => ({ id, file, mime_type, name, size_bytes }));
  }
  await retryPendingAttachmentCleanups(actorUserId, projectId).catch(() => undefined);
  return withStore<StoredPendingAttachment[]>('readonly', (store, resolve, reject) => {
    const request = store.getAll();
    request.onsuccess = () => {
      resolve((request.result as PendingAttachmentRecord[])
        .filter(record => record.key.startsWith(prefix) && !record.cleanup_requested)
        .sort((a, b) => a.updated_at - b.updated_at)
        .map(({ id, file, mime_type, name, size_bytes }) => ({ id, file, mime_type, name, size_bytes })));
    };
    request.onerror = () => reject(request.error);
  });
}

export async function deletePendingAttachment(
  actorUserId: string,
  projectId: string,
  sendKey: string,
  attachmentId: string,
): Promise<void> {
  const key = recordKey(actorUserId, projectId, sendKey, attachmentId);
  if (typeof indexedDB === 'undefined') {
    memoryFallback.delete(key);
    return;
  }
  await withStore<undefined>('readwrite', (store, resolve, reject) => {
    const request = store.delete(key);
    request.onsuccess = () => resolve(undefined);
    request.onerror = () => reject(request.error);
  });
}

export async function clearPendingAttachments(
  actorUserId: string,
  projectId: string,
  sendKey: string,
): Promise<void> {
  const prefix = scopePrefix(actorUserId, projectId, sendKey);
  if (typeof indexedDB === 'undefined') {
    for (const key of memoryFallback.keys()) {
      if (key.startsWith(prefix)) memoryFallback.delete(key);
    }
    return;
  }
  await withStore<undefined>('readwrite', (store, resolve, reject) => {
    const request = store.getAll();
    request.onsuccess = () => {
      const matching = (request.result as PendingAttachmentRecord[]).filter(record => record.key.startsWith(prefix));
      for (const record of matching) store.put({ ...record, cleanup_requested: true, updated_at: Date.now() });
      resolve(undefined);
    };
    request.onerror = () => reject(request.error);
  });
  await retryPendingAttachmentCleanups(actorUserId, projectId);
}

export async function retryPendingAttachmentCleanups(
  actorUserId?: string,
  projectId?: string,
): Promise<void> {
  if (typeof indexedDB === 'undefined') {
    for (const [key, record] of memoryFallback) {
      if (record.cleanup_requested
        && (!actorUserId || record.actor_user_id === actorUserId)
        && (!projectId || record.project_id === projectId)) memoryFallback.delete(key);
    }
    return;
  }
  await withStore<undefined>('readwrite', (store, resolve, reject) => {
    const request = store.getAll();
    request.onsuccess = () => {
      for (const record of request.result as PendingAttachmentRecord[]) {
        if (record.cleanup_requested
          && (!actorUserId || record.actor_user_id === actorUserId)
          && (!projectId || record.project_id === projectId)) store.delete(record.key);
      }
      resolve(undefined);
    };
    request.onerror = () => reject(request.error);
  });
}

export function resetPendingAttachmentMemoryStoreForTests(): void {
  memoryFallback.clear();
}
