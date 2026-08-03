import { useState } from 'react';
import {
  apiFetch,
  fetchFleetUtilization,
  fetchAgentServingShas,
  type AgentServingShasResponse,
  type PromptFingerprintAnalyticsResponse,
} from '../api';
import { usePoller, DEFAULT_POLL_MS } from './usePoller';
import type { MediumScopeMeasurement } from '../components/MediumScopeWidget';
import type { ScopeBaselinesResponse } from '../components/ScopeTrendsSection';
import type { AccountHealthEntry } from '../components/PoolAccountHealthPanel';
import type { SystemMaturityRow } from '../components/SystemMaturityCard';
import type { BlindSpotsData, DirectionSrBreakdownData } from '../components/SrDiagnosticsStack';
import type { SrMissedTask } from '../components/SrTrendBreakdown';
import type { TaskDurationStats } from '../components/TaskDurationWidget';
import type { HealthOverviewResponse } from '../components/SystemHealthCard';
import type { FleetUtilizationResponse } from '../components/FleetCapacityCard';

export interface UseProjectMetricsResult {
  measurementData: MediumScopeMeasurement | null;
  scopeBaselines: ScopeBaselinesResponse | null;
  accountHealth: AccountHealthEntry[] | null;
  accountHealthServedStale: boolean;
  accountHealthGeneratedAt: string | null;
  systemMaturity: SystemMaturityRow[] | null;
  blindSpots: BlindSpotsData | null;
  srRecentMisses: SrMissedTask[] | null;
  srRecentMissesTotal: number;
  directionSrBreakdown: DirectionSrBreakdownData | null;
  fingerprintAnalytics: PromptFingerprintAnalyticsResponse | null;
  taskDurationStats: TaskDurationStats | null;
  healthOverview: HealthOverviewResponse | null;
  fleetUtilization: FleetUtilizationResponse | null;
  agentServingShas: AgentServingShasResponse | null;
}

