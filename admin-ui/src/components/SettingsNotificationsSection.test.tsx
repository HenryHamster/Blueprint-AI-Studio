import { afterEach, describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    apiFetch: vi.fn(),
    // The real fetchDeadLetters/redriveDeadLetters (spread via ...actual)
    // close over the real apiFetch, so they would bypass our apiFetch mock
    // when called.  Explicitly override them with controlled stubs so the
    // DeadLetterPanel tests can exercise the component without leaking real
    // HTTP calls through the closure.
    fetchDeadLetters: vi.fn().mockResolvedValue({ items: [], total: 0, limit: 100, offset: 0 }),
    redriveDeadLetters: vi.fn().mockResolvedValue({ redriven: 0, errors: [] }),
  };
});

import { SettingsNotificationsSection } from './SettingsNotificationsSection';
import { apiFetch, fetchDeadLetters, redriveDeadLetters } from '../api';

const baseProps = {
  webhookUrl: '',
  onUpdateWebhookUrl: vi.fn(),
  enabledEvents: ['TASK_FAILED'],
  onUpdateEnabledEvents: vi.fn(),
  throttleSeconds: 30,
  onUpdateThrottleSeconds: vi.fn(),
  dailyCostCeilingUsd: 0,
  onUpdateDailyCostCeiling: vi.fn(),
  isAdmin: true,
};

function getToggleButton(label: string) {
  const row = screen.getByText(label).parentElement?.parentElement;
  if (!row) throw new Error(`Missing toggle row for ${label}`);
  return within(row).getByRole('button');
}

