import { useState, useMemo } from 'react';
import { apiFetch } from '../api';
import { DEFAULT_POLL_MS, usePoller } from '../hooks/usePoller';
import { SettingsSectionRouter, type SettingsSectionRouterProps } from './SettingsSectionRouter';
import { PoolAccountHealthPanel, type AccountHealthEntry } from './PoolAccountHealthPanel';
import { ProviderModelBreakdownPanel } from './ProviderModelBreakdownPanel';
import { ProviderCostPanel } from './ProviderCostPanel';
import type { RuntimeSettingsSection } from './OperatorWorkspaceSections';
import type { MaxPlanAccount } from '../types/settings';

const WINDOW_OPTIONS = [
  { label: '6h', value: 6 },
  { label: '24h', value: 24 },
  { label: '7d', value: 168 },
];

interface AccountHealthResponse {
  accounts?: AccountHealthEntry[];
  served_stale?: boolean;
  generated_at?: string | null;
}

function StatCell({
  label,
  value,
  sub,
  tone = 'neutral',
}: {
  label: string;
  value: string | number;
  sub?: string;
  tone?: 'neutral' | 'good' | 'warn' | 'bad' | 'info';
}) {
  const valueClass = {
    neutral: 'text-neutral-100',
    good: 'text-emerald-300',
    warn: 'text-amber-300',
    bad: 'text-red-300',
    info: 'text-cyan-300',
  }[tone];

  return (
    <div className="min-w-0 border-r border-b border-neutral-800 px-3 py-2 last:border-r-0 sm:border-b-0">
      <div className="text-ui-sm font-semibold uppercase tracking-wider text-neutral-500">{label}</div>
      <div className={`mt-0.5 truncate font-mono text-sm font-semibold ${valueClass}`}>{value}</div>
      {sub && <div className="mt-0.5 truncate text-ui-md text-neutral-600">{sub}</div>}
    </div>
  );
}

function highUsage(account: AccountHealthEntry): number {
  const snapshot = account.usage_snapshot;
  if (!snapshot?.known) return 0;
  return Math.max(snapshot.five_hour_utilization ?? 0, snapshot.seven_day_utilization ?? 0);
}

function plural(count: number, singular: string, pluralLabel = `${singular}s`): string {
  return `${count} ${count === 1 ? singular : pluralLabel}`;
}

function accountIsDispatchReady(account: MaxPlanAccount): boolean {
  const status = typeof account.account_status === 'string' ? account.account_status.toLowerCase() : null;
  return (
    account.enabled
    && account.runtime_ready === true
    && account.credential_configured !== false
    && !account.disabled_reason
    && status !== 'at_cap'
    && status !== 'disabled'
    && status !== 'unavailable'
    && status !== 'unconfigured'
  );
}

