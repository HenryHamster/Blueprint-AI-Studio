import { useState } from 'react';
import type { Alert } from '../types/alert';
import { API_BASE_URL, authHeaders, fetchAlerts as fetchAdminAlerts } from '../api';

/**
 * Alerts banner state: the alert list plus fetch / dismiss actions.
 * Scoped to the currently selected project (or global when none is selected).
 */
export function useAlertsState(
  addToast: (msg: string, type: 'success' | 'error') => void,
  selectedProjectId: string | null,
) {
  const [alerts, setAlerts] = useState<Alert[]>([]);

  const fetchAlerts = async (silent = false) => {
    try {
      setAlerts(await fetchAdminAlerts({ projectId: selectedProjectId }));
    } catch (err) {
      console.error('Failed to fetch alerts:', err);
      if (!silent) addToast('Failed to load alerts', 'error');
    }
  };

  const dismissAlert = async (alertId: string) => {
    const prev = alerts;
    setAlerts(prevAlerts => prevAlerts.filter(a => a.id !== alertId));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/alerts/${alertId}/dismiss`, {
        method: 'POST',
        headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error('Failed to dismiss alert:', err);
      setAlerts(prev);
      addToast('Failed to dismiss alert', 'error');
    }
  };

  const dismissAllAlerts = async () => {
    const prev = alerts;
    setAlerts([]);
    try {
      const url = selectedProjectId
        ? `${API_BASE_URL}/api/admin/alerts/dismiss-all?project_id=${selectedProjectId}`
        : `${API_BASE_URL}/api/admin/alerts/dismiss-all`;
      const res = await fetch(url, {
        method: 'POST',
        headers: await authHeaders(),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
    } catch (err) {
      console.error('Failed to dismiss all alerts:', err);
      setAlerts(prev);
      addToast('Failed to dismiss all alerts', 'error');
    }
  };

  return { alerts, fetchAlerts, dismissAlert, dismissAllAlerts };
}
