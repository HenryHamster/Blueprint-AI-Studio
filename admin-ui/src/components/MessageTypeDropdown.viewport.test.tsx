import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { MessageTypeDropdown } from './MessageTypeDropdown';
import { expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
}));

describe('MessageTypeDropdown — viewport regression', () => {
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

  test('renders without overflow at 375px in closed state', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MessageTypeDropdown
            value="FEEDBACK"
            onChange={() => {}}
          />,
        );
      });
    });
  });

  test('renders without overflow at 375px in open state', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MessageTypeDropdown
            value="FEEDBACK"
            onChange={() => {}}
          />,
        );
      });
      const button = host.querySelector('button');
      if (button) {
        await act(async () => {
          button.click();
        });
      }
    });
  });

  test('renders without overflow at 375px with BLOCKER selected', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MessageTypeDropdown
            value="BLOCKER"
            onChange={() => {}}
          />,
        );
      });
    });
  });

  test('renders without overflow at 375px with BUG_REPORT selected', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <MessageTypeDropdown
            value="BUG_REPORT"
            onChange={() => {}}
          />,
        );
      });
    });
  });
});
