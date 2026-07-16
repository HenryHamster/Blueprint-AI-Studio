import { afterEach, describe, expect, test, vi } from 'vitest';
import { finalizeLastAttachmentRemoval, type ConversationComposerIntent, updateConversationComposerIntent } from './conversationAttachmentDraftFinalization';
import { loadComposerDraft } from './conversationDraftState';

describe('final attachment draft finalization', () => {
  afterEach(() => localStorage.clear());

  test('typing and selecting a reply during deferred deletion survive finalization', async () => {
    let finishDelete!: () => void;
    const deletion = new Promise<void>(resolve => { finishDelete = resolve; });
    let currentIntent: ConversationComposerIntent = {
      type: 'FEEDBACK', content: '',
      reply_to: { id: 'question-1', content: 'Which runner?', from: 'Strategic', message_type: 'QUESTION', status: 'open' },
      source_refs: [{ kind: 'task', id: 'old-task' }], artifact_refs: [], attachment_send_key: 'attachment-draft',
    };
    const clearIdentity = vi.fn();
    const finalize = (async () => {
      await deletion;
      finalizeLastAttachmentRemoval('actor-1', 'project-1', currentIntent, clearIdentity);
    })();

    currentIntent = updateConversationComposerIntent(currentIntent, {
      type: 'NOTE',
      content: 'Use the newly selected starter prompt',
      reply_to: null,
      source_refs: [{ kind: 'artifact', id: 'board-2', revision: 4 }],
      artifact_refs: [{ kind: 'artifact', id: 'board-2', revision: 4 }],
      attachment_send_key: null,
    });
    finishDelete();
    await finalize;

    const restored = loadComposerDraft('actor-1', 'project-1');
    expect(restored?.type).toBe('NOTE');
    expect(restored?.content).toBe('Use the newly selected starter prompt');
    expect(restored?.reply_to).toBeNull();
    expect(restored?.source_refs).toEqual([{ kind: 'artifact', id: 'board-2', revision: 4 }]);
    expect(restored?.artifact_refs).toEqual([{ kind: 'artifact', id: 'board-2', revision: 4 }]);
    expect(restored?.attachment_send_key).toBeNull();
    expect(clearIdentity).not.toHaveBeenCalled();
  });
});
