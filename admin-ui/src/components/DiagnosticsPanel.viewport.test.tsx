import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { DiagnosticsPanel } from './DiagnosticsPanel';
import { expectNoOverflowAt, expectFullViewportAt } from '../test/viewport';
import type { ProjectDiagnostics } from '../types/diagnostics';

// DiagnosticsPanel polls /api/admin/failing-task-types via authGet on mount.
// Mock '../api' so no real fetch is attempted.
vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  authGet: vi.fn(async () => new Response(
    JSON.stringify({
      gated_types: [
        { task_type: 'create', failure_rate: 0.72, sample_size: 18, threshold: 0.5 },
        { task_type: 'fix', failure_rate: 0.61, sample_size: 11, threshold: 0.5 },
      ],
      global_failure_threshold: 0.5,
      anomaly_tightened: true,
      window_minutes: 60,
      generated_at: '2026-05-21T00:00:00Z',
    }),
    { status: 200 },
  )),
  API_BASE_URL: 'http://localhost:8000',
}));

const populated: ProjectDiagnostics = {
  project_id: 'proj-1',
  stuck_tasks: [
    {
      task_id: 'task-very-long-id-that-might-overflow-on-mobile-screens-abc-123',
      title: 'feat: a sufficiently long stuck-task title that exercises the truncate utility on narrow screens',
      status: 'IN_PROGRESS',
      stuck_reason: 'no_heartbeat',
      stuck_for_seconds: 4500,
      attempt_count: 3,
      failure_class: null,
      unresolvable_deps: [],
      remediation_hint: 'check VM heartbeat — this hint is intentionally long to make sure it wraps cleanly on mobile',
    },
    {
      task_id: 'task-2',
      title: 'fix: another stuck task',
      status: 'IN_PROGRESS',
      stuck_reason: 'stuck_deps',
      stuck_for_seconds: 200,
      attempt_count: 1,
      failure_class: null,
      unresolvable_deps: ['dep-aaaaaaaa-1111', 'dep-bbbbbbbb-2222'],
    },
  ],
  circuit_breaker: { state: 'OPEN', failure_count: 7, opened_at: null },
  queued_count: 4,
  in_progress_count: 2,
  zone_health: { available: 1, exhausted: ['us-central1-a', 'us-central1-b'] },
};

const actionProps = {
  handleRetry: vi.fn(),
  handleCancel: vi.fn(),
  setConfirmDialog: vi.fn(),
};

describe('DiagnosticsPanel — viewport regression', () => {
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

  test('null diagnostics — empty state has no overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DiagnosticsPanel diagnostics={null} onViewTask={() => {}} {...actionProps} />);
      });
      await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    });
  });

  test('fully-populated state — overflow, text truncation, and touch targets pass at 375px', async () => {
    await expectFullViewportAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<DiagnosticsPanel diagnostics={populated} onViewTask={() => {}} {...actionProps} />);
      });
      await act(async () => { await new Promise<void>((resolve) => setTimeout(resolve, 50)); });
    }, { waitMs: 50 });
  });
});
