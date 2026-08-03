import { useState, useRef, useEffect, useCallback } from 'react';
import { apiFetch } from '../api';
import { usePoller, DEFAULT_POLL_MS } from './usePoller';
import { assertDigestContract, type DigestData as DigestDataBase } from '../types/digest';
import type { DirectionProgress } from '../components/DirectionPortfolioSection';

export type DashboardDigestData = DigestDataBase<DirectionProgress>;

export interface SparkPoint {
  active: number;
  queued: number;
  failed: number;
}

export interface UseDashboardDigestResult {
  digest: DashboardDigestData | null;
  digestStale: boolean;
  loading: boolean;
  lastFetchError: boolean;
  sparkData: SparkPoint[];
  refetchDigest: () => void;
}

export function useDashboardDigest(
  selectedProjectId: string | null,
  windowHours: number,
  activeCount: number,
  queuedCount: number,
  enabled: boolean,
): UseDashboardDigestResult {
  const [digest, setDigest] = useState<DashboardDigestData | null>(null);
  const [digestStale, setDigestStale] = useState(false);
  const [loading, setLoading] = useState(enabled);
  const [lastFetchError, setLastFetchError] = useState(false);
  const [sparkData, setSparkData] = useState<SparkPoint[]>([]);
  const [refreshTrigger, setRefreshTrigger] = useState(0);

  // Scope key combines the inputs that define which project/window we are
  // displaying. Used to detect scope changes and invalidate scoped state.
  const scopeKey = `${selectedProjectId}:${windowHours}:${enabled}`;

  // Tracks the scope key for which we last received a successful digest
  // response. A mismatch means "no data yet" for the current scope (project
  // switch), which suppresses the stale-data banner until first success.
  // Stored in a ref so it can be read inside the poller callback without
  // being listed as a closure dep.
  const digestDataScopeRef = useRef<string | null>(null);

  // Accumulates the sparkline buffer for the current scope.
  const sparkRef = useRef<SparkPoint[]>([]);

  // Reset digest/loading/error state synchronously during render when the
  // scope changes — per the React "adjust state during rendering" pattern
  // (https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes).
  // digestDataScopeRef self-invalidates via scope key comparison; sparkRef is
  // reset inside the sparkline effect (safe because it's a ref, not state).
  const [lastResetKey, setLastResetKey] = useState(scopeKey);
  if (lastResetKey !== scopeKey) {
    setLastResetKey(scopeKey);
    setDigest(null);
    setDigestStale(false);
    setLoading(enabled);
    setLastFetchError(false);
    setSparkData([]);
  }

  // Clear the sparkline buffer when the scope changes so the new project starts
  // with a blank chart. Declared before the sparkline effect so it runs first
  // in the same post-commit flush. No setState here — only ref mutations are
  // allowed in effects without triggering the set-state-in-effect lint rule.
  useEffect(() => {
    sparkRef.current = [];
  }, [selectedProjectId, windowHours]);

  // Sparkline sampling — updates whenever task counts or failed count change.
  // Separated from the polling cycle so busy-task bursts don't trigger re-fetches.
  useEffect(() => {
    const failed = digest?.failed_count ?? 0;
    const next = [...sparkRef.current, { active: activeCount, queued: queuedCount, failed }].slice(-60);
    sparkRef.current = next;
    setSparkData(next);
  }, [activeCount, queuedCount, digest?.failed_count]);

  // Incrementing refreshTrigger forces usePoller to re-run its leading call
  // immediately (e.g. after a successful anomaly-throttle restore).
  const refetchDigest = useCallback(() => {
    if (enabled) setRefreshTrigger(n => n + 1);
  }, [enabled]);

  usePoller(async (signal) => {
    let digestOk = false;
    let diagnosticsOk = false;

    const url = selectedProjectId
      ? `/api/admin/digest?hours=${windowHours}&project_id=${encodeURIComponent(selectedProjectId)}`
      : `/api/admin/digest?hours=${windowHours}`;

    // Diagnostics is a background health probe — fire it in parallel but don't
    // gate the loading spinner on it. It can be slow (up to-500-task DB scan)
    // and its only role is to confirm the server is reachable when digest fails.
    let diagnosticsSettled = false;
    const diagnosticsPromise = selectedProjectId
      ? apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/diagnostics`)
          .then(r => { if (!signal.aborted && r.ok) diagnosticsOk = true; })
          .catch(() => { /* silent */ })
          .finally(() => { diagnosticsSettled = true; })
      : Promise.resolve().then(() => { diagnosticsSettled = true; });

    try {
      const res = await apiFetch(url);
      if (signal.aborted) return;
      if (res.ok) {
        const data = await res.json();
        if (!signal.aborted && assertDigestContract<DirectionProgress>(data)) {
          setDigest(data);
          setDigestStale(false);
          digestDataScopeRef.current = scopeKey;
          digestOk = true;
        } else if (!signal.aborted) {
          setDigestStale(true);
        }
      } else if (!signal.aborted) {
        setDigestStale(true);
      }
    } catch {
      if (!signal.aborted) setDigestStale(true);
    }

    if (signal.aborted) return;

    // Unblock the loading spinner as soon as the digest response is done —
    // don't wait for diagnostics, which runs purely as an error-detection probe.
    setLoading(false);

    // Determine error state. If diagnostics hasn't settled yet, check it only
    // once it completes (non-blocking from the operator's perspective).
    const checkError = () => {
      if (signal.aborted) return;
      const hasDataForCurrentScope = digestDataScopeRef.current === scopeKey;
      const allFailed = selectedProjectId
        ? !digestOk && !diagnosticsOk && !hasDataForCurrentScope
        : !digestOk && !hasDataForCurrentScope;
      setLastFetchError(allFailed);
    };

    if (diagnosticsSettled) {
      checkError();
    } else {
      diagnosticsPromise.then(checkError);
    }
  }, { intervalMs: DEFAULT_POLL_MS, enabled, deps: [selectedProjectId, windowHours, refreshTrigger] });

  return { digest, digestStale, loading, lastFetchError, sparkData, refetchDigest };
}
