import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ParkWakeHealthCard } from './ParkWakeHealthCard';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';
import type { ParkWakeHealth } from '../types/digest';

const healthyData: ParkWakeHealth = {
  parked_task_count: 3,
  oldest_parked_age_seconds: 1800,
  fired_undelivered_count: 0,
  oldest_fired_age_seconds: null,
  delivery_stalled: false,
  delivery_stall_threshold_seconds: 3600,
};

const stalledData: ParkWakeHealth = {
  parked_task_count: 12,
  oldest_parked_age_seconds: 7200,
  fired_undelivered_count: 5,
  oldest_fired_age_seconds: 900,
  delivery_stalled: true,
  delivery_stall_threshold_seconds: 3600,
};

describe('ParkWakeHealthCard — viewport regression', () => {
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

  test('renders without horizontal overflow at 375px (healthy state)', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<ParkWakeHealthCard data={healthyData} />);
      });
    });
  });

  test('renders without horizontal overflow at 375px (stalled state)', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<ParkWakeHealthCard data={stalledData} />);
      });
    });
  });

  test('labels are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<ParkWakeHealthCard data={stalledData} />);
      });
    });
  });

  test('touch targets pass (read-only card has no interactive controls)', async () => {
    // ParkWakeHealthCard is a read-only display card with no interactive controls.
    // Default 'button' selector finds nothing, satisfying the assertion vacuously.
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '16px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<ParkWakeHealthCard data={healthyData} />);
      });
    });
  });
});
