import { useState, useCallback } from 'react';
import { API_BASE_URL, authGet, apiFetch } from '../api';

export type ProjectMemberEntry = {
  id: string;
  user_id: string;
  email?: string;
  role: 'owner' | 'member' | 'viewer';
  created_at: string | null;
};

export function useProjectMembers() {
  const [projectMembers, setProjectMembers] = useState<ProjectMemberEntry[]>([]);

  const fetchProjectMembers = useCallback(async (projectId: string) => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/projects/${projectId}/members`);
      if (res.ok) setProjectMembers(await res.json());
    } catch { /* ignore */ }
  }, []);

  const addProjectMember = useCallback(async (projectId: string, email: string, role: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${projectId}/members`, {
      method: 'POST', body: JSON.stringify({ email, role }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ error: { message: res.statusText } }));
      throw new Error(data.error?.message || data.detail || 'Failed to add member');
    }
    await fetchProjectMembers(projectId);
  }, [fetchProjectMembers]);

  const updateProjectMemberRole = useCallback(async (projectId: string, memberId: string, role: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${projectId}/members/${memberId}`, {
      method: 'PATCH', body: JSON.stringify({ role }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ error: { message: res.statusText } }));
      throw new Error(data.error?.message || data.detail || 'Failed to update role');
    }
    await fetchProjectMembers(projectId);
  }, [fetchProjectMembers]);

  const removeProjectMember = useCallback(async (projectId: string, memberId: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${projectId}/members/${memberId}`, {
      method: 'DELETE',
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ error: { message: res.statusText } }));
      throw new Error(data.error?.message || data.detail || 'Failed to remove member');
    }
    await fetchProjectMembers(projectId);
  }, [fetchProjectMembers]);

  return { projectMembers, fetchProjectMembers, addProjectMember, updateProjectMemberRole, removeProjectMember };
}
