import { useState, type ComponentProps } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { SettingsAccountsSection } from './SettingsAccountsSection';
import type { MaxPlanAccount, ProviderCatalogEntry } from '../types/settings';

function makeAccount(overrides: Partial<MaxPlanAccount> = {}): MaxPlanAccount {
  return {
    id: 'acct-1',
    name: 'Primary',
    enabled: true,
    active_tasks: 2,
    total_tasks: 10,
    token_created_at: null,
    provider: 'openai',
    auth_mode: 'api_key',
    provider_metadata: {},
    disabled_models: {},
    ...overrides,
  };
}

function Harness({
  onAddAccount = vi.fn(),
  ...overrides
}: {
  onAddAccount?: () => void;
} & Partial<ComponentProps<typeof SettingsAccountsSection>>) {
  const onUpdateAccountCaps = overrides.onUpdateAccountCaps ?? vi.fn();
  const [newAccountName, setNewAccountName] = useState(overrides.newAccountName ?? '');
  const [newAccountProvider, setNewAccountProvider] = useState(overrides.newAccountProvider ?? 'anthropic');
  const [newAccountAuthMode, setNewAccountAuthMode] = useState(overrides.newAccountAuthMode ?? 'oauth');
  const [newAccountToken, setNewAccountToken] = useState(overrides.newAccountToken ?? '');
  const [newAccountProviderMetadata, setNewAccountProviderMetadata] = useState(overrides.newAccountProviderMetadata ?? {});
  const [newAccountClaudeConfigJson, setNewAccountClaudeConfigJson] = useState(overrides.newAccountClaudeConfigJson ?? '');
  const [newAccountDailyTaskCap, setNewAccountDailyTaskCap] = useState(overrides.newAccountDailyTaskCap ?? null);
  const [newAccountMaxConcurrentTasks, setNewAccountMaxConcurrentTasks] = useState(overrides.newAccountMaxConcurrentTasks ?? null);
  const [newAccountAutoDisableAfterRateLimits, setNewAccountAutoDisableAfterRateLimits] = useState(overrides.newAccountAutoDisableAfterRateLimits ?? null);

  return (
    <SettingsAccountsSection
      githubAccount={null}
      onConnectGitHub={vi.fn()}
      isAdmin={true}
      maxPlanAccounts={overrides.maxPlanAccounts ?? []}
      onToggleAccount={vi.fn()}
      onRemoveAccount={vi.fn()}
      onResetRateLimits={vi.fn()}
      onRefreshAccountModels={vi.fn()}
      onUpdateAccountCaps={onUpdateAccountCaps}
      showUserManagement={false}
      onToggleUserManagement={vi.fn()}
      showProjectMembers={false}
      onToggleProjectMembers={vi.fn()}
      selectedProjectId={null}
      selectedProjectMyRole={null}
      onToggleSettings={vi.fn()}
      providers={overrides.providers ?? {
        anthropic: { auth_modes: { oauth: { required_metadata_fields: [] }, api_key: { required_metadata_fields: [] } }, supported_execution_modes: [], execution_modes: [], mode_capabilities: {} },
        openai: { auth_modes: { api_key: { required_metadata_fields: [] }, oauth: { required_metadata_fields: [] } }, supported_execution_modes: [], execution_modes: [], mode_capabilities: {} },
        gemini: { auth_modes: { api_key: { required_metadata_fields: [] }, service_account: { required_metadata_fields: ['project', 'location'] } }, supported_execution_modes: [], execution_modes: [], mode_capabilities: {} },
      } as Record<string, ProviderCatalogEntry>}
      newAccountName={newAccountName}
      setNewAccountName={setNewAccountName}
      newAccountProvider={newAccountProvider}
      setNewAccountProvider={setNewAccountProvider}
      newAccountAuthMode={newAccountAuthMode}
      setNewAccountAuthMode={setNewAccountAuthMode}
      newAccountToken={newAccountToken}
      setNewAccountToken={setNewAccountToken}
      newAccountProviderMetadata={newAccountProviderMetadata}
      setNewAccountProviderMetadata={setNewAccountProviderMetadata}
      newAccountClaudeConfigJson={newAccountClaudeConfigJson}
      setNewAccountClaudeConfigJson={setNewAccountClaudeConfigJson}
      newAccountDailyTaskCap={newAccountDailyTaskCap}
      setNewAccountDailyTaskCap={setNewAccountDailyTaskCap}
      newAccountMaxConcurrentTasks={newAccountMaxConcurrentTasks}
      setNewAccountMaxConcurrentTasks={setNewAccountMaxConcurrentTasks}
      newAccountAutoDisableAfterRateLimits={newAccountAutoDisableAfterRateLimits}
      setNewAccountAutoDisableAfterRateLimits={setNewAccountAutoDisableAfterRateLimits}
      onAddAccount={onAddAccount}
    />
  );
}

describe('SettingsAccountsSection', () => {
  it('enables the add-account flow once required fields are filled', () => {
    const onAddAccount = vi.fn();
    render(<Harness onAddAccount={onAddAccount} />);

    fireEvent.click(screen.getByRole('button', { name: 'Add account' }));
    expect(screen.getByText('Claude Max / Pro subscription token.')).toBeInTheDocument();

    const addButton = screen.getByRole('button', { name: 'Add Account' });
    expect(addButton).toBeDisabled();

    fireEvent.change(screen.getByPlaceholderText('e.g. my-claude-max'), { target: { value: 'my-account' } });
    fireEvent.change(screen.getByPlaceholderText('OAuth token'), { target: { value: 'secret-token' } });

    expect(addButton).not.toBeDisabled();
    fireEvent.click(addButton);
    expect(onAddAccount).toHaveBeenCalledOnce();
  });

  it('shows the credential guide for anthropic oauth', () => {
    render(<Harness />);

    fireEvent.click(screen.getByRole('button', { name: 'Add account' }));

    expect(screen.getByText('Claude Max / Pro subscription token.')).toBeInTheDocument();
    expect(screen.getByText('claude setup-token')).toBeInTheDocument();
    expect(screen.getByText(/Copy the printed token/)).toBeInTheDocument();
  });

  it('updates usage caps for an existing account', () => {
    const onUpdateAccountCaps = vi.fn();
    render(
      <Harness
        maxPlanAccounts={[
          makeAccount({
            daily_task_cap: 3,
            max_concurrent_tasks: 4,
            auto_disable_after_rate_limits: 5,
          }),
        ]}
        onUpdateAccountCaps={onUpdateAccountCaps}
      />
    );

    fireEvent.click(screen.getByTitle('Edit usage caps'));
    const inputs = screen.getAllByRole('spinbutton');
    fireEvent.change(inputs[0], { target: { value: '7' } });
    fireEvent.change(inputs[1], { target: { value: '8' } });
    fireEvent.change(inputs[2], { target: { value: '9' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save caps' }));

    expect(onUpdateAccountCaps).toHaveBeenCalledWith('acct-1', {
      daily_task_cap: 7,
      max_concurrent_tasks: 8,
      auto_disable_after_rate_limits: 9,
    });
  });
});
