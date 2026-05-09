import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { QuickFilterPresets } from './QuickFilterPresets';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

// QuickFilterPresets has no API or router imports — no mocking needed.

const ALL_STATUSES = ['QUEUED', 'PROVISIONING', 'IN_PROGRESS', 'EVALUATING', 'FAILED', 'CANCELLED', 'COMPLETED'];

const noopSetters = {
  setStatusFilter: () => {},
  setTaskTypeFilter: () => {},
  setDateRange: () => {},
  setRequiresApproval: () => {},
  setBlockedFilter: () => {},
  setStuckFilter: () => {},
  setStuckMinSeconds: () => {},
  setSearchQuery: () => {},
};

const defaultProps = {
  statusFilter: ALL_STATUSES,
  dateRange: 'all' as const,
  requiresApproval: false,
  blockedFilter: false,
  stuckFilter: false,
  stuckCount: 0,
  stuckCount1h: 0,
  stuckMinSeconds: 0,
  searchQuery: '',
  ...noopSetters,
};

describe('QuickFilterPresets — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px with zero counters', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<QuickFilterPresets {...defaultProps} />);
      });
    });
  });

  test('renders without horizontal overflow at 375px with high counters', async () => {
    // stuckCount=47 produces "Stuck tasks (47)" and stuckCount1h=12 produces "Stuck >1hr (12)".
    // These wider labels are the worst-case overflow scenario for the chip row.
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <QuickFilterPresets {...defaultProps} stuckCount={47} stuckCount1h={12} />,
        );
      });
    });
  });

  test('active danger preset renders without overflow at 375px', async () => {
    // Activates "Stuck >1hr" preset — red chip colors, count label widened.
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <QuickFilterPresets
            {...defaultProps}
            stuckFilter={true}
            stuckMinSeconds={3600}
            stuckCount1h={5}
          />,
        );
      });
    });
  });

  test('chip labels are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <QuickFilterPresets {...defaultProps} stuckCount={47} stuckCount1h={12} />,
        );
      });
    });
  });

  // Note: expectMinTouchTargetAt is intentionally omitted. The chip buttons
  // use `py-0.5` (~16px height) by design — they are filter chips, not primary
  // action buttons. Touch-target enforcement is not appropriate for dense chip rows.
});
