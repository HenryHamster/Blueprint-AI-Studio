import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  getIdToken: vi.fn(async () => 'test-token'),
  onAuthChange: vi.fn(() => () => {}),
  signOut: vi.fn(),
}));

import { SettingsNotificationsSection } from './SettingsNotificationsSection';

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
});
