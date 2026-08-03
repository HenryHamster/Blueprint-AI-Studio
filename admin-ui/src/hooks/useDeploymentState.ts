/* eslint-disable react-hooks/set-state-in-effect */
import { useState, useEffect, useCallback, useLayoutEffect, useRef } from 'react';
import type { User } from 'firebase/auth';
import type { DeployState, OrchestratorHealth, ProviderApisHealth, FailedRollbackInfo } from '../types/settings';
import type { ProjectDiagnostics } from '../types/diagnostics';
import { API_BASE_URL, authHeaders, authGet } from '../api';
import { usePoller } from './usePoller';

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
  pollerEnabled = true,
  access: { isPlatformAdmin?: boolean; canViewProject?: boolean } = {},
) {
  const isPlatformAdmin = access.isPlatformAdmin === true;
  const canViewProject = access.canViewProject === true;
  const globalScopeRef = useRef({ generation: 0, allowed: false });
  const diagnosticsScopeRef = useRef({ generation: 0, projectId: null as string | null, allowed: false });
  useLayoutEffect(() => {
    globalScopeRef.current = {
      generation: globalScopeRef.current.generation + 1,
      allowed: Boolean(user) && isPlatformAdmin,
    };
  }, [isPlatformAdmin, user]);
  useLayoutEffect(() => {
    diagnosticsScopeRef.current = {
      generation: diagnosticsScopeRef.current.generation + 1,
      projectId: selectedProjectId,
      allowed: Boolean(user) && Boolean(selectedProjectId) && canViewProject,
    };
  }, [canViewProject, selectedProjectId, user]);

  const globalResponseIsCurrent = useCallback((generation: number) => (
    globalScopeRef.current.allowed && globalScopeRef.current.generation === generation
  ), []);
  const diagnosticsResponseIsCurrent = useCallback((generation: number, projectId: string) => (
    diagnosticsScopeRef.current.allowed
    && diagnosticsScopeRef.current.generation === generation
    && diagnosticsScopeRef.current.projectId === projectId
  ), []);
  // --- Deploy State ---
  const [deployState, setDeployState] = useState<DeployState | null>(null);
  const setDeployStateIfAllowed = useCallback((next: DeployState) => {
    if (globalScopeRef.current.allowed) setDeployState(next);
  }, []);

  const fetchDeployState = useCallback(async (silent = false) => {
    if (!isPlatformAdmin || !globalScopeRef.current.allowed) return;
    const generation = globalScopeRef.current.generation;
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/deploy-state`);
      if (res.ok) {
        const data = await res.json();
        if (globalResponseIsCurrent(generation)) setDeployState(data);
      }
    } catch (err) {
      if (!globalResponseIsCurrent(generation)) return;
      console.error('Failed to fetch deploy state:', err);
      if (!silent) addToast('Failed to load deploy state', 'error');
    }
  }, [addToast, globalResponseIsCurrent, isPlatformAdmin]);

  const handlePausePipeline = async () => {
    if (!globalScopeRef.current.allowed) return;
    const generation = globalScopeRef.current.generation;
    try {
      const headers = await authHeaders();
      if (!globalResponseIsCurrent(generation)) return;
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/pause`, {
        method: 'POST', headers,
      });
      if (!globalResponseIsCurrent(generation)) return;
      if (!res.ok) throw new Error(res.statusText);
      addToast('Pipeline paused', 'success');
      fetchDeployState();
    } catch {
      if (globalResponseIsCurrent(generation)) addToast('Failed to pause pipeline', 'error');
    }
  };

  const handleResumePipeline = async () => {
    if (!globalScopeRef.current.allowed) return;
    const generation = globalScopeRef.current.generation;
    try {
      const headers = await authHeaders();
      if (!globalResponseIsCurrent(generation)) return;
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/resume`, {
        method: 'POST', headers,
      });
      if (!globalResponseIsCurrent(generation)) return;
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        if (!globalResponseIsCurrent(generation)) return;
        addToast(data.detail || 'Failed to resume pipeline', 'error');
        return;
      }
      addToast('Pipeline resumed', 'success');
      fetchDeployState();
      fetchTasksRef.current();
    } catch {
      if (globalResponseIsCurrent(generation)) addToast('Failed to resume pipeline', 'error');
    }
  };

  const handleResetHealth = async () => {
    if (!globalScopeRef.current.allowed) return;
    const generation = globalScopeRef.current.generation;
    try {
      const headers = await authHeaders();
      if (!globalResponseIsCurrent(generation)) return;
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/reset-health`, {
        method: 'POST', headers,
      });
      if (!globalResponseIsCurrent(generation)) return;
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        if (!globalResponseIsCurrent(generation)) return;
        addToast(data.detail || 'Failed to reset health', 'error');
        return;
      }
      const data = await res.json().catch(() => null);
      if (!globalResponseIsCurrent(generation)) return;
      addToast('Health reset — pipeline resumed', 'success');
      if (data) setDeployState(data);
      fetchTasksRef.current();
    } catch {
      if (globalResponseIsCurrent(generation)) addToast('Failed to reset health', 'error');
    }
  };

  // In-flight guard: rollback is not free to repeat — a genuine double-submit
  // (double-click) would double-count toward the consecutive-rollback HALT
  // threshold. The ref blocks the second concurrent call before it issues a
  // request; the state mirror drives button-disable in the UI.
  const rollbackInFlight = useRef(false);
  const [isRollingBack, setIsRollingBack] = useState(false);

  // Durable, standing failed-rollback affordance. The error toast fired below
  // auto-dismisses after a few seconds, after which the deploy surface would
  // otherwise look indistinguishable from a successful rollback (false success
  // by absence). This state outlives the toast so the operator always sees that
  // the last rollback failed and traffic still serves the bad revision. It is
  // cleared on the operator's next resolving action (a successful rollback, a
  // mark-stable, or any deploy-state status change away from the at-failure
  // status — see the useEffect below).
  const [failedRollback, setFailedRollback] = useState<FailedRollbackInfo | null>(null);
  // The deploy-state status captured at the moment of the failed rollback. Used
  // to auto-clear the affordance once a poll observes the status move away from
  // it (the situation has been resolved some other way).
  const statusAtFailure = useRef<string | null>(null);

  const handleRollback = async () => {
    if (!globalScopeRef.current.allowed) return;
    if (rollbackInFlight.current) return;
    const generation = globalScopeRef.current.generation;
    rollbackInFlight.current = true;
    setIsRollingBack(true);
    try {
      const headers = await authHeaders();
      if (!globalResponseIsCurrent(generation)) return;
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/rollback`, {
        method: 'POST', headers,
      });
      if (!globalResponseIsCurrent(generation)) return;
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        if (!globalResponseIsCurrent(generation)) return;
        addToast(`Rollback failed: ${data.detail || res.statusText}`, 'error');
        return;
      }
      const data = await res.json();
      if (!globalResponseIsCurrent(generation)) return;
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
        // Promote the ephemeral toast into standing state so the affordance
        // survives past the toast's auto-dismiss. Capture the current deploy
        // status so the auto-clear effect can detect a later status change.
        setFailedRollback({
          serving_sha: data.serving_sha ?? null,
          gcloud_error: data.gcloud_error ?? null,
        });
        statusAtFailure.current = deployState?.status ?? null;
        fetchDeployState();
        return;
      }
      // A real traffic shift happened — clear any prior failed-rollback signal.
      setFailedRollback(null);
      statusAtFailure.current = null;
      addToast(`Rolled back: ${data.from_sha?.slice(0, 8)} → ${data.to_sha?.slice(0, 8)}`, 'success');
      fetchDeployState();
    } catch {
      if (globalResponseIsCurrent(generation)) addToast('Rollback failed', 'error');
    } finally {
      rollbackInFlight.current = false;
      setIsRollingBack(false);
    }
  };

  const handleMarkStable = async () => {
    if (!globalScopeRef.current.allowed) return;
    const generation = globalScopeRef.current.generation;
    try {
      const headers = await authHeaders();
      if (!globalResponseIsCurrent(generation)) return;
      const res = await fetch(`${API_BASE_URL}/api/admin/deploy-state/mark-stable`, {
        method: 'POST', headers,
      });
      if (!globalResponseIsCurrent(generation)) return;
      if (!res.ok) throw new Error(res.statusText);
      // Marking stable is one of the operator's resolving actions — drop the
      // standing failed-rollback affordance.
      setFailedRollback(null);
      statusAtFailure.current = null;
      addToast('Deploy marked stable', 'success');
      fetchDeployState();
    } catch {
      if (globalResponseIsCurrent(generation)) addToast('Failed to mark stable', 'error');
    }
  };

  const handleRepairState = async () => {
    if (!globalScopeRef.current.allowed) return;
    const generation = globalScopeRef.current.generation;
    try {
      const headers = await authHeaders();
      if (!globalResponseIsCurrent(generation)) return;
      const res = await fetch(`${API_BASE_URL}/api/admin/repair-state`, {
        method: 'POST', headers,
      });
      if (!globalResponseIsCurrent(generation)) return;
      if (!res.ok) throw new Error(res.statusText);
      const data = await res.json();
      if (!globalResponseIsCurrent(generation)) return;
      addToast(`Repaired ${data.count} issue${data.count !== 1 ? 's' : ''}`, 'success');
      fetchTasksRef.current();
    } catch {
      if (globalResponseIsCurrent(generation)) addToast('State repair failed', 'error');
    }
  };

  // Fetch deploy state on mount and periodically. Leading call uses the
  // non-silent variant (shows loading); subsequent shared-clock ticks are
  // silent. Pauses when the tab is hidden, catches up on return.
  useEffect(() => {
    if (!user || !pollerEnabled || !isPlatformAdmin) return;
    fetchDeployState();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user, pollerEnabled, isPlatformAdmin]);
  usePoller(() => { fetchDeployState(true); }, { intervalMs: 60_000, enabled: !!user && pollerEnabled && isPlatformAdmin, leading: false, deps: [user, pollerEnabled, isPlatformAdmin] });

  useEffect(() => {
    if (!isPlatformAdmin) setDeployState(null);
  }, [isPlatformAdmin]);

  // Auto-clear the failed-rollback affordance once the deploy-state status
  // moves away from what it was when the rollback failed. A successful retry,
  // an auto-rollback, or an external state change all resolve the situation, so
  // the standing signal must not linger as a false alarm.
  useEffect(() => {
    if (!failedRollback) return;
    if (deployState && deployState.status !== statusAtFailure.current) {
      setFailedRollback(null);
      statusAtFailure.current = null;
    }
  }, [deployState, failedRollback]);

  // --- Zone Health ---
  const [zoneHealth, setZoneHealth] = useState<Record<string, { status: string; last_success: string | null; last_failure: string | null; last_failure_reason: string | null; consecutive_failures: number; cooldown_active: boolean }> | null>(null);

  const fetchZoneHealth = async () => {
    if (!globalScopeRef.current.allowed) return;
    const generation = globalScopeRef.current.generation;
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/zone-health`);
      if (res.ok) {
        const data = await res.json();
        if (globalResponseIsCurrent(generation)) setZoneHealth(data);
      }
    } catch { /* ignore */ }
  };

  usePoller(fetchZoneHealth, { intervalMs: 60_000, enabled: !!user && pollerEnabled && isPlatformAdmin, deps: [user, pollerEnabled, isPlatformAdmin] });
  useEffect(() => {
    if (!isPlatformAdmin) setZoneHealth(null);
  }, [isPlatformAdmin]);

  // --- Orchestrator Health ---
  const [orchestratorHealth, setOrchestratorHealth] = useState<OrchestratorHealth | null>(null);

  const fetchOrchestratorHealth = async () => {
    if (!globalScopeRef.current.allowed) return;
    const generation = globalScopeRef.current.generation;
    try {
      const res = await authGet(`${API_BASE_URL}/api/debug/orchestrator`);
      if (res.ok) {
        const data = await res.json();
        if (globalResponseIsCurrent(generation)) setOrchestratorHealth(data);
      } else if (globalResponseIsCurrent(generation)) setOrchestratorHealth(null);
    } catch {
      if (globalResponseIsCurrent(generation)) setOrchestratorHealth(null);
    }
  };

  usePoller(fetchOrchestratorHealth, { intervalMs: 60_000, enabled: !!user && pollerEnabled && isPlatformAdmin, deps: [user, pollerEnabled, isPlatformAdmin] });
  useEffect(() => {
    if (!isPlatformAdmin) setOrchestratorHealth(null);
  }, [isPlatformAdmin]);

  // --- Detailed Health ---
  const [detailedHealth, setDetailedHealth] = useState<DetailedHealth | null>(null);

  const fetchDetailedHealth = async () => {
    if (!globalScopeRef.current.allowed) return;
    const generation = globalScopeRef.current.generation;
    try {
      const res = await authGet(`${API_BASE_URL}/api/health/detailed`);
      if (res.ok) {
        const data = await res.json();
        if (!globalResponseIsCurrent(generation)) return;
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
      } else if (globalResponseIsCurrent(generation)) {
        setDetailedHealth(null);
      }
    } catch {
      if (globalResponseIsCurrent(generation)) setDetailedHealth(null);
    }
  };

  usePoller(fetchDetailedHealth, { intervalMs: 60_000, enabled: !!user && pollerEnabled && isPlatformAdmin, deps: [user, pollerEnabled, isPlatformAdmin] });
  useEffect(() => {
    if (!isPlatformAdmin) setDetailedHealth(null);
  }, [isPlatformAdmin]);

  // --- Diagnostics ---
  const [diagnostics, setDiagnostics] = useState<ProjectDiagnostics | null>(null);

  const fetchDiagnostics = useCallback(async () => {
    if (!selectedProjectId || !canViewProject || !diagnosticsScopeRef.current.allowed) { setDiagnostics(null); return; }
    const projectId = selectedProjectId;
    const generation = diagnosticsScopeRef.current.generation;
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(projectId)}/diagnostics`);
      if (res.ok) {
        const data = await res.json();
        if (diagnosticsResponseIsCurrent(generation, projectId)) setDiagnostics(data);
      } else if (diagnosticsResponseIsCurrent(generation, projectId)) setDiagnostics(null);
    } catch {
      if (diagnosticsResponseIsCurrent(generation, projectId)) setDiagnostics(null);
    }
  }, [selectedProjectId, canViewProject, diagnosticsResponseIsCurrent]);

  useEffect(() => {
    if (!user || !selectedProjectId || !canViewProject) setDiagnostics(null);
  }, [user, selectedProjectId, canViewProject]);
  usePoller(fetchDiagnostics, {
    intervalMs: 60_000,
    enabled: !!user && !!selectedProjectId && pollerEnabled && canViewProject,
    deps: [user, selectedProjectId, fetchDiagnostics, pollerEnabled, canViewProject],
  });

  return {
    deployState, setDeployState: setDeployStateIfAllowed,
    handlePausePipeline, handleResumePipeline, handleResetHealth,
    handleRollback, isRollingBack, handleMarkStable, handleRepairState,
    failedRollback,
    zoneHealth,
    orchestratorHealth,
    detailedHealth,
    diagnostics,
  };
}
