/* eslint-disable react-hooks/refs, react-hooks/set-state-in-effect */
import { useCallback, useEffect, useRef, useState } from 'react';
import { fetchStrategicStateWithRevision, type StrategicSessionResponse, type StrategicStateResponse } from '../strategicStateApi';
import { DEFAULT_POLL_MS, usePoller } from './usePoller';

export function useStrategicReadModel(projectId: string | null, opts?: { pollerEnabled?: boolean }) {
  const pollerEnabled = opts?.pollerEnabled ?? true;
  const [data, setData] = useState<StrategicStateResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const currentProjectIdRef = useRef(projectId);
  currentProjectIdRef.current = projectId;
  // Monotonic request generation for sequencing. Every refresh claims the
  // latest generation when it starts; an applied mutation (applySessionResponse)
  // also advances it. A refresh only commits its response if the generation is
  // still the one it claimed — so a stale, in-flight pre-mutation GET can never
  // clobber freshly merged session state (or a newer refresh's result).
  const requestGenerationRef = useRef(0);
  // Opaque revision token returned by the server. Passed on the next poll so
  // the server can skip the expensive context-package build when inputs are
  // unchanged. Reset when the project changes.
  const stateRevisionRef = useRef<string | null>(null);

  useEffect(() => {
    setData(null);
    setError(null);
    setLoading(Boolean(projectId));
    requestGenerationRef.current += 1;
    stateRevisionRef.current = null;
  }, [projectId]);

  const refresh = useCallback(
    async (signal?: AbortSignal) => {
      if (!projectId) return;
      if (signal?.aborted) return;
      const requestedProjectId = projectId;
      const requestGeneration = (requestGenerationRef.current += 1);
      const revision = stateRevisionRef.current;
      setLoading(prev => prev || data == null);
      try {
        const next = await fetchStrategicStateWithRevision(requestedProjectId, revision, signal);
        if (
          currentProjectIdRef.current !== requestedProjectId ||
          next.project_id !== requestedProjectId ||
          requestGenerationRef.current !== requestGeneration
        ) {
          return;
        }
        stateRevisionRef.current = next.state_revision ?? null;
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
      // Advance the generation so any refresh already in flight (which fetched a
      // pre-mutation snapshot) is dropped instead of overwriting this merge.
      requestGenerationRef.current += 1;
      setError(null);
      setLoading(false);
    },
    [projectId],
  );

  usePoller(refresh, {
    intervalMs: DEFAULT_POLL_MS,
    enabled: Boolean(projectId) && pollerEnabled,
    deps: [projectId],
  });

  return { data, loading, error, refresh, applySessionResponse };
}
