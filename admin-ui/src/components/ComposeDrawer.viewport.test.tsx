import { afterEach, beforeEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectFullViewportAt } from '../test/viewport';

// ComposeDrawer reads `open` user messages on mount via apiFetch and renders
// them in a "Recent" strip above MessagesComposer. Mock '../api' so the fetch
// resolves with a couple of recent sent messages (exercising that strip) rather
// than hitting real auth at module-load time.
const recentMessagesResponse = {
  messages: [
    {
      id: 'msg-1',
      content: 'Prioritise the mobile-responsiveness direction this cycle',
      message_type: 'FEEDBACK',
      created_at: new Date(Date.now() - 3_600_000).toISOString(),
      from_type: 'user',
    },
    {
      id: 'msg-2',
      content: 'Pause the warm-pool experiment until the cost regression clears',
      message_type: 'NOTE',
      created_at: new Date(Date.now() - 7_200_000).toISOString(),
      from_type: 'user',
    },
    // A strategist message that must be filtered out (from_type !== 'user').
    {
      id: 'msg-3',
      content: 'Acknowledged — adjusting the plan.',
      message_type: 'FEEDBACK',
      created_at: new Date(Date.now() - 1_800_000).toISOString(),
      from_type: 'strategist',
    },
  ],
};

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () =>
    new Response(JSON.stringify(recentMessagesResponse), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }),
  ),
  API_BASE_URL: 'http://localhost:8000',
}));

import { ComposeDrawer } from './ComposeDrawer';

describe('ComposeDrawer — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  beforeEach(() => {
    try {
      localStorage.clear();
    } catch {
      /* ignore */
    }
  });

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

  // ComposeDrawer is a `fixed inset-x-0 bottom-0` sheet anchored to the viewport,
  // so it lays out full-bleed. A single expectFullViewportAt covers overflow +
  // text-truncation + touch-target at 375px.
  //
  // touchSelector excludes the two intentionally-compact header chrome controls:
  //   - the "Close" icon button (24px `p-1`), already carrying an
  //     `eslint-disable viewport/min-touch-target` opt-out in ComposeDrawer.tsx;
  //   - the "View all messages" tertiary text link (`text-xs`), a low-priority
  //     navigation affordance, not a primary touch target.
  // The MessagesComposer primary controls — the message-type selector (95×44)
  // and the send button (44×44) — are still asserted against the 44px floor.
  test('sheet renders without overflow/truncation/small-target at 375px', async () => {
    await expectFullViewportAt(
      375,
      async () => {
        host = document.createElement('div');
        document.body.appendChild(host);
        const r = createRoot(host);
        root = r;
        await act(async () => {
          r.render(
            <ComposeDrawer
              projectId="proj-canary"
              userEmail="operator@example.com"
              onOpenFullPanel={() => {}}
              onClose={() => {}}
              addToast={() => {}}
              strategistActive={false}
            />,
          );
        });
        // Let the recent-messages fetch resolve and the "Recent" strip render.
        await act(async () => {
          await new Promise<void>((resolve) => setTimeout(resolve, 50));
        });
      },
      {
        // `[class*="text-xs"]` matches the "View all messages" tertiary link;
        // `[title="Close"]` matches the eslint-exempt close icon.
        touchSelector: 'button:not([title="Close"]):not([class*="text-xs"])',
        waitMs: 60,
      },
    );
  });
});
