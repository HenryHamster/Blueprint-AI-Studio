import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { indexedDB as fakeIndexedDB } from 'fake-indexeddb';
import { File as NodeFile } from 'node:buffer';
import {
  clearPendingAttachments,
  listPendingAttachments,
  putPendingAttachment,
  resetPendingAttachmentMemoryStoreForTests,
} from './pendingAttachmentStore';

describe('pending attachment blob store', () => {
  beforeEach(() => {
    vi.stubGlobal('indexedDB', undefined);
    resetPendingAttachmentMemoryStoreForTests();
  });

  afterEach(() => {
    resetPendingAttachmentMemoryStoreForTests();
    vi.unstubAllGlobals();
  });

  test('restores exact bytes only for the actor, project, and send key', async () => {
    const sourceBytes = new Uint8Array([0, 255, 1, 2, 13, 10, 128, 64]);
    const file = new File([sourceBytes], 'evidence.bin', { type: 'application/octet-stream' });
    await putPendingAttachment('actor-a', 'project-a', 'send-a', {
      id: 'attachment-a',
      file,
      mime_type: file.type,
      name: file.name,
      size_bytes: file.size,
    });

    const restored = await listPendingAttachments('actor-a', 'project-a', 'send-a');
    expect(restored).toHaveLength(1);
    expect([...new Uint8Array(await restored[0].file.arrayBuffer())]).toEqual([...sourceBytes]);
    await expect(listPendingAttachments('actor-b', 'project-a', 'send-a')).resolves.toEqual([]);
    await expect(listPendingAttachments('actor-a', 'project-b', 'send-a')).resolves.toEqual([]);
    await expect(listPendingAttachments('actor-a', 'project-a', 'send-b')).resolves.toEqual([]);
  });

  test('cleans every blob in a completed send without touching another send', async () => {
    const file = new File(['one'], 'one.txt', { type: 'text/plain' });
    for (const sendKey of ['send-a', 'send-b']) {
      await putPendingAttachment('actor-a', 'project-a', sendKey, {
        id: `attachment-${sendKey}`,
        file,
        mime_type: file.type,
        name: file.name,
        size_bytes: file.size,
      });
    }
    await clearPendingAttachments('actor-a', 'project-a', 'send-a');
    await expect(listPendingAttachments('actor-a', 'project-a', 'send-a')).resolves.toEqual([]);
    await expect(listPendingAttachments('actor-a', 'project-a', 'send-b')).resolves.toHaveLength(1);
  });
  test('persists attachment bytes through IndexedDB across a module reload', async () => {
    vi.stubGlobal('indexedDB', fakeIndexedDB);
    const sourceBytes = new Uint8Array([7, 0, 255, 42, 128, 3]);
    const file = new NodeFile([sourceBytes], 'reload-proof.bin', { type: 'application/octet-stream' }) as unknown as File;
    await putPendingAttachment('actor-idb', 'project-idb', 'send-idb', {
      id: 'attachment-idb',
      file,
      mime_type: file.type,
      name: file.name,
      size_bytes: file.size,
    });

    vi.resetModules();
    const reloadedStore = await import('./pendingAttachmentStore');
    const restored = await reloadedStore.listPendingAttachments('actor-idb', 'project-idb', 'send-idb');

    expect(restored).toHaveLength(1);
    expect(restored[0].name).toBe('reload-proof.bin');
    expect(restored[0].mime_type).toBe('application/octet-stream');
    expect([...new Uint8Array(await restored[0].file.arrayBuffer())]).toEqual([...sourceBytes]);
  });
});
