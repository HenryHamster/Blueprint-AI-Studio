import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MessagesPanel } from './MessagesPanel';
import { expectNoOverflowAt } from '../test/viewport';

// Mock the api module so MessagesPanel doesn't try to fetch real data
vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => {
    return new Response(JSON.stringify({ messages: [] }), { status: 200 });
  }),
  API_BASE_URL: 'http://localhost:8000',
}));

describe('MessagesPanel — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MessagesPanel
            selectedProjectId="project-123"
            onClose={() => {}}
            addToast={() => {}}
            onMessageCountChange={() => {}}
          />,
        );
      });
      // Allow async state updates (mock fetches resolve) to settle
      await act(async () => {
        await new Promise<void>((resolve) => setTimeout(resolve, 50));
      });
    });
  });
});
