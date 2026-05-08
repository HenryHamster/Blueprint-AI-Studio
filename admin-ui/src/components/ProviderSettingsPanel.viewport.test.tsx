import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { ProviderSettingsPanel } from './ProviderSettingsPanel';
import type {
  MaxPlanAccount,
  ProviderCatalogEntry,
  ProviderSettingsByEffort,
  ProviderSettingsDefaults,
  TaskTypeProviderOverrides,
} from '../types/settings';
import { expectNoOverflowAt, expectMinTouchTargetAt } from '../test/viewport';

const providers: Record<string, ProviderCatalogEntry> = {
  anthropic: {
    auth_modes: { api_key: { required_metadata_fields: [] } },
    supported_execution_modes: ['code'],
    execution_modes: ['code'],
    mode_capabilities: {},
    settings_schema: {
      type: 'object' as const,
      properties: {
        model: { type: 'string', enum: ['claude-sonnet-4-6', 'claude-opus-4-7', 'claude-haiku-4-5-20251001'] },
        max_tokens: { type: 'integer' },
        thinking_budget_tokens: { type: 'integer', description: 'Budget for extended thinking mode token usage' },
        very_long_field_name_that_might_overflow_the_label_column: { type: 'boolean' },
      },
    },
  },
};

const maxPlanAccounts: MaxPlanAccount[] = [
  {
    id: 'acct-1',
    name: 'very-long-account-name-that-should-not-cause-overflow-at-mobile-width',
    enabled: true,
    active_tasks: 0,
    total_tasks: 0,
    token_created_at: null,
    provider: 'anthropic',
    auth_mode: 'api_key',
  },
];

const providerSettingsDefaults: ProviderSettingsDefaults = {
  anthropic: { model: 'claude-sonnet-4-6', max_tokens: 8192 },
};

const taskTypeProviderOverrides: TaskTypeProviderOverrides = {
  review: { anthropic: { model: 'claude-opus-4-7' } },
};

const providerSettingsByEffort: ProviderSettingsByEffort = {
  anthropic: { low: { model: 'claude-haiku-4-5-20251001' }, medium: { model: 'claude-sonnet-4-6' }, high: { model: 'claude-opus-4-7' }, max: { model: 'claude-opus-4-7', thinking_budget_tokens: 10000 } },
  openai: {},
  gemini: {},
};

describe('ProviderSettingsPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProviderSettingsPanel
            providers={providers}
            providerSettingsDefaults={providerSettingsDefaults}
            taskTypeProviderOverrides={taskTypeProviderOverrides}
            providerSettingsByEffort={providerSettingsByEffort}
            maxPlanAccounts={maxPlanAccounts}
            onUpdateDefaults={async () => {}}
            onUpdateOverrides={async () => {}}
            onUpdateProviderSettingsByEffort={async () => {}}
          />,
        );
      });
    });
  });

  test('interactive controls meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <ProviderSettingsPanel
            providers={providers}
            providerSettingsDefaults={providerSettingsDefaults}
            taskTypeProviderOverrides={taskTypeProviderOverrides}
            providerSettingsByEffort={providerSettingsByEffort}
            maxPlanAccounts={maxPlanAccounts}
            onUpdateDefaults={async () => {}}
            onUpdateOverrides={async () => {}}
            onUpdateProviderSettingsByEffort={async () => {}}
          />,
        );
      });
    }, { selector: 'button, a[href]' });
  });
});
