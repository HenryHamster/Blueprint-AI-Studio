import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react';
import { AlertTriangle, ChevronDown, ChevronUp, FileDown, GitBranch, HelpCircle, Loader2, Pencil, Plus, RefreshCw, Sliders, Trash2, X } from 'lucide-react';
import { parseTimestamp, timeAgo } from '../utils/time';
import type { AccountMutationResult, AccountMutationReturn, AccountUpdatePayload, AuthMode, MaxPlanAccount, ProviderCatalogEntry, ProviderName } from '../types/settings';
import { DEFAULT_POLL_MS, usePoller } from '../hooks/usePoller';

const PROVIDER_ORDER: ProviderName[] = ['anthropic', 'zai', 'deepseek', 'openai', 'gemini'];

// Availability snapshot keys the BACKEND owns on provider_metadata. The edit
// dialog never lets the operator touch these directly; when an operator edits
// other metadata we merge their changes OVER the full stored map so these
// survive the wholesale provider_metadata replace the PATCH endpoint performs.
const BACKEND_OWNED_METADATA_KEYS = new Set([
  'available_models',
  'available_models_refreshed_at',
  'available_models_error',
]);

function parseCapInput(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const n = parseInt(trimmed, 10);
  return Number.isFinite(n) && n > 0 ? n : null;
}

const FALLBACK_AUTH_MODES: Record<ProviderName, AuthMode[]> = {
  anthropic: ['oauth', 'api_key'],
  openai: ['api_key', 'oauth'],
  gemini: ['api_key', 'service_account'],
  zai: ['api_key'],
  deepseek: ['api_key'],
};

const METADATA_HINTS: Record<string, { label: string; placeholder: string }> = {
  project: { label: 'GCP project ID', placeholder: 'my-gcp-project' },
  location: { label: 'Vertex region (e.g. us-central1)', placeholder: 'us-central1' },
  organization: { label: 'OpenAI organization ID (optional)', placeholder: 'org-xxxxxxxx' },
  region: { label: 'Region', placeholder: 'us-east-1' },
};

type CredentialGuide = { summary: string; steps: string[] };

const CREDENTIAL_GUIDES: Record<string, CredentialGuide> = {
  'anthropic:oauth': {
    summary: 'Claude Max / Pro subscription token.',
    steps: [
      'Install the Claude Code CLI if you have not already.',
      'Run `claude setup-token` in your terminal and follow the browser prompt.',
      'Copy the printed token (starts with `sk-ant-oat01-`) and paste it here.',
    ],
  },
  'anthropic:api_key': {
    summary: 'Pay-as-you-go API billing.',
    steps: [
      'Go to https://console.anthropic.com/settings/keys',
      'Click "Create Key" and copy the secret (starts with `sk-ant-`).',
    ],
  },
  'openai:oauth': {
    summary: 'ChatGPT Plus/Pro subscription via Codex CLI.',
    steps: [
      'Install the codex CLI and run `codex login` (or use ChatGPT Desktop -> Sign in).',
      'It writes `~/.codex/auth.json` (or `~/.config/openai/auth.json`).',
      'Upload that JSON file below, or paste its contents.',
    ],
  },
  'openai:api_key': {
    summary: 'Pay-as-you-go API billing.',
    steps: [
      'Go to https://platform.openai.com/api-keys',
      'Click "Create new secret key" and copy it (starts with `sk-`).',
    ],
  },
  'gemini:api_key': {
    summary: 'Google AI Studio key - quick to set up.',
    steps: [
      'Go to https://aistudio.google.com/apikey',
      'Click "Create API key" and copy the value (starts with `AIza`).',
    ],
  },
  'gemini:service_account': {
    summary: 'GCP service account for Vertex AI (production-grade).',
    steps: [
      'In GCP Console -> IAM & Admin -> Service Accounts, create a service account.',
      'Grant it the `Vertex AI User` role (and `Service Account Token Creator` on itself if you use impersonation).',
      'Add a JSON key and download it.',
      'Upload the JSON below, then fill in the `project` (GCP project ID) and `location` (Vertex region) metadata fields.',
    ],
  },
  'zai:api_key': {
    summary: 'Z.AI GLM through the Claude-compatible endpoint.',
    steps: [
      'Go to https://docs.z.ai and create an API key for your Z.AI account.',
      'Paste the API key here. Blueprint maps it to Claude Code via `ANTHROPIC_BASE_URL` automatically.',
    ],
  },
  'deepseek:api_key': {
    summary: 'DeepSeek through the Claude-compatible endpoint.',
    steps: [
      'Go to https://platform.deepseek.com and create an API key for your DeepSeek account.',
      'Paste the API key here. Blueprint maps it to Claude Code via `ANTHROPIC_BASE_URL` automatically.',
    ],
  },
};

const GEMINI_PREVIEW_MODEL_IDS = new Set<string>([
  'gemini-3.1-pro-preview',
  'gemini-3-flash-preview',
  'gemini-3.1-flash-lite-preview',
]);

let clockNow = Date.now();
const clockSubscribers = new Set<() => void>();
let clockTimer: ReturnType<typeof setInterval> | null = null;

function ensureClockTimer() {
  if (clockTimer !== null) return;
  clockTimer = setInterval(() => {
    clockNow = Date.now();
    clockSubscribers.forEach(cb => cb());
  }, 60000);
}

function subscribeClock(cb: () => void) {
  clockSubscribers.add(cb);
  ensureClockTimer();
  return () => {
    clockSubscribers.delete(cb);
    if (clockSubscribers.size === 0 && clockTimer !== null) {
      clearInterval(clockTimer);
      clockTimer = null;
    }
  };
}

function useClockNow() {
  return useSyncExternalStore(subscribeClock, () => clockNow, () => clockNow);
}

interface SettingsAccountsSectionProps {
  githubAccount: string | null;
  onConnectGitHub: () => void;
  maxPlanAccounts: MaxPlanAccount[];
  onToggleAccount: (id: string, enabled: boolean) => void;
  onRemoveAccount: (id: string, force?: boolean) => AccountMutationReturn;
  onResetRateLimits: (id: string) => void;
  onRefreshAccountModels: (id: string) => void;
  onRefreshAccounts?: () => void | Promise<void>;
  onUpdateAccount: (id: string, payload: AccountUpdatePayload) => AccountMutationReturn;
  onUpdateAccountCaps: (id: string, caps: { daily_task_cap?: number | null; max_concurrent_tasks?: number | null; auto_disable_after_rate_limits?: number | null }) => void;
  providers: Record<string, ProviderCatalogEntry>;
  newAccountName: string;
  setNewAccountName: (value: string) => void;
  newAccountProvider: ProviderName;
  setNewAccountProvider: (value: ProviderName) => void;
  newAccountAuthMode: AuthMode;
  setNewAccountAuthMode: (value: AuthMode) => void;
  newAccountToken: string;
  setNewAccountToken: (value: string) => void;
  newAccountProviderMetadata: Record<string, string>;
  setNewAccountProviderMetadata: (value: Record<string, string>) => void;
  newAccountClaudeConfigJson: string;
  setNewAccountClaudeConfigJson: (value: string) => void;
  newAccountDailyTaskCap: number | null;
  setNewAccountDailyTaskCap: (value: number | null) => void;
  newAccountMaxConcurrentTasks: number | null;
  setNewAccountMaxConcurrentTasks: (value: number | null) => void;
  newAccountAutoDisableAfterRateLimits: number | null;
  setNewAccountAutoDisableAfterRateLimits: (value: number | null) => void;
  onAddAccount: () => void;
}

