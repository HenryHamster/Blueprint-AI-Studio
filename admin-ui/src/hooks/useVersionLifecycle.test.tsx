import { describe, it, expect, vi, afterEach } from 'vitest';
import { cleanup, renderHook, waitFor } from '@testing-library/react';
import { apiFetch } from '../api';
import { useVersionLifecycle } from './useVersionLifecycle';

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({ apiFetch: vi.fn() });
});

const mockApiFetch = vi.mocked(apiFetch);

describe('useVersionLifecycle', () => {
  afterEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it('does not fetch until Runtime polling is enabled, then makes the leading request', async () => {
    mockApiFetch.mockResolvedValue({
      ok: true,
      json: async () => ({ current_version: null, recent_versions: [] }),
    } as Response);
    const { rerender } = renderHook(
      ({ enabled }) => useVersionLifecycle('proj-1', { pollerEnabled: enabled }),
      { initialProps: { enabled: false } },
    );

    expect(mockApiFetch).not.toHaveBeenCalled();
    rerender({ enabled: true });
    await waitFor(() => expect(mockApiFetch).toHaveBeenCalledWith('/api/admin/projects/proj-1/version-lifecycle'));
  });
});
