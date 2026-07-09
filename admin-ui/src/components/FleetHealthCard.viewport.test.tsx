import { afterEach, beforeEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { FleetHealthCard } from './FleetHealthCard';
import { expectNoOverflowAt } from '../test/viewport';
import type { FleetHealthSummaryResponse } from '../api';

function makeData(overrides: Partial<FleetHealthSummaryResponse> = {}): FleetHealthSummaryResponse {
  return {
    total: 3,
    connected: 2,
    degraded: 1,
    stale: 0,
    offline: 0,
    low_disk: 0,
    project_count: 1,
    per_project: [
      {
        project_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        total: 3,
        connected: 2,
        degraded: 1,
        stale: 0,
        offline: 0,
        low_disk: 0,
        runners: [
          {
            id: 'r-1',
            hostname: 'host-1',
            version: '1.0',
            status: 'active',
            capabilities: { task_types: 'all', max_concurrent: 2 },
            health: {
              device_status: 'connected',
              is_live: true,
              degraded: false,
              seconds_since_last_seen: 30,
              active_task_count: 1,
              max_concurrent: 2,
              available_slots: 1,
              stale_reason: null,
              liveness_window_seconds: 300,
              consecutive_job_failures: 0,
            },
            last_seen_at: '2026-07-08T10:00:00',
            created_at: '2026-07-01T10:00:00',
          },
          {
            id: 'r-2',
            hostname: 'host-2',
            version: '1.0',
            status: 'active',
            capabilities: { task_types: 'all', max_concurrent: 2 },
            health: {
              device_status: 'connected',
              is_live: true,
              degraded: false,
              seconds_since_last_seen: 30,
              active_task_count: 0,
              max_concurrent: 2,
              available_slots: 2,
              stale_reason: null,
              liveness_window_seconds: 300,
              consecutive_job_failures: 0,
            },
            last_seen_at: '2026-07-08T10:00:00',
            created_at: '2026-07-01T10:00:00',
          },
          {
            id: 'r-3',
            hostname: 'host-3',
            version: '1.0',
            status: 'active',
            capabilities: { task_types: 'all', max_concurrent: 2 },
            health: {
              device_status: 'degraded',
              is_live: true,
              degraded: true,
              degraded_reason: 'job_failure_streak',
              seconds_since_last_seen: 30,
              active_task_count: 1,
              max_concurrent: 2,
              available_slots: 1,
              stale_reason: null,
              liveness_window_seconds: 300,
              consecutive_job_failures: 3,
            },
            last_seen_at: '2026-07-08T10:00:00',
            created_at: '2026-07-01T10:00:00',
          },
        ],
      },
    ],
    generated_at: '2026-07-08T10:00:00',
    ...overrides,
  };
}

describe('FleetHealthCard viewport', () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    container = document.createElement('div');
    container.style.padding = '12px';
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    document.body.removeChild(container);
  });

  test('renders without overflow at 375px (mobile)', async () => {
    await expectNoOverflowAt(375, async () => {
      act(() => {
        root.render(<FleetHealthCard data={makeData()} loading={false} />);
      });
    });
  });

  test('renders without overflow at 768px (tablet)', async () => {
    await expectNoOverflowAt(768, async () => {
      act(() => {
        root.render(<FleetHealthCard data={makeData()} loading={false} />);
      });
    });
  });

  test('renders without overflow at 1024px (desktop)', async () => {
    await expectNoOverflowAt(1024, async () => {
      act(() => {
        root.render(<FleetHealthCard data={makeData()} loading={false} />);
      });
    });
  });
});
