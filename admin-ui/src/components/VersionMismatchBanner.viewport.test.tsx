import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { VersionMismatchBanner } from './VersionMismatchBanner';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

// VersionMismatchBanner calls raw fetch('/api/version') on mount.
// vitest.viewport.config.ts defines VITE_EXPECTED_BACKEND_SHA = 'test-expected-sha'
// so the component's useEffect is active for all tests. Tests control the banner
// visibility by stubbing fetch to return matching or non-matching SHAs.

describe('VersionMismatchBanner — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    vi.restoreAllMocks();
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('renders null (no banner) when SHAs match — no overflow at 375px', async () => {
    // Fetch returns the same SHA as VITE_EXPECTED_BACKEND_SHA → mismatch=false → null render.
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify({ sha: 'test-expected-sha' }), { status: 200 }),
    ));

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<VersionMismatchBanner />);
      });
      // Let the fetch promise resolve and state update.
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 100));
      });
    });
  });

  test('mismatch banner renders without horizontal overflow at 375px', async () => {
    // Fetch returns a different SHA → mismatch=true → banner renders.
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify({ sha: 'different-backend-sha' }), { status: 200 }),
    ));

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<VersionMismatchBanner />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 100));
      });
    }, { waitMs: 50 });
  });

  test('dismiss button meets 44px touch target when banner is visible', async () => {
    vi.stubGlobal('fetch', vi.fn(async () =>
      new Response(JSON.stringify({ sha: 'different-backend-sha' }), { status: 200 }),
    ));

    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<VersionMismatchBanner />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 100));
      });
    }, { waitMs: 50 });
  });

  test('banner hidden when fetch fails — no overflow at 375px', async () => {
    // Network error → catch block fires → mismatch stays false → null render.
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('network error'); }));

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<VersionMismatchBanner />);
      });
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 100));
      });
    });
  });
});
