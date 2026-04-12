import { useState, useEffect, useCallback } from 'react';
import { API_BASE_URL, authGet, apiFetch } from '../api';

export type AllowedUserEntry = {
  id: string;
  email: string;
  role: string;
  display_name: string | null;
  last_login_at: string | null;
  created_at: string | null;
};

export function useUserManagement(showUserManagement: boolean) {
  const [allowedUsers, setAllowedUsers] = useState<AllowedUserEntry[]>([]);

  const fetchAllowedUsers = useCallback(async () => {
    try {
      const res = await authGet(`${API_BASE_URL}/api/admin/users`);
      if (res.ok) setAllowedUsers(await res.json());
    } catch { /* ignore */ }
  }, []);

  // eslint-disable-next-line react-hooks/set-state-in-effect
  useEffect(() => { if (showUserManagement) void fetchAllowedUsers(); }, [showUserManagement, fetchAllowedUsers]);

  const addAllowedUser = useCallback(async (email: string, role: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/users`, {
      method: 'POST', body: JSON.stringify({ email, role }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(data.detail || 'Failed to add user');
    }
    await fetchAllowedUsers();
  }, [fetchAllowedUsers]);

  const updateAllowedUserRole = useCallback(async (userId: string, role: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/users/${userId}`, {
      method: 'PATCH', body: JSON.stringify({ role }),
    });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(data.detail || 'Failed to update user');
    }
    await fetchAllowedUsers();
  }, [fetchAllowedUsers]);

  const removeAllowedUser = useCallback(async (userId: string) => {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/users/${userId}`, { method: 'DELETE' });
    if (!res.ok) {
      const data = await res.json().catch(() => ({ detail: res.statusText }));
      throw new Error(data.detail || 'Failed to remove user');
    }
    await fetchAllowedUsers();
  }, [fetchAllowedUsers]);

  return { allowedUsers, fetchAllowedUsers, addAllowedUser, updateAllowedUserRole, removeAllowedUser };
}