function usageUtilizationClass(utilization: number | null | undefined): string {
  if (utilization == null) return 'text-neutral-500';
  if (utilization >= 80) return 'text-amber-400';
  if (utilization >= 60) return 'text-yellow-500';
  return 'text-emerald-500';
}

function accountSupportsAvailabilityCheck(account: MaxPlanAccount): boolean {
  if (account.auth_mode !== 'api_key') return false;
  return account.provider === 'gemini' || account.provider === 'openai';
}

function AccountAvailabilityLine({ account, onRefresh }: { account: MaxPlanAccount; onRefresh: () => void }) {
  const metadata = account.provider_metadata || {};
  const models = Array.isArray(metadata.available_models) ? metadata.available_models : null;
  const refreshedAtRaw = typeof metadata.available_models_refreshed_at === 'string'
    ? metadata.available_models_refreshed_at
    : null;
  const error = typeof metadata.available_models_error === 'string'
    ? metadata.available_models_error
    : null;

  const refreshedAt = refreshedAtRaw ? parseTimestamp(refreshedAtRaw) : null;
  const refreshedAgo = refreshedAt ? timeAgo(refreshedAt) : null;

  let body: ReactNode;
  if (models === null && !error) {
    body = <span className="text-neutral-600 italic">availability unchecked</span>;
  } else if (models === null && error) {
    body = <span className="text-amber-500/70" title={error}>check failed &mdash; {error.length > 60 ? `${error.slice(0, 60)}...` : error}</span>;
  } else if (models !== null) {
    const isGemini = account.provider === 'gemini';
    const preview = isGemini ? models.filter(m => GEMINI_PREVIEW_MODEL_IDS.has(m)) : [];
    const modelTitle = models.length > 0 ? models.join(', ') : '(none)';
    body = (
      <span title={modelTitle}>
        <span className="text-neutral-400">{models.length} model{models.length === 1 ? '' : 's'} visible</span>
        {isGemini && preview.length > 0 && (
          <span className="ml-1.5 text-violet-400" title={`Preview-gated models visible on this key: ${preview.join(', ')}`}>
            &middot; {preview.length} preview model{preview.length === 1 ? '' : 's'}
          </span>
        )}
        {isGemini && models.length > 0 && preview.length === 0 && (
          <span className="ml-1.5 text-neutral-600" title="No preview models visible - preview requests will fall back to GA counterparts">
            &middot; no preview access
          </span>
        )}
        {refreshedAgo && <span className="ml-1.5 text-neutral-600">&middot; {refreshedAgo}</span>}
      </span>
    );
  }

  return (
    <div className="text-ui-md font-mono flex items-center gap-1.5 mt-0.5">
      {body}
      <button
        type="button"
        onClick={onRefresh}
        title="Re-check which models this key can see"
        data-viewport-allow-small-target
        className="text-neutral-600 hover:text-neutral-300 transition-colors p-0.5 rounded shrink-0"
      >
        <RefreshCw className="w-3 h-3" />
      </button>
    </div>
  );
}

