import { afterEach, describe, expect, test, vi } from 'vitest';
import { clearComposerDraftIfCurrent, loadComposerDraft, saveComposerDraft } from './conversationDraftState';

const emptyIntent = (content = '') => ({
  type: 'FEEDBACK', content, reply_to: null,
  source_refs: [] as Array<Record<string, unknown>>,
  artifact_refs: [] as Array<Record<string, unknown>>,
  attachment_send_key: null,
});

describe('conversation draft persistence', () => {
  afterEach(() => { vi.restoreAllMocks(); localStorage.clear(); });

  test('clearing the last reply removes the exact tracked envelope across remount reads', () => {
    const scope = ['actor-1', 'project-1'] as const;
    const replyWrite = saveComposerDraft(...scope, {
      ...emptyIntent(),
      reply_to: {
        id: 'question-1', content: 'Which runner?', from: 'Strategic',
        message_type: 'QUESTION', status: 'open',
      },
    });
    expect(loadComposerDraft(...scope)?.reply_to?.id).toBe('question-1');

    saveComposerDraft(...scope, emptyIntent(), undefined, replyWrite || null);

    expect(loadComposerDraft(...scope)).toBeNull();
  });

  test('keeps exact sources and intent when the operator leaves message text empty', () => {
    const scope = ['actor-1', 'project-1'] as const;
    const artifactRef = {
      id: 'artifact-7', artifact_ref_id: 'artifact-7', project_id: 'project-1',
      kind: 'notes', revision: 7,
    };
    saveComposerDraft(...scope, {
      ...emptyIntent(),
      source_refs: [{ kind: 'task', id: 'task-1', revision: 3 }],
      artifact_refs: [artifactRef],
      operator_intent: { kind: 'ask', expected_revision: 7 },
    });

    expect(loadComposerDraft(...scope)).toMatchObject({
      content: '',
      source_refs: [{ kind: 'task', id: 'task-1', revision: 3 }],
      artifact_refs: [artifactRef],
      operator_intent: { kind: 'ask', expected_revision: 7 },
    });
  });

  test('clears only the exact submitted write and preserves a newer tab write', () => {
    const scope = ['actor-1', 'project-1'] as const;
    const submittedWrite = saveComposerDraft(...scope, emptyIntent('submitted'));
    const newerWrite = saveComposerDraft(...scope, emptyIntent('newer unsent edit'));

    expect(clearComposerDraftIfCurrent(...scope, submittedWrite || null)).toBe(true);
    expect(loadComposerDraft(...scope)).toMatchObject({
      write_id: newerWrite, content: 'newer unsent edit',
    });
    expect(clearComposerDraftIfCurrent(...scope, newerWrite || null)).toBe(true);
    expect(loadComposerDraft(...scope)).toBeNull();
  });

  test('an exact old-key removal cannot consume a newer write inserted during removeItem', () => {
    const scope = ['actor-1', 'project-1'] as const;
    const submittedWrite = saveComposerDraft(...scope, emptyIntent('submitted'), 'submitted-write');
    const originalRemove = localStorage.removeItem.bind(localStorage);
    let interleaved = false;
    vi.spyOn(window.localStorage, 'removeItem').mockImplementation((storageKey: string) => {
      if (!interleaved && storageKey.endsWith(':submitted-write')) {
        interleaved = true;
        saveComposerDraft(...scope, emptyIntent('newer concurrent write'), 'newer-write');
      }
      originalRemove(storageKey);
    });

    expect(clearComposerDraftIfCurrent(...scope, submittedWrite || null)).toBe(true);

    expect(interleaved).toBe(true);
    expect(loadComposerDraft(...scope)).toMatchObject({
      write_id: 'newer-write', content: 'newer concurrent write',
    });
  });

  test('removes the tracked superseded write and bounds independent concurrent history', () => {
    const scope = ['actor-1', 'project-1'] as const;
    const first = saveComposerDraft(...scope, emptyIntent('first'), 'first');
    saveComposerDraft(...scope, emptyIntent('second'), 'second', first || null);
    const draftKeys = () => Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index))
      .filter(key => key?.includes('messages_composer_draft_entry'));
    expect(draftKeys()).toHaveLength(1);

    for (let index = 0; index < 10; index += 1) {
      saveComposerDraft(...scope, emptyIntent(`concurrent-${index}`), `concurrent-${index}`);
    }
    expect(loadComposerDraft(...scope)?.content).toBe('concurrent-9');
    expect(draftKeys()).toHaveLength(8);
  });

  test('loads and conditionally clears a legacy single-key v2 draft', () => {
    const scope = ['actor-1', 'project-1'] as const;
    localStorage.setItem('messages_composer_draft:actor-1:project-1', JSON.stringify({
      version: 2, actor_user_id: 'actor-1', project_id: 'project-1',
      ...emptyIntent('legacy draft'),
    }));
    const legacy = loadComposerDraft(...scope);
    expect(legacy).toMatchObject({ version: 4, content: 'legacy draft' });
    expect(clearComposerDraftIfCurrent(...scope, legacy?.write_id)).toBe(true);
    expect(loadComposerDraft(...scope)).toBeNull();
  });
});
