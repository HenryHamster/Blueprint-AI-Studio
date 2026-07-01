import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { __pollerInternals } from '../hooks/usePoller';
import { RuntimeAccountsPanel } from './RuntimeAccountsPanel';

const { apiFetchMock, costPanelProps, modelPanelProps } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
  costPanelProps: [] as Array<{ selectedProjectId: string | null; windowHours: number }>,
  modelPanelProps: [] as Array<{ selectedProjectId: string | null; accounts: unknown }>,
}));

vi.mock('../api', () => ({
  apiFetch: (...args: unknown[]) => apiFetchMock(...args),
}));

vi.mock('./SettingsSectionRouter', () => ({
  SettingsSectionRouter: ({ activeSection }: { activeSection: string }) => (
    <div data-testid="settings-section">{activeSection}</div>
  ),
}));

vi.mock('./ProviderCostPanel', () => ({
  ProviderCostPanel: (props: { selectedProjectId: string | null; windowHours: number }) => {
    costPanelProps.push(props);
    return <div data-testid="provider-cost-panel">cost window {props.windowHours}</div>;
  },
}));

vi.mock('./ProviderModelBreakdownPanel', () => ({
  ProviderModelBreakdownPanel: (props: { selectedProjectId: string | null; accounts: unknown }) => {
    modelPanelProps.push(props);
    return <div data-testid="provider-model-panel">model breakdown</div>;
  },
}));

function jsonResponse(data: unknown): Response {
  return { ok: true, json: async () => data } as Response;
}

function settingsProps() {
  return {
    githubAccount: 'octo-admin',
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
        credential_configured: true,
        runtime_ready: true,
        account_status: 'ready',
      },
      {
        id: 'acct-2',
        name: 'Backup',
        enabled: false,
        active_tasks: 0,
        total_tasks: 4,
        token_created_at: null,
        provider: 'openai',
        auth_mode: 'api_key',
        disabled_models: {
          'gpt-test': { hits: 3, at: '2026-06-30T10:00:00Z' },
        },
        credential_configured: false,
        runtime_ready: false,
        account_status: 'disabled',
      },
    ],
  } as never;
}

