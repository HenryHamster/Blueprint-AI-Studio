import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { afterEach, beforeEach, describe, expect, test, vi } from 'vitest';
import { useDashboardDigest } from './useDashboardDigest';
import { useOpenPRs } from './useOpenPRs';
import { useProjectMetrics } from './useProjectMetrics';

const mocks = vi.hoisted(() => ({
  apiFetch: vi.fn(),
  fetchFleetUtilization: vi.fn(),
  fetchAgentServingShas: vi.fn(),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    apiFetch: mocks.apiFetch,
    fetchFleetUtilization: mocks.fetchFleetUtilization,
    fetchAgentServingShas: mocks.fetchAgentServingShas,
  });
});

function Harness({ isAdmin }: { isAdmin: boolean }) {
  useDashboardDigest('project-1', 24, 1, 2, isAdmin);
  useOpenPRs('project-1', isAdmin);
  useProjectMetrics('project-1', 24, '', isAdmin);
  return null;
}

describe('dashboard access gating', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.useFakeTimers();
    mocks.apiFetch.mockResolvedValue(new Response('{}', { status: 200 }));
    mocks.fetchFleetUtilization.mockResolvedValue({ pools: [] });
    mocks.fetchAgentServingShas.mockResolvedValue({});
  });

  afterEach(() => {
    if (root) act(() => root?.unmount());
    host?.remove();
    root = null;
    host = null;
    vi.useRealTimers();
    vi.clearAllMocks();
  });

  test('non-admin polling never requests platform analytics and keeps project-safe metrics', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);

    await act(async () => {
      root?.render(<Harness isAdmin={false} />);
      await Promise.resolve();
    });
    await act(async () => {
      vi.advanceTimersByTime(60_000);
      await Promise.resolve();
    });

    const urls = mocks.apiFetch.mock.calls.map(([url]) => String(url));
    expect(urls.some(url => url.includes('/api/admin/digest'))).toBe(false);
    expect(urls.some(url => url.includes('/api/admin/open-prs'))).toBe(false);
    expect(urls.some(url => url.includes('/medium-scope-measurement'))).toBe(false);
    expect(urls.some(url => url.includes('/scope-baselines'))).toBe(false);
    expect(urls.some(url => url.includes('/max-plan-accounts/health'))).toBe(false);
    expect(urls.some(url => url.includes('/task-duration-stats'))).toBe(false);
    expect(urls.some(url => url.includes('/health-overview'))).toBe(false);
    expect(urls.some(url => url.includes('/sr-blind-spots'))).toBe(false);
    expect(urls.some(url => url.includes('/sr-recent-misses'))).toBe(false);
    expect(urls.some(url => url.includes('/direction-sr-breakdown'))).toBe(false);
    expect(mocks.fetchFleetUtilization).not.toHaveBeenCalled();

    expect(urls).toContain('/api/admin/projects/project-1/system-maturity');
    expect(urls).toContain('/api/admin/projects/project-1/prompt-fingerprint-analytics');
    expect(mocks.fetchAgentServingShas).toHaveBeenCalledWith('project-1');
  });

  test('admin polling includes SR diagnostic analytics', async () => {
    host = document.createElement('div');
    document.body.appendChild(host);
    root = createRoot(host);

    await act(async () => {
      root?.render(<Harness isAdmin={true} />);
      await Promise.resolve();
    });

    const urls = mocks.apiFetch.mock.calls.map(([url]) => String(url));
    expect(urls).toContain('/api/admin/projects/project-1/sr-blind-spots');
    expect(urls).toContain('/api/admin/projects/project-1/sr-recent-misses');
    expect(urls).toContain('/api/admin/projects/project-1/direction-sr-breakdown');
  });
});
