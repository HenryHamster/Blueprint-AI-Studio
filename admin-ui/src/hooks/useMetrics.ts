import { useState, useEffect, useRef, useCallback } from 'react';
import { usePersistedState } from './usePersistedState';
import { COST_PER_VM_HR } from '../api';

type MetricsPoint = { timestamp: number; activeVMs: number; costPerHr: number; completedCount: number; failedCount: number };

export function useMetrics() {
  const [metricsHistory, setMetricsHistory] = useState<MetricsPoint[]>(() => {
    try {
      const stored = localStorage.getItem('blueprint-admin:metricsHistory');
      const parsed: MetricsPoint[] = stored ? JSON.parse(stored) : [];
      return parsed.filter(p => Date.now() - p.timestamp <= 21600000);
    } catch { return []; }
  });

  useEffect(() => {
    const toStore = metricsHistory.filter(p => Date.now() - p.timestamp <= 21600000);
    try {
      localStorage.setItem('blueprint-admin:metricsHistory', JSON.stringify(toStore));
    } catch (e) {
      if (e instanceof DOMException && e.name === 'QuotaExceededError') {
        try {
          localStorage.setItem('blueprint-admin:metricsHistory', JSON.stringify(toStore.slice(-50)));
        } catch { /* storage unavailable, skip persistence */ }
      }
    }
  }, [metricsHistory]);

  const [cumulativeCost, setCumulativeCost] = usePersistedState<number>('cumulativeCost', 0);
  const [cumulativeVmHours, setCumulativeVmHours] = usePersistedState<number>('cumulativeVmHours', 0);
  const [metricsWindow, setMetricsWindow] = usePersistedState<'15m' | '30m' | '1h' | '2h' | '3h' | '6h' | '12h' | '24h' | 'all'>('metricsWindow', '1h');
  const metricsHistoryRef = useRef<MetricsPoint[]>([]);
  useEffect(() => { metricsHistoryRef.current = metricsHistory.filter(p => Date.now() - p.timestamp <= 86400000); }, [metricsHistory]);

  const addMetricsPoint = useCallback((activeCount: number | undefined, tasksForFallback: { status: string }[], completedCount: number, failedCount: number) => {
    const now = Date.now();
    const activeVMs = activeCount ?? tasksForFallback.filter(t => t.status === 'IN_PROGRESS' || t.status === 'PROVISIONING').length;
    const costPerHr = activeVMs * COST_PER_VM_HR;

    const prevHistory = metricsHistoryRef.current;
    const lastPoint = prevHistory[prevHistory.length - 1];
    if (lastPoint) {
      const intervalMs = now - lastPoint.timestamp;
      const intervalHrs = intervalMs / 3600000;
      setCumulativeCost(c => c + lastPoint.costPerHr * intervalHrs);
      setCumulativeVmHours(h => h + lastPoint.activeVMs * intervalHrs);
    }

    const filtered = prevHistory.filter((p) => now - p.timestamp <= 21600000);
    const newHistory = [...filtered, { timestamp: now, activeVMs, costPerHr, completedCount, failedCount }];
    metricsHistoryRef.current = newHistory;
    setMetricsHistory(newHistory);
  }, [setCumulativeCost, setCumulativeVmHours]);

  return {
    metricsHistory, cumulativeCost, cumulativeVmHours, metricsWindow, setMetricsWindow,
    addMetricsPoint,
  };
}
