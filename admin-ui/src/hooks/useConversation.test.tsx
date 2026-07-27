import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, test, vi } from 'vitest';
import type { ProjectMessage } from '../types/messages';
import {
  CONVERSATION_INITIAL_WINDOW,
  mergeConversationPages,
  useConversation,
} from './useConversation';

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
    status: 'open', resolution: null, resolved_at: null, delivered_at: null,
    created_at: `2026-07-15T00:00:${String(sequence % 60).padStart(2, '0')}Z`,
    sequence, surface: 'conversation',
  };
}

describe('conversation pagination', () => {
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  test('merges overlapping pages by canonical id while preserving sequence order', () => {
    const merged = mergeConversationPages(
      [message(1), message(2)],
      [{ ...message(2), content: 'updated' }, message(3)],
    );
    expect(merged.map(item => item.sequence)).toEqual([1, 2, 3]);
    expect(merged[1].content).toBe('updated');
  });

  test('preserves the existing array and message identities when a page is unchanged', () => {
    const existing = [message(1), message(2)];
    const merged = mergeConversationPages(existing, existing.map(item => ({ ...item })));
    expect(merged).toBe(existing);
    expect(merged[0]).toBe(existing[0]);
  });

  test('reconciles a canonical message by client identity without duplicating its optimistic predecessor', () => {
    const optimistic = { ...message(1), id: 'optimistic:send-1', idempotency_key: 'send-1' };
    const canonical = { ...message(2), id: 'canonical-1', client_idempotency_key: 'send-1' };
    const merged = mergeConversationPages([optimistic], [canonical]);
    expect(merged).toEqual([canonical]);
  });

  test('renders a canonical realtime projection immediately, then fills from the prior sequence', async () => {
    vi.useFakeTimers();
    let messages = [message(100)];
    fetchStrategicConversation.mockResolvedValue({
      conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [message(101)] },
    });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const { rerender } = renderHook(
      ({ realtimeMessage }) => useConversation({
        enabled: true,
        projectId: 'project-1',
        actorUserId: 'actor-1',
        includeMessageId: null,
        realtimeMessage,
        getMessages: () => messages,
        setMessages,
        onLoadingChange: vi.fn(),
        onError: vi.fn(),
      }),
      { initialProps: { realtimeMessage: null as ProjectMessage | null } },
    );

    rerender({ realtimeMessage: message(102) });
    expect(messages.map(item => item.sequence)).toEqual([100, 102]);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(fetchStrategicConversation.mock.calls[0][1]).toMatchObject({ afterSequence: 100 });
    expect(messages.map(item => item.sequence)).toEqual([100, 101, 102]);
    vi.useRealTimers();
  });

  test('preserves a full older row until a truncated realtime update is refetched by id', async () => {
    vi.useFakeTimers();
    let messages = [{ ...message(50), content: 'full durable content' }, message(100)];
    const canonicalUpdate = { ...message(50), content: 'canonical updated content' };
    fetchStrategicConversation
      .mockResolvedValueOnce({
        conversation: { limit: 1, has_more: false, next_before_sequence: null, items: [canonicalUpdate] },
      })
      .mockResolvedValueOnce({
        conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [] },
      });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const { rerender } = renderHook(
      ({ realtimeMessage, requiresRefetch }) => useConversation({
        enabled: true,
        projectId: 'project-1',
        actorUserId: 'actor-1',
        includeMessageId: null,
        realtimeMessage,
        realtimeMessageRequiresCanonicalRefetch: requiresRefetch,
        getMessages: () => messages,
        setMessages,
        onLoadingChange: vi.fn(),
        onError: vi.fn(),
      }),
      { initialProps: { realtimeMessage: null as ProjectMessage | null, requiresRefetch: false } },
    );

    rerender({
      realtimeMessage: { ...message(50), content: '', context: { projection_truncated: true } },
      requiresRefetch: true,
    });
    expect(messages.find(item => item.sequence === 50)?.content).toBe('full durable content');
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(fetchStrategicConversation.mock.calls[0][1]).toMatchObject({
      includeMessageId: 'message-50',
      limit: 1,
    });
    expect(fetchStrategicConversation.mock.calls[1][1]).toMatchObject({ afterSequence: 100 });
    expect(messages.find(item => item.sequence === 50)?.content).toBe('canonical updated content');
    vi.useRealTimers();
  });

  test('debounces search and replaces the transcript with only server matches', async () => {
    vi.useFakeTimers();
    let messages = [message(1)];
    fetchStrategicConversation.mockResolvedValue({
      conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [message(7)] },
    });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const { rerender } = renderHook(
      ({ searchQuery }) => useConversation({
        enabled: true,
        projectId: 'project-1',
        actorUserId: 'actor-1',
        includeMessageId: null,
        searchQuery,
        getMessages: () => messages,
        setMessages,
        onLoadingChange: vi.fn(),
        onError: vi.fn(),
      }),
      { initialProps: { searchQuery: '' } },
    );

    rerender({ searchQuery: 'deployment decision' });
    expect(fetchStrategicConversation).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(251); });
    expect(fetchStrategicConversation.mock.calls[0][1]).toMatchObject({ search: 'deployment decision' });
    expect(messages.map(item => item.sequence)).toEqual([7]);
    vi.useRealTimers();
  });

  test('targeted navigation takes priority over an active search selector', async () => {
    let messages: ProjectMessage[] = [];
    fetchStrategicConversation.mockResolvedValue({
      conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [message(7)] },
    });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const onTargetFetched = vi.fn();
    const { result } = renderHook(() => useConversation({
      enabled: true,
      projectId: 'project-1',
      actorUserId: 'actor-1',
      includeMessageId: 'message-7',
      searchQuery: 'deployment decision',
      getMessages: () => messages,
      setMessages,
      onLoadingChange: vi.fn(),
      onError: vi.fn(),
      onTargetFetched,
    }));

    await act(async () => { await result.current.refresh(); });
    expect(fetchStrategicConversation.mock.calls[0][1]).toMatchObject({
      includeMessageId: 'message-7',
      limit: 30,
    });
    expect(fetchStrategicConversation.mock.calls[0][1]).not.toHaveProperty('search');
    expect(onTargetFetched).toHaveBeenCalledWith('message-7');
    expect(messages.map(item => item.id)).toEqual(['message-7']);
  });

  test('fetches a target once, then resumes the live tail', async () => {
    let messages: ProjectMessage[] = [];
    fetchStrategicConversation
      .mockResolvedValueOnce({ conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [message(1)] } })
      .mockResolvedValueOnce({ conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [message(1), message(2)] } });
    const setMessages = vi.fn(update => { messages = typeof update === 'function' ? update(messages) : update; });
    const onTargetFetched = vi.fn();
    const { result } = renderHook(() => useConversation({
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
    const { result } = renderHook(() => useConversation({
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
    const { result } = renderHook(() => useConversation({
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
      ({ projectId, actorUserId }) => useConversation({
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

  test('schedules forward refresh when strategicPollEpoch transitions to nonzero', async () => {
    vi.useFakeTimers();
    let messages = [message(1)];
    fetchStrategicConversation.mockResolvedValue({
      conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [message(2)] },
    });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const { rerender } = renderHook(
      ({ strategicPollEpoch }) => useConversation({
        enabled: true,
        projectId: 'project-1',
        actorUserId: 'actor-1',
        includeMessageId: null,
        strategicPollEpoch,
        getMessages: () => messages,
        setMessages,
        onLoadingChange: vi.fn(),
        onError: vi.fn(),
      }),
      { initialProps: { strategicPollEpoch: undefined as number | undefined } },
    );

    // No refresh should be scheduled when epoch is undefined
    expect(fetchStrategicConversation).not.toHaveBeenCalled();

    // Transition to a nonzero epoch — this should schedule a forward refresh
    rerender({ strategicPollEpoch: 1 });
    expect(fetchStrategicConversation).not.toHaveBeenCalled(); // timer hasn't fired

    // Advance past the 100ms setTimeout
    await act(async () => { vi.advanceTimersByTime(150); });

    expect(fetchStrategicConversation).toHaveBeenCalledTimes(1);
    // Forward refresh uses afterSequence, not includeMessageId
    expect(fetchStrategicConversation.mock.calls[0][1]).toMatchObject({
      afterSequence: 1,
    });

    vi.useRealTimers();
  });

  test('does not schedule forward refresh when strategicPollEpoch is zero', async () => {
    vi.useFakeTimers();
    let messages: ProjectMessage[] = [];
    fetchStrategicConversation.mockResolvedValue({
      conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [] },
    });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const { rerender } = renderHook(
      ({ strategicPollEpoch }) => useConversation({
        enabled: true,
        projectId: 'project-1',
        actorUserId: 'actor-1',
        includeMessageId: null,
        strategicPollEpoch,
        getMessages: () => messages,
        setMessages,
        onLoadingChange: vi.fn(),
        onError: vi.fn(),
      }),
      { initialProps: { strategicPollEpoch: undefined as number | undefined } },
    );

    // Transition to zero epoch — must NOT schedule a refresh
    rerender({ strategicPollEpoch: 0 });
    await act(async () => { vi.advanceTimersByTime(150); });

    expect(fetchStrategicConversation).not.toHaveBeenCalled();

    vi.useRealTimers();
  });

  test('does not schedule forward refresh when enabled is false even with nonzero epoch', async () => {
    vi.useFakeTimers();
    let messages: ProjectMessage[] = [];
    fetchStrategicConversation.mockResolvedValue({
      conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [] },
    });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const { rerender } = renderHook(
      ({ enabled, strategicPollEpoch }) => useConversation({
        enabled,
        projectId: 'project-1',
        actorUserId: 'actor-1',
        includeMessageId: null,
        strategicPollEpoch,
        getMessages: () => messages,
        setMessages,
        onLoadingChange: vi.fn(),
        onError: vi.fn(),
      }),
      { initialProps: { enabled: true, strategicPollEpoch: undefined as number | undefined } },
    );

    rerender({ enabled: false, strategicPollEpoch: 1 });
    await act(async () => { vi.advanceTimersByTime(150); });

    expect(fetchStrategicConversation).not.toHaveBeenCalled();

    vi.useRealTimers();
  });
});

describe('initial window and newest-message pinning', () => {
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  test('fetchInitial loads the first bounded transcript page without waiting for a poll epoch', async () => {
    let messages: ProjectMessage[] = [];
    fetchStrategicConversation.mockResolvedValue({
      conversation: { has_more: false, next_before_sequence: null, items: [message(50)] },
    });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });

    renderHook(() => useConversation({
      enabled: true,
      fetchInitial: true,
      projectId: 'project-1',
      actorUserId: 'actor-1',
      includeMessageId: null,
      getMessages: () => messages,
      setMessages,
      onLoadingChange: vi.fn(),
      onError: vi.fn(),
    }));

    await waitFor(() => expect(fetchStrategicConversation).toHaveBeenCalledTimes(1));
    expect(fetchStrategicConversation.mock.calls[0][1]).toMatchObject({
      includeMessageId: null,
      limit: CONVERSATION_INITIAL_WINDOW,
    });
    expect(messages.map(item => item.sequence)).toEqual([50]);
  });

  test('initial fetch uses the bounded conversation window', async () => {
    let messages: ProjectMessage[] = [];
    fetchStrategicConversation.mockResolvedValue({
      conversation: { has_more: false, next_before_sequence: null, items: [] },
    });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const { result } = renderHook(() => useConversation({
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

    expect(fetchStrategicConversation).toHaveBeenCalledTimes(1);
    expect(fetchStrategicConversation.mock.calls[0][1]).toMatchObject({
      limit: CONVERSATION_INITIAL_WINDOW,
    });
    expect(fetchStrategicConversation.mock.calls[0][1]).not.toHaveProperty('afterSequence');
    expect(fetchStrategicConversation.mock.calls[0][1]).not.toHaveProperty('beforeSequence');
    expect(fetchStrategicConversation.mock.calls[0][1]).not.toHaveProperty('search');
  });

  test('CONVERSATION_INITIAL_WINDOW is uniformly bounded across layouts', () => {
    expect(CONVERSATION_INITIAL_WINDOW).toBe(30);
  });

  test('hasMore is true when server has older messages beyond the initial window', async () => {
    const initialMessages = Array.from({ length: CONVERSATION_INITIAL_WINDOW }, (_, i) =>
      message(i + 1));
    let messages: ProjectMessage[] = [];
    fetchStrategicConversation.mockResolvedValue({
      conversation: {
        has_more: true,
        next_before_sequence: 1,
        items: initialMessages,
      },
    });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const { result } = renderHook(() => useConversation({
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

    // hasMore=true means a "load older" affordance appears, enabling incremental history load
    expect(result.current.hasMore).toBe(true);
    // The initial window loaded newest-first: exactly CONVERSATION_INITIAL_WINDOW messages
    expect(messages).toHaveLength(CONVERSATION_INITIAL_WINDOW);
  });

  test('target navigation still uses limit 30 (not capped to initial window)', async () => {
    let messages: ProjectMessage[] = [];
    fetchStrategicConversation.mockResolvedValue({
      conversation: { has_more: false, next_before_sequence: null, items: [message(7)] },
    });
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const { result } = renderHook(() => useConversation({
      enabled: true,
      projectId: 'project-1',
      actorUserId: 'actor-1',
      includeMessageId: 'message-7',
      getMessages: () => messages,
      setMessages,
      onLoadingChange: vi.fn(),
      onError: vi.fn(),
    }));

    await act(async () => { await result.current.refresh(); });

    expect(fetchStrategicConversation.mock.calls[0][1]).toMatchObject({
      includeMessageId: 'message-7',
      limit: 30,
    });
  });
});

describe('sendAck callback', () => {
  afterEach(() => { cleanup(); vi.clearAllMocks(); });

  test('calls sendAck with the realtime message id when a conversation message is merged', () => {
    let messages: ProjectMessage[] = [];
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const sendAck = vi.fn();
    fetchStrategicConversation.mockResolvedValue({
      conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [] },
    });
    const { rerender } = renderHook(
      ({ realtimeMessage }) => useConversation({
        enabled: true,
        projectId: 'project-1',
        actorUserId: 'actor-1',
        includeMessageId: null,
        realtimeMessage,
        sendAck,
        getMessages: () => messages,
        setMessages,
        onLoadingChange: vi.fn(),
        onError: vi.fn(),
      }),
      { initialProps: { realtimeMessage: null as ProjectMessage | null } },
    );

    const msg = message(5);
    rerender({ realtimeMessage: msg });

    expect(sendAck).toHaveBeenCalledTimes(1);
    expect(sendAck).toHaveBeenCalledWith(msg.id);
  });

  test('acknowledges the same realtime message only once when callback identity changes', () => {
    let messages: ProjectMessage[] = [];
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const firstSendAck = vi.fn();
    const replacementSendAck = vi.fn();
    const msg = message(5);
    const { rerender } = renderHook(
      ({ realtimeMessage, sendAck }) => useConversation({
        enabled: true,
        projectId: 'project-1',
        actorUserId: 'actor-1',
        includeMessageId: null,
        realtimeMessage,
        sendAck,
        getMessages: () => messages,
        setMessages,
        onLoadingChange: vi.fn(),
        onError: vi.fn(),
      }),
      { initialProps: { realtimeMessage: null as ProjectMessage | null, sendAck: firstSendAck } },
    );

    rerender({ realtimeMessage: msg, sendAck: firstSendAck });
    rerender({ realtimeMessage: msg, sendAck: replacementSendAck });

    expect(firstSendAck).toHaveBeenCalledOnce();
    expect(replacementSendAck).not.toHaveBeenCalled();
  });
  test('keeps acknowledgement dedup across include-target and search navigation', () => {
    let messages: ProjectMessage[] = [];
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const sendAck = vi.fn();
    const msg = message(8);
    const { rerender } = renderHook(
      ({ realtimeMessage, includeMessageId, searchQuery }) => useConversation({
        enabled: true,
        projectId: 'project-1',
        actorUserId: 'actor-1',
        includeMessageId,
        searchQuery,
        realtimeMessage,
        sendAck,
        getMessages: () => messages,
        setMessages,
        onLoadingChange: vi.fn(),
        onError: vi.fn(),
      }),
      { initialProps: {
        realtimeMessage: null as ProjectMessage | null,
        includeMessageId: null as string | null,
        searchQuery: '',
      } },
    );

    rerender({ realtimeMessage: msg, includeMessageId: null, searchQuery: '' });
    rerender({ realtimeMessage: msg, includeMessageId: msg.id, searchQuery: '' });
    rerender({ realtimeMessage: msg, includeMessageId: msg.id, searchQuery: 'needle' });

    expect(sendAck).toHaveBeenCalledOnce();
    expect(sendAck).toHaveBeenCalledWith(msg.id);
  });

  test('does not call sendAck for non-conversation surface messages', () => {
    let messages: ProjectMessage[] = [];
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    const sendAck = vi.fn();
    fetchStrategicConversation.mockResolvedValue({
      conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [] },
    });
    const { rerender } = renderHook(
      ({ realtimeMessage }) => useConversation({
        enabled: true,
        projectId: 'project-1',
        actorUserId: 'actor-1',
        includeMessageId: null,
        realtimeMessage,
        sendAck,
        getMessages: () => messages,
        setMessages,
        onLoadingChange: vi.fn(),
        onError: vi.fn(),
      }),
      { initialProps: { realtimeMessage: null as ProjectMessage | null } },
    );

    const activityMsg = { ...message(6), surface: 'activity' };
    rerender({ realtimeMessage: activityMsg });

    expect(sendAck).not.toHaveBeenCalled();
  });

  test('does not throw when sendAck is omitted', () => {
    let messages: ProjectMessage[] = [];
    const setMessages = vi.fn(update => {
      messages = typeof update === 'function' ? update(messages) : update;
    });
    fetchStrategicConversation.mockResolvedValue({
      conversation: { limit: 100, has_more: false, next_before_sequence: null, items: [] },
    });
    expect(() => {
      const { rerender } = renderHook(
        ({ realtimeMessage }) => useConversation({
          enabled: true,
          projectId: 'project-1',
          actorUserId: 'actor-1',
          includeMessageId: null,
          realtimeMessage,
          getMessages: () => messages,
          setMessages,
          onLoadingChange: vi.fn(),
          onError: vi.fn(),
        }),
        { initialProps: { realtimeMessage: null as ProjectMessage | null } },
      );
      rerender({ realtimeMessage: message(7) });
    }).not.toThrow();
  });
});
