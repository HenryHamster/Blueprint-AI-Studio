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
import { buildStrategicContextDescriptor } from '../utils/strategicContextDescriptor';
import { parseTimestamp } from '../utils/time';
import type { useStrategicReadModel } from './useStrategicReadModel';

type StrategicReadModel = ReturnType<typeof useStrategicReadModel>;
type StrategicSessionRefs = StrategicWorkbenchState['selected_refs'];

interface UseStrategicSessionOptions {
  selectedProjectId: string | null;
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
  addToast,
  strategicReadModel,
  strategicLiveness,
}: UseStrategicSessionOptions) {
  const selectedProjectIdRef = useRef(selectedProjectId);
  selectedProjectIdRef.current = selectedProjectId;

  const [optimisticStrategicWorkbench, setOptimisticStrategicWorkbench] = useState<StrategicWorkbenchState | null>(null);
  const [savingStrategicWorkbench, setSavingStrategicWorkbench] = useState(false);
  const [checkingInStrategicSession, setCheckingInStrategicSession] = useState(false);
  const [strategicSessionBusy, setStrategicSessionBusy] = useState(false);
  const strategicWorkbenchStateRef = useRef<StrategicWorkbenchState | null>(null);
  const strategicWorkbenchSaveSeqRef = useRef(0);
  const strategicWorkbenchSaveQueueRef = useRef<Promise<void>>(Promise.resolve());
  const strategicCheckInIdempotencyKeyRef = useRef<string | null>(null);
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
    setCheckingInStrategicSession(false);
    setStrategicSessionBusy(false);
  }, [selectedProjectId]);

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
  const residentStrategicSessionAvailable =
    (residentSource as { resident_session_available?: boolean } | null)?.resident_session_available === true;
  const residentStrategicSessionAvailability =
    (residentSource as {
      resident_session_availability?: { available?: boolean; state?: string | null; reason?: string | null } | null;
    } | null)?.resident_session_availability ?? null;
  const strategicSessionState = residentStrategicSessionAvailability?.state
    ?? (strategicSessionActive ? 'active' : residentStrategicSessionAvailable ? 'ready' : 'off');
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

  const chatComposerContextDescriptor = useMemo(
    () => buildStrategicContextDescriptor(selectedStrategicReadModel, strategicWorkbenchState),
    [selectedStrategicReadModel, strategicWorkbenchState],
  );

  useEffect(() => {
    strategicWorkbenchStateRef.current = strategicWorkbenchState;
  }, [strategicWorkbenchState]);

  // Clear the optimistic shadow once no save is pending and the server read
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
    if (!projectId) return;
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
        try {
          const result = await updateStrategicWorkbenchState(projectId, patch);
          const armingOutcome = selectedProjectIdRef.current === projectId ? parseArmingOutcome(result.arming) : null;
          const armingToast = workbenchArmingToastCopy(armingOutcome);
          if (armingToast) addToast(armingToast, armingOutcome === 'not_armed' ? 'info' : 'success');
          if (selectedProjectIdRef.current !== projectId || strategicWorkbenchSaveSeqRef.current !== saveSeq) return;
          const saved = result.workbench;
          strategicWorkbenchStateRef.current = saved;
          setOptimisticStrategicWorkbench(saved);
          void strategicReadModel.refresh();
        } catch {
          if (selectedProjectIdRef.current === projectId && strategicWorkbenchSaveSeqRef.current === saveSeq) {
            strategicWorkbenchStateRef.current = base;
            setOptimisticStrategicWorkbench(base);
            addToast('Could not save Strategic session', 'error');
          }
        } finally {
          if (selectedProjectIdRef.current === projectId && strategicWorkbenchSaveSeqRef.current === saveSeq) setSavingStrategicWorkbench(false);
        }
      });
    strategicWorkbenchSaveQueueRef.current = save.then(
      () => undefined,
      () => undefined,
    );
  }, [selectedProjectId, addToast, strategicReadModel, strategicReadModelWorkbench]);

  const updateStrategicSessionRefs = useCallback((updateRefs: (refs: StrategicSessionRefs) => StrategicSessionRefs) => {
    const baseRefs = (strategicWorkbenchStateRef.current ?? strategicReadModelWorkbench)?.selected_refs ?? [];
    persistStrategicWorkbench({ selected_refs: updateRefs(baseRefs) });
  }, [persistStrategicWorkbench, strategicReadModelWorkbench]);

  const handleSelectStrategicRunner = useCallback((runnerId: string | null) => {
    persistStrategicWorkbench({ preferred_runner_id: runnerId });
  }, [persistStrategicWorkbench]);

  const handleStrategicCheckIn = useCallback(async () => {
    const projectId = selectedProjectId;
    if (!projectId || checkingInStrategicSession) return;
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
      if (selectedProjectIdRef.current !== projectId) return;
      strategicCheckInIdempotencyKeyRef.current = null;
      addToast('Strategic check-in recorded', 'success');
      if (strategicReadModel.data) {
        strategicReadModel.applySessionResponse(checkInResponse);
      } else {
        void strategicReadModel.refresh();
      }
    } catch {
      if (selectedProjectIdRef.current === projectId) {
        addToast('Could not record Strategic check-in', 'error');
      }
    } finally {
      if (selectedProjectIdRef.current === projectId) setCheckingInStrategicSession(false);
    }
  }, [checkingInStrategicSession, addToast, strategicReadModel, strategicReadModelWorkbench, selectedProjectId]);

  const handleStartStrategicSession = useCallback(async () => {
    const projectId = selectedProjectId;
    if (!projectId || strategicSessionBusy) return;
    setStrategicSessionBusy(true);
    try {
      const preferredRunnerId = (strategicWorkbenchStateRef.current ?? strategicReadModelWorkbench)?.preferred_runner_id ?? null;
      await startStrategicSession(projectId, preferredRunnerId ? { runner_id: preferredRunnerId } : {});
      if (selectedProjectIdRef.current !== projectId) return;
      addToast('Strategic session starting', 'success');
      void strategicReadModel.refresh();
    } catch (err) {
      if (selectedProjectIdRef.current === projectId) {
        addToast(err instanceof Error ? err.message : 'Could not start Strategic session', 'error');
      }
    } finally {
      if (selectedProjectIdRef.current === projectId) setStrategicSessionBusy(false);
    }
  }, [strategicSessionBusy, addToast, strategicReadModel, strategicReadModelWorkbench, selectedProjectId]);

  const handleStopStrategicSession = useCallback(async () => {
    const projectId = selectedProjectId;
    if (!projectId || strategicSessionBusy) return;
    setStrategicSessionBusy(true);
    try {
      await stopStrategicSession(projectId);
      if (selectedProjectIdRef.current !== projectId) return;
      addToast('Strategic session stopping', 'success');
      void strategicReadModel.refresh();
    } catch (err) {
      if (selectedProjectIdRef.current === projectId) {
        addToast(err instanceof Error ? err.message : 'Could not stop Strategic session', 'error');
      }
    } finally {
      if (selectedProjectIdRef.current === projectId) setStrategicSessionBusy(false);
    }
  }, [strategicSessionBusy, addToast, strategicReadModel, selectedProjectId]);

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
    chatComposerContextDescriptor,
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
