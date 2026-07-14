import { afterEach, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProjectToolIframe } from './ProjectToolIframe';

vi.mock('../api/projectTools', async () => {
  const actual = await vi.importActual<typeof import('../api/projectTools')>('../api/projectTools');
  return {
    ...actual,
    fetchAssetToken: vi.fn(),
  };
});

import { fetchAssetToken } from '../api/projectTools';

const MANIFEST_TOOL = {
  slug: 'custom-viewer',
  title: 'Custom Viewer',
  description: 'A project-declared viewer tool',
  placement: 'project_section' as const,
  entry: 'index.html',
  kind: 'project' as const,
  source: 'project' as const,
};

describe('ProjectToolIframe — asset token refresh', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-07-14T00:00:00Z'));
  });

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.useRealTimers();
    vi.resetAllMocks();
  });

  function currentSrc(): string {
    if (!host) throw new Error('host not mounted');
    return host.querySelector('iframe')?.getAttribute('src') ?? '';
  }

  test('re-mints the token before it expires and swaps the iframe src', async () => {
    vi.mocked(fetchAssetToken)
      .mockResolvedValueOnce({ token: 'token-1', expires_at: '2026-07-14T00:05:00Z' })
      .mockResolvedValueOnce({ token: 'token-2', expires_at: '2026-07-14T00:10:00Z' });

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<ProjectToolIframe projectId="proj-a" tool={MANIFEST_TOOL} />); });
    // Flush the initial token fetch (microtasks) without advancing wall clock.
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });

    expect(vi.mocked(fetchAssetToken)).toHaveBeenCalledTimes(1);
    expect(currentSrc()).toContain('token=token-1');

    // Just before the 5-minute TTL (refresh fires at TTL − 30s = 270s) nothing
    // has refreshed yet.
    await act(async () => { await vi.advanceTimersByTimeAsync(260_000); });
    expect(vi.mocked(fetchAssetToken)).toHaveBeenCalledTimes(1);

    // Cross the refresh threshold: a second token is minted and the src swaps.
    await act(async () => { await vi.advanceTimersByTimeAsync(20_000); });
    expect(vi.mocked(fetchAssetToken)).toHaveBeenCalledTimes(2);
    expect(currentSrc()).toContain('token=token-2');
  });

  test('stops refreshing after unmount', async () => {
    vi.mocked(fetchAssetToken).mockResolvedValue({ token: 'token-1', expires_at: '2026-07-14T00:05:00Z' });

    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<ProjectToolIframe projectId="proj-a" tool={MANIFEST_TOOL} />); });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(vi.mocked(fetchAssetToken)).toHaveBeenCalledTimes(1);

    await act(async () => { r.unmount(); });
    root = null;

    // The scheduled refresh timer must have been cleared on unmount.
    await act(async () => { await vi.advanceTimersByTimeAsync(600_000); });
    expect(vi.mocked(fetchAssetToken)).toHaveBeenCalledTimes(1);
  });
});
