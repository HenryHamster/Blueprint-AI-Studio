import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { useProjectConsultations } from './useProjectConsultations';
import type { ConsultationIndexRow } from '../consultationDetailApi';

const { fetchIndex } = vi.hoisted(() => ({ fetchIndex: vi.fn() }));

vi.mock('../consultationDetailApi', async importOriginal => {
  const actual = await importOriginal<typeof import('../consultationDetailApi')>();
  return { ...actual, fetchProjectConsultations: fetchIndex };
});

function row(overrides: Partial<ConsultationIndexRow> = {}): ConsultationIndexRow {
  return {
    consultation_id: 'c-1',
    state: 'running',
    outcome: null,
    requested_at: '2026-08-22T10:00:00Z',
    question_preview: { text: 'why is CI red', truncated: false, chars: 13 },
    step_count: 1,
    steps_truncated: false,
    claim_expired: false,
    delivered_at: null,
    ...overrides,
  };
}

describe('useProjectConsultations', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchIndex.mockReset();
  });
  afterEach(() => { vi.useRealTimers(); });

  test('keeps the same Map identity when a poll returns unchanged rows', async () => {
    fetchIndex.mockResolvedValue({ consultations: [row()], limit: 20 });
    const { result } = renderHook(() => useProjectConsultations('p', true));

    await waitFor(() => expect(result.current.rowsById.size).toBe(1));
    const first = result.current.rowsById;

    await act(async () => { await vi.advanceTimersByTimeAsync(6500); });
    await waitFor(() => expect(fetchIndex).toHaveBeenCalledTimes(2));

    // MessageBubble is memoized and this Map rides in `bubbleProps`; a fresh
    // Map every 6s would re-render the whole transcript for no new information.
    expect(result.current.rowsById).toBe(first);
  });

  test('replaces the Map when a consultation actually advances', async () => {
    fetchIndex.mockResolvedValueOnce({ consultations: [row()], limit: 20 });
    fetchIndex.mockResolvedValue({
      consultations: [row({ state: 'delivered', outcome: 'completed', step_count: 4 })],
      limit: 20,
    });
    const { result } = renderHook(() => useProjectConsultations('p', true));

    await waitFor(() => expect(result.current.rowsById.get('c-1')?.state).toBe('running'));
    const first = result.current.rowsById;

    await act(async () => { await vi.advanceTimersByTimeAsync(6500); });
    await waitFor(() => expect(result.current.rowsById.get('c-1')?.state).toBe('delivered'));
    expect(result.current.rowsById).not.toBe(first);
  });

  test('stops polling once nothing is live', async () => {
    fetchIndex.mockResolvedValue({
      consultations: [row({ state: 'delivered', outcome: 'completed' })],
      limit: 20,
    });
    const { result } = renderHook(() => useProjectConsultations('p', true));

    await waitFor(() => expect(result.current.rowsById.size).toBe(1));
    expect(fetchIndex).toHaveBeenCalledTimes(1);

    await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
    // A delivered consultation never changes again — polling it is pure cost.
    expect(fetchIndex).toHaveBeenCalledTimes(1);
  });

  test('reads nothing when the panel is not in conversation mode', async () => {
    fetchIndex.mockResolvedValue({ consultations: [row()], limit: 20 });
    const { result } = renderHook(() => useProjectConsultations('p', false));

    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(fetchIndex).not.toHaveBeenCalled();
    expect(result.current.rowsById.size).toBe(0);
  });

  test('surfaces a read failure instead of showing an empty transcript', async () => {
    fetchIndex.mockRejectedValue(new Error('403 Forbidden'));
    const { result } = renderHook(() => useProjectConsultations('p', true));

    await waitFor(() => expect(result.current.error).toBe('403 Forbidden'));
    expect(result.current.rowsById.size).toBe(0);
  });
});
