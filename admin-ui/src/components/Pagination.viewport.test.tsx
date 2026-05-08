import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { Pagination } from './Pagination';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

describe('Pagination — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px with large totals', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <Pagination
            page={11}
            totalTasks={12345}
            taskCount={25}
            serverTaskCount={25}
            limit={25}
            onPageChange={() => {}}
            onLimitChange={() => {}}
          />,
        );
      });
    });
  });

  test('previous/next buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <Pagination
            page={50}
            totalTasks={12345}
            taskCount={25}
            serverTaskCount={25}
            limit={25}
            onPageChange={() => {}}
            onLimitChange={() => {}}
          />,
        );
      });
    });
  });

  test('renders without overflow with many pages at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <Pagination
            page={250}
            totalTasks={50000}
            taskCount={25}
            serverTaskCount={25}
            limit={25}
            onPageChange={() => {}}
            onLimitChange={() => {}}
          />,
        );
      });
    });
  });
});
