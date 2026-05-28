import { useEffect, useState, useSyncExternalStore, type ReactNode } from 'react';
import { ChevronDown, ChevronUp, FileDown, GitBranch, HelpCircle, Plus, RefreshCw, Sliders, Users, X } from 'lucide-react';
import { parseTimestamp, timeAgo } from '../utils/time';
import type { AuthMode, MaxPlanAccount, ProviderCatalogEntry, ProviderName } from '../types/settings';

const PROVIDER_ORDER: ProviderName[] = ['anthropic', 'openai', 'gemini'];

const FALLBACK_AUTH_MODES: Record<ProviderName, AuthMode[]> = {
  anthropic: ['oauth', 'api_key'],
  openai: ['api_key', 'oauth'],
  gemini: ['api_key', 'service_account'],
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
  isAdmin: boolean;
  maxPlanAccounts: MaxPlanAccount[];
  onToggleAccount: (id: string, enabled: boolean) => void;
  onRemoveAccount: (id: string) => void;
  onResetRateLimits: (id: string) => void;
  onRefreshAccountModels: (id: string) => void;
  onUpdateAccountCaps: (id: string, caps: { daily_task_cap?: number | null; max_concurrent_tasks?: number | null; auto_disable_after_rate_limits?: number | null }) => void;
  showUserManagement: boolean;
  onToggleUserManagement: () => void;
  showProjectMembers: boolean;
  onToggleProjectMembers: () => void;
  selectedProjectId: string | null;
  selectedProjectMyRole: string | null;
  onToggleSettings: () => void;
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
    <div className="text-[11px] font-mono flex items-center gap-1.5 mt-0.5">
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
  onToggleAccount,
  onRemoveAccount,
  onResetRateLimits,
  onRefreshAccountModels,
  onUpdateAccountCaps,
  setExpandedAccountCaps,
  setCapDraft,
}: {
  account: MaxPlanAccount;
  isCapExpanded: boolean;
  capDraft: { daily_task_cap: string; max_concurrent_tasks: string; auto_disable_after_rate_limits: string };
  onToggleAccount: (id: string, enabled: boolean) => void;
  onRemoveAccount: (id: string) => void;
  onResetRateLimits: (id: string) => void;
  onRefreshAccountModels: (id: string) => void;
  onUpdateAccountCaps: (id: string, caps: { daily_task_cap?: number | null; max_concurrent_tasks?: number | null; auto_disable_after_rate_limits?: number | null }) => void;
  setExpandedAccountCaps: (value: string | null) => void;
  setCapDraft: (value: { daily_task_cap: string; max_concurrent_tasks: string; auto_disable_after_rate_limits: string }) => void;
}) {
  const now = useClockNow();

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
          onClick={() => onToggleAccount(account.id, !account.enabled)}
          data-viewport-allow-small-target
          className={`w-2.5 h-2.5 rounded-full shrink-0 ${isRateLimited ? 'bg-amber-500' : account.enabled ? 'bg-emerald-500' : 'bg-neutral-600'}`}
          title={isRateLimited ? 'Rate-limited' : account.enabled ? 'Enabled (click to disable)' : 'Disabled (click to enable)'}
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 min-w-0 flex-wrap">
            <div className="text-sm text-neutral-200 truncate">{account.name}</div>
            <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 border border-neutral-700">{account.provider}</span>
            <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-500 border border-neutral-700">{account.auth_mode}</span>
            {(dailyCap !== null || concurrentCap !== null || autoDisableAfter !== null) && (
              <span className="text-[10px] px-1.5 py-0.5 rounded bg-cyan-950/40 text-cyan-400 border border-cyan-900/50" title="Usage caps configured">
                {dailyCap !== null && <>{dailyCount}/{dailyCap} today</>}
                {dailyCap !== null && concurrentCap !== null && <> &middot; </>}
                {concurrentCap !== null && <>max {concurrentCap}</>}
              </span>
            )}
          </div>
          <div className="text-[11px] text-neutral-500 font-mono">
            {account.active_tasks} active
            {tokenAge && <span className={`ml-2 ${isExpiring ? 'text-amber-400' : ''}`}>&middot; token {tokenAge}</span>}
            {account.provider_metadata?.project && typeof account.provider_metadata.project === 'string' && <span className="ml-2">&middot; {account.provider_metadata.project}</span>}
            {account.provider_metadata?.location && typeof account.provider_metadata.location === 'string' && <span className="ml-2">@ {account.provider_metadata.location}</span>}
          </div>
          {accountSupportsAvailabilityCheck(account) && (
            <AccountAvailabilityLine
              account={account}
              onRefresh={() => onRefreshAccountModels(account.id)}
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
          data-viewport-allow-small-target
          className="text-neutral-600 hover:text-neutral-300 transition-colors shrink-0 p-1"
        >
          <Sliders className="w-3.5 h-3.5" />
        </button>
        <button
          onClick={() => onRemoveAccount(account.id)}
          data-viewport-allow-small-target
          className="text-neutral-600 hover:text-red-400 text-sm transition-colors shrink-0"
        >
          &times;
        </button>
      </div>
      {account.disabled_reason && (
        <div className="mt-2 pt-2 border-t border-red-900/40 text-[11px] text-red-400 flex items-center justify-between gap-2">
          <span>&#9888; {account.disabled_reason}</span>
          <button data-viewport-allow-small-target onClick={() => onToggleAccount(account.id, true)} className="text-red-300 hover:text-red-200 underline">Re-enable</button>
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
              <div key={model} className="flex items-center justify-between text-[11px] text-amber-400/80">
                <span className="font-mono">{shortModel} <span className="text-amber-500/60">hit #{info.hits}</span></span>
                <span className="text-amber-500/60">{remaining !== null && remaining > 0 ? `${remaining}m left` : 'recovering'}</span>
              </div>
            );
          })}
          <button
            onClick={() => onResetRateLimits(account.id)}
            data-viewport-allow-small-target
            className="mt-1.5 w-full text-[11px] text-amber-400/70 hover:text-amber-300 bg-amber-950/30 hover:bg-amber-950/50 rounded-lg px-2 py-1 transition-colors"
          >
            Clear rate limits
          </button>
        </div>
      )}
      {isCapExpanded && (
        <div className="mt-2 pt-2 border-t border-neutral-800/50 space-y-2">
          <div className="text-[10px] uppercase tracking-wider text-neutral-500">Usage caps</div>
          <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
            <label className="block">
              <div className="text-[10px] text-neutral-500 mb-1">Tasks/day</div>
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
              <div className="text-[10px] text-neutral-500 mb-1">Max concurrent</div>
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
              <div className="text-[10px] text-neutral-500 mb-1">Auto-disable after N RL</div>
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
              className="bg-neutral-800 hover:bg-neutral-700 text-neutral-200 text-[11px] py-1 px-3 rounded border border-neutral-700"
            >
              Save caps
            </button>
            <button
              onClick={() => setExpandedAccountCaps(null)}
              className="text-[11px] text-neutral-500 hover:text-neutral-300"
            >
              Cancel
            </button>
            <span className="text-[10px] text-neutral-600 ml-auto">Leave a field empty for unlimited/off.</span>
          </div>
        </div>
      )}
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
          <div className="text-[10px] text-neutral-500 mb-1 uppercase">Provider</div>
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
          <div className="text-[10px] text-neutral-500 mb-1 uppercase">Auth mode</div>
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
            <ol className="list-decimal list-inside mt-1.5 space-y-0.5 text-[11px] text-neutral-400">
              {guide.steps.map((step, i) => (
                <li key={i}>{renderGuideText(step)}</li>
              ))}
            </ol>
          </div>
        </div>
      )}
      <div>
        <div className="text-[10px] text-neutral-500 mb-1 uppercase">Name</div>
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
                <div className="text-[10px] text-neutral-500 mb-1 uppercase">{field}</div>
                <input
                  value={newAccountProviderMetadata[field] ?? ''}
                  onChange={e => setNewAccountProviderMetadata({ ...newAccountProviderMetadata, [field]: e.target.value })}
                  placeholder={hint?.placeholder ?? field}
                  className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                />
                {hint && <div className="text-[10px] text-neutral-600 mt-0.5">{hint.label}</div>}
              </div>
            );
          })}
        </div>
      )}
      <div>
        <div className="text-[10px] text-neutral-500 mb-1 uppercase">{secretLabel}</div>
        {((newAccountProvider === 'openai' && newAccountAuthMode === 'oauth') || (newAccountProvider === 'gemini' && newAccountAuthMode === 'service_account')) ? (
          <div className="space-y-1.5">
            <textarea
              value={newAccountToken}
              onChange={e => setNewAccountToken(e.target.value)}
              rows={4}
              placeholder={`Paste ${secretLabel} or upload a .json file below`}
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600 resize-none"
            />
            <label className="flex items-center gap-2 text-[11px] text-neutral-500 hover:text-neutral-300 cursor-pointer transition-colors">
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
          <div className="text-[10px] text-neutral-500 mb-1 uppercase flex items-center gap-1">
            claude_config_json
            <span className="text-neutral-600" title="Optional. Contents of ~/.claude.json - lets the agent skip onboarding screens on first run. Not required for most setups.">
              <HelpCircle className="w-3 h-3" />
            </span>
            <span className="text-[10px] text-neutral-600 normal-case">(optional)</span>
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
        className="flex items-center gap-1.5 text-[11px] text-neutral-500 hover:text-neutral-300 transition-colors"
      >
        {showAddAccountCaps ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        Usage limits (optional)
      </button>
      {showAddAccountCaps && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-2 bg-neutral-950 border border-neutral-800 rounded-lg p-3">
          <label className="block">
            <div className="text-[10px] text-neutral-500 mb-1 flex items-center gap-1">
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
            <div className="text-[10px] text-neutral-500 mb-1 flex items-center gap-1">
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
            <div className="text-[10px] text-neutral-500 mb-1 flex items-center gap-1">
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
        <span className="text-[11px] text-neutral-600 ml-auto">Missing: {missing.join(', ')}</span>
      </div>
    </div>
  );
}

export function SettingsAccountsSection({
  githubAccount,
  onConnectGitHub,
  isAdmin,
  maxPlanAccounts,
  onToggleAccount,
  onRemoveAccount,
  onResetRateLimits,
  onRefreshAccountModels,
  onUpdateAccountCaps,
  showUserManagement,
  onToggleUserManagement,
  showProjectMembers,
  onToggleProjectMembers,
  selectedProjectId,
  selectedProjectMyRole,
  onToggleSettings,
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
      {isAdmin && (
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">User Management</h3>
          <button
            onClick={() => { onToggleUserManagement(); onToggleSettings(); }}
            className={`flex items-center gap-2 min-h-[44px] text-xs py-2 px-4 rounded-lg transition-colors border ${
              showUserManagement
                ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-400'
                : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'
            }`}
          >
            <Users className="w-3.5 h-3.5" />
            {showUserManagement ? 'Close User Management' : 'Open User Management'}
          </button>
        </div>
      )}

      {selectedProjectId && (selectedProjectMyRole === 'owner' || isAdmin) && (
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Project Members</h3>
          <button
            data-testid="toggle-project-members"
            onClick={() => { onToggleProjectMembers(); onToggleSettings(); }}
            className={`flex items-center gap-2 min-h-[44px] text-xs py-2 px-4 rounded-lg transition-colors border ${
              showProjectMembers
                ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-400'
                : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'
            }`}
          >
            <Users className="w-3.5 h-3.5" />
            {showProjectMembers ? 'Close Project Members' : 'Manage Project Members'}
          </button>
        </div>
      )}

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
              {warn && <span className="text-amber-500 text-[11px]">degraded</span>}
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
              onToggleAccount={onToggleAccount}
              onRemoveAccount={onRemoveAccount}
              onResetRateLimits={onResetRateLimits}
              onRefreshAccountModels={onRefreshAccountModels}
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