export function RuntimeAccountsPanel({
  selectedProjectId,
  settingsRouterProps,
  availableSettingsSections = [],
  onOpenRuntimeSettingsSection,
}: {
  selectedProjectId: string | null;
  settingsRouterProps: Omit<SettingsSectionRouterProps, 'activeSection'>;
  availableSettingsSections?: RuntimeSettingsSection[];
  onOpenRuntimeSettingsSection?: (section: RuntimeSettingsSection) => void;
}) {
  const [windowHours, setWindowHours] = useState(24);
  const [accountHealth, setAccountHealth] = useState<AccountHealthEntry[] | null>(null);
  const [loadingHealth, setLoadingHealth] = useState(true);
  const [pollError, setPollError] = useState(false);
  const [servedStale, setServedStale] = useState(false);
  const [generatedAt, setGeneratedAt] = useState<string | null>(null);

  usePoller(async (signal) => {
    try {
      const res = await apiFetch('/api/admin/max-plan-accounts/health', undefined, 2, 30000, signal);
      if (signal.aborted) return;
      if (!res.ok) {
        setPollError(true);
        return;
      }
      const json = await res.json() as AccountHealthResponse;
      if (signal.aborted) return;
      setAccountHealth(Array.isArray(json.accounts) ? json.accounts : []);
      setServedStale(json.served_stale === true);
      setGeneratedAt(typeof json.generated_at === 'string' ? json.generated_at : null);
      setPollError(false);
    } catch {
      if (!signal.aborted) setPollError(true);
    } finally {
      if (!signal.aborted) setLoadingHealth(false);
    }
  }, { intervalMs: DEFAULT_POLL_MS, deps: [] });

  const {
    configuredAccounts,
    providerCount,
    enabledAccounts,
    runtimeReadyAccounts,
    activeTasks,
    cappedAccounts,
    capHeldAccounts,
    cooldowns,
    healthRows,
    nonHealthyRows,
    usagePressureRows,
    quotaPressureRows,
    rateLimitedModels,
    totalCooldowns,
  } = useMemo(() => {
    const configuredAccounts = settingsRouterProps.maxPlanAccounts ?? [];
    const providerCount = new Set(configuredAccounts.map(account => account.provider)).size;
    const enabledAccounts = configuredAccounts.filter(account => account.enabled).length;
    const runtimeReadyAccounts = configuredAccounts.filter(accountIsDispatchReady).length;
    const activeTasks = configuredAccounts.reduce((sum, account) => sum + (account.active_tasks ?? 0), 0);
    const cappedAccounts = configuredAccounts.filter(
      account => account.daily_task_cap != null || account.max_concurrent_tasks != null,
    ).length;
    const capHeldAccounts = configuredAccounts.filter(
      account => account.usage?.at_any_cap || account.account_status === 'at_cap',
    ).length;
    const cooldowns = configuredAccounts.filter(account => account.enabled).reduce(
      (sum, account) => sum + Object.keys(account.disabled_models ?? {}).length,
      0,
    );
    const healthRows = accountHealth ?? [];
    const actionableHealthRows = healthRows.filter(account => account.state !== 'disabled');
    const nonHealthyRows = healthRows.filter(account => account.state !== 'healthy').length;
    const usagePressureRows = actionableHealthRows.filter(account => highUsage(account) >= 70).length;
    const quotaPressureRows = actionableHealthRows.filter(account => highUsage(account) >= 90).length;
    const rateLimitedModels = actionableHealthRows.reduce(
      (sum, account) => sum + account.rate_limited_models.length,
      0,
    );
    const totalCooldowns = cooldowns + rateLimitedModels;
    return {
      configuredAccounts,
      providerCount,
      enabledAccounts,
      runtimeReadyAccounts,
      activeTasks,
      cappedAccounts,
      capHeldAccounts,
      cooldowns,
      healthRows,
      nonHealthyRows,
      usagePressureRows,
      quotaPressureRows,
      rateLimitedModels,
      totalCooldowns,
    };
  }, [settingsRouterProps.maxPlanAccounts, accountHealth]);
  const nextAccountAction = (() => {
    if (!settingsRouterProps.githubAccount) return 'Connect GitHub identity before dispatching account-backed work.';
    if (configuredAccounts.length === 0) return 'Add a provider account to make the runtime schedulable.';
    if (enabledAccounts === 0) return 'Enable at least one provider account.';
    if (loadingHealth && accountHealth === null) return 'Loading account health before dispatch.';
    if (pollError && accountHealth === null) return 'Restore account health polling; runtime health is unknown.';
    if (pollError) return 'Account health polling failed; using last-known rows.';
    if (servedStale) return 'Account health is stale; verify polling before dispatch.';
    if (capHeldAccounts > 0) return `Raise caps or wait for reset on ${plural(capHeldAccounts, 'held account')}.`;
    if (runtimeReadyAccounts === 0) return 'Fix account credentials or status before dispatch.';
    if (quotaPressureRows > 0) return `Move traffic off ${plural(quotaPressureRows, 'near-quota account')}.`;
    if (totalCooldowns > 0) return `Let cooldowns expire or route around ${plural(totalCooldowns, 'suppressed model')}.`;
    if (nonHealthyRows > 0) return `Inspect ${plural(nonHealthyRows, 'non-healthy account')}.`;
    if (usagePressureRows > 0) return `Watch spend and quota pressure on ${plural(usagePressureRows, 'account')}.`;
    return 'Accounts are ready for dispatch.';
  })();
  const nextAccountActionGood = nextAccountAction === 'Accounts are ready for dispatch.';
  const healthTone = loadingHealth && accountHealth === null
    ? 'neutral'
    : pollError
      ? 'bad'
      : nonHealthyRows > 0
        ? 'warn'
        : 'good';
  const availableSettings = new Set(availableSettingsSections);
  const shortcutOptions: Array<{ section: RuntimeSettingsSection; label: string; title: string }> = [
    { section: 'provider-settings', label: 'Providers', title: 'Open provider model defaults' },
    { section: 'provider-routing', label: 'Routing', title: 'Open provider routing policy' },
    { section: 'notifications', label: 'Alerts', title: 'Open spend and notification settings' },
  ];
  const shortcuts = shortcutOptions.filter(shortcut => availableSettings.has(shortcut.section));

  return (
    <div className="space-y-3">
      <section aria-label="Runtime account monitor" className="workbench-layer rounded-md border">
        <div className="flex flex-col gap-2 border-b border-neutral-800 px-3 py-2 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <h3 className="text-xs font-semibold uppercase tracking-wider text-neutral-300">Account monitor</h3>
            <p className="mt-0.5 text-ui-md text-neutral-600">
              Identity, account pool health, model regressions, and usage cost.
            </p>
          </div>
          <div className="flex min-w-0 flex-wrap items-center gap-2 sm:justify-end">
            {shortcuts.map(shortcut => (
              <button
                key={shortcut.section}
                type="button"
                onClick={() => onOpenRuntimeSettingsSection?.(shortcut.section)}
                className="inline-flex min-h-11 items-center rounded-md border border-neutral-800 bg-workbench-shell/60 px-3 text-sm font-semibold text-neutral-300 transition-colors hover:border-neutral-700 hover:bg-workbench-hover hover:text-neutral-100 sm:min-h-8 sm:text-xs"
                title={shortcut.title}
              >
                {shortcut.label}
              </button>
            ))}
            <label className="flex shrink-0 items-center gap-2 text-ui-md text-neutral-500">
              Window
              <select
                aria-label="Usage window"
                value={windowHours}
                onChange={event => setWindowHours(Number(event.target.value))}
                className="min-h-11 rounded border border-neutral-700 bg-workbench-base px-2 text-sm text-neutral-300 sm:min-h-8 sm:text-xs workbench-focus"
              >
                {WINDOW_OPTIONS.map(option => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </label>
          </div>
        </div>
        <div
          className={`border-b px-3 py-2 text-xs ${
            nextAccountActionGood
              ? 'border-emerald-500/20 bg-emerald-500/10 text-emerald-200'
              : 'border-amber-500/20 bg-amber-500/10 text-amber-200'
          }`}
          data-testid="runtime-accounts-next-action"
        >
          <span className="font-semibold">Next action:</span> {nextAccountAction}
        </div>
        <div className="grid grid-cols-2 overflow-hidden sm:grid-cols-4 lg:grid-cols-6">
          <StatCell
            label="GitHub"
            value={settingsRouterProps.githubAccount ? 'connected' : 'missing'}
            sub={settingsRouterProps.githubAccount ?? 'no identity'}
            tone={settingsRouterProps.githubAccount ? 'good' : 'warn'}
          />
          <StatCell
            label="LLM"
            value={`${enabledAccounts}/${configuredAccounts.length}`}
            sub={`${runtimeReadyAccounts} ready, ${providerCount} provider${providerCount === 1 ? '' : 's'}`}
            tone={enabledAccounts > 0 ? 'good' : 'warn'}
          />
          <StatCell
            label="Active"
            value={activeTasks}
            sub={capHeldAccounts > 0 ? `${capHeldAccounts} cap holds` : `${cappedAccounts} capped`}
            tone={capHeldAccounts > 0 ? 'warn' : activeTasks > 0 ? 'info' : 'neutral'}
          />
          <StatCell
            label="Cooldowns"
            value={totalCooldowns}
            sub={`${cooldowns} configured, ${rateLimitedModels} health`}
            tone={totalCooldowns > 0 ? 'warn' : 'neutral'}
          />
          <StatCell
            label="Health"
            value={loadingHealth && accountHealth === null ? 'loading' : nonHealthyRows}
            sub={pollError ? 'poll failed' : loadingHealth && accountHealth === null ? 'polling' : `${healthRows.length} observed`}
            tone={healthTone}
          />
          <StatCell
            label="Usage"
            value={loadingHealth && accountHealth === null ? 'loading' : usagePressureRows}
            sub={quotaPressureRows > 0 ? `${quotaPressureRows} near quota` : '>=70% window'}
            tone={quotaPressureRows > 0 ? 'bad' : usagePressureRows > 0 ? 'warn' : 'neutral'}
          />
        </div>
        {pollError && (
          <div role="alert" className="border-t border-amber-500/20 bg-amber-500/10 px-3 py-2 text-ui-md text-amber-300">
            Account health polling failed. Last-known health rows stay visible when available.
          </div>
        )}
      </section>

      <div className="grid gap-3 xl:grid-cols-[minmax(360px,0.95fr)_minmax(0,1.05fr)]">
        <section className="workbench-layer min-w-0 rounded-md border p-3" aria-label="Account configuration">
          <SettingsSectionRouter {...settingsRouterProps} hostKind="workspace" activeSection="accounts" />
        </section>

        <section className="min-w-0 space-y-3" aria-label="Account runtime signals">
          {!selectedProjectId && (
            <div className="workbench-layer rounded-md border px-3 py-2 text-ui-md text-neutral-500">
              No project selected. Provider/model and cost panels are showing fleet-wide readings.
            </div>
          )}
          <PoolAccountHealthPanel
            accounts={accountHealth}
            loading={loadingHealth && accountHealth === null && !pollError}
            servedStale={servedStale || pollError}
            generatedAt={generatedAt}
          />
          {!loadingHealth && !pollError && accountHealth && accountHealth.length === 0 && (
            <div className="workbench-layer rounded-md border px-3 py-4 text-center text-xs text-neutral-500">
              No provider account health rows are available yet.
            </div>
          )}
          <ProviderModelBreakdownPanel selectedProjectId={selectedProjectId} accounts={accountHealth} />
          <ProviderCostPanel selectedProjectId={selectedProjectId} windowHours={windowHours} />
        </section>
      </div>
    </div>
  );
}
