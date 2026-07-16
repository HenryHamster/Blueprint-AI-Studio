import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, test, vi } from 'vitest';
import type { ProjectMessage } from '../types/messages';
import { mergeResidentConversationPages, useResidentConversation } from './useResidentConversation';

const { fetchStrategicConversation } = vi.hoisted(() => ({
  fetchStrategicConversation: vi.fn(),
}));
vi.mock('../strategicStateApi', async importOriginal => ({
  ...await importOriginal<typeof import('../strategicStateApi')>(),
  fetchStrategicConversation,
}));

function message(sequence: number): ProjectMessage {
  return {
    id: `message-${sequence}`, project_id: 'project-1', message_type: 'NOTE',
    from_type: 'strategic', from_task_id: null, content: String(sequence), context: {},
    status: 'open', resolution: null, resolved_at: null,
    created_at: `2026-07-15T00:00:${String(sequence % 60).padStart(2, '0')}Z`,
    sequence, surface: 'conversation',
  };
}

describe('resident conversation pagination', () => {
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  test('merges overlapping pages by canonical id while preserving sequence order', () => {
    const merged = mergeResidentConversationPages(
      [message(1), message(2)],
      [{ ...message(2), content: 'updated' }, message(3)],
    );
    expect(merged.map(item => item.sequence)).toEqual([1, 2, 3]);
    expect(merged[1].content).toBe('updated');
  });

  test('preserves the existing array and message identities when a page is unchanged', () => {
    const existing = [message(1), message(2)];
    const merged = mergeResidentConversationPages(existing, existing.map(item => ({ ...item })));
    expect(merged).toBe(existing);
    expect(merged[0]).toBe(existing[0]);
  });

  test('fetches a target once, then resumes the live tail', async () => {
    let messages: ProjectMessage[] = [];
    fetchStrategicConversation
      .mockResolvedValueOnce({ conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [message(1)] } })
      .mockResolvedValueOnce({ conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [message(1), message(2)] } });
    const setMessages = vi.fn(update => { messages = typeof update === 'function' ? update(messages) : update; });
    const onTargetFetched = vi.fn();
    const { result } = renderHook(() => useResidentConversation({
      enabled: true,
      projectId: 'project-1',
      actorUserId: 'actor-1',
      includeMessageId: 'message-1',
      getMessages: () => messages,
      setMessages,
      onLoadingChange: vi.fn(),
      onError: vi.fn(),
      onTargetFetched,
    }));
    await act(async () => { await result.current.refresh(); });
    await act(async () => { await result.current.refresh(); });
    expect(fetchStrategicConversation.mock.calls[0][1]).toMatchObject({ includeMessageId: 'message-1' });
    expect(fetchStrategicConversation.mock.calls[1][1]).toMatchObject({ includeMessageId: null });
    expect(onTargetFetched).toHaveBeenCalledWith('message-1');
    expect(messages.map(item => item.sequence)).toEqual([1, 2]);
  });

  test('walks forward on reconnect from the last received sequence', async () => {
    let messages = [message(1)];
    fetchStrategicConversation
      .mockResolvedValueOnce({
        conversation: { limit: 100, has_more: true, next_before_sequence: null, items: [message(2), message(101)] },
      })
      .mockResolvedValueOnce({
        conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [message(102), message(103)] },
      });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const { result } = renderHook(() => useResidentConversation({
      enabled: true,
      projectId: 'project-1',
      actorUserId: 'actor-1',
      includeMessageId: null,
      getMessages: () => messages,
      setMessages,
      onLoadingChange: vi.fn(),
      onError: vi.fn(),
    }));
    await act(async () => { await result.current.refresh(true); });
    expect(fetchStrategicConversation).toHaveBeenCalledTimes(2);
    expect(fetchStrategicConversation.mock.calls[0][1]).toMatchObject({ afterSequence: 1 });
    expect(fetchStrategicConversation.mock.calls[1][1]).toMatchObject({ afterSequence: 101 });
    expect(messages.map(item => item.sequence)).toEqual([1, 2, 101, 102, 103]);
  });

  test('forward catch-up never overwrites the backward-history cursor', async () => {
    let messages: ProjectMessage[] = [];
    fetchStrategicConversation
      .mockResolvedValueOnce({
        conversation: {
          limit: 100,
          has_more: true,
          next_before_sequence: 50,
          items: [message(100)],
        },
      })
      .mockResolvedValueOnce({
        conversation: {
          limit: 100,
          has_more: false,
          next_before_sequence: null,
          items: [message(101)],
        },
      })
      .mockResolvedValueOnce({
        conversation: {
          limit: 100,
          has_more: false,
          next_before_sequence: null,
          items: [message(49), message(50)],
        },
      });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const { result } = renderHook(() => useResidentConversation({
      enabled: true,
      projectId: 'project-1',
      actorUserId: 'actor-1',
      includeMessageId: null,
      getMessages: () => messages,
      setMessages,
      onLoadingChange: vi.fn(),
      onError: vi.fn(),
    }));
    await act(async () => { await result.current.refresh(); });
    await act(async () => { await result.current.refresh(true); });
    expect(result.current.hasMore).toBe(true);
    await act(async () => { await result.current.loadOlder(); });
    expect(fetchStrategicConversation.mock.calls[2][1]).toMatchObject({ beforeSequence: 50 });
    expect(messages.map(item => item.sequence)).toEqual([49, 50, 100, 101]);
  });

  test('drops a later forward page when actor or project changes during catch-up', async () => {
    let finishSecondPage!: (value: unknown) => void;
    const secondPage = new Promise(resolve => { finishSecondPage = resolve; });
    fetchStrategicConversation
      .mockResolvedValueOnce({
        conversation: { limit: 100, has_more: true, next_before_sequence: null, items: [message(2)] },
      })
      .mockReturnValueOnce(secondPage);
    const setMessages = vi.fn();
    const { result, rerender } = renderHook(
      ({ projectId, actorUserId }) => useResidentConversation({
        enabled: true,
        projectId,
        actorUserId,
        includeMessageId: null,
        getMessages: () => [message(1)],
        setMessages,
        onLoadingChange: vi.fn(),
        onError: vi.fn(),
      }),
      { initialProps: { projectId: 'project-1', actorUserId: 'actor-1' } },
    );
    let refreshPromise: Promise<void> | undefined;
    act(() => { refreshPromise = result.current.refresh(true); });
    await waitFor(() => expect(fetchStrategicConversation).toHaveBeenCalledTimes(2));
    rerender({ projectId: 'project-2', actorUserId: 'actor-2' });
    finishSecondPage({
      conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [message(3)] },
    });
    await act(async () => { await refreshPromise; });
    expect(setMessages).not.toHaveBeenCalled();
  });
});
