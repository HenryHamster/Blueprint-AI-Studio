import { useState } from 'react';
import { API_BASE_URL, authHeaders } from '../api';

interface UseAutonomousConfigDeps {
  setError: (msg: string | null) => void;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
}

export function useAutonomousConfig({ setError, addToast }: UseAutonomousConfigDeps) {
  const [configCooldownHours, setConfigCooldownHours] = useState<number>(24);
  const [configIntervalMinutes, setConfigIntervalMinutes] = useState<number>(60);
  const [configMaxParallel, setConfigMaxParallel] = useState<number>(3);
  const [configMaxTasksPerHour, setConfigMaxTasksPerHour] = useState<number>(10);
  const [autonomousConfigOpen, setAutonomousConfigOpen] = useState(false);
  const [savingConfigField, setSavingConfigField] = useState<string | null>(null);
  const [cooldownMinutes, setCooldownMinutes] = useState<number>(240);

  async function fetchCooldownSetting() {
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, { headers: h });
      if (!res.ok) return;
      const data = await res.json();
      const val = data.autonomous_direction_cooldown_minutes;
      if (typeof val === 'number' && val > 0) setCooldownMinutes(val);
    } catch (err) {
      console.error('Failed to fetch cooldown setting:', err);
      addToast('Failed to load cooldown settings', 'error');
    }
  }

  async function fetchSettings() {
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, { headers: h });
      if (!res.ok) return;
      const data = await res.json();
      if (data.autonomous_direction_cooldown_minutes != null) setConfigCooldownHours(data.autonomous_direction_cooldown_minutes / 60);
      if (data.autonomous_research_interval_minutes != null) setConfigIntervalMinutes(data.autonomous_research_interval_minutes);
      if (data.max_parallel_research_per_project != null) setConfigMaxParallel(data.max_parallel_research_per_project);
      if (data.max_autonomous_tasks_per_hour != null) setConfigMaxTasksPerHour(data.max_autonomous_tasks_per_hour);
    } catch { /* ignore */ }
  }

  async function saveConfigField(field: string, value: number) {
    setSavingConfigField(field);
    setError(null);
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/settings`, {
        method: 'PATCH', headers: h,
        body: JSON.stringify({ [field]: value }),
      });
      if (!res.ok) throw new Error('Failed to save setting');
    } catch {
      setError('Failed to save setting. Please try again.');
    } finally {
      setSavingConfigField(null);
    }
  }

  return {
    configCooldownHours, setConfigCooldownHours,
    configIntervalMinutes, setConfigIntervalMinutes,
    configMaxParallel, setConfigMaxParallel,
    configMaxTasksPerHour, setConfigMaxTasksPerHour,
    autonomousConfigOpen, setAutonomousConfigOpen,
    savingConfigField,
    cooldownMinutes,
    fetchCooldownSetting,
    fetchSettings,
    saveConfigField,
  };
}
