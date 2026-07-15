import { useState, useRef, useCallback } from 'react';
import { apiFetch, type VersionLifecycleResponse } from '../api';
import { usePoller, DEFAULT_POLL_MS } from './usePoller';

export function hasVersionLifecycleData(versionLifecycle: VersionLifecycleResponse | null): boolean {
  return versionLifecycle !== null &&
    (versionLifecycle.current_version !== null || versionLifecycle.recent_versions.length > 0);
}

export interface UseVersionLifecycleResult {
  versionLifecycle: VersionLifecycleResponse | null;
  versionLifecycleError: string | null;
  versionLifecycleLastSuccessAt: string | null;
  versionLifecycleRefreshing: boolean;
  refreshVersionLifecycle: () => Promise<void>;
}

export function useVersionLifecycle(
  selectedProjectId: string | null,
  opts?: { pollerEnabled?: boolean },
): UseVersionLifecycleResult {
  const pollerEnabled = opts?.pollerEnabled ?? true;
  const [versionLifecycle, setVersionLifecycle] = useState<VersionLifecycleResponse | null>(null);
  const [versionLifecycleError, setVersionLifecycleError] = useState<string | null>(null);
  const [versionLifecycleLastSuccessAt, setVersionLifecycleLastSuccessAt] = useState<string | null>(null);
  const [versionLifecycleRefreshing, setVersionLifecycleRefreshing] = useState(false);
  const requestIdRef = useRef(0);

  // Reset state synchronously during render on project switch so stale data
  // never bleeds into the new project's view (React "adjust state during rendering"
  // pattern — https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes).
  const [lastProjectId, setLastProjectId] = useState(selectedProjectId);
  if (lastProjectId !== selectedProjectId) {
    setLastProjectId(selectedProjectId);
    setVersionLifecycle(null);
    setVersionLifecycleError(null);
    setVersionLifecycleLastSuccessAt(null);
    setVersionLifecycleRefreshing(false);
  }

  usePoller(async (signal) => {
    if (!selectedProjectId) return;
    try {
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/version-lifecycle`);
      if (signal.aborted) return;
      if (res.ok) {
        setVersionLifecycle(await res.json());
        setVersionLifecycleError(null);
        setVersionLifecycleLastSuccessAt(new Date().toISOString());
      } else {
        setVersionLifecycleError(`Version lifecycle unavailable (${res.status})`);
      }
    } catch {
      if (!signal.aborted) setVersionLifecycleError('Version lifecycle refresh failed');
    }
  }, { intervalMs: DEFAULT_POLL_MS, enabled: Boolean(selectedProjectId) && pollerEnabled, deps: [selectedProjectId, pollerEnabled] });

  const refreshVersionLifecycle = useCallback(async () => {
    if (!selectedProjectId) return;
    const requestId = ++requestIdRef.current;
    setVersionLifecycleRefreshing(true);
    try {
      const res = await apiFetch(`/api/admin/projects/${encodeURIComponent(selectedProjectId)}/version-lifecycle`);
      if (requestId !== requestIdRef.current) return;
      if (res.ok) {
        setVersionLifecycle(await res.json());
        setVersionLifecycleError(null);
        setVersionLifecycleLastSuccessAt(new Date().toISOString());
      } else {
        setVersionLifecycleError(`Version lifecycle unavailable (${res.status})`);
      }
    } catch {
      if (requestId === requestIdRef.current) setVersionLifecycleError('Version lifecycle refresh failed');
    } finally {
      if (requestId === requestIdRef.current) setVersionLifecycleRefreshing(false);
    }
  }, [selectedProjectId]);

  return {
    versionLifecycle,
    versionLifecycleError,
    versionLifecycleLastSuccessAt,
    versionLifecycleRefreshing,
    refreshVersionLifecycle,
  };
}
