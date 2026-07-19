import { beforeEach, describe, expect, test, vi } from 'vitest';

const { getIdTokenAfterAuthRestore } = vi.hoisted(() => ({ getIdTokenAfterAuthRestore: vi.fn() }));
vi.mock('./firebase', async () => {
  const { createFirebaseMock } = await import('./test/firebase-mock-complete');
  return createFirebaseMock({ getIdTokenAfterAuthRestore });
});

import { currentUiSurface, reportUiLoadTiming } from './uiLoadTiming';

describe('ui load timing reporter', () => {
  beforeEach(() => {
    getIdTokenAfterAuthRestore.mockReset();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response()));
    window.location.hash = '#/runtime/health';
  });

  test('posts an authenticated, typed timing sample through the shared reporter', async () => {
    getIdTokenAfterAuthRestore.mockResolvedValue('timing-token');

    reportUiLoadTiming(currentUiSurface(), 321, 'board_data_ready');
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));

    expect(fetch).toHaveBeenCalledWith('/api/admin/ui-load-timing', expect.objectContaining({
      method: 'POST',
      headers: { Authorization: 'Bearer timing-token', 'Content-Type': 'application/json' },
      body: JSON.stringify({ surface: 'runtime', page_ready_ms: 321, sample_type: 'board_data_ready' }),
    }));
  });

  test('does not post when authentication has no token', async () => {
    getIdTokenAfterAuthRestore.mockResolvedValue(null);

    reportUiLoadTiming('pipeline', 100, 'board_data_ready');
    await Promise.resolve();

    expect(fetch).not.toHaveBeenCalled();
  });

  test('posts once after auth restoration while retaining the original measurement', async () => {
    let restoreToken: (token: string) => void = () => {};
    getIdTokenAfterAuthRestore.mockImplementation(
      () => new Promise(resolve => { restoreToken = resolve; }),
    );

    reportUiLoadTiming('chat', 123, 'first_commit');
    expect(fetch).not.toHaveBeenCalled();

    restoreToken('restored-token');
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    expect(fetch).toHaveBeenCalledWith('/api/admin/ui-load-timing', expect.objectContaining({
      headers: { Authorization: 'Bearer restored-token', 'Content-Type': 'application/json' },
      body: JSON.stringify({ surface: 'chat', page_ready_ms: 123, sample_type: 'first_commit' }),
    }));
  });

  test('silently drops a timing sample when auth restoration times out', async () => {
    getIdTokenAfterAuthRestore.mockRejectedValue(new Error('Authentication restore timed out'));

    reportUiLoadTiming('chat', 123, 'first_commit');
    await vi.waitFor(() => expect(getIdTokenAfterAuthRestore).toHaveBeenCalledOnce());
    await Promise.resolve();

    expect(fetch).not.toHaveBeenCalled();
  });
});
