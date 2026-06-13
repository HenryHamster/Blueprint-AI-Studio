import { afterEach, describe, expect, test, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { DiagnosticsPanel } from './DiagnosticsPanel';
import { authGet } from '../api';
import type { ProjectDiagnostics } from '../types/diagnostics';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  authGet: vi.fn(async () => new Response(
    JSON.stringify({
      gated_types: [],
      global_failure_threshold: 0.5,
      anomaly_tightened: false,
      window_minutes: 60,
      generated_at: '2026-05-21T00:00:00Z',
    }),
    { status: 200 },
  )),
  API_BASE_URL: 'http://localhost:8000',
}));

const diagnostics: ProjectDiagnostics = {
  project_id: 'proj-1',
  stuck_tasks: [
    {
      task_id: 'task-active',
      title: 'active stuck task',
      status: 'IN_PROGRESS',
      stuck_reason: 'no_heartbeat',
      stuck_for_seconds: 1800,
      attempt_count: 2,
      failure_class: null,
      unresolvable_deps: [],
      remediation_hint: 'restart the worker and recheck the heartbeat',
    },
    {
      task_id: 'task-failed',
      title: 'failed stuck task',
      status: 'FAILED',
      stuck_reason: 'evaluation_timeout',
      stuck_for_seconds: 7200,
      attempt_count: 1,
      failure_class: null,
      unresolvable_deps: [],
    },
  ],
  circuit_breaker: { state: 'CLOSED', failure_count: 0, opened_at: null },
  queued_count: 0,
  in_progress_count: 1,
  zone_health: { available: 2, exhausted: [] },
};

describe('DiagnosticsPanel', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  test('renders inline retry for failed stuck tasks and cancel confirmation for active stuck tasks', async () => {
    const user = userEvent.setup();
    const handleRetry = vi.fn();
    const handleCancel = vi.fn();
    const setConfirmDialog = vi.fn();

    render(
      <DiagnosticsPanel
        diagnostics={diagnostics}
        onViewTask={vi.fn()}
        handleRetry={handleRetry}
        handleCancel={handleCancel}
        setConfirmDialog={setConfirmDialog}
      />,
    );

    await user.click(screen.getByRole('button', { name: 'Retry' }));
    expect(handleRetry).toHaveBeenCalledWith('task-failed');

    await user.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(setConfirmDialog).toHaveBeenCalledWith(expect.objectContaining({
      title: 'Cancel task?',
      confirmLabel: 'Cancel task',
      message: 'This will cancel "active stuck task".',
      onConfirm: expect.any(Function),
    }));

    const dialog = setConfirmDialog.mock.calls[0][0];
    dialog.onConfirm();
    expect(handleCancel).toHaveBeenCalledWith('task-active');
    expect(setConfirmDialog).toHaveBeenLastCalledWith(null);
  });

  test('renders a degraded affordance when the gate poll fails, distinct from confirmed-zero', async () => {
    // Part 1: poll returns a non-ok response → degraded affordance must appear.
    vi.mocked(authGet).mockResolvedValueOnce(new Response('', { status: 500 }));

    const { unmount } = render(
      <DiagnosticsPanel
        diagnostics={null}
        onViewTask={vi.fn()}
        handleRetry={vi.fn()}
        handleCancel={vi.fn()}
        setConfirmDialog={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.getByTitle(/gate status unavailable/i)).toBeInTheDocument();
    });
    expect(screen.getAllByText(/gate unknown/i).length).toBeGreaterThan(0);

    unmount();
    vi.clearAllMocks();

    // Part 2: poll succeeds with empty gated_types (confirmed-zero) → no degraded affordance.
    render(
      <DiagnosticsPanel
        diagnostics={null}
        onViewTask={vi.fn()}
        handleRetry={vi.fn()}
        handleCancel={vi.fn()}
        setConfirmDialog={vi.fn()}
      />,
    );

    await waitFor(() => {
      expect(screen.queryByTitle(/gate status unavailable/i)).not.toBeInTheDocument();
    });
    expect(screen.queryByText(/gate unknown/i)).not.toBeInTheDocument();
  });
});
