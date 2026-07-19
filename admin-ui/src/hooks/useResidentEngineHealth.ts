import { useEffect, useRef, useState } from 'react';
import { fetchResidentEngineHealth, type ResidentEngineHealth } from '../strategicStateApi';

interface UseResidentEngineHealthOptions {
  enabled: boolean;
  projectId: string | null;
  refreshKey?: string | number;
}

export function useResidentEngineHealth({ enabled, projectId, refreshKey }: UseResidentEngineHealthOptions) {
  const [health, setHealth] = useState<ResidentEngineHealth | null>(null);
  const requestSequenceRef = useRef(0);

  useEffect(() => {
    const requestSequence = ++requestSequenceRef.current;
    if (!enabled || !projectId) return;
    const controller = new AbortController();
    void fetchResidentEngineHealth(projectId, controller.signal)
      .then(result => {
        if (!controller.signal.aborted && requestSequence === requestSequenceRef.current) setHealth(result);
      })
      .catch(error => {
        if (!(error instanceof DOMException && error.name === 'AbortError')) {
          // Health is supplementary rollout evidence. Preserve the last valid
          // projection through transient failures instead of flickering the UI.
        }
      });
    return () => controller.abort();
  }, [enabled, projectId, refreshKey]);

  return enabled && health?.project_id === projectId ? health : null;
}