describe('RuntimeAccountsPanel', () => {
  beforeEach(() => {
    apiFetchMock.mockReset();
    costPanelProps.length = 0;
    modelPanelProps.length = 0;
  });

  afterEach(() => {
    vi.useRealTimers();
    __pollerInternals.reset();
  });

  it('co-locates account configuration with live health, model, and cost monitoring', async () => {
    apiFetchMock.mockResolvedValue(
      jsonResponse({
        accounts: [
          {
            account_id: 'acct-1',
            label: 'Primary',
            provider: 'anthropic',
            state: 'healthy',
            health_score: 0.92,
            completions_24h: 18,
            rl_events_24h: 0,
            rate_limited_models: [],
            failure_class_counts: {},
            usage_snapshot: {
              known: true,
              five_hour_utilization: 42,
              seven_day_utilization: 55,
              resets_at: {},
              probed_at: '2026-06-30T10:00:00Z',
              stale: false,
            },
          },
          {
            account_id: 'acct-2',
            label: 'Backup',
            provider: 'openai',
            state: 'disabled',
            health_score: 0.2,
            completions_24h: 2,
            rl_events_24h: 3,
            rate_limited_models: [
              { model: 'gpt-test', cooldown_seconds_remaining: 3600, cooldown_type: 'quota_exhausted' },
            ],
            failure_class_counts: {},
            usage_snapshot: {
              known: true,
              five_hour_utilization: 91,
              seven_day_utilization: 88,
              resets_at: {},
              probed_at: '2026-06-30T10:00:00Z',
              stale: false,
            },
          },
        ],
        served_stale: false,
      }),
    );

    render(<RuntimeAccountsPanel selectedProjectId="proj-1" settingsRouterProps={settingsProps()} />);
    expect(await screen.findByText('Provider Account Health')).toBeInTheDocument();

    expect(screen.getByTestId('settings-section')).toHaveTextContent('accounts');
    expect(screen.getByTestId('account-health-summary')).toHaveTextContent('1/2');
    const monitor = screen.getByRole('region', { name: 'Runtime account monitor' });
    expect(monitor).toHaveTextContent('1/2');
    expect(monitor).toHaveTextContent('2 providers');
    expect(screen.getByTestId('runtime-accounts-next-action')).toHaveTextContent('Inspect 1 non-healthy account');
    expect(screen.getByTestId('provider-model-panel')).toBeInTheDocument();
    expect(screen.getByTestId('provider-cost-panel')).toHaveTextContent('cost window 24');
    expect(modelPanelProps.at(-1)?.accounts).toHaveLength(2);
    expect(costPanelProps.at(-1)).toMatchObject({ selectedProjectId: 'proj-1', windowHours: 24 });
  });

  it('prioritizes local cap holds in the account monitor next action', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ accounts: [] }));
    const props = settingsProps() as {
      maxPlanAccounts: Array<{ usage?: { at_any_cap: boolean }; account_status?: string }>;
    };
    props.maxPlanAccounts[0].usage = { at_any_cap: true };
    props.maxPlanAccounts[0].account_status = 'at_cap';

    render(<RuntimeAccountsPanel selectedProjectId="proj-1" settingsRouterProps={props as never} />);
    await act(async () => {});

    expect(screen.getByTestId('runtime-accounts-next-action')).toHaveTextContent('Raise caps or wait for reset on 1 held account');
    expect(screen.getByText('Active').parentElement).toHaveTextContent('1 cap holds');
  });

  it('surfaces account-health poll failures while keeping cost/model monitors mounted', async () => {
    apiFetchMock.mockRejectedValue(new Error('network down'));

    render(<RuntimeAccountsPanel selectedProjectId={null} settingsRouterProps={{ githubAccount: null, maxPlanAccounts: [] } as never} />);

    expect(await screen.findByRole('alert')).toHaveTextContent('Account health polling failed');
    expect(screen.getByText(/fleet-wide readings/i)).toBeInTheDocument();
    expect(screen.getByTestId('provider-model-panel')).toBeInTheDocument();
    expect(screen.getByTestId('provider-cost-panel')).toBeInTheDocument();
    expect(modelPanelProps.at(-1)?.accounts).toBeNull();
  });

  it('does not report accounts ready while initial health is loading', () => {
    apiFetchMock.mockResolvedValue(new Promise(() => {}));

    render(<RuntimeAccountsPanel selectedProjectId="proj-1" settingsRouterProps={settingsProps()} />);

    expect(screen.getByTestId('runtime-accounts-next-action')).toHaveTextContent('Loading account health before dispatch');
    const monitor = screen.getByRole('region', { name: 'Runtime account monitor' });
    expect(monitor).toHaveTextContent('polling');
  });

  it('does not report cached health as ready after polling fails', async () => {
    apiFetchMock
      .mockResolvedValueOnce(jsonResponse({ accounts: [], served_stale: false }))
      .mockRejectedValueOnce(new Error('network down'));
    vi.useFakeTimers();

    render(<RuntimeAccountsPanel selectedProjectId="proj-1" settingsRouterProps={settingsProps()} />);
    await act(async () => {});
    expect(screen.getByTestId('runtime-accounts-next-action')).toHaveTextContent('Accounts are ready for dispatch');

    await act(async () => {
      vi.advanceTimersByTime(30_000);
    });
    await act(async () => {});

    expect(screen.getByTestId('runtime-accounts-next-action')).toHaveTextContent('Account health polling failed; using last-known rows');
  });

  it('updates provider cost window from the account monitor selector', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ accounts: [] }));
    const user = userEvent.setup();

    render(<RuntimeAccountsPanel selectedProjectId="proj-1" settingsRouterProps={settingsProps()} />);
    await act(async () => {});

    await user.selectOptions(screen.getByLabelText('Usage window'), '168');

    expect(screen.getByTestId('provider-cost-panel')).toHaveTextContent('cost window 168');
    expect(costPanelProps.at(-1)).toMatchObject({ selectedProjectId: 'proj-1', windowHours: 168 });
  });

  it('routes account monitor shortcuts to runtime settings sections', async () => {
    apiFetchMock.mockResolvedValue(jsonResponse({ accounts: [] }));
    const user = userEvent.setup();
    const onOpenRuntimeSettingsSection = vi.fn();

    render(
      <RuntimeAccountsPanel
        selectedProjectId="proj-1"
        settingsRouterProps={settingsProps()}
        availableSettingsSections={['provider-settings', 'provider-routing', 'notifications']}
        onOpenRuntimeSettingsSection={onOpenRuntimeSettingsSection}
      />,
    );
    await act(async () => {});

    await user.click(screen.getByRole('button', { name: 'Providers' }));
    await user.click(screen.getByRole('button', { name: 'Routing' }));
    await user.click(screen.getByRole('button', { name: 'Alerts' }));

    expect(onOpenRuntimeSettingsSection.mock.calls.map(([section]) => section)).toEqual([
      'provider-settings',
      'provider-routing',
      'notifications',
    ]);
  });
});
