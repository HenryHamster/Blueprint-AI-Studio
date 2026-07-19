import { describe, expect, it, vi, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { AlertsBanner } from './AlertsBanner';
import type { Alert } from '../types/alert';

const { mockApiFetch } = vi.hoisted(() => ({
  mockApiFetch: vi.fn(),
}));

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({ apiFetch: mockApiFetch });
});

const alert: Alert = {
  id: 'alert-1',
  project_id: 'project-1',
  severity: 'WARNING',
  title: 'Warm pool saturation',
  message: 'capacity crossed the warning threshold',
  domain: 'warm_pool',
  event_type: 'capacity.warning',
  source: 'warm_pool',
  dedupe_key: 'project-1:warm_pool:capacity',
  occurrence_count: 3,
  last_seen_at: '2026-06-27T08:00:00',
  task_id: null,
  created_at: '2026-06-27T07:00:00',
  dismissed_at: null,
  acknowledged_at: null,
};

describe('AlertsBanner', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders title, message, and a minimal recurrence hint', () => {
    render(<AlertsBanner alerts={[alert]} onDismiss={vi.fn()} />);

    expect(screen.getByText('Warm pool saturation')).toBeInTheDocument();
    expect(screen.getByText('capacity crossed the warning threshold')).toBeInTheDocument();
    // occurrence_count > 1 → surface the recurrence hint (with last-seen).
    expect(screen.getByText(/3 occurrences/)).toBeInTheDocument();
  });

  it('drops the internal taxonomy chips (domain / event_type / source)', () => {
    render(<AlertsBanner alerts={[alert]} onDismiss={vi.fn()} />);

    // These carried no operator-facing action and were removed in the cleanup.
    expect(screen.queryByText('Domain warm pool')).not.toBeInTheDocument();
    expect(screen.queryByText('Event capacity warning')).not.toBeInTheDocument();
    expect(screen.queryByText('Source warm pool')).not.toBeInTheDocument();
  });

  it('shows only a last-seen hint when the alert has not recurred', () => {
    const singleOccurrence = { ...alert, occurrence_count: 1 };
    render(<AlertsBanner alerts={[singleOccurrence]} onDismiss={vi.fn()} />);

    expect(screen.queryByText(/occurrences/)).not.toBeInTheDocument();
    expect(screen.getByText(/Last seen/)).toBeInTheDocument();
  });

  it('renders acknowledge button when projectId is provided and alert is not acknowledged', () => {
    render(<AlertsBanner alerts={[alert]} onDismiss={vi.fn()} projectId="project-1" />);
    expect(screen.getByRole('button', { name: 'Acknowledge alert' })).toBeInTheDocument();
    expect(screen.getByText('Acknowledge')).toBeInTheDocument();
  });

  it('does not render acknowledge button when projectId is not provided', () => {
    render(<AlertsBanner alerts={[alert]} onDismiss={vi.fn()} />);
    expect(screen.queryByRole('button', { name: 'Acknowledge alert' })).not.toBeInTheDocument();
  });

  it('renders acknowledged state when alert.acknowledged_at is set', () => {
    const ackedAlert = { ...alert, acknowledged_at: '2026-06-27T09:00:00' };
    render(<AlertsBanner alerts={[ackedAlert]} onDismiss={vi.fn()} projectId="project-1" />);
    expect(screen.getByText('Acknowledged')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Acknowledge alert' })).not.toBeInTheDocument();
  });

  it('fires API call to acknowledge endpoint on button click', async () => {
    const user = userEvent.setup();
    mockApiFetch.mockResolvedValueOnce({ ok: true, clone: () => ({ ok: true }) } as Response);

    render(<AlertsBanner alerts={[alert]} onDismiss={vi.fn()} projectId="project-1" />);

    await user.click(screen.getByRole('button', { name: 'Acknowledge alert' }));

    expect(mockApiFetch).toHaveBeenCalledWith(
      '/api/admin/alerts/alert-1/acknowledge',
      { method: 'POST' },
    );
  });

  it('transitions to acknowledged state on successful API response', async () => {
    const user = userEvent.setup();
    mockApiFetch.mockResolvedValueOnce({ ok: true, clone: () => ({ ok: true }) } as Response);

    render(<AlertsBanner alerts={[alert]} onDismiss={vi.fn()} projectId="project-1" />);

    await user.click(screen.getByRole('button', { name: 'Acknowledge alert' }));

    await waitFor(() => {
      expect(screen.getByText('Acknowledged')).toBeInTheDocument();
    });
  });

  it('shows loading spinner while acknowledge request is in flight', async () => {
    const user = userEvent.setup();
    // Return a promise that never resolves to keep the spinner visible
    let resolvePromise!: (value: Response) => void;
    const pending = new Promise<Response>(resolve => { resolvePromise = resolve; });
    mockApiFetch.mockReturnValueOnce(pending);

    render(<AlertsBanner alerts={[alert]} onDismiss={vi.fn()} projectId="project-1" />);

    await user.click(screen.getByRole('button', { name: 'Acknowledge alert' }));

    // The button should be disabled while loading
    expect(screen.getByRole('button', { name: 'Acknowledge alert' })).toBeDisabled();

    // Clean up: resolve the pending promise
    resolvePromise({ ok: true, clone: () => ({ ok: true }) } as Response);
  });

  it('disables acknowledge button (idempotency guard) while request is in flight', async () => {
    const user = userEvent.setup();
    let resolvePromise!: (value: Response) => void;
    const pending = new Promise<Response>(resolve => { resolvePromise = resolve; });
    mockApiFetch.mockReturnValueOnce(pending);

    render(<AlertsBanner alerts={[alert]} onDismiss={vi.fn()} projectId="project-1" />);

    const btn = screen.getByRole('button', { name: 'Acknowledge alert' });
    await user.click(btn);

    // Button is disabled during the request
    expect(btn).toBeDisabled();

    // No second API call fires on re-click
    await user.click(btn);
    expect(mockApiFetch).toHaveBeenCalledTimes(1);

    resolvePromise({ ok: true, clone: () => ({ ok: true }) } as Response);
  });

  it('does not show acknowledge button once acknowledged via API (idempotency guard)', async () => {
    const user = userEvent.setup();
    mockApiFetch.mockResolvedValueOnce({ ok: true, clone: () => ({ ok: true }) } as Response);

    const { rerender } = render(<AlertsBanner alerts={[alert]} onDismiss={vi.fn()} projectId="project-1" />);

    await user.click(screen.getByRole('button', { name: 'Acknowledge alert' }));
    await waitFor(() => {
      expect(screen.getByText('Acknowledged')).toBeInTheDocument();
    });

    // Rerender with same alerts — the acknowledged state persists via state
    rerender(<AlertsBanner alerts={[alert]} onDismiss={vi.fn()} projectId="project-1" />);
    expect(screen.getByText('Acknowledged')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Acknowledge alert' })).not.toBeInTheDocument();
  });
});
