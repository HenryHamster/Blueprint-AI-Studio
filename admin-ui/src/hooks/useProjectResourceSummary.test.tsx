import { act, cleanup, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { PROJECT_RESOURCE_SUMMARY_POLL_MS, useProjectResourceSummary } from './useProjectResourceSummary';
import { __pollerInternals } from './usePoller';

const mocks = vi.hoisted(() => ({ apiFetch: vi.fn() }));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({ API_BASE_URL: 'http://blueprint.test', apiFetch: mocks.apiFetch });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(next => { resolve = next; });
  return { promise, resolve };
}

function summary(projectId: string, activeTasks = 2) {
  return {
    project_id: projectId,
    policy: { execution_resources: 'project owner', attribution: 'launcher', managed_by: 'owner' },
    accounts: {
      total: 4,
      enabled: 3,
      enabled_below_cap: 2,
      at_cap: 1,
      project_active_tasks: activeTasks,
      project_daily_task_count: 7,
      project_daily_task_cap: 30,
      project_max_concurrent_tasks: 6,
    },
    runners: { total: 3, online: 2, offline: 1 },
  };
}

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

beforeEach(() => {
  mocks.apiFetch.mockReset();
  __pollerInternals.reset();
});

afterEach(() => {
  cleanup();
  __pollerInternals.reset();
  vi.useRealTimers();
});

