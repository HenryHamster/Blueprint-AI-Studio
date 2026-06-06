import { useState, useEffect, useCallback, useRef } from 'react';
import type { User } from 'firebase/auth';
import type { DeployState, OrchestratorHealth, ProviderApisHealth } from '../types/settings';
import type { ProjectDiagnostics } from '../types/diagnostics';
import { API_BASE_URL, authHeaders, authGet } from '../api';
import { usePoller, DEFAULT_POLL_MS } from './usePoller';

interface CircuitBreakerState {
  state: 'CLOSED' | 'OPEN' | 'HALF_OPEN';
  failure_count: number;
  opened_at: string | null;
}

interface DetailedHealth {
  circuit_breaker: { projects: Record<string, CircuitBreakerState> };
  max_plan_pool: { total: number; available: number; by_provider?: Record<string, { total: number; available: number }> };
  autonomous: { enabled: boolean; consecutive_failures: number };
  provider_apis: ProviderApisHealth;
  github_quota: {
    remaining: number | null;
    limit: number | null;
    reset_at: number | null;
    last_updated: string | null;
    status: 'ok' | 'degraded' | 'critical' | 'unknown';
  };
}

export function useDeploymentState(
  addToast: (msg: string, type: 'success' | 'error') => void,
  user: User | null,
  fetchTasksRef: React.MutableRefObject<() => void>,
  selectedProjectId: string | null = null,
) {
  // --- Deploy State ---
  const [deployState, setDeployState] = useState<DeployState | null>(null);

  const fetchDeployState = useCallback(async (silent = false) => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/deploy-state`);
      if (res.ok) setDeployState(await res.json());
    } catch (err) {
      console.error('Failed to fetch deploy state:', err);
      if (!silent) addToast('Failed to load deploy state', 'error');
    }
  }, [addToast]);

  const handlePausePipeline = async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/pause`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(res.statusText);
      addToast('Pipeline paused', 'success');
      fetchDeployState();
    } catch {
      addToast('Failed to pause pipeline', 'error');
    }
  };

  const handleResumePipeline = async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/resume`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        addToast(data.detail || 'Failed to resume pipeline', 'error');
        return;
      }
      addToast('Pipeline resumed', 'success');
      fetchDeployState();
      fetchTasksRef.current();
    } catch {
      addToast('Failed to resume pipeline', 'error');
    }
  };

  const handleResetHealth = async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/reset-health`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        addToast(data.detail || 'Failed to reset health', 'error');
        return;
      }
      addToast('Health reset — pipeline resumed', 'success');
      const data = await res.json().catch(() => null);
      if (data) setDeployState(data);
      fetchTasksRef.current();
    } catch {
      addToast('Failed to reset health', 'error');
    }
  };

  // In-flight guard: rollback is not free to repeat — a genuine double-submit
  // (double-click) would double-count toward the consecutive-rollback HALT
  // threshold. The ref blocks the second concurrent call before it issues a
  // request; the state mirror drives button-disable in the UI.
  const rollbackInFlight = useRef(false);
  const [isRollingBack, setIsRollingBack] = useState(false);

  const handleRollback = async () => {
    if (rollbackInFlight.current) return;
    rollbackInFlight.current = true;
    setIsRollingBack(true);
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/rollback`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        addToast(`Rollback failed: ${data.detail || res.statusText}`, 'error');
        return;
      }
      const data = await res.json();
      // The backend returns a truthful HTTP 200 with status='rollback_failed'
      // when the Cloud Run traffic shift never happened (it deliberately avoids a
      // 5xx, which would quarantine the serving instance). Treat that body as a
      // retryable failure — NOT a completed rollback — so the operator knows
      // traffic still serves the bad revision and can retry. Still refresh deploy
      // state so the UI reflects the untouched (STABLE) status.
      if (data.status === 'rollback_failed') {
        const servingSha = data.serving_sha?.slice(0, 8) ?? 'current revision';
        const detail = data.gcloud_error ? ` (${data.gcloud_error})` : '';
        addToast(`Rollback failed — traffic still serves ${servingSha}; retry${detail}`, 'error');
        fetchDeployState();
        return;
      }
      addToast(`Rolled back: ${data.from_sha?.slice(0, 8)} → ${data.to_sha?.slice(0, 8)}`, 'success');
      fetchDeployState();
    } catch {
      addToast('Rollback failed', 'error');
    } finally {
      rollbackInFlight.current = false;
      setIsRollingBack(false);
    }
  };

  const handleMarkStable = async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/mark-stable`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(res.statusText);
      addToast('Deploy marked stable', 'success');
      fetchDeployState();
    } catch {
      addToast('Failed to mark stable', 'error');
    }
  };

  const handleRepairState = async () => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/repair-state`, {
        method: 'POST', headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(res.statusText);
      const data = await res.json();
      addToast(`Repaired ${data.count} issue${data.count !== 1 ? 's' : ''}`, 'success');
      fetchTasksRef.current();
    } catch {
      addToast('State repair failed', 'error');
    }
  };

  // Fetch deploy state on mount and periodically. Leading call uses the
  // non-silent variant (shows loading); subsequent shared-clock ticks are
  // silent. Pauses when the tab is hidden, catches up on return.
  useEffect(() => {
    if (!user) return;
    fetchDeployState();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user]);
  usePoller(() => { fetchDeployState(true); }, { intervalMs: 60_000, enabled: !!user, leading: false, deps: [user] });

  // --- Zone Health ---
  const [zoneHealth, setZoneHealth] = useState<Record<string, { status: string; last_success: string | null; last_failure: string | null; last_failure_reason: string | null; consecutive_failures: number; cooldown_active: boolean }> | null>(null);

  const fetchZoneHealth = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/zone-health`);
      if (res.ok) setZoneHealth(await res.json());
    } catch { /* ignore */ }
  };

  usePoller(fetchZoneHealth, { intervalMs: DEFAULT_POLL_MS, enabled: !!user, deps: [user] });

  // --- Orchestrator Health ---
  const [orchestratorHealth, setOrchestratorHealth] = useState<OrchestratorHealth | null>(null);

  const fetchOrchestratorHealth = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/debug/orchestrator`);
      if (res.ok) setOrchestratorHealth(await res.json());
      else setOrchestratorHealth(null);
    } catch { setOrchestratorHealth(null); }
  };

  usePoller(fetchOrchestratorHealth, { intervalMs: DEFAULT_POLL_MS, enabled: !!user, deps: [user] });

  // --- Detailed Health ---
  const [detailedHealth, setDetailedHealth] = useState<DetailedHealth | null>(null);

  const fetchDetailedHealth = async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/health/detailed`);
      if (res.ok) {
        const data = await res.json();
        setDetailedHealth({
          circuit_breaker: data.components?.circuit_breaker ?? { projects: {} },
          max_plan_pool: data.components?.max_plan_pool ?? { total: 0, available: 0, by_provider: {} },
          autonomous: data.components?.autonomous ?? { enabled: false, consecutive_failures: 0 },
          provider_apis: data.components?.provider_apis ?? {
            status: 'unknown',
            fallback_enabled: false,
            required_providers: [],
            available_providers: [],
            degraded_providers: [],
            down_providers: [],
            providers: {},
          },
          github_quota: data.components?.github_quota ?? {
            remaining: null, limit: null, reset_at: null, last_updated: null, status: 'unknown',
          },
        });
      } else {
        setDetailedHealth(null);
      }
    } catch { setDetailedHealth(null); }
  };

  usePoller(fetchDetailedHealth, { intervalMs: DEFAULT_POLL_MS, enabled: !!user, deps: [user] });

  // --- Diagnostics ---
  const [diagnostics, setDiagnostics] = useState<ProjectDiagnostics | null>(null);

  const fetchDiagnostics = useCallback(async () => {
    if (!selectedProjectId) { setDiagnostics(null); return; }
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(selectedProjectId)}/diagnostics`);
      if (res.ok) setDiagnostics(await res.json());
      else setDiagnostics(null);
    } catch { setDiagnostics(null); }
  }, [selectedProjectId]);

  useEffect(() => {
    if (!user || !selectedProjectId) setDiagnostics(null);
  }, [user, selectedProjectId]);
  usePoller(fetchDiagnostics, {
    intervalMs: DEFAULT_POLL_MS,
    enabled: !!user && !!selectedProjectId,
    deps: [user, selectedProjectId, fetchDiagnostics],
  });

  return {
    deployState, setDeployState,
    handlePausePipeline, handleResumePipeline, handleResetHealth,
    handleRollback, isRollingBack, handleMarkStable, handleRepairState,
    zoneHealth,
    orchestratorHealth,
    detailedHealth,
    diagnostics,
  };
}
