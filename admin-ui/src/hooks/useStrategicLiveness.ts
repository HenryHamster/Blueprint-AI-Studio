/* eslint-disable react-hooks/refs, react-hooks/set-state-in-effect */
import { useEffect, useRef, useState } from 'react';
import { fetchStrategicLiveness, type StrategicLivenessResponse } from '../strategicStateApi';

/**
 * Lightweight probe that resolves the operator's interaction mode and the
 * resident-session liveness chip on reload BEFORE the heavy
 * {@link useStrategicReadModel} fetch lands. Hits the cheap
 * `strategic-liveness` endpoint (no Phase-1/Phase-2 assembly) once per project,
 * so the mode indicator and active/dead chip render fast; the full read model
 * hydrates afterward and its values take precedence (see `useStrategicSession`).
 *
 * Best-effort: a failed probe is swallowed — the heavy read model still
 * populates the same fields shortly after.
 */
export function useStrategicLiveness(
  projectId: string | null,
  opts?: { enabled?: boolean },
) {
  const enabled = opts?.enabled ?? true;
  const [data, setData] = useState<StrategicLivenessResponse | null>(null);
  const currentProjectIdRef = useRef(projectId);
  currentProjectIdRef.current = projectId;
  // Monotonic request generation for sequencing: a re-fetch (project change or
  // enabled toggle) claims the latest generation; an in-flight probe only
  // commits if it still holds the generation it claimed, so a stale response
  // can never clobber a newer one.
  const requestGenerationRef = useRef(0);

  useEffect(() => {
    // Clear on project change so the chip never shows a prior project's session.
    setData(null);
    requestGenerationRef.current += 1;
  }, [projectId]);

  useEffect(() => {
    if (!projectId || !enabled) return;
    const requestedProjectId = projectId;
    const requestGeneration = (requestGenerationRef.current += 1);
    const controller = new AbortController();
    void (async () => {
      try {
        const next = await fetchStrategicLiveness(requestedProjectId, controller.signal);
        if (
          currentProjectIdRef.current !== requestedProjectId ||
          next.project_id !== requestedProjectId ||
          requestGenerationRef.current !== requestGeneration
        ) {
          return;
        }
        setData(next);
      } catch {
        // Best-effort probe: the heavy read model still hydrates these fields.
      }
    })();
    return () => controller.abort();
  }, [projectId, enabled]);

  return { data };
}