function AccountRow({
  account,
  isCapExpanded,
  capDraft,
  providers,
  onToggleAccount,
  onRemoveAccount,
  onResetRateLimits,
  onRefreshAccountModels,
  onUpdateAccount,
  onUpdateAccountCaps,
  setExpandedAccountCaps,
  setCapDraft,
}: {
  account: MaxPlanAccount;
  isCapExpanded: boolean;
  capDraft: { daily_task_cap: string; max_concurrent_tasks: string; auto_disable_after_rate_limits: string };
  providers: Record<string, ProviderCatalogEntry>;
  onToggleAccount: (id: string, enabled: boolean) => void;
  onRemoveAccount: (id: string, force?: boolean) => AccountMutationReturn;
  onResetRateLimits: (id: string) => void;
  onRefreshAccountModels: (id: string) => void;
  onUpdateAccount: (id: string, payload: AccountUpdatePayload) => AccountMutationReturn;
  onUpdateAccountCaps: (id: string, caps: { daily_task_cap?: number | null; max_concurrent_tasks?: number | null; auto_disable_after_rate_limits?: number | null }) => void;
  setExpandedAccountCaps: (value: string | null) => void;
  setCapDraft: (value: { daily_task_cap: string; max_concurrent_tasks: string; auto_disable_after_rate_limits: string }) => void;
}) {
  const now = useClockNow();

  // Per-row, per-action pending state: the label of the in-flight mutation
  // (e.g. 'toggle', 'delete', 'refresh') or null when idle. Drives spinners and
  // disables every other action on the row so an operator can't double-fire.
  const [busy, setBusy] = useState<string | null>(null);
  const [isEditing, setIsEditing] = useState(false);
  // Delete confirmation: `confirming` shows the inline confirm; `conflictDetail`
  // is set after a 409 so the operator can choose a force retry.
  const [deleteState, setDeleteState] = useState<{ confirming: boolean; conflictDetail: string | null }>({
    confirming: false,
    conflictDetail: null,
  });

  // Wrap a void mutation so the row reflects a pending state and re-enables on
  // settle. Used for toggle/reset/refresh where the result is not inspected.
  const run = async (label: string, fn: () => unknown): Promise<void> => {
    setBusy(label);
    try {
      await fn();
    } finally {
      setBusy(null);
    }
  };

  const handleConfirmDelete = async (force: boolean) => {
    setBusy('delete');
    try {
      const result = (await onRemoveAccount(account.id, force)) as AccountMutationResult | undefined;
      if (result && result.conflict) {
        setDeleteState({ confirming: true, conflictDetail: result.detail || 'Account is referenced by active work.' });
        return;
      }
      // Success, or a non-conflict failure already surfaced via toast: close the
      // confirm affordance either way (the row disappears on success).
      setDeleteState({ confirming: false, conflictDetail: null });
    } finally {
      setBusy(null);
    }
  };

  const handleSave = async (payload: AccountUpdatePayload) => {
    setBusy('save');
    try {
      const result = (await onUpdateAccount(account.id, payload)) as AccountMutationResult | undefined;
      // Close on success or on a void callback (tests); keep open so the
      // operator can correct input when the backend rejected the patch.
      if (!result || result.ok) setIsEditing(false);
    } finally {
      setBusy(null);
    }
  };

  const isBusy = busy !== null;

  let tokenAge: string | null = null;
  let isExpiring = false;
  if (account.token_created_at) {
    const created = parseTimestamp(account.token_created_at);
    const diffMs = new Date().getTime() - created.getTime();
    const days = Math.floor(diffMs / 86400000);
    isExpiring = days > 300;
    if (days < 1) tokenAge = 'today';
    else if (days < 30) tokenAge = `${days}d old`;
    else tokenAge = `${Math.floor(days / 30)}mo old`;
  }

  const disabledModels = Object.entries(account.disabled_models || {});
  const isRateLimited = disabledModels.length > 0;
  const dailyCount = account.daily_task_count ?? 0;
  const dailyCap = account.daily_task_cap ?? null;
  const concurrentCap = account.max_concurrent_tasks ?? null;
  const autoDisableAfter = account.auto_disable_after_rate_limits ?? null;

  return (
    <div className={`bg-neutral-900 rounded-lg p-3 border ${isRateLimited ? 'border-amber-900/60' : account.disabled_reason ? 'border-red-900/60' : 'border-neutral-800'}`}>
      <div className="flex items-center gap-3">
        <button
          onClick={() => run('toggle', () => onToggleAccount(account.id, !account.enabled))}
          disabled={isBusy}
          data-viewport-allow-small-target
          className={`w-2.5 h-2.5 rounded-full shrink-0 disabled:opacity-50 ${busy === 'toggle' ? 'animate-pulse ' : ''}${isRateLimited ? 'bg-amber-500' : account.enabled ? 'bg-emerald-500' : 'bg-neutral-600'}`}
          title={isRateLimited ? 'Rate-limited' : account.enabled ? 'Enabled (click to disable)' : 'Disabled (click to enable)'}
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 min-w-0 flex-wrap">
            <div className="text-sm text-neutral-200 truncate">{account.name}</div>
            <span className="text-ui-sm uppercase tracking-wide px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 border border-neutral-700">{account.provider}</span>
            <span className="text-ui-sm uppercase tracking-wide px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-500 border border-neutral-700">{account.auth_mode}</span>
            {(dailyCap !== null || concurrentCap !== null || autoDisableAfter !== null) && (
              <span className="text-ui-sm px-1.5 py-0.5 rounded bg-cyan-950/40 text-cyan-400 border border-cyan-900/50" title="Usage caps configured">
                {dailyCap !== null && <>{dailyCount}/{dailyCap} today</>}
                {dailyCap !== null && concurrentCap !== null && <> &middot; </>}
                {concurrentCap !== null && <>max {concurrentCap}</>}
              </span>
            )}
          </div>
          <div className="text-ui-md text-neutral-500 font-mono">
            {account.active_tasks} active
            {tokenAge && <span className={`ml-2 ${isExpiring ? 'text-amber-400' : ''}`}>&middot; token {tokenAge}</span>}
            {account.provider_metadata?.project && typeof account.provider_metadata.project === 'string' && <span className="ml-2">&middot; {account.provider_metadata.project}</span>}
            {account.provider_metadata?.location && typeof account.provider_metadata.location === 'string' && <span className="ml-2">@ {account.provider_metadata.location}</span>}
            {account.usage_snapshot?.known ? (
              <span
                className={`ml-2 ${account.usage_snapshot.stale ? 'opacity-60' : ''}`}
                title={`5h: ${account.usage_snapshot.five_hour_utilization ?? '?'}% · 7d: ${account.usage_snapshot.seven_day_utilization ?? '?'}% plan usage${account.usage_snapshot.probed_at ? ` · probed ${account.usage_snapshot.probed_at}` : ''}${account.usage_snapshot.stale ? ' · STALE' : ''}`}
              >
                &middot; 5h&nbsp;
                <span className={usageUtilizationClass(account.usage_snapshot.five_hour_utilization)}>
                  {account.usage_snapshot.five_hour_utilization == null ? '?' : `${Math.round(account.usage_snapshot.five_hour_utilization)}%`}
                </span>
                &nbsp;7d&nbsp;
                <span className={usageUtilizationClass(account.usage_snapshot.seven_day_utilization)}>
                  {account.usage_snapshot.seven_day_utilization == null ? '?' : `${Math.round(account.usage_snapshot.seven_day_utilization)}%`}
                </span>
                {account.usage_snapshot.stale && <span className="text-neutral-500"> (stale)</span>}
                {account.usage_snapshot.last_attempt_failure_kind && (
                  <span className="text-amber-400"> (latest probe {account.usage_snapshot.last_attempt_failure_kind})</span>
                )}
              </span>
            ) : account.usage_snapshot ? (
              <span
                className={`ml-2 ${account.usage_snapshot.dark_reason === 'unsupported' ? 'text-neutral-500' : 'text-amber-400'}`}
                title={`Usage probe: ${account.usage_snapshot.last_attempt_failure_kind ?? account.usage_snapshot.dark_reason ?? 'unknown'}${account.usage_snapshot.last_attempt_at ? ` · last attempted ${account.usage_snapshot.last_attempt_at}` : ''}`}
              >
                &middot; usage {account.usage_snapshot.last_attempt_failure_kind ?? account.usage_snapshot.dark_reason ?? 'unknown'}
              </span>
            ) : null}
          </div>
          {accountSupportsAvailabilityCheck(account) && (
            <AccountAvailabilityLine
              account={account}
              onRefresh={() => run('refresh', () => onRefreshAccountModels(account.id))}
            />
          )}
        </div>
        <button
          onClick={() => {
            if (isCapExpanded) {
              setExpandedAccountCaps(null);
              return;
            }
            setExpandedAccountCaps(account.id);
            setCapDraft({
              daily_task_cap: dailyCap !== null ? String(dailyCap) : '',
              max_concurrent_tasks: concurrentCap !== null ? String(concurrentCap) : '',
              auto_disable_after_rate_limits: autoDisableAfter !== null ? String(autoDisableAfter) : '',
            });
          }}
          title={isCapExpanded ? 'Close caps editor' : 'Edit usage caps'}
          disabled={isBusy}
          data-viewport-allow-small-target
          className="text-neutral-600 hover:text-neutral-300 disabled:opacity-40 transition-colors shrink-0 p-1"
        >
          <Sliders className="w-3.5 h-3.5" />
        </button>
        <button
          onClick={() => { setIsEditing(v => !v); setExpandedAccountCaps(null); }}
          title={isEditing ? 'Close editor' : 'Edit account'}
          aria-label={`Edit account ${account.name}`}
          disabled={isBusy}
          data-viewport-allow-small-target
          className={`hover:text-neutral-200 disabled:opacity-40 transition-colors shrink-0 p-1 ${isEditing ? 'text-cyan-400' : 'text-neutral-600'}`}
        >
          <Pencil className="w-3.5 h-3.5" />
        </button>
        <button
          onClick={() => setDeleteState({ confirming: true, conflictDetail: null })}
          title="Delete account"
          aria-label={`Delete account ${account.name}`}
          disabled={isBusy}
          data-viewport-allow-small-target
          className="text-neutral-600 hover:text-red-400 disabled:opacity-40 transition-colors shrink-0 p-1"
        >
          {busy === 'delete' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Trash2 className="w-3.5 h-3.5" />}
        </button>
      </div>
      {deleteState.confirming && (
        <div className="mt-2 pt-2 border-t border-red-900/40 space-y-1.5">
          {deleteState.conflictDetail ? (
            <div className="text-ui-md text-amber-400 flex items-start gap-1.5">
              <AlertTriangle className="w-3.5 h-3.5 shrink-0 mt-px" />
              <span>{deleteState.conflictDetail}</span>
            </div>
          ) : (
            <div className="text-ui-md text-neutral-400">Delete <span className="text-neutral-200">{account.name}</span>? This revokes its stored secret.</div>
          )}
          <div className="flex items-center gap-2">
            <button
              onClick={() => handleConfirmDelete(Boolean(deleteState.conflictDetail))}
              disabled={isBusy}
              className="bg-red-950/50 hover:bg-red-900/50 disabled:opacity-40 text-red-300 text-ui-md py-1 px-3 rounded border border-red-900/60 inline-flex items-center gap-1.5"
            >
              {busy === 'delete' && <Loader2 className="w-3 h-3 animate-spin" />}
              {deleteState.conflictDetail ? 'Force delete' : 'Delete'}
            </button>
            <button
              onClick={() => setDeleteState({ confirming: false, conflictDetail: null })}
              disabled={isBusy}
              className="text-ui-md text-neutral-500 hover:text-neutral-300 disabled:opacity-40"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      {isEditing && (
        <AccountEditForm
          account={account}
          providers={providers}
          busy={busy === 'save'}
          onCancel={() => setIsEditing(false)}
          onSave={handleSave}
        />
      )}
      {account.disabled_reason && (
        <div className="mt-2 pt-2 border-t border-red-900/40 text-ui-md text-red-400 flex items-center justify-between gap-2">
          <span>&#9888; {account.disabled_reason}</span>
          <button data-viewport-allow-small-target disabled={isBusy} onClick={() => run('toggle', () => onToggleAccount(account.id, true))} className="text-red-300 hover:text-red-200 disabled:opacity-40 underline">Re-enable</button>
        </div>
      )}
      {isRateLimited && (
        <div className="mt-2 pt-2 border-t border-neutral-800/50">
          {disabledModels.map(([model, info]) => {
            const cooldownMin = Math.min(30 * Math.pow(2, (info.hits - 1)), 240);
            const disabledAt = info.at ? parseTimestamp(info.at) : null;
            const recoveryAt = disabledAt ? new Date(disabledAt.getTime() + cooldownMin * 60000) : null;
            const remaining = recoveryAt ? Math.max(0, Math.round((recoveryAt.getTime() - now) / 60000)) : null;
            const shortModel = model.replace('claude-', '').replace('-4-6', '');
            return (
              <div key={model} className="flex items-center justify-between text-ui-md text-amber-400/80">
                <span className="font-mono">{shortModel} <span className="text-amber-500/60">hit #{info.hits}</span></span>
                <span className="text-amber-500/60">{remaining !== null && remaining > 0 ? `${remaining}m left` : 'recovering'}</span>
              </div>
            );
          })}
          <button
            onClick={() => run('reset', () => onResetRateLimits(account.id))}
            disabled={isBusy}
            title="Clear Blueprint's local cooldown only; this does not reset provider quota or consume a Codex reset credit"
            data-viewport-allow-small-target
            className="mt-1.5 w-full text-ui-md text-amber-400/70 hover:text-amber-300 disabled:opacity-40 bg-amber-950/30 hover:bg-amber-950/50 rounded-lg px-2 py-1 transition-colors"
          >
            {busy === 'reset' ? 'Clearing…' : 'Clear Blueprint cooldowns'}
          </button>
        </div>
      )}
      {isCapExpanded && (
        <div className="mt-2 pt-2 border-t border-neutral-800/50 space-y-2">
          <div className="text-ui-sm uppercase tracking-wider text-neutral-500">Usage caps</div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
            <label className="block">
              <div className="text-ui-sm text-neutral-500 mb-1">Tasks/day</div>
              <input
                type="number"
                min={1}
                value={capDraft.daily_task_cap}
                placeholder="unlimited"
                onChange={e => setCapDraft({ ...capDraft, daily_task_cap: e.target.value })}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </label>
            <label className="block">
              <div className="text-ui-sm text-neutral-500 mb-1">Max concurrent</div>
              <input
                type="number"
                min={1}
                value={capDraft.max_concurrent_tasks}
                placeholder="unlimited"
                onChange={e => setCapDraft({ ...capDraft, max_concurrent_tasks: e.target.value })}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </label>
            <label className="block">
              <div className="text-ui-sm text-neutral-500 mb-1">Auto-disable after N RL</div>
              <input
                type="number"
                min={1}
                value={capDraft.auto_disable_after_rate_limits}
                placeholder="off"
                onChange={e => setCapDraft({ ...capDraft, auto_disable_after_rate_limits: e.target.value })}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </label>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => {
                const parse = (v: string): number | null => {
                  const trimmed = v.trim();
                  if (!trimmed) return null;
                  const n = parseInt(trimmed, 10);
                  return Number.isFinite(n) && n > 0 ? n : null;
                };
                onUpdateAccountCaps(account.id, {
                  daily_task_cap: parse(capDraft.daily_task_cap),
                  max_concurrent_tasks: parse(capDraft.max_concurrent_tasks),
                  auto_disable_after_rate_limits: parse(capDraft.auto_disable_after_rate_limits),
                });
                setExpandedAccountCaps(null);
              }}
              className="bg-neutral-800 hover:bg-neutral-700 text-neutral-200 text-ui-md py-1 px-3 rounded border border-neutral-700"
            >
              Save caps
            </button>
            <button
              onClick={() => setExpandedAccountCaps(null)}
              className="text-ui-md text-neutral-500 hover:text-neutral-300"
            >
              Cancel
            </button>
            <span className="text-ui-sm text-neutral-600 ml-auto">Leave a field empty for unlimited/off.</span>
          </div>
        </div>
      )}
    </div>
  );
}

// In-place editor for an existing account. Token/secret entry is strictly
// write-only — the field is never seeded from backend data, so a stored secret
// is never echoed. The PATCH body carries only the fields the operator actually
// changed (diffed against the current row), and provider_metadata edits are
// merged over the full stored map so backend-owned availability keys survive.
function AccountEditForm({
  account,
  providers,
  busy,
  onCancel,
  onSave,
}: {
  account: MaxPlanAccount;
  providers: Record<string, ProviderCatalogEntry>;
  busy: boolean;
  onCancel: () => void;
  onSave: (payload: AccountUpdatePayload) => void;
}) {
  const [name, setName] = useState(account.name);
  const [enabled, setEnabled] = useState(account.enabled);
  const [provider, setProvider] = useState<ProviderName>(account.provider);
  const [authMode, setAuthMode] = useState<AuthMode>(account.auth_mode);
  const [token, setToken] = useState('');
  const [dailyTaskCap, setDailyTaskCap] = useState(account.daily_task_cap != null ? String(account.daily_task_cap) : '');
  const [maxConcurrent, setMaxConcurrent] = useState(account.max_concurrent_tasks != null ? String(account.max_concurrent_tasks) : '');
  const [autoDisable, setAutoDisable] = useState(account.auto_disable_after_rate_limits != null ? String(account.auto_disable_after_rate_limits) : '');
  const [metaDraft, setMetaDraft] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    for (const [k, v] of Object.entries(account.provider_metadata || {})) {
      if (!BACKEND_OWNED_METADATA_KEYS.has(k) && typeof v === 'string') initial[k] = v;
    }
    return initial;
  });
  const [metaDirty, setMetaDirty] = useState(false);
  const [claudeConfigJson, setClaudeConfigJson] = useState('');
  const [claudeConfigDirty, setClaudeConfigDirty] = useState(false);
  const [clearClaudeConfig, setClearClaudeConfig] = useState(false);

  const providerNames = Object.keys(providers).length > 0 ? (Object.keys(providers) as ProviderName[]) : PROVIDER_ORDER;
  const authModes = (Object.keys(providers[provider]?.auth_modes ?? {}) as AuthMode[]).length > 0
    ? (Object.keys(providers[provider]?.auth_modes ?? {}) as AuthMode[])
    : FALLBACK_AUTH_MODES[provider];
  const requiredFields = providers[provider]?.auth_modes?.[authMode]?.required_metadata_fields ?? [];
  const metaFieldKeys = Array.from(new Set([...requiredFields, ...Object.keys(metaDraft)]));

  const providerChanged = provider !== account.provider;
  const authChanged = authMode !== account.auth_mode;
  const identityChanged = providerChanged || authChanged;
  const targetIsAnthropicOauth = provider === 'anthropic' && authMode === 'oauth';
  const isJsonSecret = (provider === 'openai' && authMode === 'oauth') || (provider === 'gemini' && authMode === 'service_account');

  const nameValid = name.trim().length > 0;
  const missingMeta = metaFieldKeys.filter(f => (requiredFields.includes(f)) && !(metaDraft[f] ?? '').trim());
  const tokenMissing = identityChanged && !token.trim();
  const canSave = nameValid && !tokenMissing && (!identityChanged || missingMeta.length === 0) && !busy;

  const buildPayload = (): AccountUpdatePayload => {
    const payload: AccountUpdatePayload = {};
    const trimmedName = name.trim();
    if (trimmedName && trimmedName !== account.name) payload.name = trimmedName;
    if (enabled !== account.enabled) payload.enabled = enabled;
    if (providerChanged) payload.provider = provider;
    if (authChanged) payload.auth_mode = authMode;
    if (token.trim()) payload.secret = token.trim();

    const newDaily = parseCapInput(dailyTaskCap);
    if (newDaily !== (account.daily_task_cap ?? null)) payload.daily_task_cap = newDaily;
    const newConcurrent = parseCapInput(maxConcurrent);
    if (newConcurrent !== (account.max_concurrent_tasks ?? null)) payload.max_concurrent_tasks = newConcurrent;
    const newAutoDisable = parseCapInput(autoDisable);
    if (newAutoDisable !== (account.auto_disable_after_rate_limits ?? null)) payload.auto_disable_after_rate_limits = newAutoDisable;

    if (metaDirty || identityChanged) {
      // Keys the operator cleared in the form. We rebuild the map without them
      // rather than `delete`-ing, and copy every other stored key forward — so
      // backend-owned availability keys (not editable here) always survive.
      const cleared = new Set(metaFieldKeys.filter(key => !(metaDraft[key] ?? '').trim()));
      const merged: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(account.provider_metadata || {})) {
        if (!cleared.has(k)) merged[k] = v;
      }
      for (const key of metaFieldKeys) {
        const val = (metaDraft[key] ?? '').trim();
        if (val) merged[key] = val;
      }
      payload.provider_metadata = merged;
    }

    if (targetIsAnthropicOauth) {
      if (clearClaudeConfig) payload.claude_config_json = null;
      else if (claudeConfigDirty && claudeConfigJson.trim()) payload.claude_config_json = claudeConfigJson;
    }
    return payload;
  };

  const inputCls = 'w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600';

  return (
    <div className="mt-2 pt-3 border-t border-neutral-800/60 bg-neutral-950/40 rounded-lg p-3 space-y-3">
      <div className="text-ui-sm font-semibold text-neutral-300 uppercase tracking-wider">Edit account</div>
      <div>
        <div className="text-ui-sm text-neutral-500 mb-1 uppercase">Name</div>
        <input aria-label="Account name" value={name} onChange={e => setName(e.target.value)} className={inputCls} />
      </div>
      <label className="flex items-center gap-2 text-xs text-neutral-300 cursor-pointer">
        <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} className="accent-emerald-500" />
        Enabled
      </label>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        <div>
          <div className="text-ui-sm text-neutral-500 mb-1 uppercase">Provider</div>
          <select
            aria-label="Provider"
            value={provider}
            onChange={e => {
              const next = e.target.value as ProviderName;
              setProvider(next);
              const modes = (Object.keys(providers[next]?.auth_modes ?? {}) as AuthMode[]);
              const fallback = modes.length ? modes : FALLBACK_AUTH_MODES[next];
              if (!fallback.includes(authMode)) setAuthMode(fallback[0]);
            }}
            className={inputCls}
          >
            {providerNames.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        </div>
        <div>
          <div className="text-ui-sm text-neutral-500 mb-1 uppercase">Auth mode</div>
          <select aria-label="Auth mode" value={authMode} onChange={e => setAuthMode(e.target.value as AuthMode)} className={inputCls}>
            {authModes.map(m => <option key={m} value={m}>{m}</option>)}
          </select>
        </div>
      </div>
      {metaFieldKeys.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {metaFieldKeys.map(field => {
            const hint = METADATA_HINTS[field];
            return (
              <div key={field}>
                <div className="text-ui-sm text-neutral-500 mb-1 uppercase">{field}{requiredFields.includes(field) && <span className="text-red-400"> *</span>}</div>
                <input
                  aria-label={`metadata ${field}`}
                  value={metaDraft[field] ?? ''}
                  onChange={e => { setMetaDraft({ ...metaDraft, [field]: e.target.value }); setMetaDirty(true); }}
                  placeholder={hint?.placeholder ?? field}
                  className={inputCls}
                />
              </div>
            );
          })}
        </div>
      )}
      <div>
        <div className="text-ui-sm text-neutral-500 mb-1 uppercase flex items-center gap-1">
          New token / secret
          {identityChanged && <span className="text-red-400 normal-case">(required — provider/auth changed)</span>}
        </div>
        {isJsonSecret ? (
          <textarea
            aria-label="New token"
            value={token}
            onChange={e => setToken(e.target.value)}
            rows={3}
            placeholder="Leave blank to keep current secret"
            className={`${inputCls} font-mono resize-none`}
          />
        ) : (
          <input
            aria-label="New token"
            value={token}
            onChange={e => setToken(e.target.value)}
            type="password"
            autoComplete="new-password"
            placeholder="Leave blank to keep current token"
            className={`${inputCls} font-mono`}
          />
        )}
      </div>
      {targetIsAnthropicOauth && (
        <div>
          <div className="text-ui-sm text-neutral-500 mb-1 uppercase">claude_config_json <span className="normal-case text-neutral-600">(optional)</span></div>
          <textarea
            aria-label="claude_config_json"
            value={claudeConfigJson}
            disabled={clearClaudeConfig}
            onChange={e => { setClaudeConfigJson(e.target.value); setClaudeConfigDirty(true); }}
            rows={2}
            placeholder='Leave blank to keep current; paste JSON to replace'
            className={`${inputCls} font-mono resize-none disabled:opacity-40`}
          />
          <label className="flex items-center gap-2 text-ui-md text-neutral-500 mt-1 cursor-pointer">
            <input type="checkbox" checked={clearClaudeConfig} onChange={e => setClearClaudeConfig(e.target.checked)} className="accent-red-500" />
            Clear stored claude_config_json
          </label>
        </div>
      )}
      <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
        <label className="block">
          <div className="text-ui-sm text-neutral-500 mb-1">Tasks/day</div>
          <input aria-label="Tasks per day cap" type="number" min={1} value={dailyTaskCap} placeholder="unlimited" onChange={e => setDailyTaskCap(e.target.value)} className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600" />
        </label>
        <label className="block">
          <div className="text-ui-sm text-neutral-500 mb-1">Max concurrent</div>
          <input aria-label="Max concurrent cap" type="number" min={1} value={maxConcurrent} placeholder="unlimited" onChange={e => setMaxConcurrent(e.target.value)} className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600" />
        </label>
        <label className="block">
          <div className="text-ui-sm text-neutral-500 mb-1">Auto-disable</div>
          <input aria-label="Auto-disable cap" type="number" min={1} value={autoDisable} placeholder="off" onChange={e => setAutoDisable(e.target.value)} className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600" />
        </label>
      </div>
      <div className="flex items-center gap-2 pt-1">
        <button
          onClick={() => { if (canSave) onSave(buildPayload()); }}
          disabled={!canSave}
          className="bg-cyan-900/40 hover:bg-cyan-800/50 disabled:opacity-30 disabled:cursor-not-allowed text-cyan-200 text-xs min-h-[44px] py-2 px-4 rounded-lg transition-colors border border-cyan-900/60 inline-flex items-center gap-1.5"
        >
          {busy && <Loader2 className="w-3.5 h-3.5 animate-spin" />}
          Save changes
        </button>
        <button onClick={onCancel} data-viewport-allow-small-target className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors">Cancel</button>
        {tokenMissing && <span className="text-ui-md text-amber-400 ml-auto">New token required</span>}
      </div>
    </div>
  );
}

