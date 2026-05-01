import { describe, it, expect, vi } from 'vitest';
import { fireEvent, render, screen, within } from '@testing-library/react';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
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
});