export function useProjectMetrics(
  selectedProjectId: string | null,
  windowHours: number,
  selectedSrCategory: string,
  isAdmin: boolean,
): UseProjectMetricsResult {
  const [measurementData, setMeasurementData] = useState<MediumScopeMeasurement | null>(null);
  const [scopeBaselines, setScopeBaselines] = useState<ScopeBaselinesResponse | null>(null);
  const [accountHealth, setAccountHealth] = useState<AccountHealthEntry[] | null>(null);
  const [accountHealthServedStale, setAccountHealthServedStale] = useState(false);
  const [accountHealthGeneratedAt, setAccountHealthGeneratedAt] = useState<string | null>(null);
  const [systemMaturity, setSystemMaturity] = useState<SystemMaturityRow[] | null>(null);
  const [blindSpots, setBlindSpots] = useState<BlindSpotsData | null>(null);
  const [srRecentMisses, setSrRecentMisses] = useState<SrMissedTask[] | null>(null);
  const [srRecentMissesTotal, setSrRecentMissesTotal] = useState<number>(0);
  const [directionSrBreakdown, setDirectionSrBreakdown] = useState<DirectionSrBreakdownData | null>(null);
  const [fingerprintAnalytics, setFingerprintAnalytics] = useState<PromptFingerprintAnalyticsResponse | null>(null);
  const [taskDurationStats, setTaskDurationStats] = useState<TaskDurationStats | null>(null);
  const [healthOverview, setHealthOverview] = useState<HealthOverviewResponse | null>(null);
  const [fleetUtilization, setFleetUtilization] = useState<FleetUtilizationResponse | null>(null);
  const [agentServingShas, setAgentServingShas] = useState<AgentServingShasResponse | null>(null);

  // Reset all metric state synchronously during render when the scope changes so
  // stale data from the previous selection never bleeds into the new view
  // (React "adjust state during rendering" pattern —
  // https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes).
  const scopeKey = `${selectedProjectId}:${windowHours}:${selectedSrCategory}:${isAdmin}`;
  const [lastResetKey, setLastResetKey] = useState(scopeKey);
  if (lastResetKey !== scopeKey) {
    setLastResetKey(scopeKey);
    setMeasurementData(null);
    setScopeBaselines(null);
    setAccountHealth(null);
    setAccountHealthServedStale(false);
    setAccountHealthGeneratedAt(null);
    setSystemMaturity(null);
    setBlindSpots(null);
    setSrRecentMisses(null);
    setSrRecentMissesTotal(0);
    setDirectionSrBreakdown(null);
    setFingerprintAnalytics(null);
    setTaskDurationStats(null);
    setHealthOverview(null);
    setFleetUtilization(null);
    setAgentServingShas(null);
  }

  usePoller(async (signal) => {
    const fetches: Promise<void>[] = [];

    if (selectedProjectId) {
      fetches.push(
        (async () => {
          if (!isAdmin) return;
          try {
            const res = await apiFetch(`/api/admin/medium-scope-measurement?project_id=${encodeURIComponent(selectedProjectId)}&lookback_days=90`);
            if (!signal.aborted && res.ok) setMeasurementData(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          if (!isAdmin) return;
          try {
            const res = await apiFetch(`/api/admin/scope-baselines?project_id=${encodeURIComponent(selectedProjectId)}&lookback_days=30`);
            if (!signal.aborted && res.ok) setScopeBaselines(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          if (!isAdmin) return;
          try {
            const res = await apiFetch(`/api/admin/max-plan-accounts/health`);
            if (!signal.aborted && res.ok) {
              const data = await res.json();
              setAccountHealth(data.accounts ?? []);
              setAccountHealthServedStale(data.served_stale === true);
              setAccountHealthGeneratedAt(typeof data.generated_at === 'string' ? data.generated_at : null);
            }
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/system-maturity`);
            if (!signal.aborted && res.ok) {
              const data = await res.json();
              setSystemMaturity(Array.isArray(data) ? data : []);
            }
          } catch { /* silent */ }
        })(),
        (async () => {
          if (!isAdmin) return;
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-blind-spots`);
            if (!signal.aborted && res.ok) setBlindSpots(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          if (!isAdmin) return;
          try {
            const params = new URLSearchParams();
            if (selectedSrCategory) params.set('category', selectedSrCategory);
            const query = params.toString();
            const url = `/api/admin/projects/${encodeURIComponent(selectedProjectId)}/sr-recent-misses${query ? `?${query}` : ''}`;
            const res = await apiFetch(url);
            if (!signal.aborted && res.ok) {
              const data = await res.json();
              setSrRecentMisses(data.misses ?? []);
              setSrRecentMissesTotal(data.total ?? (data.misses ?? []).length);
            }
          } catch { /* silent */ }
        })(),
        (async () => {
          if (!isAdmin) return;
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/direction-sr-breakdown`);
            if (!signal.aborted && res.ok) setDirectionSrBreakdown(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/prompt-fingerprint-analytics`);
            if (!signal.aborted && res.ok) setFingerprintAnalytics(await res.json());
          } catch { /* silent */ }
        })(),
        (async () => {
          try {
            const data = await fetchAgentServingShas(selectedProjectId);
            if (!signal.aborted) setAgentServingShas(data);
          } catch {
            if (!signal.aborted) setAgentServingShas(null);
          }
        })(),
      );
    }

    fetches.push(
      (async () => {
        if (!isAdmin) return;
        try {
          const url = selectedProjectId
            ? `/api/admin/task-duration-stats?project_id=${encodeURIComponent(selectedProjectId)}`
            : `/api/admin/task-duration-stats`;
          const res = await apiFetch(url);
          if (!signal.aborted && res.ok) setTaskDurationStats(await res.json());
        } catch { /* silent */ }
      })(),
      (async () => {
        if (!isAdmin) return;
        try {
          const url = selectedProjectId
            ? `/api/admin/health-overview?project_id=${encodeURIComponent(selectedProjectId)}&lookback_hours=${windowHours}`
            : `/api/admin/health-overview?lookback_hours=${windowHours}`;
          const res = await apiFetch(url);
          if (!signal.aborted) {
            if (res.ok) setHealthOverview(await res.json());
            else setHealthOverview(null);
          }
        } catch {
          if (!signal.aborted) setHealthOverview(null);
        }
      })(),
      (async () => {
        if (!isAdmin) return;
        try {
          const data = await fetchFleetUtilization();
          if (!signal.aborted) setFleetUtilization(data);
        } catch {
          if (!signal.aborted) setFleetUtilization(null);
        }
      })(),
    );

    await Promise.allSettled(fetches);
  }, { intervalMs: DEFAULT_POLL_MS, deps: [selectedProjectId, windowHours, selectedSrCategory, isAdmin] });

  return {
    measurementData,
    scopeBaselines,
    accountHealth,
    accountHealthServedStale,
    accountHealthGeneratedAt,
    systemMaturity,
    blindSpots,
    srRecentMisses,
    srRecentMissesTotal,
    directionSrBreakdown,
    fingerprintAnalytics,
    taskDurationStats,
    healthOverview,
    fleetUtilization,
    agentServingShas,
  };
}