function renderGuideText(text: string) {
  return text.split(/(`[^`]+`)/g).map((part, idx) => (
    part.startsWith('`') && part.endsWith('`')
      ? <code key={idx} className="text-cyan-300 bg-neutral-900 px-1 rounded">{part.slice(1, -1)}</code>
      : <span key={idx}>{part}</span>
  ));
}

function AccountAddForm({
  providers,
  newAccountProvider,
  setNewAccountProvider,
  newAccountAuthMode,
  setNewAccountAuthMode,
  newAccountName,
  setNewAccountName,
  newAccountToken,
  setNewAccountToken,
  newAccountProviderMetadata,
  setNewAccountProviderMetadata,
  newAccountClaudeConfigJson,
  setNewAccountClaudeConfigJson,
  newAccountDailyTaskCap,
  setNewAccountDailyTaskCap,
  newAccountMaxConcurrentTasks,
  setNewAccountMaxConcurrentTasks,
  newAccountAutoDisableAfterRateLimits,
  setNewAccountAutoDisableAfterRateLimits,
  showAddAccountCaps,
  setShowAddAccountCaps,
  onCancel,
  onAddAccount,
}: {
  providers: Record<string, ProviderCatalogEntry>;
  newAccountProvider: ProviderName;
  setNewAccountProvider: (value: ProviderName) => void;
  newAccountAuthMode: AuthMode;
  setNewAccountAuthMode: (value: AuthMode) => void;
  newAccountName: string;
  setNewAccountName: (value: string) => void;
  newAccountToken: string;
  setNewAccountToken: (value: string) => void;
  newAccountProviderMetadata: Record<string, string>;
  setNewAccountProviderMetadata: (value: Record<string, string>) => void;
  newAccountClaudeConfigJson: string;
  setNewAccountClaudeConfigJson: (value: string) => void;
  newAccountDailyTaskCap: number | null;
  setNewAccountDailyTaskCap: (value: number | null) => void;
  newAccountMaxConcurrentTasks: number | null;
  setNewAccountMaxConcurrentTasks: (value: number | null) => void;
  newAccountAutoDisableAfterRateLimits: number | null;
  setNewAccountAutoDisableAfterRateLimits: (value: number | null) => void;
  showAddAccountCaps: boolean;
  setShowAddAccountCaps: (value: boolean) => void;
  onCancel: () => void;
  onAddAccount: () => void;
}) {
  const providerNames = Object.keys(providers).length > 0 ? (Object.keys(providers) as ProviderName[]) : PROVIDER_ORDER;
  const authModes = (Object.keys(providers[newAccountProvider]?.auth_modes ?? {}) as AuthMode[]).length > 0
    ? Object.keys(providers[newAccountProvider]?.auth_modes ?? {}) as AuthMode[]
    : FALLBACK_AUTH_MODES[newAccountProvider];
  const requiredMetadataFields = providers[newAccountProvider]?.auth_modes?.[newAccountAuthMode]?.required_metadata_fields ?? [];
  const secretLabel =
    newAccountProvider === 'gemini' && newAccountAuthMode === 'service_account'
      ? 'Service account JSON'
      : newAccountProvider === 'openai' && newAccountAuthMode === 'oauth'
      ? 'auth.json'
      : newAccountProvider === 'gemini'
      ? 'Gemini API key'
      : newAccountProvider === 'zai'
      ? 'Z.AI API key'
      : newAccountProvider === 'deepseek'
      ? 'DeepSeek API key'
      : newAccountProvider === 'openai'
      ? 'OpenAI API key'
      : newAccountAuthMode === 'api_key'
      ? 'Anthropic API key'
      : 'OAuth token';
  const guide = CREDENTIAL_GUIDES[`${newAccountProvider}:${newAccountAuthMode}`];
  const missingMeta = requiredMetadataFields.filter(f => !(newAccountProviderMetadata[f] ?? '').trim());
  const missing: string[] = [];
  if (!newAccountName) missing.push('name');
  if (!newAccountToken) missing.push(secretLabel.toLowerCase());
  if (missingMeta.length) missing.push(...missingMeta);
  const disabled = missing.length > 0;

  return (
    <div className="mt-4 bg-neutral-900 border border-neutral-800 rounded-lg p-4 space-y-3">
      <div className="text-xs font-semibold text-neutral-200 uppercase tracking-wider">New account</div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
        <div>
          <div className="text-ui-sm text-neutral-500 mb-1 uppercase">Provider</div>
          <select
            value={newAccountProvider}
            onChange={e => setNewAccountProvider(e.target.value as ProviderName)}
            className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
          >
            {providerNames.map(providerName => (
              <option key={providerName} value={providerName}>{providerName}</option>
            ))}
          </select>
        </div>
        <div>
          <div className="text-ui-sm text-neutral-500 mb-1 uppercase">Auth mode</div>
          <select
            value={newAccountAuthMode}
            onChange={e => setNewAccountAuthMode(e.target.value as AuthMode)}
            className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
          >
            {authModes.map(mode => (
              <option key={mode} value={mode}>{mode}</option>
            ))}
          </select>
        </div>
      </div>
      {guide && (
        <div className="bg-neutral-950 border border-neutral-800 rounded-lg p-3 flex gap-2">
          <HelpCircle className="w-4 h-4 text-cyan-400/80 shrink-0 mt-0.5" />
          <div className="min-w-0 flex-1">
            <div className="text-xs text-neutral-300">{guide.summary}</div>
            <ol className="list-decimal list-inside mt-1.5 space-y-0.5 text-ui-md text-neutral-400">
              {guide.steps.map((step, i) => (
                <li key={i}>{renderGuideText(step)}</li>
              ))}
            </ol>
          </div>
        </div>
      )}
      <div>
        <div className="text-ui-sm text-neutral-500 mb-1 uppercase">Name</div>
        <input
          value={newAccountName}
          onChange={e => setNewAccountName(e.target.value)}
          placeholder="e.g. my-claude-max"
          className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
        />
      </div>
      {requiredMetadataFields.length > 0 && (
        <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
          {requiredMetadataFields.map(field => {
            const hint = METADATA_HINTS[field];
            return (
              <div key={field}>
                <div className="text-ui-sm text-neutral-500 mb-1 uppercase">{field}</div>
                <input
                  value={newAccountProviderMetadata[field] ?? ''}
                  onChange={e => setNewAccountProviderMetadata({ ...newAccountProviderMetadata, [field]: e.target.value })}
                  placeholder={hint?.placeholder ?? field}
                  className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                />
                {hint && <div className="text-ui-sm text-neutral-600 mt-0.5">{hint.label}</div>}
              </div>
            );
          })}
        </div>
      )}
      <div>
        <div className="text-ui-sm text-neutral-500 mb-1 uppercase">{secretLabel}</div>
        {((newAccountProvider === 'openai' && newAccountAuthMode === 'oauth') || (newAccountProvider === 'gemini' && newAccountAuthMode === 'service_account')) ? (
          <div className="space-y-1.5">
            <textarea
              value={newAccountToken}
              onChange={e => setNewAccountToken(e.target.value)}
              rows={4}
              placeholder={`Paste ${secretLabel} or upload a .json file below`}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600 resize-none"
            />
            <label className="flex items-center gap-2 text-ui-md text-neutral-500 hover:text-neutral-300 cursor-pointer transition-colors">
              <span className="inline-flex items-center gap-1 bg-neutral-800 border border-neutral-700 rounded px-2 py-1 hover:bg-neutral-700">
                <FileDown className="w-3 h-3" /> Upload .json file
              </span>
              <input
                type="file"
                accept=".json,application/json"
                className="hidden"
                onChange={e => {
                  const file = e.target.files?.[0];
                  if (!file) return;
                  const reader = new FileReader();
                  reader.onload = ev => {
                    const text = String(ev.target?.result ?? '');
                    setNewAccountToken(text);
                    if (!newAccountName) setNewAccountName(file.name.replace(/\.json$/i, ''));
                  };
                  reader.readAsText(file);
                  e.target.value = '';
                }}
              />
            </label>
          </div>
        ) : (
          <input
            value={newAccountToken}
            onChange={e => setNewAccountToken(e.target.value)}
            placeholder={secretLabel}
            type="password"
            className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600 font-mono"
          />
        )}
      </div>
      {newAccountProvider === 'anthropic' && newAccountAuthMode === 'oauth' && (
        <div>
          <div className="text-ui-sm text-neutral-500 mb-1 uppercase flex items-center gap-1">
            claude_config_json
            <span className="text-neutral-600" title="Optional. Contents of ~/.claude.json - lets the agent skip onboarding screens on first run. Not required for most setups.">
              <HelpCircle className="w-3 h-3" />
            </span>
            <span className="text-ui-sm text-neutral-600 normal-case">(optional)</span>
          </div>
          <textarea
            value={newAccountClaudeConfigJson}
            onChange={e => setNewAccountClaudeConfigJson(e.target.value)}
            rows={2}
            placeholder='{"onboardingAccepted": true}'
            className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600 resize-none"
          />
        </div>
      )}
      <button
        onClick={() => setShowAddAccountCaps(!showAddAccountCaps)}
        data-viewport-allow-small-target
        className="flex items-center gap-1.5 text-ui-md text-neutral-500 hover:text-neutral-300 transition-colors"
      >
        {showAddAccountCaps ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        Usage limits (optional)
      </button>
      {showAddAccountCaps && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2 bg-neutral-950 border border-neutral-800 rounded-lg p-3">
          <label className="block">
            <div className="text-ui-sm text-neutral-500 mb-1 flex items-center gap-1">
              Tasks/day
              <span title="Hard cap on tasks dispatched to this account per rolling 24h window. Empty = unlimited.">
                <HelpCircle className="w-3 h-3 text-neutral-600" />
              </span>
            </div>
            <input
              type="number"
              min={1}
              value={newAccountDailyTaskCap ?? ''}
              onChange={e => setNewAccountDailyTaskCap(e.target.value ? parseInt(e.target.value, 10) : null)}
              placeholder="unlimited"
              className="w-full bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </label>
          <label className="block">
            <div className="text-ui-sm text-neutral-500 mb-1 flex items-center gap-1">
              Max concurrent
              <span title="Maximum number of tasks running on this account at once. Empty = unlimited.">
                <HelpCircle className="w-3 h-3 text-neutral-600" />
              </span>
            </div>
            <input
              type="number"
              min={1}
              value={newAccountMaxConcurrentTasks ?? ''}
              onChange={e => setNewAccountMaxConcurrentTasks(e.target.value ? parseInt(e.target.value, 10) : null)}
              placeholder="unlimited"
              className="w-full bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </label>
          <label className="block">
            <div className="text-ui-sm text-neutral-500 mb-1 flex items-center gap-1">
              Auto-disable
              <span title="Disable the account (needs manual re-enable) after this many consecutive rate-limit hits. Empty = off.">
                <HelpCircle className="w-3 h-3 text-neutral-600" />
              </span>
            </div>
            <input
              type="number"
              min={1}
              value={newAccountAutoDisableAfterRateLimits ?? ''}
              onChange={e => setNewAccountAutoDisableAfterRateLimits(e.target.value ? parseInt(e.target.value, 10) : null)}
              placeholder="off"
              className="w-full bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
            />
          </label>
        </div>
      )}
      <div className="flex items-center gap-2 pt-1">
        <button
          onClick={onAddAccount}
          disabled={disabled}
          title={disabled ? `Missing: ${missing.join(', ')}` : undefined}
          className="bg-emerald-900/40 hover:bg-emerald-800/50 disabled:opacity-30 disabled:cursor-not-allowed text-emerald-300 text-xs min-h-[44px] py-2 px-4 rounded-lg transition-colors border border-emerald-900/60"
        >
          Add Account
        </button>
        <button
          onClick={onCancel}
          data-viewport-allow-small-target
          className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
        >
          Cancel
        </button>
        <span className="text-ui-md text-neutral-600 ml-auto">Missing: {missing.join(', ')}</span>
      </div>
    </div>
  );
}

export function SettingsAccountsSection({
  githubAccount,
  onConnectGitHub,
  maxPlanAccounts,
  onToggleAccount,
  onRemoveAccount,
  onResetRateLimits,
  onRefreshAccountModels,
  onRefreshAccounts,
  onUpdateAccount,
  onUpdateAccountCaps,
  providers,
  newAccountName,
  setNewAccountName,
  newAccountProvider,
  setNewAccountProvider,
  newAccountAuthMode,
  setNewAccountAuthMode,
  newAccountToken,
  setNewAccountToken,
  newAccountProviderMetadata,
  setNewAccountProviderMetadata,
  newAccountClaudeConfigJson,
  setNewAccountClaudeConfigJson,
  newAccountDailyTaskCap,
  setNewAccountDailyTaskCap,
  newAccountMaxConcurrentTasks,
  setNewAccountMaxConcurrentTasks,
  newAccountAutoDisableAfterRateLimits,
  setNewAccountAutoDisableAfterRateLimits,
  onAddAccount,
}: SettingsAccountsSectionProps) {
  const [showAddAccountForm, setShowAddAccountForm] = useState(false);
  const [showAddAccountCaps, setShowAddAccountCaps] = useState(false);
  const [expandedAccountCaps, setExpandedAccountCaps] = useState<string | null>(null);
  const [capDraft, setCapDraft] = useState<{ daily_task_cap: string; max_concurrent_tasks: string; auto_disable_after_rate_limits: string }>({
    daily_task_cap: '',
    max_concurrent_tasks: '',
    auto_disable_after_rate_limits: '',
  });

  // The roster response owns the persisted usage snapshots. Refresh it while
  // this section is mounted so a background probe becoming known is reflected
  // without requiring the operator to leave and reopen Settings.
  usePoller(() => onRefreshAccounts?.(), {
    intervalMs: DEFAULT_POLL_MS,
    enabled: typeof onRefreshAccounts === 'function',
    leading: false,
    deps: [],
  });

  useEffect(() => {
    const availableAuthModes = (Object.keys(providers[newAccountProvider]?.auth_modes ?? {}) as AuthMode[]).length > 0
      ? Object.keys(providers[newAccountProvider]?.auth_modes ?? {}) as AuthMode[]
      : FALLBACK_AUTH_MODES[newAccountProvider];
    if (!availableAuthModes.includes(newAccountAuthMode) && availableAuthModes.length > 0) {
      setNewAccountAuthMode(availableAuthModes[0]);
    }
  }, [newAccountAuthMode, newAccountProvider, providers, setNewAccountAuthMode]);

  return (
    <div className="space-y-6">
      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">GitHub</h3>
        {githubAccount ? (
          <div className="flex items-center gap-2 bg-neutral-900 rounded-lg p-3 border border-neutral-800">
            <span className="w-2 h-2 rounded-full bg-emerald-500 shrink-0" />
            <span className="text-sm text-neutral-200">Connected: <span className="font-mono">{githubAccount}</span></span>
          </div>
        ) : (
          <button
            onClick={onConnectGitHub}
            className="bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-xs min-h-[44px] py-2 px-4 rounded-lg transition-colors border border-neutral-700 flex items-center gap-2"
          >
            <GitBranch className="w-3.5 h-3.5" /> Connect GitHub
          </button>
        )}
      </div>

      <div>
        <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">LLM Accounts</h3>
        {maxPlanAccounts.length > 0 && (() => {
          const total = maxPlanAccounts.length;
          const available = maxPlanAccounts.filter(a => a.enabled && !Object.keys(a.disabled_models || {}).length).length;
          const warn = available < total;
          return (
            <div className={`flex items-center justify-between text-xs mb-3 px-3 py-2 rounded-lg border ${warn ? 'bg-amber-950/30 border-amber-900/50 text-amber-400' : 'bg-neutral-900 border-neutral-800 text-neutral-400'}`}>
              <span>Pool: <span className={warn ? 'text-amber-300 font-medium' : 'text-neutral-200 font-medium'}>{available}/{total}</span> available</span>
              {warn && <span className="text-amber-500 text-ui-md">degraded</span>}
            </div>
          );
        })()}
        {maxPlanAccounts.length === 0 && (
          <p className="text-xs text-neutral-600 mb-3">No accounts &mdash; using API key fallback</p>
        )}
        <div className="space-y-2">
          {maxPlanAccounts.map(account => (
            <AccountRow
              key={account.id}
              account={account}
              isCapExpanded={expandedAccountCaps === account.id}
              capDraft={capDraft}
              providers={providers}
              onToggleAccount={onToggleAccount}
              onRemoveAccount={onRemoveAccount}
              onResetRateLimits={onResetRateLimits}
              onRefreshAccountModels={onRefreshAccountModels}
              onUpdateAccount={onUpdateAccount}
              onUpdateAccountCaps={onUpdateAccountCaps}
              setExpandedAccountCaps={setExpandedAccountCaps}
              setCapDraft={setCapDraft}
            />
          ))}
        </div>
        {!showAddAccountForm ? (
        <button
          onClick={() => setShowAddAccountForm(true)}
          className="mt-4 w-full flex items-center justify-center gap-2 bg-neutral-900 hover:bg-neutral-800 border border-dashed border-neutral-700 text-neutral-400 text-xs min-h-[44px] py-2.5 px-4 rounded-lg transition-colors"
        >
            <Plus className="w-3.5 h-3.5" /> Add account
          </button>
        ) : (
          <div className="mt-4">
            <div className="flex items-center justify-between mb-2">
              <div className="text-xs font-semibold text-neutral-200 uppercase tracking-wider">New account</div>
              <button
                onClick={() => setShowAddAccountForm(false)}
                data-viewport-allow-small-target
                className="text-neutral-600 hover:text-neutral-300 transition-colors"
                title="Cancel"
              >
                <X className="w-4 h-4" />
              </button>
            </div>
            <AccountAddForm
              providers={providers}
              newAccountProvider={newAccountProvider}
              setNewAccountProvider={setNewAccountProvider}
              newAccountAuthMode={newAccountAuthMode}
              setNewAccountAuthMode={setNewAccountAuthMode}
              newAccountName={newAccountName}
              setNewAccountName={setNewAccountName}
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
              showAddAccountCaps={showAddAccountCaps}
              setShowAddAccountCaps={setShowAddAccountCaps}
              onCancel={() => setShowAddAccountForm(false)}
              onAddAccount={onAddAccount}
            />
          </div>
        )}
      </div>
    </div>
  );
}
