import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { RuntimeAccountsPanel } from './RuntimeAccountsPanel';
import { expectMinTouchTargetAt, expectNoOverflowAt } from '../test/viewport';

vi.mock('../api', () => ({
  apiFetch: async () => ({
    ok: true,
    json: async () => ({
      accounts: [
        {
          account_id: 'acct-very-long-provider-name',
          label: 'Primary account with a long label',
          provider: 'anthropic',
          state: 'healthy',
          health_score: 0.94,
          completions_24h: 22,
          rl_events_24h: 0,
          rate_limited_models: [],
          failure_class_counts: {},
        },
      ],
    }),
  }),
}));

vi.mock('./SettingsSectionRouter', () => ({
  SettingsSectionRouter: ({ activeSection }: { activeSection: string }) => (
    <div className="min-h-[220px] rounded-md border border-neutral-800 p-3" data-testid="settings-section">
      {activeSection}
    </div>
  ),
}));

vi.mock('./ProviderCostPanel', () => ({
  ProviderCostPanel: () => <div className="min-h-[180px] rounded-md border border-neutral-800 p-3">cost</div>,
}));

vi.mock('./ProviderModelBreakdownPanel', () => ({
  ProviderModelBreakdownPanel: () => <div className="min-h-[180px] rounded-md border border-neutral-800 p-3">models</div>,
}));

function settingsProps() {
  return {
    githubAccount: 'octo-admin-long-identity',
    maxPlanAccounts: [
      {
        id: 'acct-1',
        name: 'Primary',
        enabled: true,
        active_tasks: 2,
        total_tasks: 10,
        token_created_at: null,
        provider: 'anthropic',
        auth_mode: 'api_key',
        daily_task_cap: 20,
        max_concurrent_tasks: null,
        disabled_models: {},
      },
    ],
  } as never;
}

describe('RuntimeAccountsPanel viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  function cleanupHost() {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
    }
    root = null;
    host?.remove();
    host = null;
  }

  afterEach(() => {
    cleanupHost();
  });

  async function renderPanel() {
    cleanupHost();
    host = document.createElement('div');
    host.style.height = '760px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <RuntimeAccountsPanel
          selectedProjectId="proj-1"
          settingsRouterProps={settingsProps()}
          availableSettingsSections={['provider-settings', 'provider-routing', 'notifications']}
          onOpenRuntimeSettingsSection={() => {}}
        />,
      );
    });
  }

  test('mobile account monitor shortcuts fit and keep touch targets usable', async () => {
    await expectNoOverflowAt(375, renderPanel, { height: 760, waitMs: 25 });
    await expectMinTouchTargetAt(375, renderPanel, { height: 760, waitMs: 25 });
  });
});
