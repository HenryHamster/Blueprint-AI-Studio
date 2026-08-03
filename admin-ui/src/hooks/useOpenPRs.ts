import { useState, useRef, useCallback } from 'react';
import { apiFetch } from '../api';
import { usePoller, DEFAULT_POLL_MS } from './usePoller';
import type { OpenPR } from '../components/PRMonitorSection';

interface OpenPrsResponse {
  open_prs?: OpenPR[];
  project_integration_branch?: string | null;
  project_promotion_target?: string | null;
  degraded?: boolean;
  degraded_reason?: string | null;
}

export interface UseOpenPRsResult {
  openPRs: OpenPR[];
  openPRsStale: boolean;
  openPRsDegraded: boolean;
  openPRsIntegrationBranch: string | null;
  openPRsPromotionTarget: string | null;
  openPRsRefreshing: boolean;
  fetchOpenPRs: () => Promise<void>;
}

function applyResponse(
  data: OpenPrsResponse,
  setOpenPRs: (v: OpenPR[]) => void,
  setIntegrationBranch: (v: string | null) => void,
  setPromotionTarget: (v: string | null) => void,
  setDegraded: (v: boolean) => void,
  setStale: (v: boolean) => void,
): void {
  setOpenPRs(data.open_prs ?? []);
  setIntegrationBranch(data.project_integration_branch ?? null);
  setPromotionTarget(data.project_promotion_target ?? null);
  setDegraded(Boolean(data.degraded));
  setStale(false);
}

export function useOpenPRs(selectedProjectId: string | null, enabled: boolean): UseOpenPRsResult {
  const [openPRs, setOpenPRs] = useState<OpenPR[]>([]);
  const [openPRsStale, setOpenPRsStale] = useState(false);
  const [openPRsDegraded, setOpenPRsDegraded] = useState(false);
  const [openPRsIntegrationBranch, setOpenPRsIntegrationBranch] = useState<string | null>(null);
  const [openPRsPromotionTarget, setOpenPRsPromotionTarget] = useState<string | null>(null);
  const [openPRsRefreshing, setOpenPRsRefreshing] = useState(false);
  const requestIdRef = useRef(0);

  // Reset PR state synchronously during render on project switch so stale data
  // never bleeds into the new project's view (React "adjust state during rendering"
  // pattern — https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes).
  const scopeKey = `${selectedProjectId}:${enabled}`;
  const [lastScopeKey, setLastScopeKey] = useState(scopeKey);
  if (lastScopeKey !== scopeKey) {
    setLastScopeKey(scopeKey);
    setOpenPRs([]);
    setOpenPRsStale(false);
    setOpenPRsDegraded(false);
    setOpenPRsIntegrationBranch(null);
    setOpenPRsPromotionTarget(null);
  }

  usePoller(async (signal) => {
    if (!selectedProjectId) return;
    try {
      const res = await apiFetch(`/api/admin/open-prs?project_id=${encodeURIComponent(selectedProjectId)}`);
      if (signal.aborted) return;
      if (res.ok) {
        applyResponse(await res.json() as OpenPrsResponse, setOpenPRs, setOpenPRsIntegrationBranch, setOpenPRsPromotionTarget, setOpenPRsDegraded, setOpenPRsStale);
      } else {
        setOpenPRsStale(true);
        setOpenPRsDegraded(false);
      }
    } catch {
      if (!signal.aborted) {
        setOpenPRsStale(true);
        setOpenPRsDegraded(false);
      }
    }
  }, { intervalMs: DEFAULT_POLL_MS, enabled, deps: [selectedProjectId] });

  const fetchOpenPRs = useCallback(async () => {
    if (!enabled || !selectedProjectId) return;
    const requestId = ++requestIdRef.current;
    setOpenPRsRefreshing(true);
    try {
      const res = await apiFetch(`/api/admin/open-prs?project_id=${encodeURIComponent(selectedProjectId)}`);
      if (requestId !== requestIdRef.current) return;
      if (res.ok) {
        applyResponse(await res.json() as OpenPrsResponse, setOpenPRs, setOpenPRsIntegrationBranch, setOpenPRsPromotionTarget, setOpenPRsDegraded, setOpenPRsStale);
      } else {
        setOpenPRsStale(true);
        setOpenPRsDegraded(false);
      }
    } catch { /* silent */ }
    finally {
      if (requestId === requestIdRef.current) setOpenPRsRefreshing(false);
    }
  }, [enabled, selectedProjectId]);

  return {
    openPRs,
    openPRsStale,
    openPRsDegraded,
    openPRsIntegrationBranch,
    openPRsPromotionTarget,
    openPRsRefreshing,
    fetchOpenPRs,
  };
}
