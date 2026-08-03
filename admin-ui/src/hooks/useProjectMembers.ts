import { useState, useCallback, useRef } from 'react';
import { API_BASE_URL, authGet, apiFetch } from '../api';
import type { ProjectMemberEntry } from '../types/project';

export function useProjectMembers() {
  const [projectMembers, setProjectMembers] = useState<ProjectMemberEntry[]>([]);
  const [projectMembersProjectId, setProjectMembersProjectId] = useState<string | null>(null);
  const activeProjectIdRef = useRef<string | null>(null);
  const requestGenerationRef = useRef(0);

  const activateProjectMembers = useCallback((projectId: string | null) => {
    if (activeProjectIdRef.current === projectId) return;
    activeProjectIdRef.current = projectId;
    requestGenerationRef.current += 1;
    setProjectMembersProjectId(projectId);
    setProjectMembers([]);
  }, []);

  const fetchProjectMembers = useCallback(async (projectId: string) => {
    activateProjectMembers(projectId);
    const generation = ++requestGenerationRef.current;
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/projects/${projectId}/members?limit=500`);
      if (
        res.ok
        && activeProjectIdRef.current === projectId
        && requestGenerationRef.current === generation
      ) {
        const data = await res.json();
        if (
          activeProjectIdRef.current === projectId
          && requestGenerationRef.current === generation
        ) {
          setProjectMembers(Array.isArray(data) ? data : data.members ?? []);
        }
      }
    } catch { /* ignore */ }
  }, [activateProjectMembers]);

  const addProjectMember = useCallback(async (projectId: string, email: string, role: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${projectId}/members`, {
      method: 'POST', body: JSON.stringify({ email, role }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ error: { message: res.statusText } }));
      throw new Error(data.error?.message || data.detail || 'Failed to add member');
    }
    if (activeProjectIdRef.current === projectId) await fetchProjectMembers(projectId);
  }, [fetchProjectMembers]);

  const updateProjectMemberRole = useCallback(async (projectId: string, memberId: string, role: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${projectId}/members/${memberId}`, {
      method: 'PATCH', body: JSON.stringify({ role }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ error: { message: res.statusText } }));
      throw new Error(data.error?.message || data.detail || 'Failed to update role');
    }
    if (activeProjectIdRef.current === projectId) await fetchProjectMembers(projectId);
  }, [fetchProjectMembers]);

  const removeProjectMember = useCallback(async (projectId: string, memberId: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${projectId}/members/${memberId}`, {
      method: 'DELETE',
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ error: { message: res.statusText } }));
      throw new Error(data.error?.message || data.detail || 'Failed to remove member');
    }
    if (activeProjectIdRef.current === projectId) await fetchProjectMembers(projectId);
  }, [fetchProjectMembers]);

  return {
    projectMembers,
    projectMembersProjectId,
    activateProjectMembers,
    fetchProjectMembers,
    addProjectMember,
    updateProjectMemberRole,
    removeProjectMember,
  };
}
