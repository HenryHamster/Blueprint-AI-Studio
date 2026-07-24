import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SearchFilterBar } from './SearchFilterBar';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

describe('SearchFilterBar — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px with search active', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.display = 'flex';
      host.style.gap = '0.5rem';
      host.style.flexWrap = 'wrap';
      host.style.alignItems = 'center';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <SearchFilterBar
            searchQuery="test query"
            onSearchChange={() => {}}
            taskTypeFilter={['create', 'fix']}
            onTypeFilterChange={() => {}}
            dateRange="24h"
            onDateRangeChange={() => {}}
            blockedFilter={true}
            onBlockedFilterChange={() => {}}
          />,
        );
      });
    });
  });

  test('renders without overflow at 375px with multiple filters active and clear button', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.display = 'flex';
      host.style.gap = '0.5rem';
      host.style.flexWrap = 'wrap';
      host.style.alignItems = 'center';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <SearchFilterBar
            searchQuery="migration"
            onSearchChange={() => {}}
            taskTypeFilter={['create', 'fix', 'refactor']}
            onTypeFilterChange={() => {}}
            dateRange="7d"
            onDateRangeChange={() => {}}
            blockedFilter={true}
            onBlockedFilterChange={() => {}}
          />,
        );
      });
    });
  });

  test('all interactive elements meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.display = 'flex';
      host.style.gap = '0.5rem';
      host.style.flexWrap = 'wrap';
      host.style.alignItems = 'center';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <SearchFilterBar
            searchQuery="test"
            onSearchChange={() => {}}
            taskTypeFilter={['create', 'review', 'fix']}
            onTypeFilterChange={() => {}}
            dateRange="24h"
            onDateRangeChange={() => {}}
            blockedFilter={false}
            onBlockedFilterChange={() => {}}
          />,
        );
      });
    });
  });

  test('renders without overflow at 375px when search query is very long', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.display = 'flex';
      host.style.gap = '0.5rem';
      host.style.flexWrap = 'wrap';
      host.style.alignItems = 'center';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <SearchFilterBar
            searchQuery="very long search query to test overflow behavior"
            onSearchChange={() => {}}
            taskTypeFilter={['create', 'fix']}
            onTypeFilterChange={() => {}}
            dateRange="all"
            onDateRangeChange={() => {}}
            blockedFilter={false}
            onBlockedFilterChange={() => {}}
          />,
        );
      });
    });
  });

  test('renders without horizontal overflow at 768px (md: breakpoint — input transitions to w-64)', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.display = 'flex';
      host.style.gap = '0.5rem';
      host.style.flexWrap = 'wrap';
      host.style.alignItems = 'center';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <SearchFilterBar
            searchQuery="test query"
            onSearchChange={() => {}}
            taskTypeFilter={['create', 'fix']}
            onTypeFilterChange={() => {}}
            dateRange="24h"
            onDateRangeChange={() => {}}
            blockedFilter={true}
            onBlockedFilterChange={() => {}}
          />,
        );
      });
    });
  });

  test('text labels do not truncate silently at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      host.style.display = 'flex';
      host.style.gap = '0.5rem';
      host.style.flexWrap = 'wrap';
      host.style.alignItems = 'center';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <SearchFilterBar
            searchQuery="test query"
            onSearchChange={() => {}}
            taskTypeFilter={['create', 'fix']}
            onTypeFilterChange={() => {}}
            dateRange="24h"
            onDateRangeChange={() => {}}
            blockedFilter={true}
            onBlockedFilterChange={() => {}}
          />,
        );
      });
    });
  });
});
