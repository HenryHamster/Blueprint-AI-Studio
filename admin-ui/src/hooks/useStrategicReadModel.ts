import { useCallback, useEffect, useState } from 'react';
import { fetchStrategicState, type StrategicStateResponse } from '../strategicStateApi';
import { DEFAULT_POLL_MS, usePoller } from './usePoller';

export function useStrategicReadModel(projectId: string | null) {
  const [data, setData] = useState<StrategicStateResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setData(null);
    setError(null);
    setLoading(Boolean(projectId));
  }, [projectId]);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      if (!projectId) return;
      if (signal?.aborted) return;
      setLoading(prev => prev || data == null);
      try {
        const next = await fetchStrategicState(projectId, signal);
        setData(next);
        setError(null);
      } catch (err) {
        if (err instanceof Error && err.name === 'AbortError') return;
        setError(err instanceof Error ? err.message : 'Failed to fetch strategic state');
      } finally {
        setLoading(false);
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
