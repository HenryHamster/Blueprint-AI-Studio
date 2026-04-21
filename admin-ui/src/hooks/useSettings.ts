import { useState } from 'react';
import type { AuthMode, MaxPlanAccount, ModelPreference, ProviderCatalogEntry, ProviderName } from '../types/settings';
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
  const [githubAccount, setGithubAccount] = useState<string | null>(null);
  const [requireApprovalGates, setRequireApprovalGates] = useState(true);
  const [autoArchiveEnabled, setAutoArchiveEnabled] = useState(true);
  const [autoArchiveTerminalAfterDays, setAutoArchiveTerminalAfterDays] = useState(7);
  const [autoArchiveMaxTerminalTasksPerProject, setAutoArchiveMaxTerminalTasksPerProject] = useState(200);
  const [autoArchiveIntervalMinutes, setAutoArchiveIntervalMinutes] = useState(60);
  const [notificationConfig, setNotificationConfig] = useState<NotificationConfig>({
    webhook_url: '',
    enabled_events: ['TASK_FAILED', 'APPROVAL_NEEDED', 'DEPLOY_HEALTH_ALERT'],
    throttle_seconds: 30,
  });
  const [providerTierModelMap, setProviderTierModelMap] = useState<Record<string, Record<string, string>>>({});
  const [taskTypeProviderPreference, setTaskTypeProviderPreference] = useState<Record<string, ProviderName[]>>({});
  const [taskTypeModelPreferences, setTaskTypeModelPreferences] = useState<Record<string, ModelPreference[]>>({});
  const [providerExecutionModes, setProviderExecutionModes] = useState<Record<string, string[]>>({});
  const [providerModeCapabilities, setProviderModeCapabilities] = useState<Record<string, Record<string, Record<string, boolean | number | string | null>>>>({});
  const [taskTypeExecutionDefaults, setTaskTypeExecutionDefaults] = useState<Record<string, string>>({});
  const [crossProviderFallbackEnabled, setCrossProviderFallbackEnabled] = useState(false);
  const [providers, setProviders] = useState<Record<string, ProviderCatalogEntry>>({});
  const [buildConfig, setBuildConfig] = useState<BuildConfig | null>(null);

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
        setTaskTypeProviderPreference(data.task_type_provider_preference ?? {});
        setTaskTypeModelPreferences(data.task_type_model_preferences ?? {});
        setProviderExecutionModes(data.provider_execution_modes ?? {});
        setProviderModeCapabilities(data.provider_mode_capabilities ?? {});
        setTaskTypeExecutionDefaults(data.task_type_execution_defaults ?? {});
        setCrossProviderFallbackEnabled(data.cross_provider_fallback_enabled ?? false);
        setProviders(data.providers ?? {});
        setGithubAccount(data.github_account || null);
        setRequireApprovalGates(data.require_approval_gates ?? true);
        setAutoArchiveEnabled(data.auto_archive_enabled ?? true);
        setAutoArchiveTerminalAfterDays(data.auto_archive_terminal_after_days ?? 7);
        setAutoArchiveMaxTerminalTasksPerProject(data.auto_archive_max_terminal_tasks_per_project ?? 200);
        setAutoArchiveIntervalMinutes(data.auto_archive_interval_minutes ?? 60);
        if (data.notification_config) {
          setNotificationConfig({
            webhook_url: data.notification_config.webhook_url ?? '',
            enabled_events: data.notification_config.enabled_events ?? ['TASK_FAILED', 'APPROVAL_NEEDED', 'DEPLOY_HEALTH_ALERT'],
            throttle_seconds: data.notification_config.throttle_seconds ?? 30,
          });
        }
        // Seed localStorage with server-authoritative task type defaults
        const serverModels = (data.task_type_models ?? {}) as Record<string, string>;
        const serverEfforts = (data.task_type_efforts ?? {}) as Record<string, string>;
        for (const [t, m] of Object.entries(serverModels)) {
          localStorage.setItem(`blueprint-admin:taskTypeModel:${t}`, JSON.stringify(m));
        }
        for (const [t, e] of Object.entries(serverEfforts)) {
          localStorage.setItem(`blueprint-admin:taskTypeEffort:${t}`, JSON.stringify(e));
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

  const connectGitHub = async () => {
    try {
      const redirectUri = window.location.origin;
      const res = await authGet(`${API_BASE_URL}/api/github/oauth/authorize?redirect_uri=${encodeURIComponent(redirectUri)}`);
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

  const updateTaskTypeProviderPreferences = async (value: Record<string, ProviderName[]>) => {
    const prev = taskTypeProviderPreference;
    setTaskTypeProviderPreference(value);
    try {
      await patchSettings({ task_type_provider_preference: value });
    } catch (err) {
      console.error('Failed to update provider preference:', err);
      setTaskTypeProviderPreference(prev);
      addToast('Failed to update provider preference', 'error');
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

  const updateTaskTypeExecutionDefaultConfig = async (value: Record<string, string>) => {
    const prev = taskTypeExecutionDefaults;
    setTaskTypeExecutionDefaults(value);
    try {
      await patchSettings({ task_type_execution_defaults: value });
    } catch (err) {
      console.error('Failed to update execution defaults:', err);
      setTaskTypeExecutionDefaults(prev);
      addToast('Failed to update execution defaults', 'error');
    }
  };

  const updateCrossProviderFallback = async (value: boolean) => {
    const prev = crossProviderFallbackEnabled;
    setCrossProviderFallbackEnabled(value);
    try {
      await patchSettings({ cross_provider_fallback_enabled: value });
    } catch (err) {
      console.error('Failed to update fallback setting:', err);
      setCrossProviderFallbackEnabled(prev);
      addToast('Failed to update cross-provider fallback', 'error');
    }
  };

  const syncTaskTypeDefaults = async () => {
    const types = ['create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor', 'security_audit', 'qa', 'deps', 'docs', 'smoketest', 'debug', 'perf', 'investigate', 'rollup', 'strategic_review', 'design_doc_update'];
    const models: Record<string, string> = {};
    const efforts: Record<string, string> = {};
    for (const t of types) {
      const m = JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeModel:${t}`) ?? 'null') ?? '';
      const e = JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeEffort:${t}`) ?? 'null') ?? '';
      if (m) models[t] = m;
      if (e) efforts[t] = e;
    }
    try {
      await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ task_type_models: models, task_type_efforts: efforts }),
      });
    } catch (err) {
      console.error('Failed to sync task type defaults:', err);
    }
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
    setBuildConfig(config);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${projectId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ build_config: config }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch {
      setBuildConfig(prev);
      throw new Error('Failed to save build config');
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
    githubAccount, setGithubAccount,
    requireApprovalGates,
    autoArchiveEnabled, autoArchiveTerminalAfterDays,
    autoArchiveMaxTerminalTasksPerProject, autoArchiveIntervalMinutes,
    notificationConfig,
    providerTierModelMap, taskTypeProviderPreference, taskTypeModelPreferences,
    providerExecutionModes, providerModeCapabilities, taskTypeExecutionDefaults,
    crossProviderFallbackEnabled, providers,
    buildConfig, setBuildConfig,
    // Handlers
    fetchSettings,
    addMaxPlanAccount, removeMaxPlanAccount, toggleMaxPlanAccount, resetAccountRateLimits,
    connectGitHub,
    updateAgentPromptSuffixes,
    updateProviderTierModels, updateTaskTypeProviderPreferences, updateTaskTypeModelPreferenceConfig,
    updateTaskTypeExecutionDefaultConfig, updateCrossProviderFallback,
    syncTaskTypeDefaults,
    updateMaxVMs, updateMaxRetries,
    handleUpdateMaxAutonomousRate, handleUpdateAutonomousResearchInterval,
    handleUpdateDirectionCooldownMinutes, handleUpdateStrategicResearchFrequency,
    updateHealthCheckUrls, updateRequireApprovalGates,
    updateAutoArchiveEnabled, updateAutoArchiveTerminalAfterDays,
    updateAutoArchiveMaxTerminalTasksPerProject, updateAutoArchiveIntervalMinutes,
    updateNotificationConfig,
    updateBuildConfig,
  };
}
