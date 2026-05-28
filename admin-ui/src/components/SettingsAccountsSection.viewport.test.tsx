import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { screen } from '@testing-library/react';
import { SettingsAccountsSection } from './SettingsAccountsSection';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';
import type { MaxPlanAccount } from '../types/settings';

function makeAccount() {
  const account: MaxPlanAccount = {
    id: 'acct-1',
    name: 'Primary Claude Max Account With Long Name',
    enabled: true,
    active_tasks: 3,
    total_tasks: 12,
    token_created_at: '2024-01-01T00:00:00Z',
    provider: 'gemini',
    auth_mode: 'api_key',
    provider_metadata: {
      project: 'my-super-long-gcp-project-id',
      location: 'us-central1',
      available_models: ['gemini-3.1-pro-preview', 'gemini-3-flash-preview', 'gemini-2.5-pro'],
    },
    disabled_models: {
      'claude-sonnet-4-6': { hits: 2, at: '2024-01-01T00:00:00Z' },
    },
    daily_task_cap: 30,
    max_concurrent_tasks: 5,
    auto_disable_after_rate_limits: 3,
  };
  return account;
}

function renderSection() {
  return (
    <SettingsAccountsSection
      githubAccount={null}
      onConnectGitHub={() => {}}
      isAdmin={true}
      maxPlanAccounts={[makeAccount()]}
      onToggleAccount={() => {}}
      onRemoveAccount={() => {}}
      onResetRateLimits={() => {}}
      onRefreshAccountModels={() => {}}
      onUpdateAccountCaps={() => {}}
      showUserManagement={false}
      onToggleUserManagement={() => {}}
      showProjectMembers={false}
      onToggleProjectMembers={() => {}}
      selectedProjectId="project-123"
      selectedProjectMyRole="owner"
      onToggleSettings={() => {}}
      providers={{
        anthropic: { auth_modes: { oauth: { required_metadata_fields: [] }, api_key: { required_metadata_fields: [] } }, supported_execution_modes: [], execution_modes: [], mode_capabilities: {} },
        openai: { auth_modes: { api_key: { required_metadata_fields: [] }, oauth: { required_metadata_fields: [] } }, supported_execution_modes: [], execution_modes: [], mode_capabilities: {} },
        gemini: { auth_modes: { api_key: { required_metadata_fields: [] }, service_account: { required_metadata_fields: ['project', 'location'] } }, supported_execution_modes: [], execution_modes: [], mode_capabilities: {} },
      }}
      newAccountName=""
      setNewAccountName={() => {}}
      newAccountProvider="anthropic"
      setNewAccountProvider={() => {}}
      newAccountAuthMode="oauth"
      setNewAccountAuthMode={() => {}}
      newAccountToken=""
      setNewAccountToken={() => {}}
      newAccountProviderMetadata={{}}
      setNewAccountProviderMetadata={() => {}}
      newAccountClaudeConfigJson=""
      setNewAccountClaudeConfigJson={() => {}}
      newAccountDailyTaskCap={null}
      setNewAccountDailyTaskCap={() => {}}
      newAccountMaxConcurrentTasks={null}
      setNewAccountMaxConcurrentTasks={() => {}}
      newAccountAutoDisableAfterRateLimits={null}
      setNewAccountAutoDisableAfterRateLimits={() => {}}
      onAddAccount={() => {}}
    />
  );
}

describe('SettingsAccountsSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders without horizontal overflow at 375px with an open add form', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(renderSection());
      });
      await act(async () => {
        screen.getByRole('button', { name: 'Add account' }).click();
      });
      await act(async () => {
        screen.getByRole('button', { name: 'Usage limits (optional)' }).click();
      });
    });
  });

  test('form controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(renderSection());
      });
      await act(async () => {
        screen.getByRole('button', { name: 'Add account' }).click();
      });
      await act(async () => {
        screen.getByRole('button', { name: 'Usage limits (optional)' }).click();
      });
    }, { selector: 'button, a[href]' });
  });

  test('text does not silently truncate at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(renderSection());
      });
      await act(async () => {
        screen.getByRole('button', { name: 'Add account' }).click();
      });
      await act(async () => {
        screen.getByRole('button', { name: 'Usage limits (optional)' }).click();
      });
    });
  });
});
