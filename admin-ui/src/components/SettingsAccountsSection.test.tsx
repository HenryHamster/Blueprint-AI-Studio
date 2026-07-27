import { useRef, useState, type ComponentProps } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { SettingsAccountsSection } from './SettingsAccountsSection';
import type { AccountMutationResult, MaxPlanAccount, ProviderCatalogEntry } from '../types/settings';
import { DEFAULT_POLL_MS } from '../hooks/usePoller';

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
  const onUpdateAccount = overrides.onUpdateAccount ?? vi.fn();
  const onRemoveAccount = overrides.onRemoveAccount ?? vi.fn();
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
      maxPlanAccounts={overrides.maxPlanAccounts ?? []}
      onToggleAccount={overrides.onToggleAccount ?? vi.fn()}
      onRemoveAccount={onRemoveAccount}
      onResetRateLimits={vi.fn()}
      onRefreshAccountModels={vi.fn()}
      onRefreshAccounts={overrides.onRefreshAccounts ?? vi.fn()}
      onUpdateAccount={onUpdateAccount}
      onUpdateAccountCaps={onUpdateAccountCaps}
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
  it('refreshes an unknown usage snapshot while the accounts surface remains active', async () => {
    vi.useFakeTimers();
    try {
      function RefreshingHarness() {
        const [accounts, setAccounts] = useState([
          makeAccount({
            usage_snapshot: {
              known: false,
              five_hour_utilization: null,
              seven_day_utilization: null,
              resets_at: {},
              probed_at: null,
              stale: false,
              dark_reason: 'endpoint',
              last_attempt_at: '2026-07-26T21:42:15Z',
              last_attempt_failure_kind: 'endpoint',
            },
          }),
        ]);
        const refreshCount = useRef(0);

        return (
          <Harness
            maxPlanAccounts={accounts}
            onRefreshAccounts={() => {
              refreshCount.current += 1;
              if (refreshCount.current === 1) {
                setAccounts([
                  makeAccount({
                    usage_snapshot: {
                      known: true,
                      five_hour_utilization: 42,
                      seven_day_utilization: 57,
                      resets_at: {},
                      probed_at: '2026-07-26T21:43:00Z',
                      stale: false,
                      dark_reason: null,
                      last_attempt_at: '2026-07-26T21:43:00Z',
                      last_attempt_failure_kind: null,
                    },
                  }),
                ]);
              }
            }}
          />
        );
      }

      render(<RefreshingHarness />);
      expect(screen.getByText(/usage unavailable \(probe endpoint\)/)).toBeInTheDocument();

      await act(async () => {
        vi.advanceTimersByTime(DEFAULT_POLL_MS);
      });

      expect(screen.getByText('42%')).toBeInTheDocument();
      expect(screen.getByText('57%')).toBeInTheDocument();
      expect(screen.queryByText(/usage unavailable/)).not.toBeInTheDocument();
    } finally {
      vi.useRealTimers();
    }
  });

  it('renders a weekly-only Codex window without inventing a five-hour period', () => {
    render(
      <Harness
        maxPlanAccounts={[
          makeAccount({
            provider: 'openai',
            usage_snapshot: {
              known: true,
              five_hour_utilization: null,
              seven_day_utilization: 48,
              resets_at: { seven_day: '2026-08-01T20:47:28Z' },
              probed_at: '2026-07-27T01:46:49Z',
              stale: false,
              dark_reason: null,
              last_attempt_at: '2026-07-27T01:46:49Z',
              last_attempt_failure_kind: null,
            },
          }),
        ]}
      />,
    );

    expect(screen.getByText('48%')).toBeInTheDocument();
    expect(screen.queryByText('5h')).not.toBeInTheDocument();
    expect(screen.getByText(/7d/)).toBeInTheDocument();
  });

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

  it('opens_edit_dialog_and_patches_full_account_payload_without_secret_echo', () => {
    const onUpdateAccount = vi.fn().mockResolvedValue({ ok: true } as AccountMutationResult);
    render(
      <Harness
        maxPlanAccounts={[
          makeAccount({
            id: 'acct-9',
            name: 'Old Name',
            enabled: true,
            provider: 'anthropic',
            auth_mode: 'oauth',
            daily_task_cap: 5,
            max_concurrent_tasks: null,
            auto_disable_after_rate_limits: null,
          }),
        ]}
        onUpdateAccount={onUpdateAccount}
      />
    );

    // Open the per-row edit dialog.
    fireEvent.click(screen.getByRole('button', { name: 'Edit account Old Name' }));

    // The token/secret field is strictly write-only: it must NOT echo any
    // stored secret — it starts empty even though the account exists.
    const tokenInput = screen.getByLabelText('New token') as HTMLInputElement;
    expect(tokenInput.value).toBe('');
    expect(tokenInput.getAttribute('type')).toBe('password');

    // Edit a full set of fields: rename, disable, change a cap, rotate token.
    fireEvent.change(screen.getByLabelText('Account name'), { target: { value: 'New Name' } });
    fireEvent.click(screen.getByLabelText('Enabled'));
    fireEvent.change(screen.getByLabelText('Tasks per day cap'), { target: { value: '12' } });
    fireEvent.change(tokenInput, { target: { value: 'sk-ant-oat01-rotated' } });

    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    expect(onUpdateAccount).toHaveBeenCalledTimes(1);
    const [id, payload] = onUpdateAccount.mock.calls[0];
    expect(id).toBe('acct-9');
    expect(payload).toMatchObject({
      name: 'New Name',
      enabled: false,
      daily_task_cap: 12,
      secret: 'sk-ant-oat01-rotated',
    });
    // Unchanged identity is not echoed back into the patch.
    expect(payload).not.toHaveProperty('provider');
    expect(payload).not.toHaveProperty('auth_mode');
  });

  it('requires a new token when provider/auth mode changes', () => {
    const onUpdateAccount = vi.fn().mockResolvedValue({ ok: true } as AccountMutationResult);
    render(
      <Harness
        maxPlanAccounts={[makeAccount({ id: 'acct-2', name: 'Switcher', provider: 'openai', auth_mode: 'api_key' })]}
        onUpdateAccount={onUpdateAccount}
      />
    );
    fireEvent.click(screen.getByRole('button', { name: 'Edit account Switcher' }));
    // Switch auth mode without supplying a token.
    fireEvent.change(screen.getByLabelText('Auth mode'), { target: { value: 'oauth' } });

    const save = screen.getByRole('button', { name: 'Save changes' });
    expect(save).toBeDisabled();
    expect(screen.getByText('New token required')).toBeInTheDocument();
    fireEvent.click(save);
    expect(onUpdateAccount).not.toHaveBeenCalled();
  });

  it('surfaces delete conflict detail and offers a force delete', async () => {
    const onRemoveAccount = vi.fn()
      .mockResolvedValueOnce({ ok: false, conflict: true, detail: '2 active task(s) block removal' } as AccountMutationResult)
      .mockResolvedValueOnce({ ok: true } as AccountMutationResult);
    render(<Harness maxPlanAccounts={[makeAccount({ name: 'Primary' })]} onRemoveAccount={onRemoveAccount} />);

    fireEvent.click(screen.getByRole('button', { name: 'Delete account Primary' }));
    fireEvent.click(screen.getByRole('button', { name: 'Delete' }));

    expect(await screen.findByText('2 active task(s) block removal')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Force delete' }));

    await waitFor(() => expect(onRemoveAccount).toHaveBeenCalledTimes(2));
    expect(onRemoveAccount).toHaveBeenNthCalledWith(1, 'acct-1', false);
    expect(onRemoveAccount).toHaveBeenNthCalledWith(2, 'acct-1', true);
  });

  it('disables row actions while an edit save is pending', async () => {
    let resolveSave: (v: AccountMutationResult) => void = () => {};
    const onUpdateAccount = vi.fn(() => new Promise<AccountMutationResult>(res => { resolveSave = res; }));
    render(<Harness maxPlanAccounts={[makeAccount({ name: 'Primary' })]} onUpdateAccount={onUpdateAccount} />);

    fireEvent.click(screen.getByRole('button', { name: 'Edit account Primary' }));
    fireEvent.change(screen.getByLabelText('Account name'), { target: { value: 'Renamed' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save changes' }));

    // Sibling row actions are disabled while the save is in flight.
    expect(screen.getByRole('button', { name: 'Delete account Primary' })).toBeDisabled();

    resolveSave({ ok: true });
    await waitFor(() => expect(onUpdateAccount).toHaveBeenCalledTimes(1));
  });
});
