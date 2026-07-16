import { act, cleanup, renderHook } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test } from 'vitest';
import { useConversationSend } from './useConversationSend';
import { savePendingConversationSend, type PendingConversationSend } from '../utils/conversationSendState';

function pending(key: string, content: string): PendingConversationSend {
  return {
    actor_user_id: 'actor-a',
    project_id: 'project-a',
    key,
    content,
    created_at: '2026-07-15T00:00:00Z',
    message_type: 'FEEDBACK',
    state: 'needs_retry',
    payload: { content, idempotency_key: key },
  };
}

describe('useConversationSend', () => {
  beforeEach(() => sessionStorage.clear());
  afterEach(() => { cleanup(); sessionStorage.clear(); });

  test('switches projects atomically without deleting or leaking either pending send', () => {
    savePendingConversationSend(sessionStorage, 'optimisticSend:actor-a:project-a', pending('a', 'from a'));
    savePendingConversationSend(sessionStorage, 'optimisticSend:actor-a:project-b', {
      ...pending('b', 'from b'),
      project_id: 'project-b',
    });
    const { result, rerender } = renderHook(
      ({ projectId }) => useConversationSend(projectId, 'actor-a'),
      { initialProps: { projectId: 'project-a' as string | null } },
    );
    expect(result.current.pending?.content).toBe('from a');
    rerender({ projectId: 'project-b' });
    expect(result.current.pending?.content).toBe('from b');
    expect(JSON.parse(sessionStorage.getItem('optimisticSend:actor-a:project-a') ?? '{}').key).toBe('a');
    expect(JSON.parse(sessionStorage.getItem('optimisticSend:actor-a:project-b') ?? '{}').key).toBe('b');
  });

  test('persists one immutable payload and transitions its exact identity', () => {
    const { result } = renderHook(() => useConversationSend('project-a', 'actor-a'));
    act(() => result.current.begin(pending('send-1', 'inspect this')));
    act(() => result.current.transition('waiting', 'paused'));
    expect(result.current.pending).toMatchObject({
      key: 'send-1',
      state: 'waiting',
      wakeup_state: 'paused',
      payload: { content: 'inspect this', idempotency_key: 'send-1' },
    });
  });

  test('retains an accepted wake receipt while allocating a fresh identity for the next turn', () => {
    const { result } = renderHook(() => useConversationSend('project-a', 'actor-a'));
    act(() => result.current.begin(pending('send-1', 'first')));
    act(() => result.current.transition('accepted', 'queued'));
    let nextIdentity = '';
    act(() => { nextIdentity = result.current.ensureIdentity(() => 'send-2'); });
    expect(result.current.pending).toMatchObject({ key: 'send-1', state: 'accepted', wakeup_state: 'queued' });
    expect(nextIdentity).toBe('send-2');
    expect(result.current.identity).toBe('send-2');
  });

  test('rejects a pending record owned by a different actor', () => {
    savePendingConversationSend(sessionStorage, 'optimisticSend:actor-a:project-a', {
      ...pending('send-1', 'private draft'),
      actor_user_id: 'actor-b',
    });
    const { result } = renderHook(() => useConversationSend('project-a', 'actor-a'));
    expect(result.current.pending).toBeNull();
  });
});
