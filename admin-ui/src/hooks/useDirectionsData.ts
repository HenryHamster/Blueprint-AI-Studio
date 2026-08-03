import { useState, useEffect, useRef, useCallback } from 'react';
import { apiFetch, fetchVersionLifecycle, submitChurnRetirementDecision } from '../api';
import type { CurrentVersion } from '../api';
import { generateIdempotencyKey } from '../utils/idempotencyKey';
import { useDirectionForm } from './useDirectionForm';
import { useAutonomousConfig } from './useAutonomousConfig';
import { usePoller } from './usePoller';
import type { Direction } from '../components/DirectionsPanel';
import type { ProblemModel, ProblemModelSizeHealth, RunningUpdateReconciliation, SelfReflectionEntry } from '../types';

interface UseDirectionsDataDeps {
  selectedProjectId: string | null;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  canOperateProject?: boolean;
  canManageGlobalSettings?: boolean;
  // Optional ref the live WS handler (useLogViewer) calls to trigger a refetch
  // when the strategist emits an update. Populated here, reset to a no-op on unmount.
  fetchDirectionsRef?: React.MutableRefObject<() => void>;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function areJsonLikeEqual(a: unknown, b: unknown): boolean {
  if (Object.is(a, b)) return true;
  if (Array.isArray(a) && Array.isArray(b)) {
    if (a.length !== b.length) return false;
    for (let i = 0; i < a.length; i += 1) {
      if (!areJsonLikeEqual(a[i], b[i])) return false;
    }
    return true;
  }
  if (isPlainObject(a) && isPlainObject(b)) {
    const aKeys = Object.keys(a);
    const bKeys = Object.keys(b);
    if (aKeys.length !== bKeys.length) return false;
    for (const key of aKeys) {
      if (!Object.prototype.hasOwnProperty.call(b, key)) return false;
      if (!areJsonLikeEqual(a[key], b[key])) return false;
    }
    return true;
  }
  return false;
}

function preserveDirectionArray(prev: Direction[], next: Direction[]): Direction[] {
  if (prev.length === 0) return next;
  const prevById = new Map(prev.map(direction => [direction.id, direction]));
  let changed = prev.length !== next.length;
  const shared = next.map((direction, index) => {
    const previous = prevById.get(direction.id);
    if (previous && areJsonLikeEqual(previous, direction)) {
      if (previous !== prev[index]) changed = true;
      return previous;
    }
    changed = true;
    return direction;
  });
  return changed ? shared : prev;
}

function preserveRecordValues<T extends Record<string, unknown>>(prev: T, next: T): T {
  if (areJsonLikeEqual(prev, next)) return prev;
  const shared = { ...next } as T;
  for (const key of Object.keys(next) as Array<keyof T>) {
    if (Object.prototype.hasOwnProperty.call(prev, key) && areJsonLikeEqual(prev[key], next[key])) {
      shared[key] = prev[key];
    }
  }
  return shared;
}

/**
 * Owns all data state, API handlers, and derived grouping for DirectionsPanel.
 *
 * Extracted from the former god-component verbatim — behavior is identical.
 * The `useDirectionForm` and `useAutonomousConfig` hooks are instantiated here
 * (rather than in the panel) so they share the same `fetchDirections` /
 * `setError` / `addToast` closures the API handlers below rely on.
 */
export function useDirectionsData({
  selectedProjectId,
  addToast,
  fetchDirectionsRef,
  canOperateProject = false,
  canManageGlobalSettings = false,
}: UseDirectionsDataDeps) {
  const projectScopeRef = useRef({
    projectId: selectedProjectId,
    generation: 0,
  });
  if (projectScopeRef.current.projectId !== selectedProjectId) {
    projectScopeRef.current = {
      projectId: selectedProjectId,
      generation: projectScopeRef.current.generation + 1,
    };
  }
  const projectScopeIsCurrent = useCallback((projectId: string, generation: number) => (
    projectScopeRef.current.projectId === projectId
    && projectScopeRef.current.generation === generation
  ), []);
  // Mutation callbacks are passed through several memoized workspace layers and
  // can outlive the render that created them. Keep one render-synchronous
  // capability cell so callbacks captured before a role downgrade fail closed.
  const canOperateProjectRef = useRef(canOperateProject);
  canOperateProjectRef.current = canOperateProject;
  type ProjectMutationScope = { projectId: string; generation: number };
  const captureProjectMutationScope = useCallback((projectId: string | null): ProjectMutationScope | null => (
    projectId
      ? { projectId, generation: projectScopeRef.current.generation }
      : null
  ), []);
  const projectMutationScopeIsCurrent = useCallback((scope: ProjectMutationScope | null) => (
    scope != null
    && canOperateProjectRef.current
    && projectScopeIsCurrent(scope.projectId, scope.generation)
  ), [projectScopeIsCurrent]);
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
  const [selfReflectionLog, setSelfReflectionLog] = useState<SelfReflectionEntry[]>([]);
  // The currently-open release version number, used to gate the "stage for next
  // version" affordance: staging requires an open version (the backend stamps
  // staged_for_version from it and 404s when none is open).
  const [openVersionNumber, setOpenVersionNumber] = useState<number | null>(null);
  const [currentVersion, setCurrentVersion] = useState<CurrentVersion | null>(null);

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
    if (canManageGlobalSettings) config.fetchCooldownSetting();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canManageGlobalSettings]);

  // Project-scoped state must disappear in the same render as a project switch
  // so data and mutation feedback from the previous project cannot flash in the
  // next one. React supports this conditional state-adjustment pattern for
  // prop-driven resets without a cascading setState-in-effect render.
  const [previousProjectId, setPreviousProjectId] = useState(selectedProjectId);
  if (previousProjectId !== selectedProjectId) {
    setPreviousProjectId(selectedProjectId);
    setDirections([]);
    setAutonomousEnabled(false);
    setLoading(false);
    setTaskCounts({});
    setActiveTasks({});
    setDesignDoc({});
    setProblemModel({});
    setProblemModelSizeHealth(null);
    setRunningUpdate(null);
    setRunningUpdateAckAt(null);
    setRunningUpdateReconciliation(null);
    setSelfReflectionLog([]);
    setOpenVersionNumber(null);
    setCurrentVersion(null);
    setError(null);
    setResearchingIds({});
    setResearchSuccessIds({});
    setForceConfirmId(null);
  }

  useEffect(() => {
    if (selectedProjectId) {
      fetchDirections();
      if (canManageGlobalSettings) config.fetchSettings();
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId, canManageGlobalSettings]);

  // Periodically refresh directions to keep time-derived badges (cooldown countdown,
  // hold auto-recovery countdown, staged waiting age) live while the panel is open.
  // Uses lite=true to skip heavy stable fields (problem_model, design_doc, etc.)
  // that don't change on a 10-second cadence.
  usePoller(
    () => {
      if (selectedProjectId) {
        return fetchDirectionsLatestRef.current({ lite: true });
      }
    },
    {
      intervalMs: 10_000,
      enabled: Boolean(selectedProjectId),
      leading: false,
      deps: [selectedProjectId],
    },
  );

  // Always points at the latest fetchDirections closure (recreated each render).
  const fetchDirectionsLatestRef = useRef(fetchDirections);
  fetchDirectionsLatestRef.current = fetchDirections;

  // Stable ref so memoized handlers can read current directions without deps churn.
  const directionsRef = useRef(directions);
  directionsRef.current = directions;

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

  async function fetchDirections({ lite = false }: { lite?: boolean } = {}) {
    const projectId = selectedProjectId;
    if (!projectId || projectScopeRef.current.projectId !== projectId) return;
    const generation = projectScopeRef.current.generation;
    const isCurrent = () => projectScopeIsCurrent(projectId, generation);
    if (!lite) setLoading(true);
    try {
      const baseUrl = `/api/admin/projects/${projectId}/directions`;
      const directionsUrl = lite ? `${baseUrl}?lite=1` : baseUrl;

      // On the initial (full) load, kick off all three requests in parallel:
      // directions, task-counts, and version-lifecycle. On lite poll ticks,
      // skip version-lifecycle since staging availability doesn't change at 10s.
      const versionLifecyclePromise = !lite
        ? fetchVersionLifecycle(projectId).catch(() => null)
        : null;

      const [res, countsRes] = await Promise.all([
        apiFetch(directionsUrl),
        apiFetch(`${baseUrl}/task-counts`),
      ]);
      if (!res.ok) throw new Error('Failed to fetch directions');
      const data = await res.json();
      if (!isCurrent()) return;
      const nextDirections = data.directions ?? [];
      setDirections(prev => preserveDirectionArray(prev, nextDirections));
      setAutonomousEnabled(data.autonomous_enabled ?? false);
      setRunningUpdate(typeof data.running_update === 'string' ? data.running_update : null);
      setRunningUpdateAckAt(data.running_update_acknowledged_at ?? null);
      // Heavy stable fields are only present on full (non-lite) fetches; don't
      // overwrite existing state on poll ticks that intentionally omit them.
      if (!lite) {
        setDesignDoc(data.design_doc ?? {});
        setProblemModel(data.problem_model ?? {});
        setProblemModelSizeHealth(data.problem_model_size_health ?? null);
        setRunningUpdateReconciliation(data.running_update_reconciliation ?? null);
        setSelfReflectionLog(data.self_reflection_log ?? []);
      }
      if (countsRes.ok) {
        const countsData = await countsRes.json();
        if (!isCurrent()) return;
        const nextCounts = countsData.counts ?? {};
        const nextActiveTasks = countsData.active_tasks ?? {};
        setTaskCounts(prev => preserveRecordValues(prev, nextCounts));
        setActiveTasks(prev => preserveRecordValues(prev, nextActiveTasks));
      }
      // Await the version lifecycle that was already running in parallel (full load only).
      if (versionLifecyclePromise) {
        const lifecycle = await versionLifecyclePromise;
        if (!isCurrent()) return;
        setOpenVersionNumber(lifecycle?.current_version?.version_number ?? null);
        setCurrentVersion(lifecycle?.current_version ?? null);
      }
    } catch (err) {
      if (!isCurrent()) return;
      console.error('Failed to fetch directions:', err);
      addToast('Failed to load directions', 'error');
    }
    if (!lite && isCurrent()) setLoading(false);
  }

  const triggerCycle = useCallback(async (phase: 'design_doc' | 'strategic_review' | 'full') => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    const apiPhase = phase === 'full' ? 'design_doc' : phase;
    const labels = {
      design_doc: 'Architecture review',
      strategic_review: 'Strategic review',
      full: 'Full cycle',
    } as const;
    try {
      const res = await apiFetch(
        `/api/admin/projects/${scope.projectId}/strategic-cycle?phase=${apiPhase}`,
        { method: 'POST' },
      );
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        if (!projectMutationScopeIsCurrent(scope)) return;
        throw new Error(body?.detail || `HTTP ${res.status}`);
      }
      if (!projectMutationScopeIsCurrent(scope)) return;
      addToast(`${labels[phase]} triggered`, 'success');
    } catch (err) {
      if (!projectMutationScopeIsCurrent(scope)) return;
      addToast(
        `Failed to trigger ${labels[phase].toLowerCase()}: ${err instanceof Error ? err.message : String(err)}`,
        'error',
      );
    }
  }, [selectedProjectId, addToast]);

  const addDirection = useCallback(async () => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!form.newText.trim() || !scope) return;
    setError(null);
    try {
      const body: Record<string, unknown> = { text: form.newText.trim(), priority: form.newPriority, paths: form.newPathTags };
      if (form.newCategory) body.category = form.newCategory;
      body.completion_criteria = form.newCompletionCriteria.trim() || null;
      body.success_evidence = form.newSuccessEvidence.trim() || null;
      body.retirement_condition = form.newRetirementCondition.trim() || null;
      // Atomic born-staged create: when the operator opted to stage for the next
      // version, the backend stamps staged_for_version from the open version in a
      // single create call (404s if none is open), so the direction never passes
      // through a research-eligible active state (no create-then-edit race).
      if (form.newStaged && openVersionNumber != null) body.staged = true;
      if (form.newLinkedGapId) body.gap_id = form.newLinkedGapId;
      // Reuse one client idempotency identity across double-clicks / failed
      // retries / lost-response replays so the backend collapses them into a
      // single direction. The key is minted lazily here and held until the add
      // succeeds; resetAddForm() clears it so the next logical add mints a fresh
      // one. A thrown error leaves the key in place for the retry.
      form.addIdempotencyKeyRef.current ??= generateIdempotencyKey();
      body.idempotency_key = form.addIdempotencyKeyRef.current;
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions`, {
        method: 'POST',
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error('Failed to add direction');
      // A duplicate replay (`status: "duplicate"`) is the backend telling us the
      // logical add already landed — that is a clean success, not an error.
      await res.json().catch(() => ({}));
      if (!projectMutationScopeIsCurrent(scope)) return;
      form.resetAddForm();
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      form.markAddAttemptFailed();
      setError('Failed to add direction. Please try again.');
    }
  }, [selectedProjectId, openVersionNumber, form.newText, form.newPriority, form.newPathTags, form.newCategory, form.newCompletionCriteria, form.newSuccessEvidence, form.newRetirementCondition, form.newStaged, form.newLinkedGapId, form.resetAddForm, form.markAddAttemptFailed]);

  const updateDirection = useCallback(async (id: string) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const costBudget = form.editCostBudget.trim() !== '' ? parseFloat(form.editCostBudget) : null;
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ text: form.editText, priority: form.editPriority, status: form.editStatus, paths: form.editPathTags, category: form.editCategory ?? null, completion_criteria: form.editCompletionCriteria.trim() || null, success_evidence: form.editSuccessEvidence.trim() || null, retirement_condition: form.editRetirementCondition.trim() || null, cost_budget_usd: (costBudget != null && !Number.isNaN(costBudget)) ? costBudget : null }),
      });
      if (!res.ok) throw new Error('Failed to update direction');
      if (!projectMutationScopeIsCurrent(scope)) return;
      form.setEditingId(null);
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to update direction. Please try again.');
    }
  }, [selectedProjectId, form.editCostBudget, form.editText, form.editPriority, form.editStatus, form.editPathTags, form.editCategory, form.editCompletionCriteria, form.editSuccessEvidence, form.editRetirementCondition, form.setEditingId]);

  const quickToggleStatus = useCallback(async (id: string, currentStatus: 'active' | 'paused') => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    const newStatus = currentStatus === 'active' ? 'paused' : 'active';
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: newStatus }),
      });
      if (!res.ok) throw new Error('Failed to update status');
      if (!projectMutationScopeIsCurrent(scope)) return;
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to update status. Please try again.');
    }
  }, [selectedProjectId]);

  const reactivateDirection = useCallback(async (id: string) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'active' }),
      });
      if (!res.ok) throw new Error('Failed to reactivate direction');
      if (!projectMutationScopeIsCurrent(scope)) return;
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to reactivate direction. Please try again.');
    }
  }, [selectedProjectId]);

  // Activate a staged direction immediately instead of waiting for the next
  // version to open. Reuses the PATCH status:'active' transition.
  const activateStagedDirection = useCallback(async (id: string) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'active' }),
      });
      if (!res.ok) throw new Error('Failed to activate staged direction');
      if (!projectMutationScopeIsCurrent(scope)) return;
      addToast('Staged direction activated', 'success');
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to activate staged direction. Please try again.');
    }
  }, [selectedProjectId, addToast]);

  // Retarget an already-staged direction to a different (current-or-future)
  // version. Sends only staged_for_version so the backend keeps the direction
  // staged and re-binds its marker; it validates the target (rejecting an
  // already-promoted version) and 409s if the direction is no longer staged.
  const retargetStagedDirection = useCallback(async (id: string, version: number) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ staged_for_version: version }),
      });
      if (!res.ok) throw new Error('Failed to retarget staged direction');
      if (!projectMutationScopeIsCurrent(scope)) return;
      addToast(`Retargeted to v${version}`, 'success');
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to retarget staged direction. Please try again.');
    }
  }, [selectedProjectId, addToast]);

  // Cancel a staged direction. DELETE leaves a tombstone (handled backend-side)
  // so the strategist won't immediately re-stage the same idea.
  const cancelStagedDirection = useCallback(async (id: string) => {
    if (!window.confirm('Cancel this staged direction? It will not auto-activate at the next version.')) return;
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}`, {
        method: 'DELETE',
      });
      if (!res.ok) throw new Error('Failed to cancel staged direction');
      if (!projectMutationScopeIsCurrent(scope)) return;
      addToast('Staged direction cancelled', 'info');
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to cancel staged direction. Please try again.');
    }
  }, [selectedProjectId, addToast]);

  const unholdDirection = useCallback(async (id: string) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}/unhold`, {
        method: 'POST',
      });
      if (!res.ok) throw new Error('Failed to unhold direction');
      if (!projectMutationScopeIsCurrent(scope)) return;
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to unhold direction. Please try again.');
    }
  }, [selectedProjectId]);

  const markDirectionComplete = useCallback(async (id: string) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'completed' }),
      });
      if (!res.ok) throw new Error('Failed to mark direction as complete');
      if (!projectMutationScopeIsCurrent(scope)) return;
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to mark direction as complete. Please try again.');
    }
  }, [selectedProjectId]);

  const approveProposal = useCallback(async (id: string) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'active' }),
      });
      if (!res.ok) throw new Error('Failed to approve proposal');
      if (!projectMutationScopeIsCurrent(scope)) return;
      addToast('Direction approved and activated', 'success');
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to approve proposal. Please try again.');
    }
  }, [selectedProjectId, addToast]);

  const rejectProposal = useCallback(async (id: string) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}`, {
        method: 'DELETE',
      });
      if (!res.ok) throw new Error('Failed to reject proposal');
      if (!projectMutationScopeIsCurrent(scope)) return;
      addToast('Direction proposal rejected', 'info');
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to reject proposal. Please try again.');
    }
  }, [selectedProjectId, addToast]);

  const reviveProposal = useCallback(async (id: string) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ status: 'proposed' }),
      });
      if (!res.ok) throw new Error('Failed to revive proposal');
      if (!projectMutationScopeIsCurrent(scope)) return;
      addToast('Expired proposal revived', 'success');
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to revive proposal. Please try again.');
    }
  }, [selectedProjectId, addToast]);

  const applyDirectionUpdate = useCallback(async (d: Direction) => {
    if (!d.pending_update) return;
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const updated: Record<string, unknown> = { pending_update: null };
      if (d.pending_update.suggested_text) updated.text = d.pending_update.suggested_text;
      if (d.pending_update.suggested_priority) updated.priority = d.pending_update.suggested_priority;
      if (d.pending_update.suggested_paths) updated.paths = d.pending_update.suggested_paths;
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${d.id}`, {
        method: 'PATCH',
        body: JSON.stringify(updated),
      });
      if (!res.ok) throw new Error('Failed to apply direction update');
      if (!projectMutationScopeIsCurrent(scope)) return;
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to apply direction update. Please try again.');
    }
  }, [selectedProjectId]);

  const dismissDirectionUpdate = useCallback(async (id: string) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}`, {
        method: 'PATCH',
        body: JSON.stringify({ pending_update: null }),
      });
      if (!res.ok) throw new Error('Failed to dismiss direction update');
      if (!projectMutationScopeIsCurrent(scope)) return;
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to dismiss direction update. Please try again.');
    }
  }, [selectedProjectId]);

  const deleteDirection = useCallback(async (id: string) => {
    if (!window.confirm('Are you sure you want to delete this direction?')) return;
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}`, {
        method: 'DELETE',
      });
      if (!res.ok) throw new Error('Failed to delete direction');
      if (!projectMutationScopeIsCurrent(scope)) return;
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to delete direction. Please try again.');
    }
  }, [selectedProjectId]);

  const toggleAutonomous = useCallback(async () => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    const newValue = !autonomousEnabled;
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/autonomous`, {
        method: 'PATCH',
        body: JSON.stringify({ enabled: newValue }),
      });
      if (!res.ok) throw new Error('Failed to toggle autonomous mode');
      if (!projectMutationScopeIsCurrent(scope)) return;
      setAutonomousEnabled(newValue);
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to toggle autonomous mode. Please try again.');
    }
  }, [selectedProjectId, autonomousEnabled]);

  const reorderDirections = useCallback(async (newOrder: Direction[]) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    // Capture previous directions via ref to avoid including `directions` in deps.
    const previousDirections = directionsRef.current;
    setDirections(prev => {
      const nonActive = prev.filter(d => d.status !== 'active');
      return [...newOrder, ...nonActive];
    });
    setError(null);
    try {
      const nonActive = previousDirections.filter(d => d.status !== 'active');
      const allOrdered = [...newOrder, ...nonActive];
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/reorder`, {
        method: 'PATCH',
        body: JSON.stringify({ direction_ids: allOrdered.map(d => d.id) }),
      });
      if (!res.ok) throw new Error('Failed to reorder directions');
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setDirections(previousDirections);
      setError('Failed to reorder directions. Please try again.');
    }
  }, [selectedProjectId]);

  const linkDirectionToGap = useCallback(async (directionId: string, gapId: string) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${directionId}`, {
        method: 'PATCH',
        body: JSON.stringify({ gap_id: gapId }),
      });
      if (!res.ok) throw new Error('Failed to link direction to gap');
      if (!projectMutationScopeIsCurrent(scope)) return;
      addToast('Direction linked to gap', 'success');
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to link direction to gap. Please try again.');
    }
  }, [selectedProjectId, addToast]);

  const decideChurnRetirement = useCallback(async (id: string, decision: 'KEEP' | 'RETIRE', rationale?: string) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setError(null);
    try {
      await submitChurnRetirementDecision(scope.projectId, id, decision, rationale);
      if (!projectMutationScopeIsCurrent(scope)) return;
      addToast(decision === 'KEEP' ? 'Direction kept — retirement recommendation cleared' : 'Direction retired', 'success');
      fetchDirectionsLatestRef.current();
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to submit churn-retirement decision. Please try again.');
    }
  }, [selectedProjectId, addToast]);

  const triggerResearch = useCallback(async (id: string, bypass = false) => {
    const scope = captureProjectMutationScope(selectedProjectId);
    if (!scope) return;
    setResearchingIds(prev => ({ ...prev, [id]: true }));
    setForceConfirmId(null);
    setError(null);
    try {
      const res = await apiFetch(`/api/admin/projects/${scope.projectId}/directions/${id}/research${bypass ? '?bypass_cooldown=true' : ''}`, { method: 'POST' });
      if (!res.ok) throw new Error('Failed to trigger research');
      if (!projectMutationScopeIsCurrent(scope)) return;
      setResearchSuccessIds(prev => ({ ...prev, [id]: true }));
      setTimeout(() => {
        if (!projectMutationScopeIsCurrent(scope)) return;
        setResearchSuccessIds(prev => ({ ...prev, [id]: false }));
      }, 3000);
    } catch {
      if (!projectMutationScopeIsCurrent(scope)) return;
      setError('Failed to trigger research. Please try again.');
    } finally {
      if (projectMutationScopeIsCurrent(scope)) {
        setResearchingIds(prev => ({ ...prev, [id]: false }));
      }
    }
  }, [selectedProjectId]);

  function guardProjectMutation<TArgs extends unknown[]>(
    handler: (...args: TArgs) => Promise<void>,
  ): (...args: TArgs) => Promise<void> {
    const projectId = selectedProjectId;
    const generation = projectScopeRef.current.generation;
    return async (...args: TArgs) => {
      if (
        !projectId
        || !canOperateProjectRef.current
        || !projectScopeIsCurrent(projectId, generation)
      ) return;
      await handler(...args);
    };
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
    selfReflectionLog,
    openVersionNumber,
    currentVersion,
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
    triggerCycle: guardProjectMutation(triggerCycle),
    addDirection: guardProjectMutation(addDirection),
    updateDirection: guardProjectMutation(updateDirection),
    quickToggleStatus: guardProjectMutation(quickToggleStatus),
    reactivateDirection: guardProjectMutation(reactivateDirection),
    activateStagedDirection: guardProjectMutation(activateStagedDirection),
    retargetStagedDirection: guardProjectMutation(retargetStagedDirection),
    cancelStagedDirection: guardProjectMutation(cancelStagedDirection),
    unholdDirection: guardProjectMutation(unholdDirection),
    markDirectionComplete: guardProjectMutation(markDirectionComplete),
    approveProposal: guardProjectMutation(approveProposal),
    rejectProposal: guardProjectMutation(rejectProposal),
    reviveProposal: guardProjectMutation(reviveProposal),
    applyDirectionUpdate: guardProjectMutation(applyDirectionUpdate),
    dismissDirectionUpdate: guardProjectMutation(dismissDirectionUpdate),
    deleteDirection: guardProjectMutation(deleteDirection),
    toggleAutonomous: guardProjectMutation(toggleAutonomous),
    reorderDirections: guardProjectMutation(reorderDirections),
    triggerResearch: guardProjectMutation(triggerResearch),
    decideChurnRetirement: guardProjectMutation(decideChurnRetirement),
    linkDirectionToGap: guardProjectMutation(linkDirectionToGap),
  };
}