describe('SettingsNotificationsSection', () => {
  afterEach(() => {
    vi.clearAllMocks();
  });

  it('shows remediation escalation and toggles it in enabled_events', () => {
    const onUpdateEnabledEvents = vi.fn();
    const { rerender } = render(
      <SettingsNotificationsSection {...baseProps} enabledEvents={['TASK_FAILED', 'REMEDIATION_ESCALATED']} onUpdateEnabledEvents={onUpdateEnabledEvents} />
    );

    expect(screen.getByText('Remediation Escalation')).toBeInTheDocument();
    fireEvent.click(getToggleButton('Remediation Escalation'));
    expect(onUpdateEnabledEvents).toHaveBeenLastCalledWith(['TASK_FAILED']);

    rerender(
      <SettingsNotificationsSection {...baseProps} enabledEvents={['TASK_FAILED']} onUpdateEnabledEvents={onUpdateEnabledEvents} />
    );
    fireEvent.click(getToggleButton('Remediation Escalation'));
    expect(onUpdateEnabledEvents).toHaveBeenLastCalledWith(['TASK_FAILED', 'REMEDIATION_ESCALATED']);
  });

  it('shows account/model regression quarantined and toggles it in enabled_events', () => {
    const onUpdateEnabledEvents = vi.fn();
    const { rerender } = render(
      <SettingsNotificationsSection {...baseProps} enabledEvents={['TASK_FAILED', 'account_model_regression_quarantined']} onUpdateEnabledEvents={onUpdateEnabledEvents} />
    );

    expect(screen.getByText('Account/Model Regression Quarantined')).toBeInTheDocument();
    fireEvent.click(getToggleButton('Account/Model Regression Quarantined'));
    expect(onUpdateEnabledEvents).toHaveBeenLastCalledWith(['TASK_FAILED']);

    rerender(
      <SettingsNotificationsSection {...baseProps} enabledEvents={['TASK_FAILED']} onUpdateEnabledEvents={onUpdateEnabledEvents} />
    );
    fireEvent.click(getToggleButton('Account/Model Regression Quarantined'));
    expect(onUpdateEnabledEvents).toHaveBeenLastCalledWith(['TASK_FAILED', 'account_model_regression_quarantined']);
  });

  it('describes the active daily cost ceiling as alert-only, not a pause', () => {
    render(<SettingsNotificationsSection {...baseProps} dailyCostCeilingUsd={50} />);

    const help = screen.getByText(/daily spend exceeds \$50/i);
    expect(help).toHaveTextContent('warning alert');
    expect(help).toHaveTextContent('Autonomous tasks keep running');
    expect(help.textContent?.toLowerCase()).not.toContain('pause autonomous');
  });

  it('describes a zero ceiling as no daily cost limit', () => {
    render(<SettingsNotificationsSection {...baseProps} dailyCostCeilingUsd={0} />);

    expect(screen.getByText(/No ceiling — pipeline runs without a daily cost limit/i)).toBeInTheDocument();
  });

  it('shows merged-without-test-verification and toggles it in enabled_events', () => {
    const onUpdateEnabledEvents = vi.fn();
    const { rerender } = render(
      <SettingsNotificationsSection {...baseProps} enabledEvents={['TASK_FAILED', 'MERGE_UNVERIFIED']} onUpdateEnabledEvents={onUpdateEnabledEvents} />
    );

    expect(screen.getByText('Merged Without Test Verification')).toBeInTheDocument();
    fireEvent.click(getToggleButton('Merged Without Test Verification'));
    expect(onUpdateEnabledEvents).toHaveBeenLastCalledWith(['TASK_FAILED']);

    rerender(
      <SettingsNotificationsSection {...baseProps} enabledEvents={['TASK_FAILED']} onUpdateEnabledEvents={onUpdateEnabledEvents} />
    );
    fireEvent.click(getToggleButton('Merged Without Test Verification'));
    expect(onUpdateEnabledEvents).toHaveBeenLastCalledWith(['TASK_FAILED', 'MERGE_UNVERIFIED']);
  });

  it('fetches project webhook health and shows a healthy delivery summary', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        total_dispatched: 12,
        total_delivered: 12,
        total_failed: 0,
        last_failure_reason: '',
        last_failure_at: null,
        retry_queue_depth: 0,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );

    render(<SettingsNotificationsSection {...baseProps} webhookUrl="https://hooks.example.com/health" selectedProjectId="project-123" />);

    expect(await screen.findByText('Delivery healthy')).toBeInTheDocument();
    expect(screen.getByText(/12 delivered · 0 failed · 0 queued/i)).toBeInTheDocument();
    expect(vi.mocked(apiFetch)).toHaveBeenCalledWith(
      '/api/admin/notification-dispatch/stats?project_id=project-123',
      undefined,
      2,
      30000,
      expect.any(AbortSignal),
    );
  });

  it('shows degraded delivery health when failures or retry backlog exist', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        total_dispatched: 8,
        total_delivered: 6,
        total_failed: 2,
        last_failure_reason: 'timeout',
        last_failure_at: '2026-07-05T00:00:00Z',
        retry_queue_depth: 1,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );

    render(<SettingsNotificationsSection {...baseProps} webhookUrl="https://hooks.example.com/health" selectedProjectId="project-123" />);

    expect(await screen.findByText('Delivery degraded')).toBeInTheDocument();
    expect(screen.getByText(/6 delivered · 2 failed · 1 queued · last: timeout/i)).toBeInTheDocument();
  });

  it('falls back to not configured when the webhook url is cleared after prior health stats', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        total_dispatched: 7,
        total_delivered: 7,
        total_failed: 0,
        last_failure_reason: '',
        last_failure_at: null,
        retry_queue_depth: 0,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );

    const { rerender } = render(<SettingsNotificationsSection {...baseProps} webhookUrl="https://hooks.example.com/health" selectedProjectId="project-123" />);

    expect(await screen.findByText('Delivery healthy')).toBeInTheDocument();

    rerender(<SettingsNotificationsSection {...baseProps} webhookUrl="" selectedProjectId="project-123" />);

    expect(screen.getByText('Webhook not configured')).toBeInTheDocument();
    expect(screen.getByText(/Add a webhook URL to inspect delivery health/i)).toBeInTheDocument();
    // apiFetch called once for health stats; dead letters go through
    // fetchDeadLetters mock (separate mock)
    expect(vi.mocked(apiFetch)).toHaveBeenCalledTimes(1);
  });

  // ── DeadLetterPanel ──────────────────────────────────────────────────────

  it('shows dead-lettered notifications with event type, reason, and re-drive button', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        total_dispatched: 0, total_delivered: 0, total_failed: 0,
        last_failure_reason: '', last_failure_at: null, retry_queue_depth: 0,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    vi.mocked(fetchDeadLetters).mockResolvedValueOnce({ items: [
      {
        id: 'dl-1',
        project_id: 'project-123',
        event_type: 'TASK_FAILED',
        reason: 'exhausted',
        attempts: 4,
        last_status: 503,
        webhook_url: 'https://hooks.example.com/secret',
        payload: {},
        body: {},
        created_at: '2026-07-01T00:00:00',
      },
      {
        id: 'dl-2',
        project_id: 'project-123',
        event_type: 'RUNNER_DEGRADED',
        reason: 'evicted',
        attempts: 0,
        last_status: null,
        webhook_url: 'https://hooks.example.com/secret',
        payload: {},
        body: {},
        created_at: '2026-07-02T00:00:00',
      },
    ], total: 2, limit: 100, offset: 0 });

    render(<SettingsNotificationsSection {...baseProps} webhookUrl="https://hooks.example.com/health" selectedProjectId="project-123" />);

    // Should show the dead-letter count and entries
    expect(await screen.findByText(/2 dead-lettered notifications/)).toBeInTheDocument();
    // First entry (newest since mock array order is newest-first from API)
    expect(screen.getByText('TASK_FAILED')).toBeInTheDocument();
    expect(screen.getByText(/exhausted · HTTP 503 · 4 attempts/)).toBeInTheDocument();
    expect(screen.getByText('RUNNER_DEGRADED')).toBeInTheDocument();
    expect(screen.getByText(/evicted/)).toBeInTheDocument();
    // Re-drive buttons
    expect(screen.getAllByText('Re-drive')).toHaveLength(2);
    expect(vi.mocked(fetchDeadLetters)).toHaveBeenCalledWith('project-123');
  });

  it('shows loading state while dead letters are being fetched', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        total_dispatched: 0, total_delivered: 0, total_failed: 0,
        last_failure_reason: '', last_failure_at: null, retry_queue_depth: 0,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    // Never-resolving promise keeps the loading state visible
    vi.mocked(fetchDeadLetters).mockReturnValueOnce(new Promise(() => {}));

    render(<SettingsNotificationsSection {...baseProps} webhookUrl="https://hooks.example.com/health" selectedProjectId="project-123" />);

    expect(await screen.findByText(/Loading dead-lettered notifications/)).toBeInTheDocument();
  });

  it('shows error state when dead-letter fetch fails', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        total_dispatched: 0, total_delivered: 0, total_failed: 0,
        last_failure_reason: '', last_failure_at: null, retry_queue_depth: 0,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    vi.mocked(fetchDeadLetters).mockRejectedValueOnce(new Error('Network failure'));

    render(<SettingsNotificationsSection {...baseProps} webhookUrl="https://hooks.example.com/health" selectedProjectId="project-123" />);

    expect(await screen.findByText(/Failed to load dead letters: Network failure/)).toBeInTheDocument();
  });

  it('hides dead-letter panel when webhook url is empty', () => {
    render(<SettingsNotificationsSection {...baseProps} webhookUrl="" selectedProjectId="project-123" />);

    // No dead-letter loading, error, or entries should appear
    expect(screen.queryByText(/dead-lettered/)).not.toBeInTheDocument();
    expect(screen.queryByText('Re-drive')).not.toBeInTheDocument();
    expect(screen.queryByText(/Loading dead-lettered/)).not.toBeInTheDocument();
  });

  it('hides dead-letter panel when there are no dead letters', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        total_dispatched: 0, total_delivered: 0, total_failed: 0,
        last_failure_reason: '', last_failure_at: null, retry_queue_depth: 0,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    vi.mocked(fetchDeadLetters).mockResolvedValueOnce({ items: [], total: 0, limit: 100, offset: 0 });

    render(<SettingsNotificationsSection {...baseProps} webhookUrl="https://hooks.example.com/health" selectedProjectId="project-123" />);

    // The health card renders its loading/not-configured state while the
    // dead-letter panel fetches.  Wait for the dead-letter fetch to
    // complete (the loading text disappears) before asserting the
    // dead-letter panel is hidden.
    await waitFor(() => {
      expect(screen.queryByText(/Loading dead-lettered/)).not.toBeInTheDocument();
    });
    // With an empty dead-letter list the panel returns null — no
    // dead-letter UI visible at all.
    expect(screen.queryByText(/dead-lettered/)).not.toBeInTheDocument();
    expect(screen.queryByText('Re-drive')).not.toBeInTheDocument();
  });

  it('re-drive button triggers redrive API and shows spinner', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        total_dispatched: 0, total_delivered: 0, total_failed: 0,
        last_failure_reason: '', last_failure_at: null, retry_queue_depth: 0,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    vi.mocked(fetchDeadLetters).mockResolvedValueOnce({ items: [
      {
        id: 'dl-redrive',
        project_id: 'project-123',
        event_type: 'TASK_FAILED',
        reason: 'exhausted',
        attempts: 3,
        last_status: 500,
        webhook_url: 'https://hooks.example.com/secret',
        payload: {},
        body: {},
        created_at: '2026-07-01T00:00:00',
      },
    ], total: 1, limit: 100, offset: 0 });

    render(<SettingsNotificationsSection {...baseProps} webhookUrl="https://hooks.example.com/health" selectedProjectId="project-123" />);

    const reDriveBtn = await screen.findByText('Re-drive');
    expect(reDriveBtn).toBeInTheDocument();

    // Mock the redrive API response
    vi.mocked(redriveDeadLetters).mockResolvedValueOnce({ redriven: 1, errors: [] });
    // Mock the subsequent reload of dead letters (empty after redrive)
    vi.mocked(fetchDeadLetters).mockResolvedValueOnce({ items: [], total: 0, limit: 100, offset: 0 });

    fireEvent.click(reDriveBtn);

    // The button should be disabled while redriving
    const spinningBtn = screen.getByText('Re-drive');
    expect(spinningBtn.closest('button')).toBeDisabled();

    // Wait for the redrive to complete
    await waitFor(() => {
      expect(vi.mocked(redriveDeadLetters)).toHaveBeenCalledWith('project-123', ['dl-redrive']);
    });

    // After redrive completes, the list should have been reloaded
    await waitFor(() => {
      expect(vi.mocked(fetchDeadLetters)).toHaveBeenCalledTimes(2);
    });
  });

  it('shows redrive error when redrive API fails', async () => {
    vi.mocked(apiFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({
        total_dispatched: 0, total_delivered: 0, total_failed: 0,
        last_failure_reason: '', last_failure_at: null, retry_queue_depth: 0,
      }), { status: 200, headers: { 'Content-Type': 'application/json' } }),
    );
    vi.mocked(fetchDeadLetters).mockResolvedValueOnce({ items: [
      {
        id: 'dl-fail',
        project_id: 'project-123',
        event_type: 'MERGE_UNVERIFIED',
        reason: 'exhausted',
        attempts: 1,
        last_status: 502,
        webhook_url: 'https://hooks.example.com/secret',
        payload: {},
        body: {},
        created_at: '2026-07-01T00:00:00',
      },
    ], total: 1, limit: 100, offset: 0 });

    render(<SettingsNotificationsSection {...baseProps} webhookUrl="https://hooks.example.com/health" selectedProjectId="project-123" />);

    const reDriveBtn = await screen.findByText('Re-drive');

    // Mock the redrive API to fail — reject with a non-Error so the
    // component's catch handler uses the fallback string 'Redrive failed'
    // (the handler does: e instanceof Error ? e.message : 'Redrive failed')
    vi.mocked(redriveDeadLetters).mockRejectedValueOnce('Redrive failed');

    fireEvent.click(reDriveBtn);

    expect(await screen.findByText(/Redrive failed/)).toBeInTheDocument();
  });
});
