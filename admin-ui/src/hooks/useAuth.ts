import { useState, useEffect } from 'react';
import type { User } from 'firebase/auth';
import { onAuthChange } from '../firebase';
import { API_BASE_URL, authGet } from '../api';

export function useAuth() {
  const [user, setUser] = useState<User | null>(null);
  const [authLoading, setAuthLoading] = useState(true);
  const [userRole, setUserRole] = useState<'admin' | 'user' | null>(null);
  const [accessDenied, setAccessDenied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Firebase auth state
  useEffect(() => {
    const unsub = onAuthChange(
      (u) => { setUser(u); setAuthLoading(false); },
      (message) => { setError(message); setAuthLoading(false); },
    );
    return unsub;
  }, []);

  // Fetch user role from backend after Firebase auth
  useEffect(() => {
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (!user) { setUserRole(null); setAccessDenied(false); return; }
    let cancelled = false;
    (async () => {
      try {
        const res = await authGet(`${API_BASE_URL}/api/me`);
        if (cancelled) return;
        if (res.status === 403) { setAccessDenied(true); return; }
        if (!res.ok) { setError(`Failed to verify access: ${res.statusText}`); return; }
        const data = await res.json();
        setUserRole(data.role);
        setAccessDenied(false);
      } catch {
        if (!cancelled) setError('Failed to connect to backend server.');
      }
    })();
    return () => { cancelled = true; };
  }, [user]);

  return { user, authLoading, userRole, accessDenied, error, setError };
}
