import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
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
    const file = new File(['runner evidence'], 'run.txt', { type: 'text/plain' });
    await putPendingAttachment('actor-a', 'project-a', 'send-a', {
      id: 'attachment-a',
      file,
      mime_type: file.type,
      name: file.name,
      size_bytes: file.size,
    });

    const restored = await listPendingAttachments('actor-a', 'project-a', 'send-a');
    expect(restored).toHaveLength(1);
    expect(await restored[0].file.text()).toBe('runner evidence');
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
});
