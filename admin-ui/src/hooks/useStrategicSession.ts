/* eslint-disable react-hooks/refs, react-hooks/set-state-in-effect */
import { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import {
  checkInStrategicSession,
  startStrategicSession,
  stopStrategicSession,
  updateStrategicWorkbenchState,
  type StrategicLivenessResponse,
  type StrategicResidentRunnerOption,
  type StrategicResidentSessionSnapshot,
  type StrategicWorkbenchState,
} from '../strategicStateApi';
import { parseArmingOutcome, workbenchArmingToastCopy } from '../components/messageArming';
import { buildOptimisticStrategicWorkbench } from '../utils/strategicWorkbenchSession';
import { parseTimestamp } from '../utils/time';
import type { useStrategicReadModel } from './useStrategicReadModel';

type StrategicReadModel = ReturnType<typeof useStrategicReadModel>;
type StrategicSessionRefs = StrategicWorkbenchState['selected_refs'];

interface UseStrategicSessionOptions {
  selectedProjectId: string | null;
  canOperateProject: boolean;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  strategicReadModel: StrategicReadModel;
  /**
   * Lightweight liveness probe ({@link useStrategicLiveness}) used purely as a
   * FALLBACK source for the mode indicator and resident-session liveness chip on
   * reload, before the heavy read model hydrates. Once `strategicReadModel.data`
   * lands for the selected project its values take precedence, so no flicker.
   */
  strategicLiveness?: StrategicLivenessResponse | null;
}

export function useStrategicSession({
  selectedProjectId,
  canOperateProject,
  addToast,
  strategicReadModel,
  strategicLiveness,
}: UseStrategicSessionOptions) {
  const selectedProjectIdRef = useRef(selectedProjectId);
  selectedProjectIdRef.current = selectedProjectId;
  const canOperateProjectRef = useRef(canOperateProject);
  canOperateProjectRef.current = canOperateProject;
  const hasCurrentOperateAccess = useCallback(
    (projectId: string) => canOperateProjectRef.current && selectedProjectIdRef.current === projectId,
    [],
  );

  const [optimisticStrategicWorkbench, setOptimisticStrategicWorkbench] = useState<StrategicWorkbenchState | null>(null);
  const [savingStrategicWorkbench, setSavingStrategicWorkbench] = useState(false);
  const [checkingInStrategicSession, setCheckingInStrategicSession] = useState(false);
  const [strategicSessionBusy, setStrategicSessionBusy] = useState(false);
  const [strategicWorkdayRecoveryTick, setStrategicWorkdayRecoveryTick] = useState(0);
  const strategicWorkbenchStateRef = useRef<StrategicWorkbenchState | null>(null);
  const strategicWorkbenchSaveSeqRef = useRef(0);
  const strategicWorkbenchSaveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const strategicCheckInIdempotencyKeyRef = useRef<string | null>(null);
  const strategicWorkdayEnsuredKeysRef = useRef<Set<string>>(new Set());
  const strategicWorkdayEnsureInflightRef = useRef<
    Map<string, ReturnType<typeof startStrategicSession>>
  >(new Map());
  const strategicWorkdayExplicitlyStoppedProjectRef = useRef<string | null>(null);
  const strategicWorkdayExplicitStartRef = useRef<{
    projectId: string;
    sessionId: string | null;
    workerGeneration: number | null;
  } | null>(null);
  const strategicRunnerSelectionRef = useRef<{
    touched: boolean;
    value: string | null;
  }>({ touched: false, value: null });
  const strategicWorkbenchOperatorSurfaceTouchedRef = useRef(false);
  const strategicWorkbenchRestoreProjectRef = useRef<string | null>(null);

  useEffect(() => {
    strategicWorkbenchStateRef.current = null;
    setOptimisticStrategicWorkbench(null);
    setSavingStrategicWorkbench(false);
    strategicWorkbenchSaveSeqRef.current += 1;
    strategicWorkbenchSaveQueueRef.current = Promise.resolve();
    strategicWorkbenchOperatorSurfaceTouchedRef.current = false;
    strategicWorkbenchRestoreProjectRef.current = null;
    strategicCheckInIdempotencyKeyRef.current = null;
    strategicWorkdayEnsuredKeysRef.current.clear();
    strategicWorkdayExplicitlyStoppedProjectRef.current = null;
    strategicWorkdayExplicitStartRef.current = null;
    strategicRunnerSelectionRef.current = { touched: false, value: null };
    setCheckingInStrategicSession(false);
    setStrategicSessionBusy(false);
  }, [selectedProjectId]);

  useEffect(() => {
    if (canOperateProject) return;
    // Invalidate saves which were queued by callbacks rendered before the
    // downgrade. The queued continuation also checks the live capability ref
    // before making its write.
    strategicWorkbenchSaveSeqRef.current += 1;
    strategicWorkbenchSaveQueueRef.current = Promise.resolve();
    setSavingStrategicWorkbench(false);
    setCheckingInStrategicSession(false);
    setStrategicSessionBusy(false);
  }, [canOperateProject]);

  const strategicReadModelWorkbench = strategicReadModel.data?.project_id === selectedProjectId
    ? strategicReadModel.data?.workbench ?? null
    : null;
  const selectedStrategicReadModel = strategicReadModel.data?.project_id === selectedProjectId
    ? strategicReadModel.data
    : null;
  // Fast-path liveness probe, used only as a FALLBACK until the heavy read model
  // hydrates for this project. Guarded on project id so a stale probe from a
  // previous project is never applied.
  const selectedStrategicLiveness =
    strategicLiveness && strategicLiveness.project_id === selectedProjectId
      ? strategicLiveness
      : null;

  // Resident-session fields: the heavy read model is fully authoritative ONCE
  // it has loaded for this project (including a legitimate null session), so it
  // is not `??`-merged with the probe — that would let a stale liveness snapshot
  // resurface after the read model reports the session gone. The liveness probe
  // is used ONLY while the read model is still absent (pre-hydration reload).
  const residentSource = selectedStrategicReadModel ?? selectedStrategicLiveness;
  const residentStrategicSession =
    (residentSource as { resident_session?: StrategicResidentSessionSnapshot | null } | null)?.resident_session
    ?? null;
  const strategicSessionActive = residentStrategicSession?.status === 'active' && !residentStrategicSession?.stop_requested;
  const strategicSessionStatus = residentStrategicSession?.status ?? null;
  const residentStrategicSessionAvailability =
    (residentSource as {
      resident_session_availability?: { available?: boolean; state?: string | null; reason?: string | null } | null;
    } | null)?.resident_session_availability ?? null;
  const residentStrategicSessionAvailable = residentStrategicSessionAvailability?.available !== false;
  const strategicSessionState = residentStrategicSessionAvailability?.state
    ?? (strategicSessionActive
      ? 'active'
      : residentSource
        ? residentStrategicSessionAvailable ? 'ready' : 'off'
        : 'off');
  const strategicSessionReason = residentStrategicSessionAvailability?.reason ?? null;
  const strategicWorkbenchState = optimisticStrategicWorkbench ?? strategicReadModelWorkbench;
  // Mode indicator source: prefer the workbench state (optimistic or heavy read
  // model), fall back to the fast liveness probe so the mode chip renders on
  // reload before the heavy fetch lands. Deliberately does NOT synthesize a full
  // workbench object — active_surface stays absent until the read model loads,
  // so the tab-restore effect never fires on a phantom surface.
  const strategicInteractionMode =
    strategicWorkbenchState?.interaction_mode
    ?? selectedStrategicLiveness?.interaction_mode
    ?? null;
  const residentStrategicSessionRunners =
    (selectedStrategicReadModel as { resident_session_runners?: StrategicResidentRunnerOption[] | null } | null)
      ?.resident_session_runners
    ?? selectedStrategicLiveness?.resident_session_runners
    ?? null;
  const strategicSessionSelectedRunnerId =
    strategicWorkbenchState?.preferred_runner_id
    ?? residentStrategicSession?.runner_id
    ?? null;
  const refreshStrategicReadModel = strategicReadModel.refresh;

  useEffect(() => {
    const explicitStart = strategicWorkdayExplicitStartRef.current;
    const observedSessionId = residentStrategicSession?.session_id ?? null;
    const observedGeneration = residentStrategicSession?.worker_generation ?? null;
    const matchesExplicitStart = Boolean(
      explicitStart
      && explicitStart.projectId === selectedProjectId
      && explicitStart.sessionId === observedSessionId
      && (
        explicitStart.workerGeneration === null
        || explicitStart.workerGeneration === observedGeneration
      )
    );
    if (matchesExplicitStart && strategicSessionActive) {
      const ensuredRunnerId = residentStrategicSession?.runner_id
        ?? strategicSessionSelectedRunnerId;
      strategicWorkdayEnsuredKeysRef.current.add(
        `${selectedProjectId}:continuous:${ensuredRunnerId ?? 'auto'}:hydrate`,
      );
      strategicWorkdayExplicitStartRef.current = null;
      strategicWorkbenchOperatorSurfaceTouchedRef.current = false;
    }
  }, [
    residentStrategicSession,
    selectedProjectId,
    strategicSessionActive,
    strategicSessionSelectedRunnerId,
  ]);

  // Polling snapshots can remain byte-for-byte unchanged while a provisional
  // worker crosses the backend's five-minute stale-claim threshold. Schedule a
  // single local wake-up at that boundary so recovery does not depend on a
  // reload or on some unrelated read-model field changing.
  useEffect(() => {
    if (
      !canOperateProject
      ||
      !selectedProjectId
      || strategicInteractionMode !== 'continuous'
      || strategicSessionState?.trim().toLowerCase() !== 'starting'
    ) return;
    const workerTimestamp = parseTimestamp(
      residentStrategicSession?.started_at
      ?? null,
    ).getTime();
    if (!Number.isFinite(workerTimestamp)) return;
    const delayMs = 5 * 60_000 - (Date.now() - workerTimestamp);
    if (delayMs <= 0) return;
    const timer = window.setTimeout(
      () => setStrategicWorkdayRecoveryTick(tick => tick + 1),
      delayMs + 25,
    );
    return () => window.clearTimeout(timer);
  }, [
    selectedProjectId,
    canOperateProject,
    strategicInteractionMode,
    strategicSessionState,
    residentStrategicSession?.started_at,
  ]);

  // Entering Workday is not the only time the resident must be ensured. A full
  // page reload can hydrate directly into continuous mode, bypassing the mode
  // transition handler entirely. Ensure once per project/mode hydration so the
  // backend can idempotently keep a healthy worker or replace a genuinely stale
  // one. An explicit retry hold is already healthy/dormant and must not be poked.
  useEffect(() => {
    const projectId = selectedProjectId;
    if (!canOperateProject || !projectId || strategicInteractionMode !== 'continuous') {
      // Leaving Workday is an explicit re-entry boundary. A later transition
      // back to continuous may start the resident again.
      strategicWorkdayExplicitlyStoppedProjectRef.current = null;
      strategicWorkdayEnsuredKeysRef.current.clear();
      return;
    }
    // A live operator transition into Workday is followed by an explicit Start
    // after runner selection. Auto-ensure is only for reload hydration/recovery;
    // otherwise a stale saved runner can win the start race before the new
    // selection is persisted.
    if (strategicWorkbenchOperatorSurfaceTouchedRef.current) return;
    if (strategicWorkdayExplicitlyStoppedProjectRef.current === projectId) return;
    // Retry-wait is dormant/healthy only while the session itself is active.
    // End/stop paths may preserve the last worker fields, and that stale value
    // must not suppress the idempotent start that restores Workday on reload.
    if (strategicSessionActive && residentStrategicSession?.worker_state === 'retry_wait') return;
    const workerState = residentStrategicSession?.worker_state?.trim().toLowerCase() ?? null;
    const lifecycleState = strategicSessionState?.trim().toLowerCase() ?? null;
    const workerTimestamp = parseTimestamp(
      residentStrategicSession?.started_at
      ?? null,
    ).getTime();
    const startingIsWedged = lifecycleState === 'starting'
      && (!Number.isFinite(workerTimestamp) || Date.now() - workerTimestamp > 5 * 60_000);
    const needsRecovery = !strategicSessionActive
      || workerState === 'restart_failed'
      || lifecycleState === 'error'
      || lifecycleState === 'blocked'
      || startingIsWedged;
    // Hydration is ensured once, while each observed failed worker generation
    // gets one independent recovery attempt. Keeping the completed epochs in a
    // set prevents a stale polling snapshot from causing a restart loop, but a
    // later generation can still recover on the same page.
    const ensureEpoch = needsRecovery
      ? [
          'recover',
          residentStrategicSession?.session_id ?? 'none',
          residentStrategicSession?.worker_generation ?? 0,
          residentStrategicSession?.status ?? 'none',
          workerState ?? lifecycleState ?? 'unknown',
        ].join(':')
      : 'hydrate';
    const preferredRunnerId = strategicSessionSelectedRunnerId;
    const ensureKey = `${projectId}:continuous:${preferredRunnerId ?? 'auto'}:${ensureEpoch}`;
    if (strategicWorkdayEnsuredKeysRef.current.has(ensureKey)) return;
    let cancelled = false;
    let retryTimer: number | null = null;
    let retryAttempt = 0;
    const ensureWorkday = async () => {
      if (!hasCurrentOperateAccess(projectId) || strategicWorkdayExplicitlyStoppedProjectRef.current === projectId) return;
      try {
        let request = strategicWorkdayEnsureInflightRef.current.get(ensureKey);
        if (!request) {
          if (!hasCurrentOperateAccess(projectId)) return;
          request = startStrategicSession(projectId, {
            ...(preferredRunnerId ? { runner_id: preferredRunnerId } : {}),
            expected_interaction_mode: 'continuous',
          });
          strategicWorkdayEnsureInflightRef.current.set(ensureKey, request);
          void request.finally(() => {
            if (strategicWorkdayEnsureInflightRef.current.get(ensureKey) === request) {
              strategicWorkdayEnsureInflightRef.current.delete(ensureKey);
            }
          }).catch(() => undefined);
        }
        const response = await request;
        if (response.ensure_outcome === 'mode_changed') {
          if (cancelled || !hasCurrentOperateAccess(projectId)) return;
          const retryDelayMs = Math.min(250 * (2 ** retryAttempt), 10_000);
          retryAttempt += 1;
          retryTimer = window.setTimeout(() => {
            retryTimer = null;
            if (!hasCurrentOperateAccess(projectId) || strategicWorkdayExplicitlyStoppedProjectRef.current === projectId) return;
            void ensureWorkday();
          }, retryDelayMs);
          return;
        }
        if (cancelled || !hasCurrentOperateAccess(projectId)) return;
        const returnedSession = response.session;
        const returnedWorker = returnedSession?.worker_state?.trim().toLowerCase();
        const returnedStatus = returnedSession?.status?.trim().toLowerCase();
        const returnedSessionId = returnedSession?.session_id?.trim();
        const returnedStartedAtValue = returnedSession?.started_at?.trim();
        const returnedStartedAt = parseTimestamp(returnedStartedAtValue ?? null).getTime();
        // A successful HTTP response is not a successful ensure unless it
        // carries an actual session snapshot. During a replacement race the
        // backend may briefly return null (or an all-empty compatibility
        // object); marking that epoch complete would strand Workday until a
        // reload.
        const returnedSessionIsEmpty = !returnedSession
          || !(returnedSessionId || returnedWorker || returnedStatus || returnedStartedAtValue);
        const returnedStartingIsWedged = returnedWorker === 'starting'
          && (
            !Number.isFinite(returnedStartedAt)
            || Date.now() - returnedStartedAt > 5 * 60_000
          );
        const returnedNeedsRetry = returnedSessionIsEmpty
          || returnedWorker === 'restart_failed'
          || returnedStatus === 'blocked'
          || returnedStatus === 'error'
          || returnedStatus === 'ended'
          || returnedStartingIsWedged;
        if (returnedNeedsRetry) {
          const retryDelayMs = Math.min(500 * (2 ** retryAttempt), 10_000);
          retryAttempt += 1;
          retryTimer = window.setTimeout(() => {
            retryTimer = null;
            if (!hasCurrentOperateAccess(projectId)) return;
            void ensureWorkday();
          }, retryDelayMs);
          return;
        }
        strategicWorkdayEnsuredKeysRef.current.add(ensureKey);
        if (hasCurrentOperateAccess(projectId)) void refreshStrategicReadModel();
      } catch {
        if (cancelled || !hasCurrentOperateAccess(projectId)) return;
        const retryDelayMs = Math.min(250 * (2 ** retryAttempt), 10_000);
        retryAttempt += 1;
        retryTimer = window.setTimeout(() => {
          retryTimer = null;
          if (!hasCurrentOperateAccess(projectId) || strategicWorkdayExplicitlyStoppedProjectRef.current === projectId) return;
          void ensureWorkday();
        }, retryDelayMs);
      }
    };
    void ensureWorkday();
    return () => {
      cancelled = true;
      if (retryTimer != null) window.clearTimeout(retryTimer);
    };
  }, [
    selectedProjectId,
    canOperateProject,
    strategicInteractionMode,
    strategicSessionActive,
    residentStrategicSession?.worker_state,
    residentStrategicSession?.worker_generation,
    residentStrategicSession?.session_id,
    residentStrategicSession?.status,
    residentStrategicSession?.started_at,
    strategicSessionState,
    strategicSessionSelectedRunnerId,
    refreshStrategicReadModel,
    strategicWorkdayRecoveryTick,
    hasCurrentOperateAccess,
  ]);

  useEffect(() => {
    strategicWorkbenchStateRef.current = strategicWorkbenchState;
  }, [strategicWorkbenchState]);

  // Clear the optimistic state once no save is pending and the server read
  // model has caught up to (or surpassed) the optimistic timestamp.
  useEffect(() => {
    if (savingStrategicWorkbench) return;
    if (!optimisticStrategicWorkbench || !strategicReadModelWorkbench) return;
    const optimisticTs = parseTimestamp(optimisticStrategicWorkbench.last_interaction_at).getTime();
    const serverTs = parseTimestamp(strategicReadModelWorkbench.last_interaction_at).getTime();
    if (serverTs >= optimisticTs) {
      setOptimisticStrategicWorkbench(null);
    }
  }, [savingStrategicWorkbench, optimisticStrategicWorkbench, strategicReadModelWorkbench]);

  const persistStrategicWorkbench = useCallback((patch: Partial<StrategicWorkbenchState>) => {
    const projectId = selectedProjectId;
    if (!projectId || !hasCurrentOperateAccess(projectId)) return;
    if (patch.interaction_mode !== undefined) {
      strategicWorkbenchOperatorSurfaceTouchedRef.current = true;
    }
    const saveSeq = strategicWorkbenchSaveSeqRef.current + 1;
    strategicWorkbenchSaveSeqRef.current = saveSeq;
    const base = strategicWorkbenchStateRef.current ?? strategicReadModelWorkbench;
    const optimistic = buildOptimisticStrategicWorkbench(base, patch);
    strategicWorkbenchStateRef.current = optimistic;
    setOptimisticStrategicWorkbench(optimistic);
    setSavingStrategicWorkbench(true);
    const save = strategicWorkbenchSaveQueueRef.current
      .catch(() => undefined)
      .then(async () => {
        if (!hasCurrentOperateAccess(projectId)) return;
        try {
          const result = await updateStrategicWorkbenchState(projectId, patch);
          const armingOutcome = hasCurrentOperateAccess(projectId) ? parseArmingOutcome(result.arming) : null;
          const armingToast = workbenchArmingToastCopy(armingOutcome);
          if (armingToast) addToast(armingToast, armingOutcome === 'not_armed' ? 'info' : 'success');
          if (!hasCurrentOperateAccess(projectId) || strategicWorkbenchSaveSeqRef.current !== saveSeq) return;
          const saved = result.workbench;
          strategicWorkbenchStateRef.current = saved;
          setOptimisticStrategicWorkbench(saved);
          void strategicReadModel.refresh();
        } catch {
          if (hasCurrentOperateAccess(projectId) && strategicWorkbenchSaveSeqRef.current === saveSeq) {
            strategicWorkbenchStateRef.current = base;
            setOptimisticStrategicWorkbench(base);
            if (patch.preferred_runner_id !== undefined) {
              strategicRunnerSelectionRef.current = {
                touched: false,
                value: base?.preferred_runner_id ?? null,
              };
            }
            addToast('Could not save Strategic session', 'error');
          }
        } finally {
          if (hasCurrentOperateAccess(projectId) && strategicWorkbenchSaveSeqRef.current === saveSeq) setSavingStrategicWorkbench(false);
        }
      });
    strategicWorkbenchSaveQueueRef.current = save.then(
      () => undefined,
      () => undefined,
    );
  }, [selectedProjectId, addToast, strategicReadModel, strategicReadModelWorkbench, hasCurrentOperateAccess]);

  const updateStrategicSessionRefs = useCallback((updateRefs: (refs: StrategicSessionRefs) => StrategicSessionRefs) => {
    const baseRefs = (strategicWorkbenchStateRef.current ?? strategicReadModelWorkbench)?.selected_refs ?? [];
    persistStrategicWorkbench({ selected_refs: updateRefs(baseRefs) });
  }, [persistStrategicWorkbench, strategicReadModelWorkbench]);

  const handleSelectStrategicRunner = useCallback((runnerId: string | null) => {
    const projectId = selectedProjectIdRef.current;
    if (!projectId || !hasCurrentOperateAccess(projectId)) return;
    strategicRunnerSelectionRef.current = { touched: true, value: runnerId };
    persistStrategicWorkbench({ preferred_runner_id: runnerId });
  }, [persistStrategicWorkbench, hasCurrentOperateAccess]);

  const handleStrategicCheckIn = useCallback(async () => {
    const projectId = selectedProjectId;
    if (!projectId || !hasCurrentOperateAccess(projectId) || checkingInStrategicSession) return;
    setCheckingInStrategicSession(true);
    try {
      const workbenchContext = strategicWorkbenchStateRef.current ?? strategicReadModelWorkbench;
      if (!strategicCheckInIdempotencyKeyRef.current) {
        strategicCheckInIdempotencyKeyRef.current = `strategic-check-in:${projectId}:${Date.now()}`;
      }
      const checkInResponse = await checkInStrategicSession(projectId, {
        kind: 'manual',
        workbench_context: workbenchContext ? { ...workbenchContext } : undefined,
        idempotency_key: strategicCheckInIdempotencyKeyRef.current,
      });
      if (!hasCurrentOperateAccess(projectId)) return;
      strategicCheckInIdempotencyKeyRef.current = null;
      addToast('Strategic check-in recorded', 'success');
      if (strategicReadModel.data) {
        strategicReadModel.applySessionResponse(checkInResponse);
      } else {
        void strategicReadModel.refresh();
      }
    } catch {
      if (hasCurrentOperateAccess(projectId)) {
        addToast('Could not record Strategic check-in', 'error');
      }
    } finally {
      if (hasCurrentOperateAccess(projectId)) setCheckingInStrategicSession(false);
    }
  }, [checkingInStrategicSession, addToast, strategicReadModel, strategicReadModelWorkbench, selectedProjectId, hasCurrentOperateAccess]);

  const handleStartStrategicSession = useCallback(async () => {
    const projectId = selectedProjectId;
    if (!projectId || !hasCurrentOperateAccess(projectId) || strategicSessionBusy) return;
    strategicWorkdayExplicitlyStoppedProjectRef.current = null;
    setStrategicSessionBusy(true);
    try {
      // Wait before reading the selection: a failed save reconciles the
      // optimistic runner ref to the still-visible persisted value.
      await strategicWorkbenchSaveQueueRef.current;
      if (!hasCurrentOperateAccess(projectId)) return;
      const selectedRunnerId = strategicRunnerSelectionRef.current.touched
        ? strategicRunnerSelectionRef.current.value
        : (
          strategicWorkbenchStateRef.current ?? strategicReadModelWorkbench
        )?.preferred_runner_id ?? null;
      const startResponse = await startStrategicSession(
        projectId,
        selectedRunnerId ? { runner_id: selectedRunnerId } : {},
      );
      if (startResponse.session) {
        strategicWorkdayExplicitStartRef.current = {
          projectId,
          sessionId: startResponse.session.session_id ?? null,
          workerGeneration: startResponse.session.worker_generation ?? null,
        };
      }
      if (!hasCurrentOperateAccess(projectId)) return;
      addToast('Strategic session starting', 'success');
      void strategicReadModel.refresh();
    } catch (err) {
      if (hasCurrentOperateAccess(projectId)) {
        addToast(err instanceof Error ? err.message : 'Could not start Strategic session', 'error');
      }
    } finally {
      if (hasCurrentOperateAccess(projectId)) {
        setStrategicSessionBusy(false);
      }
    }
  }, [strategicSessionBusy, addToast, strategicReadModel, strategicReadModelWorkbench, selectedProjectId, hasCurrentOperateAccess]);

  const handleStopStrategicSession = useCallback(async () => {
    const projectId = selectedProjectId;
    if (!projectId || !hasCurrentOperateAccess(projectId) || strategicSessionBusy) return;
    // Suppress auto-ensure immediately so an already-scheduled recovery cannot
    // undo the operator's explicit Stop while the request is in flight.
    strategicWorkdayExplicitlyStoppedProjectRef.current = projectId;
    setStrategicSessionBusy(true);
    try {
      await stopStrategicSession(projectId);
      if (!hasCurrentOperateAccess(projectId)) return;
      addToast('Strategic session stopping', 'success');
      void strategicReadModel.refresh();
    } catch (err) {
      if (strategicWorkdayExplicitlyStoppedProjectRef.current === projectId) {
        strategicWorkdayExplicitlyStoppedProjectRef.current = null;
      }
      if (hasCurrentOperateAccess(projectId)) {
        addToast(err instanceof Error ? err.message : 'Could not stop Strategic session', 'error');
      }
    } finally {
      if (hasCurrentOperateAccess(projectId)) setStrategicSessionBusy(false);
    }
  }, [strategicSessionBusy, addToast, strategicReadModel, selectedProjectId, hasCurrentOperateAccess]);

  const taskContextSessionProps = useMemo(() => ({
    sessionRefs: strategicWorkbenchState?.selected_refs,
    savingSessionRefs: savingStrategicWorkbench,
    onUpdateSessionRefs: updateStrategicSessionRefs,
  }), [strategicWorkbenchState?.selected_refs, savingStrategicWorkbench, updateStrategicSessionRefs]);

  return {
    strategicWorkbenchState,
    strategicInteractionMode,
    residentStrategicSession,
    strategicSessionActive,
    strategicSessionStatus,
    residentStrategicSessionAvailable,
    residentStrategicSessionAvailability,
    strategicSessionState,
    strategicSessionReason,
    residentStrategicSessionRunners,
    strategicSessionSelectedRunnerId,
    savingStrategicWorkbench,
    checkingInStrategicSession,
    strategicSessionBusy,
    taskContextSessionProps,
    persistStrategicWorkbench,
    updateStrategicSessionRefs,
    handleSelectStrategicRunner,
    handleStrategicCheckIn,
    handleStartStrategicSession,
    handleStopStrategicSession,
    strategicWorkbenchOperatorSurfaceTouchedRef,
    strategicWorkbenchRestoreProjectRef,
  };
}
