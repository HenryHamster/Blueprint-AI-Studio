import { beforeEach, describe, expect, test, vi } from 'vitest';

const { getIdToken } = vi.hoisted(() => ({ getIdToken: vi.fn() }));
vi.mock('./firebase', () => ({ getIdToken }));

import { currentUiSurface, reportUiLoadTiming } from './uiLoadTiming';

describe('ui load timing reporter', () => {
  beforeEach(() => {
    getIdToken.mockReset();
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response()));
    window.location.hash = '#/runtime/health';
  });

  test('posts an authenticated, typed timing sample through the shared reporter', async () => {
    getIdToken.mockResolvedValue('timing-token');

    reportUiLoadTiming(currentUiSurface(), 321, 'board_data_ready');
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));

    expect(fetch).toHaveBeenCalledWith('/api/admin/ui-load-timing', expect.objectContaining({
      method: 'POST',
      headers: { Authorization: 'Bearer timing-token', 'Content-Type': 'application/json' },
      body: JSON.stringify({ surface: 'runtime', page_ready_ms: 321, sample_type: 'board_data_ready' }),
    }));
  });

  test('does not post when authentication has no token', async () => {
    getIdToken.mockResolvedValue(null);

    reportUiLoadTiming('pipeline', 100, 'board_data_ready');
    await Promise.resolve();

    expect(fetch).not.toHaveBeenCalled();
  });
});
