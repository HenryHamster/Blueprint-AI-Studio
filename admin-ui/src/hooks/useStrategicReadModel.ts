import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchStrategicState, type StrategicStateResponse } from '../strategicStateApi';
import { DEFAULT_POLL_MS, usePoller } from './usePoller';

export function useStrategicReadModel(projectId: string | null) {
  const [data, setData] = useState<StrategicStateResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const currentProjectIdRef = useRef(projectId);
  currentProjectIdRef.current = projectId;

  useEffect(() => {
    setData(null);
    setError(null);
    setLoading(Boolean(projectId));
  }, [projectId]);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      if (!projectId) return;
      if (signal?.aborted) return;
      const requestedProjectId = projectId;
      setLoading(prev => prev || data == null);
      try {
        const next = await fetchStrategicState(requestedProjectId, signal);
        if (
          currentProjectIdRef.current !== requestedProjectId ||
          next.project_id !== requestedProjectId
        ) {
          return;
        }
        setData(next);
        setError(null);
      } catch (err) {
        if (currentProjectIdRef.current !== requestedProjectId) return;
        if (err instanceof Error && err.name === 'AbortError') return;
        setError(err instanceof Error ? err.message : 'Failed to fetch strategic state');
      } finally {
        if (currentProjectIdRef.current === requestedProjectId) {
          setLoading(false);
        }
      }
    },
    [data, projectId],
  );

  usePoller(refresh, {
    intervalMs: DEFAULT_POLL_MS,
    enabled: Boolean(projectId),
    deps: [projectId],
  });

  return { data, loading, error, refresh };
}
