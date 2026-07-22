import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { renderHook, act, cleanup, waitFor } from '@testing-library/react';
import { useStrategicLiveness } from './useStrategicLiveness';
import { fetchStrategicLiveness, type StrategicLivenessResponse } from '../strategicStateApi';

vi.mock('../strategicStateApi', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../strategicStateApi')>()),
  fetchStrategicLiveness: vi.fn(),
}));

const mockFetch = vi.mocked(fetchStrategicLiveness);

function makeLiveness(projectId = 'proj-1'): StrategicLivenessResponse {
  return {
    project_id: projectId,
    interaction_mode: 'chat',
    resident_session: { session_id: `ss-${projectId}`, status: 'active', stop_requested: false },
    resident_session_availability: { available: true, state: 'active', reason: null },
    resident_session_runners: [],
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('useStrategicLiveness', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });
  afterEach(() => {
    cleanup();
  });

  it('returns null data and does not fetch when projectId is null', () => {
    const { result } = renderHook(() => useStrategicLiveness(null));
    expect(result.current.data).toBeNull();
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('does not fetch when disabled', () => {
    renderHook(() => useStrategicLiveness('proj-1', { enabled: false }));
    expect(mockFetch).not.toHaveBeenCalled();
  });

  it('fetches on mount and populates data', async () => {
    mockFetch.mockResolvedValueOnce(makeLiveness('proj-1'));
    const { result } = renderHook(() => useStrategicLiveness('proj-1'));
    await waitFor(() => expect(result.current.data?.project_id).toBe('proj-1'));
    expect(result.current.data?.resident_session?.session_id).toBe('ss-proj-1');
    expect(mockFetch).toHaveBeenCalledTimes(1);
  });

  it('clears data immediately on project change and refetches for the new project', async () => {
    mockFetch.mockResolvedValueOnce(makeLiveness('proj-1'));
    const { result, rerender } = renderHook(
      ({ projectId }: { projectId: string | null }) => useStrategicLiveness(projectId),
      { initialProps: { projectId: 'proj-1' as string | null } },
    );
    await waitFor(() => expect(result.current.data?.project_id).toBe('proj-1'));

    mockFetch.mockResolvedValueOnce(makeLiveness('proj-2'));
    act(() => { rerender({ projectId: 'proj-2' }); });
    // Data cleared synchronously before the new fetch resolves.
    expect(result.current.data).toBeNull();
    await waitFor(() => expect(result.current.data?.project_id).toBe('proj-2'));
  });

  it('drops a stale response whose project no longer matches (generation guard)', async () => {
    const slow = deferred<StrategicLivenessResponse>();
    mockFetch.mockReturnValueOnce(slow.promise);
    const { result, rerender } = renderHook(
      ({ projectId }: { projectId: string | null }) => useStrategicLiveness(projectId),
      { initialProps: { projectId: 'proj-1' as string | null } },
    );

    // Switch project while proj-1's probe is still in flight.
    mockFetch.mockResolvedValueOnce(makeLiveness('proj-2'));
    act(() => { rerender({ projectId: 'proj-2' }); });
    await waitFor(() => expect(result.current.data?.project_id).toBe('proj-2'));

    // The stale proj-1 probe resolves late — it must NOT overwrite proj-2's data.
    await act(async () => {
      slow.resolve(makeLiveness('proj-1'));
      await slow.promise;
    });
    expect(result.current.data?.project_id).toBe('proj-2');
  });

  it('swallows a failed probe and leaves data null', async () => {
    mockFetch.mockRejectedValueOnce(new Error('boom'));
    const { result } = renderHook(() => useStrategicLiveness('proj-1'));
    await waitFor(() => expect(mockFetch).toHaveBeenCalledTimes(1));
    expect(result.current.data).toBeNull();
  });
});
