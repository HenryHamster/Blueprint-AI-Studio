import { useState } from 'react';
import type { AuthMode, CategoryRoutingEntry, MaxPlanAccount, ModelPreference, ProviderCatalogEntry, ProviderName, ProviderSettingsByEffort, ProviderSettingsDefaults, TaskTypeProviderOverrides, TaskTypeRoutingEntry } from '../types/settings';
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
    enabled_events: ['TASK_FAILED', 'APPROVAL_NEEDED', 'DEPLOY_HEALTH_ALERT', 'REMEDIATION_ESCALATED'],
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
  // Settings-bag system: provider-specific defaults + sparse per-task-type
  // overrides. Replaces the retired task_type_models/task_type_efforts and
  // task_type_execution_defaults / provider_execution_modes keys.
  const [providerSettingsDefaults, setProviderSettingsDefaults] = useState<ProviderSettingsDefaults>({});
  const [taskTypeProviderOverrides, setTaskTypeProviderOverrides] = useState<TaskTypeProviderOverrides>({});
  const [buildConfig, setBuildConfig] = useState<BuildConfig | null>(null);
  const [dailyCostCeilingUsd, setDailyCostCeilingUsd] = useState<number>(0);

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
        setMaxPlanAccounts(data.max_plan_accounts || []);
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
        setProviderSettingsDefaults((data.provider_settings_defaults ?? {}) as ProviderSettingsDefaults);
        setTaskTypeProviderOverrides((data.task_type_provider_overrides ?? {}) as TaskTypeProviderOverrides);
        setGithubAccount(data.github_account || null);
        setRequireApprovalGates(data.require_approval_gates ?? true);
        setAutoArchiveEnabled(data.auto_archive_enabled ?? true);
        setAutoArchiveTerminalAfterDays(data.auto_archive_terminal_after_days ?? 7);
        setAutoArchiveMaxTerminalTasksPerProject(data.auto_archive_max_terminal_tasks_per_project ?? 200);
        setAutoArchiveIntervalMinutes(data.auto_archive_interval_minutes ?? 60);
        setDailyCostCeilingUsd(data.daily_cost_ceiling_usd ?? 0);
        if (data.notification_config) {
          setNotificationConfig({
            webhook_url: data.notification_config.webhook_url ?? '',
            enabled_events: data.notification_config.enabled_events ?? ['TASK_FAILED', 'APPROVAL_NEEDED', 'DEPLOY_HEALTH_ALERT', 'REMEDIATION_ESCALATED'],
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
        fetchSettings();
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

  const removeMaxPlanAccount = async (id: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}`, {
        method: 'DELETE', headers: await authHeaders(),
      });
      if (res.ok) {
        fetchSettings();
      } else {
        addToast('Failed to remove account', 'error');
      }
    } catch (err) { console.error('Failed to remove account:', err); addToast('Failed to remove account', 'error'); }
  };

  const toggleMaxPlanAccount = async (id: string, enabled: boolean) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts/${id}`, {
        method: 'PATCH', headers: await authHeaders(),
        body: JSON.stringify({ enabled }),
      });
      if (res.ok) {
        fetchSettings();
      } else {
        addToast('Failed to update account', 'error');
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
        fetchSettings();
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
        addToast('Rate limits cleared', 'success');
        fetchSettings();
      } else {
        addToast('Failed to reset rate limits', 'error');
      }
    } catch (err) { console.error('Failed to reset rate limits:', err); addToast('Failed to reset rate limits', 'error'); }
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
        fetchSettings();
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
    providerTierModelMap, taskTypeModelPreferences,
    taskTypeCategories, categoryRouting, taskTypeRouting,
    providerSettingsByEffort,
    providerSelectionMode,
    crossProviderFallbackEnabled, providers,
    providerSettingsDefaults, setProviderSettingsDefaults,
    taskTypeProviderOverrides, setTaskTypeProviderOverrides,
    buildConfig, setBuildConfig,
    // Handlers
    fetchSettings,
    addMaxPlanAccount, removeMaxPlanAccount, toggleMaxPlanAccount, resetAccountRateLimits,
    refreshAccountModels,
    updateAccountCaps,
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
    updateBuildConfig,
  };
}
