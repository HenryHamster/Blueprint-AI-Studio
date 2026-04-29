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
  const [configMaxTotalResearch, setConfigMaxTotalResearch] = useState<number>(5);
  const [configMaxTasksPerHour, setConfigMaxTasksPerHour] = useState<number>(10);
  const [configMaxConcurrentAutoRemediations, setConfigMaxConcurrentAutoRemediations] = useState<number>(2);
  const [configResidualRemediationCooldownHours, setConfigResidualRemediationCooldownHours] = useState<number>(24);
  const [configMaxActiveDirections, setConfigMaxActiveDirections] = useState<number>(10);
  const [configArchitectureReviewFrequency, setConfigArchitectureReviewFrequency] = useState<number>(3);
  const [configMaxProvisionRetries, setConfigMaxProvisionRetries] = useState<number>(3);
  const [configMaxRollupRetries, setConfigMaxRollupRetries] = useState<number>(5);
  const [configDriftOverClaimMinClaims, setConfigDriftOverClaimMinClaims] = useState<number>(3);
  const [configDriftConsistencyThreshold, setConfigDriftConsistencyThreshold] = useState<number>(0.4);
  const [configDriftStallMinClaims, setConfigDriftStallMinClaims] = useState<number>(5);
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
      if (data.max_total_research_per_project != null) setConfigMaxTotalResearch(data.max_total_research_per_project);
      if (data.max_autonomous_tasks_per_hour != null) setConfigMaxTasksPerHour(data.max_autonomous_tasks_per_hour);
      if (data.max_concurrent_auto_remediations != null) setConfigMaxConcurrentAutoRemediations(data.max_concurrent_auto_remediations);
      if (data.residual_remediation_cooldown_hours != null) setConfigResidualRemediationCooldownHours(data.residual_remediation_cooldown_hours);
      if (data.max_active_directions != null) setConfigMaxActiveDirections(data.max_active_directions);
      if (data.architecture_review_frequency != null) setConfigArchitectureReviewFrequency(data.architecture_review_frequency);
      if (data.max_provision_retries != null) setConfigMaxProvisionRetries(data.max_provision_retries);
      if (data.max_rollup_retries != null) setConfigMaxRollupRetries(data.max_rollup_retries);
      if (data.drift_over_claim_min_claims != null) setConfigDriftOverClaimMinClaims(data.drift_over_claim_min_claims);
      if (data.drift_over_claim_consistency_threshold != null) setConfigDriftConsistencyThreshold(data.drift_over_claim_consistency_threshold);
      if (data.drift_stall_min_claims != null) setConfigDriftStallMinClaims(data.drift_stall_min_claims);
    } catch { /* ignore */ }
  }

  async function clearResidualFingerprints(): Promise<number | null> {
    setError(null);
    try {
      const h = await authHeaders();
      const res = await fetch(`${API_BASE_URL}/api/admin/residual-fingerprints/clear`, {
        method: 'POST', headers: h,
      });
      if (!res.ok) throw new Error('Failed to clear fingerprints');
      const data = await res.json();
      const cleared = typeof data.cleared === 'number' ? data.cleared : 0;
      addToast(`Cleared ${cleared} residual fingerprint${cleared === 1 ? '' : 's'}.`, 'success');
      return cleared;
    } catch {
      setError('Failed to clear residual fingerprints.');
      addToast('Failed to clear fingerprints', 'error');
      return null;
    }
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
    configMaxTotalResearch, setConfigMaxTotalResearch,
    configMaxTasksPerHour, setConfigMaxTasksPerHour,
    configMaxConcurrentAutoRemediations, setConfigMaxConcurrentAutoRemediations,
    configResidualRemediationCooldownHours, setConfigResidualRemediationCooldownHours,
    configMaxActiveDirections, setConfigMaxActiveDirections,
    configArchitectureReviewFrequency, setConfigArchitectureReviewFrequency,
    configMaxProvisionRetries, setConfigMaxProvisionRetries,
    configMaxRollupRetries, setConfigMaxRollupRetries,
    configDriftOverClaimMinClaims, setConfigDriftOverClaimMinClaims,
    configDriftConsistencyThreshold, setConfigDriftConsistencyThreshold,
    configDriftStallMinClaims, setConfigDriftStallMinClaims,
    autonomousConfigOpen, setAutonomousConfigOpen,
    savingConfigField,
    cooldownMinutes,
    fetchCooldownSetting,
    fetchSettings,
    saveConfigField,
    clearResidualFingerprints,
  };
}
