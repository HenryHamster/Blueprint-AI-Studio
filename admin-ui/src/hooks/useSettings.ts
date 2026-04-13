import { useState } from 'react';
import type { MaxPlanAccount } from '../types/settings';
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
  const [newAccountToken, setNewAccountToken] = useState('');
  const [githubAccount, setGithubAccount] = useState<string | null>(null);
  const [requireApprovalGates, setRequireApprovalGates] = useState(true);

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
        setGithubAccount(data.github_account || null);
        setRequireApprovalGates(data.require_approval_gates ?? true);
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

  const addMaxPlanAccount = async () => {
    if (!newAccountName || !newAccountToken) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/max-plan-accounts`, {
        method: 'POST', headers: await authHeaders(),
        body: JSON.stringify({ name: newAccountName, oauth_token: newAccountToken }),
      });
      if (res.ok) {
        setNewAccountName('');
        setNewAccountToken('');
        fetchSettings();
      } else {
        addToast('Failed to add account', 'error');
      }
    } catch (err) { console.error('Failed to add account:', err); addToast('Failed to add account', 'error'); }
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

  const syncTaskTypeDefaults = async () => {
    const types = ['create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor', 'security_audit', 'qa', 'deps'];
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
    const clamped = Math.max(1, Math.min(100, value));
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

  return {
    // State
    maxConcurrentVMs, maxRetries, maxAutonomousTasksPerHour,
    autonomousResearchIntervalMinutes, directionCooldownMinutes, strategicResearchFrequency,
    healthCheckUrls, agentPromptSuffixes, usingMaxPlan,
    maxPlanAccounts, newAccountName, setNewAccountName, newAccountToken, setNewAccountToken,
    githubAccount, setGithubAccount,
    requireApprovalGates,
    // Handlers
    fetchSettings,
    addMaxPlanAccount, removeMaxPlanAccount, toggleMaxPlanAccount, resetAccountRateLimits,
    connectGitHub,
    updateAgentPromptSuffixes,
    syncTaskTypeDefaults,
    updateMaxVMs, updateMaxRetries,
    handleUpdateMaxAutonomousRate, handleUpdateAutonomousResearchInterval,
    handleUpdateDirectionCooldownMinutes, handleUpdateStrategicResearchFrequency,
    updateHealthCheckUrls, updateRequireApprovalGates,
  };
}
