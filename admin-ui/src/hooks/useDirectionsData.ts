import { useState, useEffect, useRef } from 'react';
import { apiFetch, fetchVersionLifecycle } from '../api';
import { useDirectionForm } from './useDirectionForm';
import { useAutonomousConfig } from './useAutonomousConfig';
import type { Direction } from '../components/DirectionsPanel';
import type { ProblemModel, ProblemModelSizeHealth, RunningUpdateReconciliation } from '../types';

interface UseDirectionsDataDeps {
  selectedProjectId: string | null;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  // Optional ref the live WS handler (useLogViewer) calls to trigger a refetch
  // when the strategist emits an update. Populated here, reset to a no-op on unmount.
  fetchDirectionsRef?: React.MutableRefObject<() => void>;
}

/**
 * Owns all data state, API handlers, and derived grouping for DirectionsPanel.
 *
 * Extracted from the former god-component verbatim — behavior is identical.
 * The `useDirectionForm` and `useAutonomousConfig` hooks are instantiated here
 * (rather than in the panel) so they share the same `fetchDirections` /
 * `setError` / `addToast` closures the API handlers below rely on.
 */
export function useDirectionsData({ selectedProjectId, addToast, fetchDirectionsRef }: UseDirectionsDataDeps) {
  const [directions, setDirections] = useState<Direction[]>([]);
  const [autonomousEnabled, setAutonomousEnabled] = useState(false);
  const [loading, setLoading] = useState(false);
  const [taskCounts, setTaskCounts] = useState<Record<string, number>>({});
  const [activeTasks, setActiveTasks] = useState<Record<string, Array<{ id: string; status: string; title: string; latest_status_message?: string }>>>({});
  const [designDoc, setDesignDoc] = useState<Record<string, unknown>>({});
  const [problemModel, setProblemModel] = useState<ProblemModel>({});
  const [problemModelSizeHealth, setProblemModelSizeHealth] = useState<ProblemModelSizeHealth | null>(null);
  const [runningUpdate, setRunningUpdate] = useState<string | null>(null);
  const [runningUpdateAckAt, setRunningUpdateAckAt] = useState<string | null>(null);
  const [runningUpdateReconciliation, setRunningUpdateReconciliation] =
    useState<RunningUpdateReconciliation | null>(null);
  // The currently-open release version number, used to gate the "stage for next
  // version" affordance: staging requires an open version (the backend stamps
  // staged_for_version from it and 404s when none is open).
  const [openVersionNumber, setOpenVersionNumber] = useState<number | null>(null);

  // Error feedback
  const [error, setError] = useState<string | null>(null);

  // Research button state
  const [researchingIds, setResearchingIds] = useState<Record<string, boolean>>({});
  const [researchSuccessIds, setResearchSuccessIds] = useState<Record<string, boolean>>({});

  // Force-research confirmation popover
  const [forceConfirmId, setForceConfirmId] = useState<string | null>(null);

  // Form state for add/edit
  const form = useDirectionForm();

  // Autonomous config state + handlers
  const config = useAutonomousConfig({ setError, addToast });

  useEffect(() => {
    config.fetchCooldownSetting();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (selectedProjectId) {
      fetchDirections();
      config.fetchSettings();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId]);

  // Always points at the latest fetchDirections closure (recreated each render).
  const fetchDirectionsLatestRef = useRef(fetchDirections);
  fetchDirectionsLatestRef.current = fetchDirections;

  // Register a stable callback into the externally-owned ref so the live WS
  // handler can trigger a refetch. Reset to a no-op on unmount to avoid a
  // stale-closure setState after the panel closes.
  useEffect(() => {
    if (!fetchDirectionsRef) return;
    fetchDirectionsRef.current = () => fetchDirectionsLatestRef.current();
    return () => {
      fetchDirectionsRef.current = () => {};
    };
  }, [fetchDirectionsRef]);

  async function fetchDirections() {
    setLoading(true);
    try {
      const [res, countsRes] = await Promise.all([
        apiFetch(`/api/admin/projects/${selectedProjectId}/directions`),
        apiFetch(`/api/admin/projects/${selectedProjectId}/directions/task-counts`),
      ]);
      if (!res.ok) throw new Error('Failed to fetch directions');
      const data = await res.json();
      setDirections(data.directions ?? []);
      setAutonomousEnabled(data.autonomous_enabled ?? false);
      setDesignDoc(data.design_doc ?? {});
      setProblemModel(data.problem_model ?? {});
      setProblemModelSizeHealth(data.problem_model_size_health ?? null);
      setRunningUpdate(typeof data.running_update === 'string' ? data.running_update : null);
      setRunningUpdateAckAt(data.running_update_acknowledged_at ?? null);
      setRunningUpdateReconciliation(data.running_update_reconciliation ?? null);
      if (countsRes.ok) {
        const countsData = await countsRes.json();
        setTaskCounts(countsData.counts ?? {});
        setActiveTasks(countsData.active_tasks ?? {});
      }
    } catch (err) {
      console.error('Failed to fetch directions:', err);
      addToast('Failed to load directions', 'error');
    }
    // Fetch the open version separately so a version-lifecycle failure never
    // blocks the directions list. A null result simply disables staging in the UI.
    if (selectedProjectId) {
      try {
        const lifecycle = await fetchVersionLifecycle(selectedProjectId);
        setOpenVersionNumber(lifecycle.current_version?.version_number ?? null);
      } catch (err) {
        console.error('Failed to fetch version lifecycle:', err);
        setOpenVersionNumber(null);
      }
    }
    setLoading(false);
  }

  async function triggerCycle(phase: 'design_doc' | 'strategic_review' | 'full') {
    if (!selectedProjectId) return;
    const apiPhase = phase === 'full' ? 'design_doc' : phase;
    const labels = {
      design_doc: 'Architecture review',
      strategic_review: 'Strategic review',
      full: 'Full cycle',
    } as const;
    try {
      const res = await apiFetch(
        `/api/admin/projects/${selectedProjectId}/strategic-cycle?phase=${apiPhase}`,
        { method: 'POST' },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail || `HTTP ${res.status}`);
      }
      addToast(`${labels[phase]} triggered`, 'success');
    } catch (err) {
      addToast(
        `Failed to trigger ${labels[phase].toLowerCase()}: ${err instanceof Error ? err.message : String(err)}`,
        'error',
      );
    }
  }

  async function addDirection() {
    if (!form.newText.trim() || !selectedProjectId) return;
    setError(null);
    try {
      const body: Record<string, unknown> = { text: form.newText.trim(), priority: form.newPriority, paths: form.newPathTags };
      if (form.newCategory) body.category = form.newCategory;
      body.completion_criteria = form.newCompletionCriteria.trim() || null;
      // Atomic born-staged create: when the operator opted to stage for the next
      // version, the backend stamps staged_for_version from the open version in a
      // single create call (404s if none is open), so the direction never passes
      // through a research-eligible active state (no create-then-edit race).
      if (form.newStaged && openVersionNumber != null) body.staged = true;
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error('Failed to add direction');
      form.resetAddForm();
      fetchDirections();
    } catch {
      setError('Failed to add direction. Please try again.');
    }
  }

  async function updateDirection(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ text: form.editText, priority: form.editPriority, status: form.editStatus, paths: form.editPathTags, category: form.editCategory ?? null, completion_criteria: form.editCompletionCriteria.trim() || null }),
      });
      if (!res.ok) throw new Error('Failed to update direction');
      form.setEditingId(null);
      fetchDirections();
    } catch {
      setError('Failed to update direction. Please try again.');
    }
  }

  async function quickToggleStatus(id: string, currentStatus: 'active' | 'paused') {
    setError(null);
    const newStatus = currentStatus === 'active' ? 'paused' : 'active';
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) throw new Error('Failed to update status');
      fetchDirections();
    } catch {
      setError('Failed to update status. Please try again.');
    }
  }

  async function reactivateDirection(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'active' }),
      });
      if (!res.ok) throw new Error('Failed to reactivate direction');
      fetchDirections();
    } catch {
      setError('Failed to reactivate direction. Please try again.');
    }
  }

  // Activate a staged direction immediately instead of waiting for the next
  // version to open. Reuses the PATCH status:'active' transition.
  async function activateStagedDirection(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'active' }),
      });
      if (!res.ok) throw new Error('Failed to activate staged direction');
      addToast('Staged direction activated', 'success');
      fetchDirections();
    } catch {
      setError('Failed to activate staged direction. Please try again.');
    }
  }

  // Retarget an already-staged direction to a different (current-or-future)
  // version. Sends only staged_for_version so the backend keeps the direction
  // staged and re-binds its marker; it validates the target (rejecting an
  // already-promoted version) and 409s if the direction is no longer staged.
  async function retargetStagedDirection(id: string, version: number) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ staged_for_version: version }),
      });
      if (!res.ok) throw new Error('Failed to retarget staged direction');
      addToast(`Retargeted to v${version}`, 'success');
      fetchDirections();
    } catch {
      setError('Failed to retarget staged direction. Please try again.');
    }
  }

  // Cancel a staged direction. DELETE leaves a tombstone (handled backend-side)
  // so the strategist won't immediately re-stage the same idea.
  async function cancelStagedDirection(id: string) {
    if (!window.confirm('Cancel this staged direction? It will not auto-activate at the next version.')) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'DELETE',
      });
      if (!res.ok) throw new Error('Failed to cancel staged direction');
      addToast('Staged direction cancelled', 'info');
      fetchDirections();
    } catch {
      setError('Failed to cancel staged direction. Please try again.');
    }
  }

  async function unholdDirection(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}/unhold`, {
        method: 'POST',
      });
      if (!res.ok) throw new Error('Failed to unhold direction');
      fetchDirections();
    } catch {
      setError('Failed to unhold direction. Please try again.');
    }
  }

  async function markDirectionComplete(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'completed' }),
      });
      if (!res.ok) throw new Error('Failed to mark direction as complete');
      fetchDirections();
    } catch {
      setError('Failed to mark direction as complete. Please try again.');
    }
  }

  async function approveProposal(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'active' }),
      });
      if (!res.ok) throw new Error('Failed to approve proposal');
      addToast('Direction approved and activated', 'success');
      fetchDirections();
    } catch {
      setError('Failed to approve proposal. Please try again.');
    }
  }

  async function rejectProposal(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'DELETE',
      });
      if (!res.ok) throw new Error('Failed to reject proposal');
      addToast('Direction proposal rejected', 'info');
      fetchDirections();
    } catch {
      setError('Failed to reject proposal. Please try again.');
    }
  }

  async function reviveProposal(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'proposed' }),
      });
      if (!res.ok) throw new Error('Failed to revive proposal');
      addToast('Expired proposal revived', 'success');
      fetchDirections();
    } catch {
      setError('Failed to revive proposal. Please try again.');
    }
  }

  async function applyDirectionUpdate(d: Direction) {
    if (!d.pending_update) return;
    setError(null);
    try {
      const updated: Record<string, unknown> = { pending_update: null };
      if (d.pending_update.suggested_text) updated.text = d.pending_update.suggested_text;
      if (d.pending_update.suggested_priority) updated.priority = d.pending_update.suggested_priority;
      if (d.pending_update.suggested_paths) updated.paths = d.pending_update.suggested_paths;
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${d.id}`, {
        method: 'PATCH',
        body: JSON.stringify(updated),
      });
      if (!res.ok) throw new Error('Failed to apply direction update');
      fetchDirections();
    } catch {
      setError('Failed to apply direction update. Please try again.');
    }
  }

  async function dismissDirectionUpdate(id: string) {
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ pending_update: null }),
      });
      if (!res.ok) throw new Error('Failed to dismiss direction update');
      fetchDirections();
    } catch {
      setError('Failed to dismiss direction update. Please try again.');
    }
  }

  async function deleteDirection(id: string) {
    if (!window.confirm('Are you sure you want to delete this direction?')) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}`, {
        method: 'DELETE',
      });
      if (!res.ok) throw new Error('Failed to delete direction');
      fetchDirections();
    } catch {
      setError('Failed to delete direction. Please try again.');
    }
  }

  async function toggleAutonomous() {
    setError(null);
    const newValue = !autonomousEnabled;
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/autonomous`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: newValue }),
      });
      if (!res.ok) throw new Error('Failed to toggle autonomous mode');
      setAutonomousEnabled(newValue);
    } catch {
      setError('Failed to toggle autonomous mode. Please try again.');
    }
  }

  async function reorderDirections(newOrder: Direction[]) {
    const previousDirections = directions;
    setDirections(prev => {
      const nonActive = prev.filter(d => d.status !== 'active');
      return [...newOrder, ...nonActive];
    });
    setError(null);
    try {
      const nonActive = previousDirections.filter(d => d.status !== 'active');
      const allOrdered = [...newOrder, ...nonActive];
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/reorder`, {
        method: 'PATCH',
        body: JSON.stringify({ direction_ids: allOrdered.map(d => d.id) }),
      });
      if (!res.ok) throw new Error('Failed to reorder directions');
    } catch {
      setDirections(previousDirections);
      setError('Failed to reorder directions. Please try again.');
    }
  }

  async function triggerResearch(id: string, bypass = false) {
    setResearchingIds(prev => ({ ...prev, [id]: true }));
    setForceConfirmId(null);
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${selectedProjectId}/directions/${id}/research${bypass ? '?bypass_cooldown=true' : ''}`, { method: 'POST' });
      if (!res.ok) throw new Error('Failed to trigger research');
      setResearchSuccessIds(prev => ({ ...prev, [id]: true }));
      setTimeout(() => setResearchSuccessIds(prev => ({ ...prev, [id]: false })), 3000);
    } catch {
      setError('Failed to trigger research. Please try again.');
    } finally {
      setResearchingIds(prev => ({ ...prev, [id]: false }));
    }
  }

  return {
    // Data state
    directions,
    autonomousEnabled,
    loading,
    taskCounts,
    activeTasks,
    designDoc,
    problemModel,
    setProblemModel,
    problemModelSizeHealth,
    runningUpdate,
    setRunningUpdate,
    runningUpdateAckAt,
    setRunningUpdateAckAt,
    runningUpdateReconciliation,
    openVersionNumber,
    error,
    setError,
    researchingIds,
    researchSuccessIds,
    forceConfirmId,
    setForceConfirmId,
    // Sub-hooks
    form,
    config,
    // Handlers
    fetchDirections,
    triggerCycle,
    addDirection,
    updateDirection,
    quickToggleStatus,
    reactivateDirection,
    activateStagedDirection,
    retargetStagedDirection,
    cancelStagedDirection,
    unholdDirection,
    markDirectionComplete,
    approveProposal,
    rejectProposal,
    reviveProposal,
    applyDirectionUpdate,
    dismissDirectionUpdate,
    deleteDirection,
    toggleAutonomous,
    reorderDirections,
    triggerResearch,
  };
}
