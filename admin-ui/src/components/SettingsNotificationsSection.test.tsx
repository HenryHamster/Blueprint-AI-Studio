import { afterEach, describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

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
  };
});

import { SettingsNotificationsSection } from './SettingsNotificationsSection';
import { apiFetch } from '../api';

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
    expect(vi.mocked(apiFetch)).toHaveBeenCalledTimes(1);
  });
});
