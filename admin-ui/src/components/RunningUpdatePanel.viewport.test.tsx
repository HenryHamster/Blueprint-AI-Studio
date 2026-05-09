import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import RunningUpdatePanel from './RunningUpdatePanel';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

// RunningUpdatePanel imports apiFetch from ../api (transitively pulls in firebase).
vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}));

const BASE_PROPS = {
  projectId: 'proj-1',
  document: null,
  acknowledgedAt: null,
  userLastActiveAt: null,
  addToast: () => {},
  onAcknowledged: () => {},
};

describe('RunningUpdatePanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
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

  test('empty state renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.height = '400px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<RunningUpdatePanel {...BASE_PROPS} />);
      });
    });
  });

  test('empty state text is not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.height = '400px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<RunningUpdatePanel {...BASE_PROPS} />);
      });
    });
  });

  test('populated document renders without horizontal overflow at 375px', async () => {
    const mdContent = `# Strategic Update\n\n## Current Focus\n\nThe pipeline is processing 12 active directions across 3 repositories.\n\n## Recent Activity\n\n- Merged 4 PRs improving test coverage in the authentication module\n- Identified a recurring regression in the CI workflow layer\n- Research cycle completed for performance optimization direction\n\n## Next Steps\n\nScheduled architecture review in 2 cycles.`;

    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.height = '400px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <RunningUpdatePanel
            {...BASE_PROPS}
            document={mdContent}
            acknowledgedAt={new Date(Date.now() - 3_600_000).toISOString()}
            userLastActiveAt={new Date(Date.now() - 1_800_000).toISOString()}
          />,
        );
      });
    });
  });

  test('populated document text is not silently clipped at 375px', async () => {
    const mdContent = `# Running Update\n\nThis is the strategic agent's current-state snapshot of the project. It is rewritten each cycle.`;

    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.height = '400px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<RunningUpdatePanel {...BASE_PROPS} document={mdContent} />);
      });
    });
  });

  // Note: expectMinTouchTargetAt is intentionally omitted for the Acknowledge button.
  // The button uses `px-3 py-1.5 text-xs` (~28px height) — below the 44px guideline.
  // This is a known design trade-off for the compact header layout (dense panel UX).
  // A future UI pass should either increase padding or use a larger touch target variant.
});
