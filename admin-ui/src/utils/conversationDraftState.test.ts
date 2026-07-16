import { afterEach, describe, expect, test } from 'vitest';
import { loadComposerDraft, saveComposerDraft } from './conversationDraftState';

describe('conversation draft persistence', () => {
  afterEach(() => localStorage.clear());

  test('clearing the last reply removes the scoped envelope across remount reads', () => {
    const scope = ['actor-1', 'project-1'] as const;
    saveComposerDraft(...scope, {
      type: 'FEEDBACK',
      content: '',
      reply_to: {
        id: 'question-1',
        content: 'Which runner?',
        from: 'Strategic',
        message_type: 'QUESTION',
        status: 'open',
      },
      source_refs: [],
      artifact_refs: [],
      attachment_send_key: null,
    });
    expect(loadComposerDraft(...scope)?.reply_to?.id).toBe('question-1');

    saveComposerDraft(...scope, {
      type: 'FEEDBACK',
      content: '',
      reply_to: null,
      source_refs: [],
      artifact_refs: [],
      attachment_send_key: null,
    });

    expect(loadComposerDraft(...scope)).toBeNull();
  });
});
