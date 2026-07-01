import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchStrategicState, type StrategicSessionResponse, type StrategicStateResponse } from '../strategicStateApi';
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

  const applySessionResponse = useCallback(
    (sessionResponse: StrategicSessionResponse) => {
      if (
        !projectId ||
        currentProjectIdRef.current !== projectId ||
        sessionResponse.project_id !== projectId
      ) {
        return;
      }
      setData(prev => {
        if (!prev || prev.project_id !== sessionResponse.project_id) return prev;
        return {
          ...prev,
          strategic: sessionResponse.strategic ?? prev.strategic,
          runtime: sessionResponse.runtime ?? prev.runtime,
          context_package: sessionResponse.context_package ?? prev.context_package,
          workbench: sessionResponse.workbench ?? prev.workbench,
          work_session: sessionResponse.session ?? prev.work_session,
        };
      });
      setError(null);
      setLoading(false);
    },
    [projectId],
  );

  usePoller(refresh, {
    intervalMs: DEFAULT_POLL_MS,
    enabled: Boolean(projectId),
    deps: [projectId],
  });

  return { data, loading, error, refresh, applySessionResponse };
}
