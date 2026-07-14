import { useState } from 'react';
import type { Project } from '../types/project';
import { API_BASE_URL, authHeaders, authGet } from '../api';

/**
 * Project dashboard state: the project list, paginated fetch, and the
 * project-level actions (pause-all-autonomous, clear wave deps).
 *
 * `fetchTasksRef` is used so handleClearWaveDeps can refresh the task list
 * after mutating wave dependencies without re-creating this hook when the
 * task fetcher identity changes.
 */
export function useProjectsState(
  addToast: (msg: string, type: 'success' | 'error') => void,
  fetchTasksRef: React.MutableRefObject<() => Promise<void> | void>,
) {
  const [projects, setProjects] = useState<Project[]>([]);
  const [projectsFetchError, setProjectsFetchError] = useState<string | null>(null);
  const [projectsLoaded, setProjectsLoaded] = useState(false);

  const fetchProjects = async (silent = false) => {
    try {
      const PAGE_LIMIT = 500;
      const allItems: unknown[] = [];
      let offset = 0;
      let hasMore = true;
      while (hasMore) {
        const res = await authGet(`${API_BASE_URL}/api/admin/projects?limit=${PAGE_LIMIT}&offset=${offset}`);
        if (!res.ok) {
          setProjectsFetchError('Failed to load projects');
          setProjectsLoaded(true);
          if (!silent) addToast('Failed to load projects', 'error');
          return;
        }
        const data = await res.json() as { items: unknown[]; has_more: boolean };
        allItems.push(...(data.items || []));
        hasMore = data.has_more ?? false;
        offset += PAGE_LIMIT;
      }
      setProjects(allItems as never[]);
      setProjectsFetchError(null);
    } catch (err) {
      console.error('Failed to fetch projects:', err);
      setProjectsFetchError('Failed to load projects');
      if (!silent) addToast('Failed to load projects', 'error');
    } finally {
      setProjectsLoaded(true);
    }
  };

  const handlePauseAllAutonomous = async () => {
    const res = await fetch(`${API_BASE_URL}/api/admin/autonomous/pause-all`, {
      method: 'POST',
      headers: await authHeaders(),
    });
    if (res.ok) {
      await fetchProjects();
      addToast('All autonomous projects paused', 'success');
    } else {
      addToast('Failed to pause autonomous projects', 'error');
    }
  };

  const handleClearWaveDeps = async (projectId: string) => {
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/projects/${projectId}/clear-wave-deps`, {
        method: 'POST',
        headers: await authHeaders(),
        body: '{}',
      });
      const data = await res.json();
      const cleared = data.cleared ?? 0;
      const preserved = data.preserved ?? 0;
      addToast(
        `Cleared stale deps on ${cleared} tasks; kept prerequisites on ${preserved}`,
        'success',
      );
      fetchTasksRef.current();
    } catch (error) {
      console.error('Failed to clear wave deps:', error);
      addToast('Failed to clear wave dependencies', 'error');
    }
  };

  return {
    projects, setProjects, projectsFetchError, projectsLoaded,
    fetchProjects, handlePauseAllAutonomous, handleClearWaveDeps,
  };
}
