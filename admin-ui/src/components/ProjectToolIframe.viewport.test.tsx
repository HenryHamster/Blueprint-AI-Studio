import { afterEach, beforeEach, describe, test, expect, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProjectToolIframe } from './ProjectToolIframe';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

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

describe('ProjectToolIframe — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    vi.mocked(fetchAssetToken).mockResolvedValue({ token: 'mock-token', expires_at: '2026-07-11T12:00:00Z' });
  });

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
    vi.resetAllMocks();
  });

  test('renders iframe without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<ProjectToolIframe projectId="proj-a" tool={MANIFEST_TOOL} />); });
      await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    });
    if (!host) throw new Error('host not mounted');
    // The mocked token fetch resolves inside act, so the component has settled
    // into its ready state: the loading placeholder is gone and the sandboxed
    // opaque-origin iframe is the real signal that the tool rendered.
    const iframe = host.querySelector('iframe');
    expect(iframe).not.toBeNull();
    expect(iframe?.getAttribute('sandbox')).toBe('allow-scripts');
  });

  test('renders iframe without overflow at 1440px', async () => {
    await expectNoOverflowAt(1440, async () => {
      host = document.createElement('div');
      host.style.width = '1440px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<ProjectToolIframe projectId="proj-a" tool={MANIFEST_TOOL} />); });
      await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    });
    if (!host) throw new Error('host not mounted');
    const iframe = host.querySelector('iframe');
    expect(iframe).not.toBeNull();
    expect(iframe?.getAttribute('sandbox')).toBe('allow-scripts');
  });

  test('shows error state without overflow at 375px', async () => {
    vi.mocked(fetchAssetToken).mockRejectedValue(new Error('Token fetch failed'));
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<ProjectToolIframe projectId="proj-a" tool={MANIFEST_TOOL} />); });
      await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    });
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('Token fetch failed');
  });

  test('no text truncation at 375px while loading', async () => {
    // Hold the token fetch pending so the component actually stays in its
    // loading state for the duration of the truncation check.
    vi.mocked(fetchAssetToken).mockReturnValue(new Promise(() => {}));
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<ProjectToolIframe projectId="proj-a" tool={MANIFEST_TOOL} />); });
      await act(async () => await new Promise(resolve => setTimeout(resolve, 50)));
    });
    if (!host) throw new Error('host not mounted');
    expect(host.textContent).toContain('Loading tool');
  });
});
