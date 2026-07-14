import { describe, test, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
import { FleetHealthCard } from './FleetHealthCard';
import type { FleetHealthSummaryResponse } from '../api';

function makeSummary(overrides: Partial<FleetHealthSummaryResponse> = {}): FleetHealthSummaryResponse {
  return {
    total: 5,
    connected: 3,
    degraded: 1,
    stale: 0,
    offline: 1,
    low_disk: 1,
    project_count: 2,
    per_project: [
      {
        project_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
        total: 3,
        connected: 2,
        degraded: 1,
        stale: 0,
        offline: 0,
        low_disk: 1,
        runners: [
          {
            id: 'runner-1',
            hostname: 'host-ok',
            version: '1.0',
            status: 'active',
            capabilities: { task_types: 'all', max_concurrent: 2 },
            health: {
              device_status: 'connected',
              device_status_reason: null,
              is_live: true,
              degraded: false,
              degraded_reason: null,
              seconds_since_last_seen: 30,
              active_task_count: 1,
              max_concurrent: 2,
              available_slots: 1,
              stale_reason: null,
              liveness_window_seconds: 300,
              consecutive_job_failures: 0,
              disk_free_bytes: 50 * 1024 ** 3,
              disk_low: false,
            },
            last_seen_at: '2026-07-08T10:00:00',
            created_at: '2026-07-01T10:00:00',
          },
          {
            id: 'runner-2',
            hostname: 'host-degraded',
            version: '1.0',
            status: 'active',
            capabilities: { task_types: 'all', max_concurrent: 2 },
            health: {
              device_status: 'degraded',
              device_status_reason: 'job_failure_streak',
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
              disk_free_bytes: 50 * 1024 ** 3,
              disk_low: false,
            },
            last_seen_at: '2026-07-08T10:00:00',
            created_at: '2026-07-01T10:00:00',
          },
          {
            id: 'runner-3',
            hostname: 'host-lowdisk',
            version: '1.0',
            status: 'active',
            capabilities: { task_types: 'all', max_concurrent: 2, disk_free_bytes: 1024 * 1024 },
            health: {
              device_status: 'connected',
              device_status_reason: null,
              is_live: true,
              degraded: false,
              degraded_reason: null,
              seconds_since_last_seen: 30,
              active_task_count: 1,
              max_concurrent: 2,
              available_slots: 1,
              stale_reason: null,
              liveness_window_seconds: 300,
              consecutive_job_failures: 0,
              disk_free_bytes: 1024 * 1024,
              disk_low: true,
            },
            last_seen_at: '2026-07-08T10:00:00',
            created_at: '2026-07-01T10:00:00',
          },
        ],
      },
      {
        project_id: '11111111-2222-3333-4444-555555555555',
        total: 2,
        connected: 1,
        degraded: 0,
        stale: 0,
        offline: 1,
        low_disk: 0,
        runners: [
          {
            id: 'runner-4',
            hostname: 'host-p2-ok',
            version: '1.0',
            status: 'active',
            capabilities: { task_types: 'all', max_concurrent: 2 },
            health: {
              device_status: 'connected',
              device_status_reason: null,
              is_live: true,
              degraded: false,
              degraded_reason: null,
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
            id: 'runner-5',
            hostname: 'host-p2-offline',
            version: '1.0',
            status: 'inactive',
            capabilities: { task_types: 'all', max_concurrent: 2 },
            health: {
              device_status: 'offline',
              device_status_reason: 'inactive',
              is_live: false,
              degraded: false,
              degraded_reason: null,
              seconds_since_last_seen: null,
              active_task_count: 0,
              max_concurrent: 2,
              available_slots: null,
              stale_reason: 'inactive',
              liveness_window_seconds: 300,
              consecutive_job_failures: 0,
            },
            last_seen_at: null,
            created_at: '2026-07-01T10:00:00',
          },
        ],
      },
    ],
    generated_at: '2026-07-08T10:00:00',
    ...overrides,
  };
}

describe('FleetHealthCard', () => {
  test('shows skeleton while loading', () => {
    const { container } = render(<FleetHealthCard data={null} loading={true} />);
    expect(container.querySelector('.animate-pulse')).toBeTruthy();
  });

  test('shows unavailable message when data is null and not loading', () => {
    const { getByText } = render(<FleetHealthCard data={null} loading={false} />);
    expect(getByText(/unavailable/i)).toBeTruthy();
  });

  test('shows no-runners message when fleet is empty', () => {
    const { getByText } = render(
      <FleetHealthCard
        data={makeSummary({ total: 0, connected: 0, degraded: 0, stale: 0, offline: 0, low_disk: 0, project_count: 0, per_project: [] })}
        loading={false}
      />,
    );
    expect(getByText(/no runners/i)).toBeTruthy();
  });

  test('renders online count and runner total', () => {
    const { getByText } = render(
      <FleetHealthCard data={makeSummary()} loading={false} />,
    );
    expect(getByText('5 runners · 2 projects')).toBeTruthy();
    expect(getByText('3')).toBeTruthy();
    // The text "online" appears next to the connected count
    expect(getByText('online', { selector: 'span' })).toBeTruthy();
  });

  test('renders failing count when degraded runners exist', () => {
    const { getByText } = render(
      <FleetHealthCard data={makeSummary()} loading={false} />,
    );
    // "failing" text appears next to degraded count
    expect(getByText('failing', { selector: 'span' })).toBeTruthy();
  });

  test('renders offline count', () => {
    const { getByText } = render(
      <FleetHealthCard data={makeSummary()} loading={false} />,
    );
    expect(getByText('offline', { selector: 'span' })).toBeTruthy();
  });

  test('renders low disk count', () => {
    const { getByText } = render(
      <FleetHealthCard data={makeSummary()} loading={false} />,
    );
    expect(getByText('low disk', { selector: 'span' })).toBeTruthy();
  });

  test('renders low memory count when the backend reports memory-starved runners', () => {
    const { getByText } = render(
      <FleetHealthCard data={makeSummary({ low_memory: 2 })} loading={false} />,
    );
    // Backend emits low_memory but the card previously dropped it — assert it
    // now renders symmetric with the low-disk warning.
    const lowMem = getByText('low memory', { selector: 'span' });
    expect(lowMem).toBeTruthy();
    expect(lowMem.textContent).toContain('2');
  });

  test('omits low memory count when no runner is memory-starved', () => {
    const { queryByText } = render(
      <FleetHealthCard data={makeSummary({ low_memory: 0 })} loading={false} />,
    );
    expect(queryByText('low memory')).toBeNull();
  });

  test('expanded project shows a low-memory icon on a memory-starved runner', () => {
    const summary = makeSummary({ low_memory: 1 });
    // Mark host-lowdisk as also memory-starved to exercise the per-runner icon.
    const starved = summary.per_project[0].runners[2];
    if (starved.health) {
      starved.health.memory_low = true;
      starved.health.memory_free_bytes = 100 * 1024 ** 2;
    }
    const { getByText, getByLabelText } = render(
      <FleetHealthCard data={summary} loading={false} />,
    );
    fireEvent.click(getByText('aaaaaaaa'));
    expect(getByLabelText('Low memory')).toBeTruthy();
  });

  test('shows per-project section', () => {
    const { getByText } = render(
      <FleetHealthCard data={makeSummary()} loading={false} />,
    );
    expect(getByText('By Project')).toBeTruthy();
  });

  test('shows per-project row with truncated project id', () => {
    const { getByText } = render(
      <FleetHealthCard data={makeSummary()} loading={false} />,
    );
    expect(getByText('aaaaaaaa')).toBeTruthy();
    expect(getByText('11111111')).toBeTruthy();
  });

  test('does not show failing/offline/low-disk when all counts are zero', () => {
    const { queryByText } = render(
      <FleetHealthCard
        data={makeSummary({
          total: 2,
          connected: 2,
          degraded: 0,
          stale: 0,
          offline: 0,
          low_disk: 0,
          per_project: [],
        })}
        loading={false}
      />,
    );
    expect(queryByText('failing')).toBeNull();
    expect(queryByText('offline')).toBeNull();
    expect(queryByText('low disk')).toBeNull();
  });

  test('expanding a project shows runner detail rows', () => {
    const { getByText, queryByText } = render(
      <FleetHealthCard data={makeSummary()} loading={false} />,
    );
    // Before expand: runner hostnames are not visible
    expect(queryByText('host-ok')).toBeNull();

    // Click the first project row to expand
    fireEvent.click(getByText('aaaaaaaa'));

    // After expand: runner hostnames are visible
    expect(getByText('host-ok')).toBeTruthy();
    expect(getByText('host-degraded')).toBeTruthy();
    expect(getByText('host-lowdisk')).toBeTruthy();
  });

  test('expanded project shows device status labels', () => {
    const { getByText, getAllByText } = render(
      <FleetHealthCard data={makeSummary()} loading={false} />,
    );
    fireEvent.click(getByText('aaaaaaaa'));

    // Two "Online" spans (host-ok and host-lowdisk are both connected)
    expect(getAllByText('Online').length).toBe(2);
    expect(getByText('Failing')).toBeTruthy();
  });

  test('collapsing a project hides runner details', () => {
    const { getByText, queryByText } = render(
      <FleetHealthCard data={makeSummary()} loading={false} />,
    );
    // Expand
    fireEvent.click(getByText('aaaaaaaa'));
    expect(getByText('host-ok')).toBeTruthy();

    // Collapse
    fireEvent.click(getByText('aaaaaaaa'));
    expect(queryByText('host-ok')).toBeNull();
  });
});
