import { useState } from 'react';
import type { AccountMutationResult, AccountUpdatePayload, AuthMode, CategoryRoutingEntry, MaxPlanAccount, ModelPreference, ProviderCatalogEntry, ProviderName, ProviderSettingsByEffort, ProviderSettingsDefaults, TaskCapabilityContract, TaskTypeProviderOverrides, TaskTypeRoutingEntry } from '../types/settings';
import { resolveTaskCapabilities } from '../taskCapabilities';
import type { NotificationConfig } from '../components/SettingsNotificationsSection';
import type { BuildConfig } from '../types/project';
import { API_BASE_URL, authHeaders, authGet } from '../api';

type AddToast = (message: string, type: 'success' | 'error' | 'info') => void;

export function useSettings(addToast: AddToast) {
  // ── Settings state ──────────────────────────────────────────────────────
  const [maxConcurrentVMs, setMaxConcurrentVMs] = useState(5);
  const [maxRetries, setMaxRetries] = useState(3);
  const [maxAutonomousTasksPerHour, setMaxAutonomousTasksPerHour] = useState(10);
  const [autonomousResearchIntervalMinutes, setAutonomousResearchIntervalMinutes] = useState(30);
  const [directionCooldownMinutes, setDirectionCooldownMinutes] = useState(240);
  const [strategicResearchFrequency, setStrategicResearchFrequency] = useState(6);
  const [healthCheckUrls, setHealthCheckUrls] = useState<string[]>([]);
  const [agentPromptSuffixes, setAgentPromptSuffixes] = useState<Record<string, string>>({});
  const [usingMaxPlan, setUsingMaxPlan] = useState(false);
  const [maxPlanAccounts, setMaxPlanAccounts] = useState<MaxPlanAccount[]>([]);
  const [newAccountName, setNewAccountName] = useState('');
  const [newAccountProvider, setNewAccountProvider] = useState<ProviderName>('anthropic');
  const [newAccountAuthMode, setNewAccountAuthMode] = useState<AuthMode>('oauth');
  const [newAccountToken, setNewAccountToken] = useState('');
  const [newAccountProviderMetadata, setNewAccountProviderMetadata] = useState<Record<string, string>>({});
  const [newAccountClaudeConfigJson, setNewAccountClaudeConfigJson] = useState('');
  const [newAccountDailyTaskCap, setNewAccountDailyTaskCap] = useState<number | null>(null);
  const [newAccountMaxConcurrentTasks, setNewAccountMaxConcurrentTasks] = useState<number | null>(null);
  const [newAccountAutoDisableAfterRateLimits, setNewAccountAutoDisableAfterRateLimits] = useState<number | null>(null);
  const [githubAccount, setGithubAccount] = useState<string | null>(null);
  const [requireApprovalGates, setRequireApprovalGates] = useState(true);
  const [autoArchiveEnabled, setAutoArchiveEnabled] = useState(true);
  const [autoArchiveTerminalAfterDays, setAutoArchiveTerminalAfterDays] = useState(7);
  const [autoArchiveMaxTerminalTasksPerProject, setAutoArchiveMaxTerminalTasksPerProject] = useState(200);
  const [autoArchiveIntervalMinutes, setAutoArchiveIntervalMinutes] = useState(60);
  const [notificationConfig, setNotificationConfig] = useState<NotificationConfig>({
    webhook_url: '',
    enabled_events: ['TASK_FAILED', 'APPROVAL_NEEDED', 'DEPLOY_HEALTH_ALERT', 'REMEDIATION_ESCALATED', 'MERGE_UNVERIFIED', 'RUNNER_DEACTIVATED', 'RUNNER_DEGRADED', 'RUNNER_DEGRADED_ESCALATED', 'RUNNER_FLEET_OFFLINE', 'RUNNER_LOW_DISK', 'RUNNER_OUTAGE_ESCALATED'],
    throttle_seconds: 30,
  });
  const [providerTierModelMap, setProviderTierModelMap] = useState<Record<string, Record<string, string>>>({});
  const [taskTypeCategories, setTaskTypeCategories] = useState<Record<string, string[]>>({});
  const [categoryRouting, setCategoryRouting] = useState<Record<string, CategoryRoutingEntry>>({});
  const [taskTypeRouting, setTaskTypeRouting] = useState<Record<string, TaskTypeRoutingEntry>>({});
  const [providerSettingsByEffort, setProviderSettingsByEffort] = useState<ProviderSettingsByEffort>({} as ProviderSettingsByEffort);
  const [taskTypeModelPreferences, setTaskTypeModelPreferences] = useState<Record<string, ModelPreference[]>>({});
  const [providerSelectionMode, setProviderSelectionMode] = useState<'balanced' | 'preferred' | 'exclusive'>('balanced');
  const [crossProviderFallbackEnabled, setCrossProviderFallbackEnabled] = useState(true);
  const [providers, setProviders] = useState<Record<string, ProviderCatalogEntry>>({});
  // Backend-published task-capability contract. Null until the first settings
  // fetch resolves (or when an older backend omits the key) — consumers fall
  // back to the built-in capability table in that case.
  const [taskCapabilityContract, setTaskCapabilityContract] = useState<TaskCapabilityContract | null>(null);
  // Settings-bag system: provider-specific defaults + sparse per-task-type
  // overrides. Replaces the retired task_type_models/task_type_efforts and
  // task_type_execution_defaults / provider_execution_modes keys.
  const [providerSettingsDefaults, setProviderSettingsDefaults] = useState<ProviderSettingsDefaults>({});
  const [taskTypeProviderOverrides, setTaskTypeProviderOverrides] = useState<TaskTypeProviderOverrides>({});
  const [buildConfig, setBuildConfig] = useState<BuildConfig | null>(null);
  const [dailyCostCeilingUsd, setDailyCostCeilingUsd] = useState<number>(0);
  const [maxGlobalAutonomousTasksPerHour, setMaxGlobalAutonomousTasksPerHour] = useState(50);
  const [maxPipelineSlots, setMaxPipelineSlots] = useState(4);
  const [reservedSlotsForTypes, setReservedSlotsForTypes] = useState<Record<string, number>>({ create: 1, research: 1 });
  const [evaluatingPipelineTimeoutMinutes, setEvaluatingPipelineTimeoutMinutes] = useState(120);
  const [evaluatingFailHours, setEvaluatingFailHours] = useState(48);
  const [ciBackstopTimeoutMinutes, setCiBackstopTimeoutMinutes] = useState(60);
  // Direction guardrail knobs
  const [directionChurnWindowCycles, setDirectionChurnWindowCycles] = useState(5);
  const [directionChurnNonMergeThreshold, setDirectionChurnNonMergeThreshold] = useState(15);
  const [directionChurnMinMergeRate, setDirectionChurnMinMergeRate] = useState(0.10);
  const [directionLifetimeNonMergeThreshold, setDirectionLifetimeNonMergeThreshold] = useState(150);
  const [directionLifetimeMinMergeRate, setDirectionLifetimeMinMergeRate] = useState(0.10);
  const [directionReviewRecommendationThreshold, setDirectionReviewRecommendationThreshold] = useState(2);

  // ── Handlers ────────────────────────────────────────────────────────────

  const fetchSettings = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/settings`);
      if (res.ok) {
        const data = await res.json();
        setMaxConcurrentVMs(data.max_concurrent_vms);
        setMaxRetries(data.max_retries ?? 3);
        setMaxAutonomousTasksPerHour(data.max_autonomous_tasks_per_hour ?? 10);
        setAutonomousResearchIntervalMinutes(data.autonomous_research_interval_minutes ?? 30);
        setDirectionCooldownMinutes(data.autonomous_direction_cooldown_minutes ?? 240);
        setStrategicResearchFrequency(data.strategic_research_frequency ?? 6);
        setHealthCheckUrls(data.health_check_urls ?? []);
        setAgentPromptSuffixes(data.agent_prompt_suffixes ?? {});
        setUsingMaxPlan(data.using_max_plan ?? false);
        // NB: the account roster is intentionally NOT read from /settings here.
        // It is owned by the dedicated, always-fresh /max-plan-accounts roster
        // endpoint (fetchMaxPlanAccounts) so add/edit/delete mutations reflect
        // immediately and the UI never races a stale, cache-derived settings
        // copy. /settings still carries `max_plan_accounts` for older clients.
        setProviderTierModelMap(data.provider_tier_model_map ?? {});
        setTaskTypeCategories(data.task_type_categories ?? {});
        setCategoryRouting(data.category_routing ?? {});
        setTaskTypeRouting(data.task_type_routing ?? {});
        setProviderSettingsByEffort((data.provider_settings_by_effort ?? {}) as ProviderSettingsByEffort);
        setTaskTypeModelPreferences(data.task_type_model_preferences ?? {});
        const rawMode = data.provider_selection_mode;
        const resolvedMode: 'balanced' | 'preferred' | 'exclusive' =
          rawMode === 'balanced' || rawMode === 'preferred' || rawMode === 'exclusive'
            ? rawMode
            : data.cross_provider_fallback_enabled ? 'preferred' : 'exclusive';
        setProviderSelectionMode(resolvedMode);
        setCrossProviderFallbackEnabled(data.cross_provider_fallback_enabled ?? (resolvedMode !== 'exclusive'));
        setProviders(data.providers ?? {});
        setTaskCapabilityContract(
          data.task_capabilities && typeof data.task_capabilities === 'object'
            ? (data.task_capabilities as TaskCapabilityContract)
            : null,
        );
        setProviderSettingsDefaults((data.provider_settings_defaults ?? {}) as ProviderSettingsDefaults);
        setTaskTypeProviderOverrides((data.task_type_provider_overrides ?? {}) as TaskTypeProviderOverrides);
        setGithubAccount(data.github_account || null);
        setRequireApprovalGates(data.require_approval_gates ?? true);
        setAutoArchiveEnabled(data.auto_archive_enabled ?? true);
        setAutoArchiveTerminalAfterDays(data.auto_archive_terminal_after_days ?? 7);
        setAutoArchiveMaxTerminalTasksPerProject(data.auto_archive_max_terminal_tasks_per_project ?? 200);
        setAutoArchiveIntervalMinutes(data.auto_archive_interval_minutes ?? 60);
        setDailyCostCeilingUsd(data.daily_cost_ceiling_usd ?? 0);
        setMaxGlobalAutonomousTasksPerHour(data.max_global_autonomous_tasks_per_hour ?? 50);
        setMaxPipelineSlots(data.max_pipeline_slots ?? 4);
        setReservedSlotsForTypes(data.reserved_slots_for_types ?? { create: 1, research: 1 });
        setEvaluatingPipelineTimeoutMinutes(data.evaluating_pipeline_timeout_minutes ?? 120);
        setEvaluatingFailHours(data.evaluating_fail_hours ?? 48);
        setCiBackstopTimeoutMinutes(data.ci_backstop_timeout_minutes ?? 60);
        setDirectionChurnWindowCycles(data.direction_churn_window_cycles ?? 5);
        setDirectionChurnNonMergeThreshold(data.direction_churn_non_merge_threshold ?? 15);
        setDirectionChurnMinMergeRate(data.direction_churn_min_merge_rate ?? 0.10);
        setDirectionLifetimeNonMergeThreshold(data.direction_lifetime_non_merge_threshold ?? 150);
        setDirectionLifetimeMinMergeRate(data.direction_lifetime_min_merge_rate ?? 0.10);
        setDirectionReviewRecommendationThreshold(data.direction_review_recommendation_threshold ?? 2);
        if (data.notification_config) {
          setNotificationConfig({
            webhook_url: data.notification_config.webhook_url ?? '',
            enabled_events: data.notification_config.enabled_events ?? ['TASK_FAILED', 'APPROVAL_NEEDED', 'DEPLOY_HEALTH_ALERT', 'REMEDIATION_ESCALATED', 'MERGE_UNVERIFIED', 'RUNNER_DEACTIVATED', 'RUNNER_DEGRADED', 'RUNNER_DEGRADED_ESCALATED', 'RUNNER_FLEET_OFFLINE', 'RUNNER_LOW_DISK', 'RUNNER_OUTAGE_ESCALATED'],
            throttle_seconds: data.notification_config.throttle_seconds ?? 30,
          });
        }
      }
    } catch (err) {
      console.error('Failed to fetch settings:', err);
    }
  };

  const patchSettings = async (payload: Record<string, unknown>) => {
    const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
      method: 'PATCH',
      headers: await authHeaders(),
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({}));
      throw new Error(data.detail || `HTTP ${res.status}`);
    }
  };

  // Authoritative roster fetch. Every account mutation funnels its post-write
  // refresh through here (not fetchSettings) so the interactive roster stays
  // consistent and decoupled from the heavy /settings + health/provider-model
  // polling paths. Returns nothing; failures are logged and leave prior state.
  const fetchMaxPlanAccounts = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/max-plan-accounts`);
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        if (Array.isArray(data?.accounts)) {
          setMaxPlanAccounts(data.accounts as MaxPlanAccount[]);
        }
      } else {
        console.error('Failed to fetch max-plan accounts: HTTP', res.status);
      }
    } catch (err) {
      console.error('Failed to fetch max-plan accounts:', err);
    }
  };

  const addMaxPlanAccount = async () => {
    if (!newAccountName || !newAccountToken) return;
    try {
      const payload: Record<string, unknown> = {
        name: newAccountName,
        provider: newAccountProvider,
        auth_mode: newAccountAuthMode,
        secret: newAccountToken,
      };
      if (Object.keys(newAccountProviderMetadata).length > 0) payload.provider_metadata = newAccountProviderMetadata;
      if (newAccountClaudeConfigJson.trim()) payload.claude_config_json = newAccountClaudeConfigJson;
      if (newAccountDailyTaskCap !== null) payload.daily_task_cap = newAccountDailyTaskCap;
      if (newAccountMaxConcurrentTasks !== null) payload.max_concurrent_tasks = newAccountMaxConcurrentTasks;
      if (newAccountAutoDisableAfterRateLimits !== null) payload.auto_disable_after_rate_limits = newAccountAutoDisableAfterRateLimits;
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts`, {
        method: 'POST', headers: await authHeaders(),
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        setNewAccountName('');
        setNewAccountProvider('anthropic');
        setNewAccountAuthMode('oauth');
        setNewAccountToken('');
        setNewAccountProviderMetadata({});
        setNewAccountClaudeConfigJson('');
        setNewAccountDailyTaskCap(null);
        setNewAccountMaxConcurrentTasks(null);
        setNewAccountAutoDisableAfterRateLimits(null);
        await fetchMaxPlanAccounts();
        addToast('Account added', 'success');
      } else {
        const data = await res.json().catch(() => ({}));
        const detail = typeof data?.detail === 'string' ? data.detail : null;
        addToast(detail ? `Add account failed: ${detail}` : `Add account failed (HTTP ${res.status})`, 'error');
      }
    } catch (err) {
      console.error('Failed to add account:', err);
      const msg = err instanceof Error ? err.message : 'unknown error';
      addToast(`Add account failed: ${msg}`, 'error');
    }
  };

  // Delete an account. On HTTP 409 (active task / warm-VM references) the
  // result carries `conflict: true` + the backend detail so the caller can
  // re-issue with force=true; other failures toast and return ok:false.
  const removeMaxPlanAccount = async (id: string, force = false): Promise<AccountMutationResult> => {
    try {
      const url = `${API_BASE_URL}/api/admin/max-plan-accounts/${id}${force ? '?force=true' : ''}`;
      const res = await fetch(url, { method: 'DELETE', headers: await authHeaders() });
      if (res.ok) {
        await fetchMaxPlanAccounts();
        addToast('Account removed', 'success');
        return { ok: true };
      }
      const data = await res.json().catch(() => ({}));
      const detail = typeof data?.detail === 'string' ? data.detail : null;
      if (res.status === 409) {
        // Surface the conflict to the caller (offer a force retry) WITHOUT a
        // toast — the inline affordance carries the detail instead.
        return { ok: false, conflict: true, detail };
      }
      addToast(detail ? `Remove failed: ${detail}` : `Remove failed (HTTP ${res.status})`, 'error');
      return { ok: false, detail };
    } catch (err) {
      console.error('Failed to remove account:', err);
      const msg = err instanceof Error ? err.message : 'unknown error';
      addToast(`Remove failed: ${msg}`, 'error');
      return { ok: false, detail: msg };
    }
  };

  // Generic PATCH for the edit dialog: name/enabled/caps/provider/auth_mode/
  // secret/provider_metadata/claude_config_json. Returns a structured result so
  // the dialog can keep itself open and show the backend detail on failure.
  const updateMaxPlanAccount = async (id: string, payload: AccountUpdatePayload): Promise<AccountMutationResult> => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify(payload),
      });
      if (res.ok) {
        await fetchMaxPlanAccounts();
        addToast('Account updated', 'success');
        return { ok: true };
      }
      const data = await res.json().catch(() => ({}));
      const detail = typeof data?.detail === 'string' ? data.detail : null;
      addToast(detail ? `Update failed: ${detail}` : `Update failed (HTTP ${res.status})`, 'error');
      return { ok: false, detail };
    } catch (err) {
      console.error('Failed to update account:', err);
      const msg = err instanceof Error ? err.message : 'unknown error';
      addToast(`Update failed: ${msg}`, 'error');
      return { ok: false, detail: msg };
    }
  };

  const toggleMaxPlanAccount = async (id: string, enabled: boolean) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ enabled }),
      });
      if (res.ok) {
        await fetchMaxPlanAccounts();
      } else {
        const data = await res.json().catch(() => ({}));
        const detail = typeof data?.detail === 'string' ? data.detail : null;
        addToast(detail ? `Failed to update account: ${detail}` : 'Failed to update account', 'error');
      }
    } catch (err) { console.error('Failed to toggle account:', err); addToast('Failed to update account', 'error'); }
  };

  const updateAccountCaps = async (
    id: string,
    caps: { daily_task_cap?: number | null; max_concurrent_tasks?: number | null; auto_disable_after_rate_limits?: number | null }
  ) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify(caps),
      });
      if (res.ok) {
        await fetchMaxPlanAccounts();
        addToast('Caps updated', 'success');
      } else {
        const data = await res.json().catch(() => ({}));
        const detail = typeof data?.detail === 'string' ? data.detail : null;
        addToast(detail ? `Update failed: ${detail}` : `Update failed (HTTP ${res.status})`, 'error');
      }
    } catch (err) {
      console.error('Failed to update account caps:', err);
      const msg = err instanceof Error ? err.message : 'unknown error';
      addToast(`Update failed: ${msg}`, 'error');
    }
  };

  const resetAccountRateLimits = async (id: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}/reset-rate-limits`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (res.ok) {
        addToast('Blueprint cooldowns cleared; provider quota unchanged', 'success');
        await fetchMaxPlanAccounts();
      } else {
        const data = await res.json().catch(() => ({}));
        const detail = typeof data?.detail === 'string' ? data.detail : null;
        addToast(detail ? `Failed to clear Blueprint cooldowns: ${detail}` : 'Failed to clear Blueprint cooldowns', 'error');
      }
    } catch (err) { console.error('Failed to clear Blueprint cooldowns:', err); addToast('Failed to clear Blueprint cooldowns', 'error'); }
  };

  const refreshAccountModels = async (id: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}/refresh-models`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (res.ok) {
        const data = await res.json().catch(() => ({}));
        const modelCount = Array.isArray(data?.available_models) ? data.available_models.length : null;
        const err = typeof data?.error === 'string' ? data.error : null;
        if (err) {
          addToast(`Refresh failed: ${err}`, 'error');
        } else if (modelCount !== null) {
          addToast(`Found ${modelCount} model${modelCount === 1 ? '' : 's'} on this key`, 'success');
        } else {
          addToast('Model availability refreshed', 'success');
        }
        await fetchMaxPlanAccounts();
      } else {
        const data = await res.json().catch(() => ({}));
        const detail = typeof data?.detail === 'string' ? data.detail : null;
        addToast(detail ? `Refresh failed: ${detail}` : `Refresh failed (HTTP ${res.status})`, 'error');
      }
    } catch (err) { console.error('Failed to refresh account models:', err); addToast('Failed to refresh account models', 'error'); }
  };

  const connectGitHub = async () => {
    try {
      const params = new URLSearchParams({ redirect_uri: window.location.origin });
      const res = await authGet(`${API_BASE_URL}/api/github/oauth/authorize?${params.toString()}`);
      if (res.ok) {
        const { url } = await res.json();
        window.location.href = url;
      } else {
        addToast('Failed to start GitHub connection', 'error');
      }
    } catch (err) { console.error('Failed to start GitHub OAuth:', err); addToast('Failed to start GitHub connection', 'error'); }
  };

  const updateAgentPromptSuffixes = async (suffixes: Record<string, string>) => {
    const prev = agentPromptSuffixes;
    setAgentPromptSuffixes(suffixes);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ agent_prompt_suffixes: suffixes }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error('Failed to update agent prompt suffixes:', err);
      setAgentPromptSuffixes(prev);
      addToast('Failed to update prompt suffixes', 'error');
    }
  };

  const updateProviderTierModels = async (value: Record<string, Record<string, string>>) => {
    const prev = providerTierModelMap;
    setProviderTierModelMap(value);
    try {
      await patchSettings({ provider_tier_model_map: value });
    } catch (err) {
      console.error('Failed to update provider tier model map:', err);
      setProviderTierModelMap(prev);
      addToast('Failed to update provider tier model map', 'error');
    }
  };

  const updateTaskTypeCategories = async (value: Record<string, string[]>) => {
    const prev = taskTypeCategories;
    setTaskTypeCategories(value);
    try {
      await patchSettings({ task_type_categories: value });
    } catch (err) {
      console.error('Failed to update task-type categories:', err);
      setTaskTypeCategories(prev);
      addToast('Failed to update task-type categories', 'error');
    }
  };

  const updateCategoryRouting = async (value: Record<string, CategoryRoutingEntry>) => {
    const prev = categoryRouting;
    setCategoryRouting(value);
    try {
      await patchSettings({ category_routing: value });
    } catch (err) {
      console.error('Failed to update category routing:', err);
      setCategoryRouting(prev);
      addToast('Failed to update category routing', 'error');
    }
  };

  const updateTaskTypeRouting = async (value: Record<string, TaskTypeRoutingEntry>) => {
    const prev = taskTypeRouting;
    setTaskTypeRouting(value);
    try {
      await patchSettings({ task_type_routing: value });
    } catch (err) {
      console.error('Failed to update task-type routing:', err);
      setTaskTypeRouting(prev);
      addToast('Failed to update task-type routing', 'error');
    }
  };

  const updateProviderSettingsByEffort = async (value: ProviderSettingsByEffort) => {
    const prev = providerSettingsByEffort;
    setProviderSettingsByEffort(value);
    try {
      await patchSettings({ provider_settings_by_effort: value });
    } catch (err) {
      console.error('Failed to update provider settings by effort:', err);
      setProviderSettingsByEffort(prev);
      addToast('Failed to update effort matrix', 'error');
    }
  };

  const updateTaskTypeModelPreferenceConfig = async (value: Record<string, ModelPreference[]>) => {
    const prev = taskTypeModelPreferences;
    setTaskTypeModelPreferences(value);
    try {
      await patchSettings({ task_type_model_preferences: value });
    } catch (err) {
      console.error('Failed to update model preferences:', err);
      setTaskTypeModelPreferences(prev);
      addToast('Failed to update model preferences', 'error');
    }
  };

  const updateProviderSettingsDefaults = async (value: ProviderSettingsDefaults) => {
    const prev = providerSettingsDefaults;
    setProviderSettingsDefaults(value);
    try {
      await patchSettings({ provider_settings_defaults: value });
    } catch (err) {
      console.error('Failed to update provider settings defaults:', err);
      setProviderSettingsDefaults(prev);
      addToast('Failed to update provider settings defaults', 'error');
    }
  };

  const updateTaskTypeProviderOverrides = async (value: TaskTypeProviderOverrides) => {
    const prev = taskTypeProviderOverrides;
    setTaskTypeProviderOverrides(value);
    try {
      await patchSettings({ task_type_provider_overrides: value });
    } catch (err) {
      console.error('Failed to update task-type provider overrides:', err);
      setTaskTypeProviderOverrides(prev);
      addToast('Failed to update task-type overrides', 'error');
    }
  };

  const updateProviderSelectionMode = async (value: 'balanced' | 'preferred' | 'exclusive') => {
    const prevMode = providerSelectionMode;
    const prevFallback = crossProviderFallbackEnabled;
    setProviderSelectionMode(value);
    setCrossProviderFallbackEnabled(value !== 'exclusive');
    try {
      await patchSettings({ provider_selection_mode: value });
    } catch (err) {
      console.error('Failed to update provider selection mode:', err);
      setProviderSelectionMode(prevMode);
      setCrossProviderFallbackEnabled(prevFallback);
      addToast('Failed to update provider selection mode', 'error');
    }
  };

  const updateCrossProviderFallback = async (value: boolean) => {
    await updateProviderSelectionMode(value ? 'preferred' : 'exclusive');
  };

  const updateMaxVMs = async (value: number) => {
    const clamped = Math.max(0, Math.min(100, value));
    const prev = maxConcurrentVMs;
    setMaxConcurrentVMs(clamped);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ max_concurrent_vms: clamped }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error('Failed to update settings:', err);
      setMaxConcurrentVMs(prev);
      addToast('Failed to update max VMs', 'error');
    }
  };

  const updateMaxRetries = async (value: number) => {
    const v = Math.max(0, Math.min(10, value));
    const prev = maxRetries;
    setMaxRetries(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ max_retries: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setMaxRetries(prev);
      addToast('Failed to update max retries', 'error');
    }
  };

  const updateAutoArchiveEnabled = async (value: boolean) => {
    const prev = autoArchiveEnabled;
    setAutoArchiveEnabled(value);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ auto_archive_enabled: value }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setAutoArchiveEnabled(prev);
      addToast('Failed to update auto-archive', 'error');
    }
  };

  const updateAutoArchiveTerminalAfterDays = async (value: number) => {
    const v = Math.max(1, Math.min(90, value));
    const prev = autoArchiveTerminalAfterDays;
    setAutoArchiveTerminalAfterDays(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ auto_archive_terminal_after_days: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setAutoArchiveTerminalAfterDays(prev);
      addToast('Failed to update archive age', 'error');
    }
  };

  const updateAutoArchiveMaxTerminalTasksPerProject = async (value: number) => {
    const v = Math.max(0, Math.min(10000, value));
    const prev = autoArchiveMaxTerminalTasksPerProject;
    setAutoArchiveMaxTerminalTasksPerProject(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ auto_archive_max_terminal_tasks_per_project: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setAutoArchiveMaxTerminalTasksPerProject(prev);
      addToast('Failed to update archive cap', 'error');
    }
  };

  const updateAutoArchiveIntervalMinutes = async (value: number) => {
    const v = Math.max(5, Math.min(1440, value));
    const prev = autoArchiveIntervalMinutes;
    setAutoArchiveIntervalMinutes(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ auto_archive_interval_minutes: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setAutoArchiveIntervalMinutes(prev);
      addToast('Failed to update archive interval', 'error');
    }
  };

  const handleUpdateMaxAutonomousRate = async (value: number) => {
    const v = Math.max(1, Math.min(50, value));
    const prev = maxAutonomousTasksPerHour;
    setMaxAutonomousTasksPerHour(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ max_autonomous_tasks_per_hour: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setMaxAutonomousTasksPerHour(prev);
      addToast('Failed to update autonomous rate', 'error');
    }
  };

  const handleUpdateAutonomousResearchInterval = async (value: number) => {
    const v = Math.max(1, Math.min(1440, value));
    const prev = autonomousResearchIntervalMinutes;
    setAutonomousResearchIntervalMinutes(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ autonomous_research_interval_minutes: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setAutonomousResearchIntervalMinutes(prev);
      addToast('Failed to update research interval', 'error');
    }
  };

  const handleUpdateDirectionCooldownMinutes = async (value: number) => {
    const v = Math.max(1, Math.min(10080, value));
    const prev = directionCooldownMinutes;
    setDirectionCooldownMinutes(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ autonomous_direction_cooldown_minutes: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setDirectionCooldownMinutes(prev);
      addToast('Failed to update direction cooldown', 'error');
    }
  };

  const handleUpdateStrategicResearchFrequency = async (value: number) => {
    const v = Math.max(0, Math.min(100, value));
    const prev = strategicResearchFrequency;
    setStrategicResearchFrequency(v);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ strategic_research_frequency: v }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setStrategicResearchFrequency(prev);
      addToast('Failed to update strategic research frequency', 'error');
    }
  };

  const updateRequireApprovalGates = async (value: boolean) => {
    const prev = requireApprovalGates;
    setRequireApprovalGates(value);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ require_approval_gates: value }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setRequireApprovalGates(prev);
      addToast('Failed to update approval gates setting', 'error');
    }
  };

  const updateHealthCheckUrls = async (urls: string[]) => {
    const prev = healthCheckUrls;
    setHealthCheckUrls(urls);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ health_check_urls: urls }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      addToast('Health check URLs saved', 'success');
    } catch {
      setHealthCheckUrls(prev);
      addToast('Failed to save health check URLs', 'error');
    }
  };

  const updateBuildConfig = async (projectId: string, config: BuildConfig) => {
    const prev = buildConfig;
    // Operator-configured fields are not auto-detectable; preserve them from the
    // current state when the caller doesn't supply them (e.g. detect replaced
    // buildConfig without these fields, and the component spread passed undefined).
    const merged: BuildConfig = {
      ...config,
      task_branch_namespace: config.task_branch_namespace ?? prev?.task_branch_namespace,
      task_branch_prefix: config.task_branch_prefix ?? prev?.task_branch_prefix,
      pr_title_prefix: config.pr_title_prefix ?? prev?.pr_title_prefix,
    };
    setBuildConfig(merged);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${projectId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ build_config: merged }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setBuildConfig(prev);
      throw new Error('Failed to save build config');
    }
  };

  const updateDailyCostCeiling = async (value: number) => {
    const prev = dailyCostCeilingUsd;
    setDailyCostCeilingUsd(value);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ daily_cost_ceiling_usd: value }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setDailyCostCeilingUsd(prev);
      addToast('Failed to update daily cost ceiling', 'error');
    }
  };

  const updateMaxGlobalAutonomousRate = async (value: number) => {
    const v = Math.max(1, Math.min(500, value));
    const prev = maxGlobalAutonomousTasksPerHour;
    setMaxGlobalAutonomousTasksPerHour(v);
    try {
      await patchSettings({ max_global_autonomous_tasks_per_hour: v });
    } catch {
      setMaxGlobalAutonomousTasksPerHour(prev);
      addToast('Failed to update global autonomous rate', 'error');
    }
  };

  const updateMaxPipelineSlots = async (value: number) => {
    const v = Math.max(0, Math.min(100, value));
    const prev = maxPipelineSlots;
    setMaxPipelineSlots(v);
    try {
      await patchSettings({ max_pipeline_slots: v });
    } catch {
      setMaxPipelineSlots(prev);
      addToast('Failed to update pipeline slots', 'error');
    }
  };

  const updateReservedSlotsForTypes = async (value: Record<string, number>) => {
    // Allowlist derives from the published contract's reservable flag (falling
    // back to the built-in table) so a newly registered reservable type is
    // accepted without editing this guard — mirroring the backend validator.
    const reservableIds = resolveTaskCapabilities(taskCapabilityContract).reservableIds;
    const sanitized: Record<string, number> = {};
    for (const key of reservableIds) {
      const n = value[key];
      if (typeof n === 'number' && n > 0) sanitized[key] = Math.max(0, Math.min(10, Math.floor(n)));
    }
    const prev = reservedSlotsForTypes;
    setReservedSlotsForTypes(sanitized);
    try {
      await patchSettings({ reserved_slots_for_types: sanitized });
    } catch {
      setReservedSlotsForTypes(prev);
      addToast('Failed to update reserved slots', 'error');
    }
  };

  const updateEvaluatingPipelineTimeout = async (value: number) => {
    const v = Math.max(1, Math.min(1440, value));
    const prev = evaluatingPipelineTimeoutMinutes;
    setEvaluatingPipelineTimeoutMinutes(v);
    try {
      await patchSettings({ evaluating_pipeline_timeout_minutes: v });
    } catch {
      setEvaluatingPipelineTimeoutMinutes(prev);
      addToast('Failed to update pipeline timeout', 'error');
    }
  };

  const updateEvaluatingFailHours = async (value: number) => {
    const v = Math.max(1, Math.min(168, value));
    const prev = evaluatingFailHours;
    setEvaluatingFailHours(v);
    try {
      await patchSettings({ evaluating_fail_hours: v });
    } catch {
      setEvaluatingFailHours(prev);
      addToast('Failed to update evaluating fail hours', 'error');
    }
  };

  const updateCiBackstopTimeout = async (value: number) => {
    const v = Math.max(1, Math.min(1440, value));
    const prev = ciBackstopTimeoutMinutes;
    setCiBackstopTimeoutMinutes(v);
    try {
      await patchSettings({ ci_backstop_timeout_minutes: v });
    } catch {
      setCiBackstopTimeoutMinutes(prev);
      addToast('Failed to update CI backstop timeout', 'error');
    }
  };

  const updateDirectionChurnWindowCycles = async (value: number) => {
    const v = Math.max(1, Math.min(100, Math.round(value)));
    const prev = directionChurnWindowCycles;
    setDirectionChurnWindowCycles(v);
    try {
      await patchSettings({ direction_churn_window_cycles: v });
    } catch {
      setDirectionChurnWindowCycles(prev);
      addToast('Failed to update churn window cycles', 'error');
    }
  };

  const updateDirectionChurnNonMergeThreshold = async (value: number) => {
    const v = Math.max(1, Math.min(10000, Math.round(value)));
    const prev = directionChurnNonMergeThreshold;
    setDirectionChurnNonMergeThreshold(v);
    try {
      await patchSettings({ direction_churn_non_merge_threshold: v });
    } catch {
      setDirectionChurnNonMergeThreshold(prev);
      addToast('Failed to update churn non-merge threshold', 'error');
    }
  };

  const updateDirectionChurnMinMergeRate = async (value: number) => {
    const v = Math.max(0, Math.min(1, value));
    const prev = directionChurnMinMergeRate;
    setDirectionChurnMinMergeRate(v);
    try {
      await patchSettings({ direction_churn_min_merge_rate: v });
    } catch {
      setDirectionChurnMinMergeRate(prev);
      addToast('Failed to update churn min merge rate', 'error');
    }
  };

  const updateDirectionLifetimeNonMergeThreshold = async (value: number) => {
    const v = Math.max(1, Math.min(100000, Math.round(value)));
    const prev = directionLifetimeNonMergeThreshold;
    setDirectionLifetimeNonMergeThreshold(v);
    try {
      await patchSettings({ direction_lifetime_non_merge_threshold: v });
    } catch {
      setDirectionLifetimeNonMergeThreshold(prev);
      addToast('Failed to update lifetime non-merge threshold', 'error');
    }
  };

  const updateDirectionLifetimeMinMergeRate = async (value: number) => {
    const v = Math.max(0, Math.min(1, value));
    const prev = directionLifetimeMinMergeRate;
    setDirectionLifetimeMinMergeRate(v);
    try {
      await patchSettings({ direction_lifetime_min_merge_rate: v });
    } catch {
      setDirectionLifetimeMinMergeRate(prev);
      addToast('Failed to update lifetime min merge rate', 'error');
    }
  };

  const updateDirectionReviewRecommendationThreshold = async (value: number) => {
    const v = Math.max(1, Math.min(100, Math.round(value)));
    const prev = directionReviewRecommendationThreshold;
    setDirectionReviewRecommendationThreshold(v);
    try {
      await patchSettings({ direction_review_recommendation_threshold: v });
    } catch {
      setDirectionReviewRecommendationThreshold(prev);
      addToast('Failed to update review recommendation threshold', 'error');
    }
  };

  const updateNotificationConfig = async (config: Partial<NotificationConfig>) => {
    const prev = notificationConfig;
    const next = { ...notificationConfig, ...config };
    setNotificationConfig(next);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ notification_config: next }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setNotificationConfig(prev);
      addToast('Failed to save notification settings', 'error');
    }
  };

  return {
    // State
    maxConcurrentVMs, maxRetries, maxAutonomousTasksPerHour,
    autonomousResearchIntervalMinutes, directionCooldownMinutes, strategicResearchFrequency,
    healthCheckUrls, agentPromptSuffixes, usingMaxPlan,
    maxPlanAccounts, newAccountName, setNewAccountName, newAccountProvider, setNewAccountProvider,
    newAccountAuthMode, setNewAccountAuthMode, newAccountToken, setNewAccountToken,
    newAccountProviderMetadata, setNewAccountProviderMetadata, newAccountClaudeConfigJson, setNewAccountClaudeConfigJson,
    newAccountDailyTaskCap, setNewAccountDailyTaskCap,
    newAccountMaxConcurrentTasks, setNewAccountMaxConcurrentTasks,
    newAccountAutoDisableAfterRateLimits, setNewAccountAutoDisableAfterRateLimits,
    githubAccount, setGithubAccount,
    requireApprovalGates,
    autoArchiveEnabled, autoArchiveTerminalAfterDays,
    autoArchiveMaxTerminalTasksPerProject, autoArchiveIntervalMinutes,
    notificationConfig, dailyCostCeilingUsd,
    maxGlobalAutonomousTasksPerHour, maxPipelineSlots, reservedSlotsForTypes,
    evaluatingPipelineTimeoutMinutes, evaluatingFailHours, ciBackstopTimeoutMinutes,
    directionChurnWindowCycles, directionChurnNonMergeThreshold, directionChurnMinMergeRate,
    directionLifetimeNonMergeThreshold, directionLifetimeMinMergeRate,
    directionReviewRecommendationThreshold,
    providerTierModelMap, taskTypeModelPreferences,
    taskTypeCategories, categoryRouting, taskTypeRouting,
    providerSettingsByEffort,
    providerSelectionMode,
    crossProviderFallbackEnabled, providers,
    taskCapabilityContract,
    providerSettingsDefaults, setProviderSettingsDefaults,
    taskTypeProviderOverrides, setTaskTypeProviderOverrides,
    buildConfig, setBuildConfig,
    // Handlers
    fetchSettings,
    fetchMaxPlanAccounts,
    addMaxPlanAccount, removeMaxPlanAccount, toggleMaxPlanAccount, resetAccountRateLimits,
    refreshAccountModels,
    updateAccountCaps, updateMaxPlanAccount,
    connectGitHub,
    updateAgentPromptSuffixes,
    updateProviderTierModels, updateTaskTypeModelPreferenceConfig,
    updateTaskTypeCategories, updateCategoryRouting, updateTaskTypeRouting,
    updateProviderSettingsByEffort,
    updateProviderSelectionMode,
    updateCrossProviderFallback,
    updateProviderSettingsDefaults, updateTaskTypeProviderOverrides,
    updateMaxVMs, updateMaxRetries,
    handleUpdateMaxAutonomousRate, handleUpdateAutonomousResearchInterval,
    handleUpdateDirectionCooldownMinutes, handleUpdateStrategicResearchFrequency,
    updateHealthCheckUrls, updateRequireApprovalGates,
    updateAutoArchiveEnabled, updateAutoArchiveTerminalAfterDays,
    updateAutoArchiveMaxTerminalTasksPerProject, updateAutoArchiveIntervalMinutes,
    updateNotificationConfig, updateDailyCostCeiling,
    updateMaxGlobalAutonomousRate, updateMaxPipelineSlots, updateReservedSlotsForTypes,
    updateEvaluatingPipelineTimeout, updateEvaluatingFailHours, updateCiBackstopTimeout,
    updateDirectionChurnWindowCycles, updateDirectionChurnNonMergeThreshold,
    updateDirectionChurnMinMergeRate, updateDirectionLifetimeNonMergeThreshold,
    updateDirectionLifetimeMinMergeRate, updateDirectionReviewRecommendationThreshold,
    updateBuildConfig,
  };
}