describe('useProjectResourceSummary', () => {
  test('loads the aggregate only for an enabled, confirmed project scope', async () => {
    mocks.apiFetch.mockResolvedValue(response(summary('project-a')));
    const { result } = renderHook(() => useProjectResourceSummary('project-a', true));

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.data?.project_id).toBe('project-a'));
    expect(mocks.apiFetch).toHaveBeenCalledWith(
      'http://blueprint.test/api/admin/projects/project-a/resource-summary',
      { method: 'GET' },
      2,
      30_000,
      expect.any(AbortSignal),
      false,
    );
    expect(result.current.data?.accounts.enabled_below_cap).toBe(2);
  });

  test('makes no request for a missing, unrelated, or disabled project scope', () => {
    const { result, rerender } = renderHook(
      ({ projectId, enabled }) => useProjectResourceSummary(projectId, enabled),
      { initialProps: { projectId: null as string | null, enabled: true } },
    );
    expect(result.current).toMatchObject({ data: null, loading: false, error: null });
    rerender({ projectId: 'project-a', enabled: false });
    expect(result.current).toMatchObject({ data: null, loading: false, error: null });
    expect(mocks.apiFetch).not.toHaveBeenCalled();
  });

  test('clears immediately and ignores a late A response after switching to B', async () => {
    const projectA = deferred<Response>();
    const projectB = deferred<Response>();
    mocks.apiFetch.mockImplementation((url: string) => (
      url.includes('project-a') ? projectA.promise : projectB.promise
    ));
    const { result, rerender } = renderHook(
      ({ projectId }) => useProjectResourceSummary(projectId, true),
      { initialProps: { projectId: 'project-a' } },
    );

    rerender({ projectId: 'project-b' });
    expect(result.current.data).toBeNull();
    await act(async () => { projectB.resolve(response(summary('project-b', 5))); });
    await waitFor(() => expect(result.current.data?.accounts.project_active_tasks).toBe(5));
    await act(async () => { projectA.resolve(response(summary('project-a', 99))); });
    expect(result.current.data?.project_id).toBe('project-b');
    expect(result.current.data?.accounts.project_active_tasks).toBe(5);
  });

  test('clears and ignores an in-flight response when access is revoked', async () => {
    const pending = deferred<Response>();
    mocks.apiFetch.mockReturnValue(pending.promise);
    const { result, rerender } = renderHook(
      ({ enabled }) => useProjectResourceSummary('project-a', enabled),
      { initialProps: { enabled: true } },
    );

    rerender({ enabled: false });
    expect(result.current).toMatchObject({ data: null, loading: false, error: null });
    await act(async () => { pending.resolve(response(summary('project-a'))); });
    expect(result.current.data).toBeNull();
  });

  test('surfaces canonical API errors and rejects malformed counts', async () => {
    mocks.apiFetch
      .mockResolvedValueOnce(response({ error: { message: 'Project access denied' } }, 403))
      .mockResolvedValueOnce(response({
        ...summary('project-a'),
        accounts: { ...summary('project-a').accounts, total: -1, enabled_below_cap: 'many', project_daily_task_cap: 'many' },
        runners: { total: null, online: -2, offline: 3 },
      }));

    const { result, rerender } = renderHook(
      ({ nonce }) => useProjectResourceSummary('project-a', nonce > 0),
      { initialProps: { nonce: 1 } },
    );
    await waitFor(() => expect(result.current.error).toBe('Project access denied'));
    rerender({ nonce: 0 });
    rerender({ nonce: 2 });
    await waitFor(() => expect(result.current.error).toBe('Invalid project resource summary'));
    expect(result.current.data).toBeNull();
  });

  test.each([
    ['missing required count', (value: ReturnType<typeof summary>) => { delete (value.accounts as Partial<typeof value.accounts>).total; }],
    ['string count', (value: ReturnType<typeof summary>) => { value.accounts.project_active_tasks = '2' as unknown as number; }],
    ['fractional count', (value: ReturnType<typeof summary>) => { value.runners.online = 1.5; }],
    ['negative count', (value: ReturnType<typeof summary>) => { value.accounts.project_daily_task_count = -1; }],
    ['missing nullable cap', (value: ReturnType<typeof summary>) => { delete (value.accounts as Partial<typeof value.accounts>).project_daily_task_cap; }],
    ['malformed nullable cap', (value: ReturnType<typeof summary>) => { value.accounts.project_max_concurrent_tasks = 'none' as unknown as number; }],
    ['enabled above total', (value: ReturnType<typeof summary>) => { value.accounts.enabled = 5; value.accounts.enabled_below_cap = 4; }],
    ['account partition mismatch', (value: ReturnType<typeof summary>) => { value.accounts.at_cap = 0; }],
    ['runner partition mismatch', (value: ReturnType<typeof summary>) => { value.runners.offline = 0; }],
  ])('rejects an invalid exact contract: %s', async (_label, mutate) => {
    const invalid = structuredClone(summary('project-a'));
    mutate(invalid);
    mocks.apiFetch.mockResolvedValue(response(invalid));

    const { result } = renderHook(() => useProjectResourceSummary('project-a', true));

    await waitFor(() => expect(result.current.error).toBe('Invalid project resource summary'));
    expect(result.current.data).toBeNull();
  });

  test('polls on one shared cadence and aborts/tears down immediately when disabled', async () => {
    vi.useFakeTimers();
    const second = deferred<Response>();
    mocks.apiFetch
      .mockResolvedValueOnce(response(summary('project-a')))
      .mockReturnValueOnce(second.promise);
    const { result, rerender } = renderHook(
      ({ enabled }) => useProjectResourceSummary('project-a', enabled),
      { initialProps: { enabled: true } },
    );

    await act(async () => { await Promise.resolve(); await Promise.resolve(); });
    expect(result.current.data?.project_id).toBe('project-a');
    expect(mocks.apiFetch).toHaveBeenCalledTimes(1);
    expect(__pollerInternals.subscriberCount(PROJECT_RESOURCE_SUMMARY_POLL_MS)).toBe(1);

    rerender({ enabled: true });
    expect(mocks.apiFetch).toHaveBeenCalledTimes(1);
    expect(__pollerInternals.subscriberCount(PROJECT_RESOURCE_SUMMARY_POLL_MS)).toBe(1);

    await act(async () => {
      vi.advanceTimersByTime(PROJECT_RESOURCE_SUMMARY_POLL_MS);
      await Promise.resolve();
    });
    expect(mocks.apiFetch).toHaveBeenCalledTimes(2);
    const inFlightSignal = mocks.apiFetch.mock.calls[1]?.[4] as AbortSignal;
    expect(inFlightSignal.aborted).toBe(false);

    rerender({ enabled: false });
    expect(inFlightSignal.aborted).toBe(true);
    expect(result.current).toMatchObject({ data: null, loading: false, error: null });
    expect(__pollerInternals.subscriberCount(PROJECT_RESOURCE_SUMMARY_POLL_MS)).toBe(0);
    expect(__pollerInternals.hasActiveTimers()).toBe(false);

    act(() => { vi.advanceTimersByTime(PROJECT_RESOURCE_SUMMARY_POLL_MS * 2); });
    expect(mocks.apiFetch).toHaveBeenCalledTimes(2);
  });

  test('aborts an in-flight request and removes its timer on unmount', () => {
    vi.useFakeTimers();
    mocks.apiFetch.mockReturnValue(new Promise<Response>(() => {}));
    const { unmount } = renderHook(() => useProjectResourceSummary('project-a', true));

    expect(mocks.apiFetch).toHaveBeenCalledTimes(1);
    const signal = mocks.apiFetch.mock.calls[0]?.[4] as AbortSignal;
    expect(signal.aborted).toBe(false);
    expect(__pollerInternals.subscriberCount(PROJECT_RESOURCE_SUMMARY_POLL_MS)).toBe(1);

    unmount();
    expect(signal.aborted).toBe(true);
    expect(__pollerInternals.subscriberCount(PROJECT_RESOURCE_SUMMARY_POLL_MS)).toBe(0);
    expect(__pollerInternals.hasActiveTimers()).toBe(false);
  });
});
