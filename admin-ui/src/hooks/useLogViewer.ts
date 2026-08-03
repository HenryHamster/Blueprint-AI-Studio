import { useState, useEffect, useRef, useCallback } from 'react';
import type { Task, TaskStatus, TrackLogEntry } from '../types/task';
import type { DeployState } from '../types/settings';
import type { StrategicChallengeSummary } from '../types/digest';
import type { FailingTaskTypeEntry, FailingTaskTypes } from '../types/diagnostics';
import type { ProjectMessage } from '../types/messages';
import type { StrategicStateResponse, StrategicStateCycle } from '../strategicStateApi';
import {
  workbenchMessageMatchesProject,
  workbenchRefetchSurfacesFromMessage,
} from '../types/workbenchEvents';
import { API_BASE_URL, authHeaders, fetchTask } from '../api';

interface UseLogViewerDeps {
  fetchTasksRef: React.MutableRefObject<() => void>;
  setDeployState: (ds: DeployState) => void;
  fetchDirectionsRef?: React.MutableRefObject<() => void>;
  refreshStrategicReadModelRef?: React.MutableRefObject<() => void>;
  refreshMessagesRef?: React.MutableRefObject<(message?: ProjectMessage, canonicalRefetchRequired?: boolean) => void>;
  fetchAlertsRef?: React.MutableRefObject<() => void>;
  refreshFleetRunnersRef?: React.MutableRefObject<() => void>;
  selectedProjectId?: string | null;
  realtimeWsEnabled?: boolean;
  /** Current durable Strategic read-model snapshot, populated by App. */
  strategicReadModelRef?: React.MutableRefObject<StrategicStateResponse | null>;
}

/** Live state captured from a strategic_cycle_started event. */
export interface StrategicCycleProgress {
  task_id: string;
  cycle_id?: string;
  phase_number?: number;
  phase_label?: string;
  phase_state?: string;
  steering_origin?: string;
  steering_excerpt?: string;
}

/** Outcome captured from a strategic_cycle_completed event's completion sub-object. */
export interface StrategicCycleCompletion {
  cycle_id?: string;
  status?: string;
  created_count?: number;
  retired_count?: number;
  staged_count?: number;
  updated_count?: number;
  tasks_spawned?: number;
  directions_changed?: boolean;
  problem_model_changed?: boolean;
  running_update_changed?: boolean;
  created_direction_ids?: string[];
  retired_direction_ids?: string[];
  status_message?: string;
  failure_reason?: string;
  // Compact strategic-judgment-challenge summary; present only when the cycle
  // carried a challenge record (the backend omits it otherwise). The handler
  // passes msg.data.completion verbatim, so no handler change is needed.
  strategic_judgment_challenge?: StrategicChallengeSummary;
}

function strategicCycleIsActive(cycle: StrategicStateCycle | null | undefined): cycle is StrategicStateCycle {
  return Boolean(cycle?.has_active_task && !cycle.stale);
}

function strategicCycleProgressFromDurableState(cycle: StrategicStateCycle): StrategicCycleProgress {
  const phaseNumber = cycle.phase2_task_id ? 2 : 1;
  return {
    task_id: cycle.phase2_task_id ?? cycle.phase1_task_id ?? '',
    cycle_id: cycle.id,
    phase_number: phaseNumber,
    phase_label: phaseNumber === 2 ? 'Strategic review' : 'Architecture review',
    phase_state: cycle.outcome,
  };
}

function strategicCycleCompletionFromDurableState(cycle: StrategicStateCycle): StrategicCycleCompletion {
  const outcome = cycle.outcome.toLowerCase();
  const status = outcome === 'completed'
    ? 'completed'
    : outcome.includes('cancel')
      ? 'CANCELLED'
      : 'FAILED';
  return {
    cycle_id: cycle.id,
    status,
    created_count: cycle.directions_created,
    retired_count: cycle.directions_retired,
    tasks_spawned: cycle.tasks_spawned,
    problem_model_changed: cycle.problem_model_changed,
    running_update_changed: cycle.running_update_changed,
    directions_changed: Boolean(
      cycle.directions_created
      || cycle.directions_retired
      || cycle.problem_model_changed
      || cycle.running_update_changed,
    ),
    status_message: cycle.summary_text ?? undefined,
    failure_reason: cycle.failure_reason ?? undefined,
  };
}

/** One strategic-session transcript delta, from a `strategic_session` event. */
export interface StrategicSessionDelta {
  /** Local monotonic id for stable React keys (the wire has no unique id). */
  id: number;
  session_id?: string;
  task_id?: string;
  seq?: number;
  kind?: string;
  text: string;
  ts?: string;
  /** Optional lifecycle marker: `responding` opens an operator turn (working
   *  indicator), `done` closes it (clears the ephemeral activity strip). */
  status?: string;
}

let _trackLogIdCounter = 0;
let _sessionDeltaIdCounter = 0;

// Cap the live session transcript so a long-running strategic session can't grow
// it without bound; keep the most recent tail when the cap is hit.
const SESSION_TRANSCRIPT_CAP = 2000;
const SESSION_TRANSCRIPT_KEEP = 1500;

const TERMINAL_TASK_STATUSES: ReadonlySet<TaskStatus> = new Set(['COMPLETED', 'FAILED', 'CANCELLED']);
const LOG_STREAM_TASK_STATUSES: ReadonlySet<TaskStatus> = new Set(['IN_PROGRESS', 'PROVISIONING']);
const KNOWN_TASK_STATUSES: ReadonlySet<TaskStatus> = new Set([
  'QUEUED',
  'PROVISIONING',
  'IN_PROGRESS',
  'EVALUATING',
  'BLOCKED',
  'PARKED',
  'COMPLETED',
  'FAILED',
  'CANCELLED',
]);

function isTerminalTaskStatus(status: unknown): status is TaskStatus {
  return typeof status === 'string' && TERMINAL_TASK_STATUSES.has(status as TaskStatus);
}

function isKnownTaskStatus(status: unknown): status is TaskStatus {
  return typeof status === 'string' && KNOWN_TASK_STATUSES.has(status as TaskStatus);
}

function isLogStreamTaskStatus(status: unknown): status is TaskStatus {
  return typeof status === 'string' && LOG_STREAM_TASK_STATUSES.has(status as TaskStatus);
}

function canonicalProjectMessage(value: unknown): ProjectMessage | null {
  if (!value || typeof value !== 'object') return null;
  const candidate = value as Partial<ProjectMessage>;
  if (
    typeof candidate.id !== 'string'
    || typeof candidate.project_id !== 'string'
    || typeof candidate.message_type !== 'string'
    || typeof candidate.from_type !== 'string'
    || typeof candidate.content !== 'string'
    || typeof candidate.status !== 'string'
    || candidate.surface !== 'conversation'
    || !candidate.context
    || typeof candidate.context !== 'object'
    || !Number.isFinite(candidate.sequence)
  ) return null;
  return candidate as ProjectMessage;
}

const STRATEGIC_READ_MODEL_REFRESH_EVENTS = new Set([
  'task_updated',
  'artifact_created',
  'alert_created',
  'alert_dismissed',
  'message_created',
  'message_updated',
  'messages_superseded',
  'strategic_updated',
  'strategic_cycle_started',
  'strategic_cycle_completed',
]);

export function useLogViewer({
  fetchTasksRef,
  setDeployState,
  fetchDirectionsRef,
  refreshStrategicReadModelRef,
  refreshMessagesRef,
  fetchAlertsRef,
  refreshFleetRunnersRef,
  selectedProjectId,
  realtimeWsEnabled = true,
  strategicReadModelRef,
}: UseLogViewerDeps) {
  const [logTask, setLogTask] = useState<Task | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [logsConnecting, setLogsConnecting] = useState(false);
  const [wsDisconnected, setWsDisconnected] = useState(false);
  const [wsDisconnectReason, setWsDisconnectReason] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [pipelineThrottle, setPipelineThrottle] = useState<{ reason: string; detail: string; recovery_seconds: number | null } | null>(null);
  const [gatedTypesMap, setGatedTypesMap] = useState<Map<string, FailingTaskTypeEntry>>(new Map());
  // Lightweight "strategist is working" indicator, toggled by strategic_cycle_started/completed events.
  const [strategistActive, setStrategistActive] = useState(false);
  // Phase-level progress from the current running cycle (null when idle).
  const [cycleProgress, setCycleProgress] = useState<StrategicCycleProgress | null>(null);
  // Outcome receipt from the most recently completed cycle (null until first completion).
  const [lastCycleCompletion, setLastCycleCompletion] = useState<StrategicCycleCompletion | null>(null);
  // Live strategic-session transcript buffer, appended from strategic_session events.
  const [strategicSessionTranscript, setStrategicSessionTranscript] = useState<StrategicSessionDelta[]>([]);
  const projectStrategicState = useCallback((state: StrategicStateResponse | null) => {
    if (!state || (
      selectedProjectIdRef.current != null
      && state.project_id !== selectedProjectIdRef.current
    )) return;
    const activeCycle = state.strategic.active_cycle;
    if (strategicCycleIsActive(activeCycle)) {
      setStrategistActive(true);
      setCycleProgress(previous => {
        const next = strategicCycleProgressFromDurableState(activeCycle);
        // Keep live steering metadata while refreshing the durable phase/task
        // identity; the durable read model does not carry the excerpt.
        return previous?.cycle_id === next.cycle_id ? { ...next, ...previous } : next;
      });
      setLastCycleCompletion(previous => (
        previous?.cycle_id && previous.cycle_id !== activeCycle.id ? null : previous
      ));
      return;
    }

    setStrategistActive(false);
    setCycleProgress(null);
    const latestCycle = state.strategic.latest_cycle;
    if (!latestCycle) return;
    const durableCompletion = strategicCycleCompletionFromDurableState(latestCycle);
    setLastCycleCompletion(previous => (
      previous?.cycle_id === durableCompletion.cycle_id ? previous : durableCompletion
    ));
  }, []);
  const gatedTypesRefreshSeqRef = useRef(0);
  const [ceilingDeferredProjectIds, setCeilingDeferredProjectIds] = useState<Set<string>>(new Set());
  const pipelineThrottleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const wsConnectingProjectRef = useRef<string | null>(null);
  const intentionallyClosedProjectRef = useRef<string | null>(null);
  const wsReconnectAttemptRef = useRef<number>(0);
  const establishedWsProjectsRef = useRef<Set<string>>(new Set());
  const wsReconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fetchTasksDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fetchDirectionsDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshStrategicReadModelDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshMessagesDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fetchAlertsDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wsPingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastPongRef = useRef<number>(0);
  const wsPongTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // --- Task Tracking State ---
  const [trackedTaskIds, setTrackedTaskIds] = useState<Set<string>>(new Set());
  const [trackedLogs, setTrackedLogs] = useState<Map<string, TrackLogEntry[]>>(new Map());
  const trackedTaskIdsRef = useRef<Set<string>>(trackedTaskIds);
  const logTaskRef = useRef<Task | null>(null);
  const selectedProjectIdRef = useRef<string | null | undefined>(selectedProjectId);
  // Keep a ref to the current project_id for WS connections opened for tracking
  const trackingProjectIdRef = useRef<string | null>(null);
  // Tracked tasks that currently justify a foreground project WebSocket.
  const streamingTrackedTaskIdsRef = useRef<Set<string>>(new Set());
  // Track which tracked tasks have reached terminal state (for WS auto-close)
  const terminalTrackedRef = useRef<Set<string>>(new Set());

  // Sync refs
  useEffect(() => { trackedTaskIdsRef.current = trackedTaskIds; }, [trackedTaskIds]);
  useEffect(() => { logTaskRef.current = logTask; }, [logTask]);
  useEffect(() => { selectedProjectIdRef.current = selectedProjectId; }, [selectedProjectId]);

  const refreshGatedTaskTypes = useCallback(async () => {
    const seq = ++gatedTypesRefreshSeqRef.current;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/failing-task-types`, {
        headers: await authHeaders(),
      });
      if (!res.ok) return;
      const data = await res.json() as FailingTaskTypes;
      if (gatedTypesRefreshSeqRef.current !== seq) return;
      const threshold = typeof data.global_failure_threshold === 'number'
        ? data.global_failure_threshold
        : 0.7;
      const gatedTypes = Array.isArray(data.gated_types) ? data.gated_types : [];
      const next = new Map<string, FailingTaskTypeEntry>();
      for (const entry of gatedTypes) {
        next.set(entry.task_type, { ...entry, threshold });
      }
      setGatedTypesMap(next);
    } catch {
      // Preserve the current banner state if the snapshot probe fails.
    }
  }, []);

  // Clear wsDisconnected banner when task reaches a terminal state
  useEffect(() => {
    if (logTask && ['COMPLETED', 'FAILED', 'CANCELLED'].includes(logTask.status)) {
      setWsDisconnected(false);
      setWsDisconnectReason(null);
    }
  }, [logTask, logTask?.status]);

  // Client-side safety net: strategistActive is hook-global (not per-project),
  // so a stale "working" state from a previous project must not bleed onto the
  // newly-selected project's directions panel. Reset all cycle state whenever
  // the selected project changes; the project's own strategic_cycle_started will re-arm it.
  useEffect(() => {
    setStrategistActive(false);
    setCycleProgress(null);
    setLastCycleCompletion(null);
    // The transcript is a per-project live buffer; a fresh project starts empty
    // and the strategic session re-streams its own strategic_session deltas.
    setStrategicSessionTranscript([]);
    // Per-type gate state is global, but clear on project switch so stale gates
    // from a previous project don't bleed onto the newly-selected project's view.
    gatedTypesRefreshSeqRef.current += 1;
    setGatedTypesMap(new Map());
  }, [selectedProjectId]);

  // Clean up WebSocket and timers on unmount
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      if (wsPingIntervalRef.current) {
        clearInterval(wsPingIntervalRef.current);
      }
      if (wsPongTimeoutRef.current) {
        clearTimeout(wsPongTimeoutRef.current);
      }
      if (pipelineThrottleTimerRef.current) {
        clearTimeout(pipelineThrottleTimerRef.current);
      }
      if (fetchTasksDebounceRef.current) {
        clearTimeout(fetchTasksDebounceRef.current);
      }
      if (fetchDirectionsDebounceRef.current) {
        clearTimeout(fetchDirectionsDebounceRef.current);
      }
      if (refreshStrategicReadModelDebounceRef.current) {
        clearTimeout(refreshStrategicReadModelDebounceRef.current);
      }
      if (refreshMessagesDebounceRef.current) {
        clearTimeout(refreshMessagesDebounceRef.current);
      }
      if (fetchAlertsDebounceRef.current) {
        clearTimeout(fetchAlertsDebounceRef.current);
      }
    };
  }, []);

  // Keep a stable ref to connectWs for the online listener (avoids re-registering on each render)
  const connectWsRef = useRef<((projectId: string) => Promise<void>) | null>(null);

  const hasActiveWsForProject = useCallback((projectId: string) => {
    const ws = wsRef.current;
    return trackingProjectIdRef.current === projectId
      && (
        wsConnectingProjectRef.current === projectId
        || (ws != null && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING))
      );
  }, []);

  const hasActiveTrackedTasks = useCallback(() => streamingTrackedTaskIdsRef.current.size > 0, []);

  const hasForegroundLogTask = useCallback(() => {
    const task = logTaskRef.current;
    return Boolean(task && isLogStreamTaskStatus(task.status));
  }, []);

  const clearReconnectTimer = useCallback(() => {
    if (wsReconnectTimerRef.current) {
      clearTimeout(wsReconnectTimerRef.current);
      wsReconnectTimerRef.current = null;
    }
  }, []);

  const closeCurrentWs = useCallback((reason: string) => {
    clearReconnectTimer();
    if (wsPingIntervalRef.current) {
      clearInterval(wsPingIntervalRef.current);
      wsPingIntervalRef.current = null;
    }
    if (wsPongTimeoutRef.current) {
      clearTimeout(wsPongTimeoutRef.current);
      wsPongTimeoutRef.current = null;
    }
    trackingProjectIdRef.current = null;
    wsConnectingProjectRef.current = null;
    intentionallyClosedProjectRef.current = null;
    if (wsRef.current) {
      wsRef.current.close(1000, reason);
      wsRef.current = null;
    }
    setWsConnected(false);
  }, [clearReconnectTimer]);

  const restoreSelectedProjectStream = useCallback((reason: string) => {
    const selectedProjectIdForRestore = selectedProjectIdRef.current;
    if (!realtimeWsEnabled || !selectedProjectIdForRestore) {
      closeCurrentWs(reason);
      return;
    }
    if (hasActiveWsForProject(selectedProjectIdForRestore)) return;
    closeCurrentWs(reason);
    wsReconnectAttemptRef.current = 0;
    void connectWsRef.current?.(selectedProjectIdForRestore);
  }, [closeCurrentWs, hasActiveWsForProject, realtimeWsEnabled]);

  // Reconnect immediately when the network comes back online
  useEffect(() => {
    const handleOnline = () => {
      const projectId = trackingProjectIdRef.current;
      if (!projectId) return;
      const ws = wsRef.current;
      if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;
      // Clear any pending backoff timer and reconnect immediately
      if (wsReconnectTimerRef.current) {
        clearTimeout(wsReconnectTimerRef.current);
        wsReconnectTimerRef.current = null;
      }
      wsReconnectAttemptRef.current = 0;
      if (connectWsRef.current) {
        connectWsRef.current(projectId);
      }
    };
    window.addEventListener('online', handleOnline);
    return () => window.removeEventListener('online', handleOnline);
  }, []); // empty deps — we use refs throughout

  const appendTrackLog = useCallback((taskId: string, type: TrackLogEntry['type'], msg: string) => {
    const entry: TrackLogEntry = { id: ++_trackLogIdCounter, t: new Date().toISOString(), type, msg };
    setTrackedLogs(prev => {
      const next = new Map(prev);
      const existing = next.get(taskId) || [];
      // Cap at 2000 entries per task to prevent memory issues
      const updated = existing.length >= 2000 ? [...existing.slice(-1500), entry] : [...existing, entry];
      next.set(taskId, updated);
      return next;
    });
  }, []);

  const refreshStrategicReadModel = useCallback(() => {
    if (!refreshStrategicReadModelRef?.current) return;
    if (refreshStrategicReadModelDebounceRef.current) {
      clearTimeout(refreshStrategicReadModelDebounceRef.current);
    }
    refreshStrategicReadModelDebounceRef.current = setTimeout(() => {
      refreshStrategicReadModelDebounceRef.current = null;
      refreshStrategicReadModelRef.current();
    }, 500);
  }, [refreshStrategicReadModelRef]);

  const refreshMessages = useCallback(() => {
    if (!refreshMessagesRef?.current) return;
    if (refreshMessagesDebounceRef.current) {
      clearTimeout(refreshMessagesDebounceRef.current);
    }
    refreshMessagesDebounceRef.current = setTimeout(() => {
      refreshMessagesDebounceRef.current = null;
      refreshMessagesRef.current();
    }, 500);
  }, [refreshMessagesRef]);

  const refreshAlerts = useCallback(() => {
    if (!fetchAlertsRef?.current) return;
    if (fetchAlertsDebounceRef.current) {
      clearTimeout(fetchAlertsDebounceRef.current);
    }
    fetchAlertsDebounceRef.current = setTimeout(() => {
      fetchAlertsDebounceRef.current = null;
      fetchAlertsRef.current();
    }, 500);
  }, [fetchAlertsRef]);

  // Shared WS message handler — dispatches to log drawer AND tracked tasks
  const handleWsMessage = useCallback((event: MessageEvent) => {
    try {
      const msg = JSON.parse(event.data);
      const envelopeProjectId = selectedProjectIdRef.current ?? trackingProjectIdRef.current;
      const refetchSurfaces = workbenchRefetchSurfacesFromMessage(msg, envelopeProjectId);
      const projectEventApplies = workbenchMessageMatchesProject(msg, envelopeProjectId);

      // Pong response — update last-pong timestamp for heartbeat tracking
      if (msg.event === 'pong') {
        lastPongRef.current = Date.now();
        return;
      }

      const taskId = msg.data?.task_id;
      const currentLogTask = logTaskRef.current;

      // Log drawer events (existing behavior)
      if (msg.event === 'agent_log' && taskId && currentLogTask && taskId === currentLogTask.id) {
        setLogLines(prev => [...prev, msg.data.log]);
      } else if (msg.event === 'task_updated' && taskId && currentLogTask && taskId === currentLogTask.id) {
        const status = msg.data.status;
        setLogLines(prev => [...prev, `[Status: ${status}]`]);
        if (isKnownTaskStatus(status)) {
          const nextLogTask = { ...currentLogTask, status };
          logTaskRef.current = nextLogTask;
          setLogTask(nextLogTask);
        }
        if (!isLogStreamTaskStatus(status)) {
          if (!hasActiveTrackedTasks()) {
            restoreSelectedProjectStream(isTerminalTaskStatus(status) ? 'Log task completed' : 'Log task stopped streaming');
          }
        }
      }

      // Tracked task events
      if (taskId && trackedTaskIdsRef.current.has(taskId)) {
        if (msg.event === 'agent_log') {
          appendTrackLog(taskId, 'log', msg.data.log);
        } else if (msg.event === 'task_updated') {
          const status = msg.data.status;
          const statusMsg = msg.data.message;
          appendTrackLog(taskId, 'status', statusMsg ? `[${status}] ${statusMsg}` : `Status: ${status}`);
          if (isLogStreamTaskStatus(status)) {
            streamingTrackedTaskIdsRef.current.add(taskId);
          } else {
            streamingTrackedTaskIdsRef.current.delete(taskId);
          }
          if (isTerminalTaskStatus(status)) {
            terminalTrackedRef.current.add(taskId);
          }
          if (!isLogStreamTaskStatus(status) && !hasActiveTrackedTasks() && !hasForegroundLogTask()) {
            restoreSelectedProjectStream(isTerminalTaskStatus(status) ? 'Tracked tasks completed' : 'Tracked tasks stopped streaming');
          }
        } else if (msg.event === 'artifact_created') {
          const aType = msg.data.artifact_type || 'artifact';
          const prUrl = msg.data.pr_url;
          appendTrackLog(taskId, 'artifact', prUrl ? `${aType}: ${prUrl}` : aType);
        }
      }

      // Global events (deploy state, pipeline throttle, task list refresh)
      if (projectEventApplies && msg.event === 'deploy_state') {
        setDeployState(msg.data);
      }
      if (projectEventApplies && msg.event === 'pipeline_throttle') {
        setPipelineThrottle(msg.data);
        if (pipelineThrottleTimerRef.current) clearTimeout(pipelineThrottleTimerRef.current);
        const clearMs = (msg.data.recovery_seconds ?? 60) * 1000;
        pipelineThrottleTimerRef.current = setTimeout(() => setPipelineThrottle(null), clearMs);
      }
      if (projectEventApplies && msg.event === 'project_spend_ceiling_deferred') {
        const pid: string | undefined = msg.data?.project_id;
        if (pid) setCeilingDeferredProjectIds(prev => new Set(prev).add(pid));
      }
      if (projectEventApplies && msg.event === 'project_spend_ceiling_restored') {
        const pid: string | undefined = msg.data?.project_id;
        if (pid) setCeilingDeferredProjectIds(prev => {
          const next = new Set(prev);
          next.delete(pid);
          return next;
        });
      }
      if (projectEventApplies && msg.event === 'type_throttle' && typeof msg.data?.type === 'string') {
        void refreshGatedTaskTypes();
      }
      if (projectEventApplies && msg.event === 'type_unthrottle' && typeof msg.data?.type === 'string') {
        void refreshGatedTaskTypes();
      }
      if (projectEventApplies && (
        msg.event === 'task_updated'
        || msg.event === 'artifact_created'
        || refetchSurfaces.has('pipeline')
      )) {
        if (fetchTasksDebounceRef.current) clearTimeout(fetchTasksDebounceRef.current);
        fetchTasksDebounceRef.current = setTimeout(() => { fetchTasksRef.current(); }, 500);
      }
      if (projectEventApplies && (
        STRATEGIC_READ_MODEL_REFRESH_EVENTS.has(msg.event)
        || refetchSurfaces.has('strategic_session')
      )) {
        refreshStrategicReadModel();
      }
      if (projectEventApplies && (
        msg.event === 'message_created'
        || msg.event === 'message_updated'
        || msg.event === 'messages_superseded'
        || refetchSurfaces.has('messages')
      )) {
        const canonicalMessage = (msg.event === 'message_created' || msg.event === 'message_updated')
          ? canonicalProjectMessage(msg.data?.message)
          : null;
        const canonicalRefetchRequired = msg.data?.canonical_refetch_required === true
          || msg.data?.message?.projection_truncated === true
          || msg.data?.message?.context?.projection_truncated === true;
        if (canonicalMessage) {
          refreshMessagesRef?.current?.(canonicalMessage, canonicalRefetchRequired);
          // Acknowledge delivery so the backend stamps delivered_at.
          // Without this, the recovery-on-reconnect path re-sends every
          // undelivered message from the last 10 minutes, and the liveness
          // audit reports permanent false-positive undelivered gaps.
          if (wsRef.current?.readyState === WebSocket.OPEN) {
            wsRef.current.send(JSON.stringify({ type: 'message_ack', message_id: canonicalMessage.id }));
          }
        } else refreshMessages();
      }
      if (projectEventApplies && (
        msg.event === 'alert_created'
        || msg.event === 'alert_dismissed'
        || refetchSurfaces.has('runtime')
      )) {
        refreshAlerts();
        refreshFleetRunnersRef?.current?.();
      }

      // Strategist activity events — drive a live refresh of the directions view.
      // These only trigger refetches (the data still comes from the REST endpoints);
      // event payloads are never applied directly as state.
      const refetchDirections = () => {
        if (fetchDirectionsDebounceRef.current) clearTimeout(fetchDirectionsDebounceRef.current);
        fetchDirectionsDebounceRef.current = setTimeout(() => { fetchDirectionsRef?.current?.(); }, 500);
      };
      if (!projectEventApplies) {
        return;
      }
      // Strategic-session live transcript — mirror the agent_log append: each
      // strategic_session event carries one coarsened delta whose text we push
      // onto the transcript buffer the hook exposes. Deltas are already
      // secret-sanitized + length-bounded server-side (strategic_events.py).
      if (msg.event === 'strategic_session') {
        const text = typeof msg.data?.text === 'string' ? msg.data.text : '';
        const status = typeof msg.data?.status === 'string' ? msg.data.status : undefined;
        // A lifecycle frame (responding/done) may carry empty text but must still
        // be recorded so the UI can open/close the working state + activity strip.
        if (text || status) {
          const delta: StrategicSessionDelta = {
            id: ++_sessionDeltaIdCounter,
            session_id: msg.data?.session_id,
            task_id: msg.data?.task_id,
            seq: msg.data?.seq,
            kind: msg.data?.kind,
            text,
            ts: msg.data?.ts,
            status,
          };
          setStrategicSessionTranscript(prev => (
            prev.length >= SESSION_TRANSCRIPT_CAP
              ? [...prev.slice(-SESSION_TRANSCRIPT_KEEP), delta]
              : [...prev, delta]
          ));
        }
        return;
      }
      if (msg.event === 'strategic_cycle_started') {
        setStrategistActive(true);
        setLastCycleCompletion(null); // clear previous receipt when new cycle begins
        setCycleProgress({
          task_id: msg.data?.task_id ?? '',
          cycle_id: msg.data?.cycle_id,
          phase_number: msg.data?.phase_number,
          phase_label: msg.data?.phase_label,
          phase_state: msg.data?.phase_state,
          steering_origin: msg.data?.steering_origin,
          steering_excerpt: msg.data?.steering_excerpt,
        });
      } else if (msg.event === 'strategic_cycle_completed') {
        setStrategistActive(false);
        setCycleProgress(null);
        if (msg.data?.completion && typeof msg.data.completion === 'object') {
          const completion = {
            ...(msg.data.completion as StrategicCycleCompletion),
            cycle_id: typeof msg.data?.cycle_id === 'string' ? msg.data.cycle_id : undefined,
          };
          setLastCycleCompletion(previous => (
            completion.cycle_id && previous?.cycle_id === completion.cycle_id
              ? previous
              : completion
          ));
        }
      }
      // Handle lifecycle state above before the canonical envelope's directions
      // surface schedules the shared refresh. Legacy completion events still
      // refresh directions even without a refetch surface.
      if (
        msg.event === 'strategic_updated'
        || msg.event === 'strategic_cycle_completed'
        || refetchSurfaces.has('directions')
      ) {
        refetchDirections();
      }
    } catch {
      // ignore parse errors
    }
  }, [appendTrackLog, hasActiveTrackedTasks, hasForegroundLogTask, refreshGatedTaskTypes, restoreSelectedProjectStream, setDeployState, fetchTasksRef, fetchDirectionsRef, refreshStrategicReadModel, refreshMessages, refreshMessagesRef, refreshAlerts, refreshFleetRunnersRef]);

  const connectWs = useCallback(async (projectId: string) => {
    if (hasActiveWsForProject(projectId)) return;
    intentionallyClosedProjectRef.current = null;
    trackingProjectIdRef.current = projectId;
    wsConnectingProjectRef.current = projectId;
    try {
      const ticketRes = await fetch(`${API_BASE_URL}/api/projects/${projectId}/ws-ticket`, {
        method: 'POST',
        headers: await authHeaders(),
      });
      if (!ticketRes.ok) {
        if (wsConnectingProjectRef.current === projectId) wsConnectingProjectRef.current = null;
        setLogsConnecting(false);
        setLogLines(prev => [...prev, '[Failed to get WebSocket ticket]']);
        return;
      }
      const { ticket } = await ticketRes.json();
      if (trackingProjectIdRef.current !== projectId || wsConnectingProjectRef.current !== projectId) {
        return;
      }

      const wsUrl = (API_BASE_URL || window.location.origin).replace(/^http/, 'ws');
      const ws = new WebSocket(`${wsUrl}/api/projects/${projectId}/ws`);
      wsRef.current = ws;
      trackingProjectIdRef.current = projectId;
      wsConnectingProjectRef.current = null;
      let wasConnected = false;
      let pingInterval: ReturnType<typeof setInterval> | null = null;
      let pongTimeout: ReturnType<typeof setTimeout> | null = null;
      const clearConnectionHeartbeat = () => {
        if (pingInterval) {
          clearInterval(pingInterval);
          if (wsPingIntervalRef.current === pingInterval) wsPingIntervalRef.current = null;
          pingInterval = null;
        }
        if (pongTimeout) {
          clearTimeout(pongTimeout);
          if (wsPongTimeoutRef.current === pongTimeout) wsPongTimeoutRef.current = null;
          pongTimeout = null;
        }
      };

      ws.onopen = () => {
        const isReconnect = establishedWsProjectsRef.current.has(projectId);
        establishedWsProjectsRef.current.add(projectId);
        ws.send(JSON.stringify({ ticket }));
        wsReconnectAttemptRef.current = 0;
        setLogLines(prev => [...prev, '[Connected to live log stream]']);
        setWsConnected(true);
        setLogsConnecting(false);
        setWsDisconnected(false);
        wasConnected = true;
        // Project the durable snapshot before the async refresh lands. This
        // both preserves an active cycle and repairs a dropped completion.
        if (strategicReadModelRef) {
          projectStrategicState(strategicReadModelRef.current);
        } else {
          // Isolated consumers without the durable model retain the original
          // conservative reset; production App always supplies the ref.
          setStrategistActive(false);
          setCycleProgress(null);
        }
        if (isReconnect) refreshStrategicReadModel();
        // Add system log for all tracked tasks
        for (const tid of trackedTaskIdsRef.current) {
          appendTrackLog(tid, 'system', 'Stream connected');
        }

        // Start application-level ping/pong heartbeat (30s interval)
        let lastPingTime = 0;
        if (wsPingIntervalRef.current) clearInterval(wsPingIntervalRef.current);
        if (wsPongTimeoutRef.current) clearTimeout(wsPongTimeoutRef.current);
        pingInterval = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            lastPingTime = Date.now();
            ws.send(JSON.stringify({ type: 'ping' }));

            // Expect a pong within 10 seconds
            if (pongTimeout) clearTimeout(pongTimeout);
            pongTimeout = setTimeout(() => {
              if (lastPongRef.current < lastPingTime && ws.readyState === WebSocket.OPEN) {
                // No pong received — connection is silently dead, force reconnect
                ws.close(4001, 'Pong timeout');
              }
            }, 10000);
            wsPongTimeoutRef.current = pongTimeout;
          }
        }, 30000);
        wsPingIntervalRef.current = pingInterval;

        // Close both task and conversation gaps after every (re)connect. Message
        // refresh uses the transcript's last durable sequence, so missed frames
        // are fetched canonically instead of waiting for another WS event.
        setLogLines(prev => [...prev, '[Refreshing project state after reconnect]']);
        fetchTasksRef.current();
        refreshMessagesRef?.current?.();
        void refreshGatedTaskTypes();
      };

      ws.onmessage = handleWsMessage;

      ws.onerror = (e) => {
        if (trackingProjectIdRef.current !== projectId) return;
        console.error('[WS] error:', e);
        setLogLines(prev => [...prev, `[WebSocket error — check browser console (F12)]`]);
        setLogsConnecting(false);
        if (wasConnected) setWsDisconnected(true);
      };

      ws.onclose = (e) => {
        clearConnectionHeartbeat();
        if (trackingProjectIdRef.current !== projectId) return;
        setLogLines(prev => [...prev, `[Stream disconnected: code=${e.code} reason=${e.reason || 'none'}]`]);
        setWsConnected(false);
        setLogsConnecting(false);

        if (!wasConnected) return;

        // 1000 = normal close, 1008 = auth/policy failure — don't reconnect
        const isIntentional = e.code === 1000 || e.code === 1008;

        if (!isIntentional) {
          const attempt = wsReconnectAttemptRef.current;
          wsReconnectAttemptRef.current = attempt + 1;
          // Progressive backoff: 1s, 2s, 4s, 8s, 16s, 30s, then stay at 30s
          const delay = Math.min(1000 * Math.pow(2, attempt), 30000);
          const delaySec = Math.round(delay / 1000);
          setWsDisconnected(true);
          setWsDisconnectReason(`Connection lost — reconnecting in ${delaySec}s (attempt ${attempt + 1})...`);
          wsReconnectTimerRef.current = setTimeout(() => {
            wsReconnectTimerRef.current = null;
            setLogsConnecting(true);
            connectWs(projectId);
          }, delay);
        } else {
          if (wsRef.current === ws) wsRef.current = null;
          trackingProjectIdRef.current = null;
          wsConnectingProjectRef.current = null;
          intentionallyClosedProjectRef.current = projectId;
          setWsDisconnected(true);
          const reason = e.code === 1008
            ? 'Authentication failed — please refresh the page'
            : `Disconnected (code ${e.code})`;
          setWsDisconnectReason(reason);
        }
      };
    } catch (err) {
      if (wsConnectingProjectRef.current === projectId) wsConnectingProjectRef.current = null;
      setLogsConnecting(false);
      setLogLines(prev => [...prev, `[Connection error: ${err}]`]);
    }
  }, [handleWsMessage, appendTrackLog, fetchTasksRef, hasActiveWsForProject, refreshGatedTaskTypes, refreshMessagesRef, projectStrategicState, refreshStrategicReadModel, strategicReadModelRef]);

  // Keep connectWsRef in sync so the online listener can call the latest version
  useEffect(() => { connectWsRef.current = connectWs; }, [connectWs]);

  useEffect(() => {
    const hasForegroundConsumers = hasActiveTrackedTasks() || hasForegroundLogTask();
    if (!realtimeWsEnabled || !selectedProjectId) {
      if (!hasActiveTrackedTasks() && !hasForegroundLogTask()) {
        trackingProjectIdRef.current = null;
        wsConnectingProjectRef.current = null;
        closeCurrentWs(!realtimeWsEnabled ? 'Realtime disabled' : 'Project cleared');
        setLogsConnecting(false);
      }
      return;
    }

    if (intentionallyClosedProjectRef.current && intentionallyClosedProjectRef.current !== selectedProjectId) {
      intentionallyClosedProjectRef.current = null;
    }
    if (intentionallyClosedProjectRef.current === selectedProjectId) return;

    if (hasActiveWsForProject(selectedProjectId)) return;
    if (hasForegroundConsumers) return;

    closeCurrentWs('Project changed');
    wsReconnectAttemptRef.current = 0;
    void connectWs(selectedProjectId);
  }, [closeCurrentWs, connectWs, hasActiveTrackedTasks, hasActiveWsForProject, hasForegroundLogTask, realtimeWsEnabled, selectedProjectId]);

  const handleViewLogs = useCallback((task: Task) => {
    // Update drawer state immediately (non-destructive)
    logTaskRef.current = task;
    setLogTask(task);
    setLogLines(task.error_logs ? [`[Error Log]\n${task.error_logs}`] : []);
    const openLogStream = (streamTask: Task) => {
      if (!isLogStreamTaskStatus(streamTask.status)) return;
      if (!streamTask.project_id) return;
      if (hasActiveWsForProject(streamTask.project_id)) return;

      closeCurrentWs('Switching log project');
      wsReconnectAttemptRef.current = 0;
      setLogsConnecting(true);
      connectWs(streamTask.project_id);
    };
    fetchTask(task.id).then(fresh => {
      if (logTaskRef.current?.id !== task.id) return;
      setLogTask(fresh);
      logTaskRef.current = fresh;
      if (!isLogStreamTaskStatus(fresh.status) && !hasActiveTrackedTasks()) {
        restoreSelectedProjectStream('Log task stopped streaming');
        return;
      }
      openLogStream(fresh);
    }).catch(() => { openLogStream(task); });
    setWsDisconnected(false);
  }, [closeCurrentWs, connectWs, hasActiveTrackedTasks, hasActiveWsForProject, restoreSelectedProjectStream]);

  const closeLogViewer = useCallback(() => {
    const shouldCloseWs = !hasActiveTrackedTasks() && !selectedProjectIdRef.current;
    const selectedProjectIdForRestore = selectedProjectIdRef.current;
    logTaskRef.current = null;
    if (shouldCloseWs) {
      wsReconnectAttemptRef.current = 0;
      closeCurrentWs('Log viewer closed');
    } else if (!hasActiveTrackedTasks() && selectedProjectIdForRestore && !hasActiveWsForProject(selectedProjectIdForRestore)) {
      restoreSelectedProjectStream('Restoring selected project stream');
    }
    setLogsConnecting(false);
    setWsDisconnected(false);
    setWsDisconnectReason(null);
    setLogTask(null);
    setLogLines([]);
  }, [closeCurrentWs, hasActiveTrackedTasks, hasActiveWsForProject, restoreSelectedProjectStream]);

  // --- Track / Untrack ---
  const trackTask = useCallback((task: Task) => {
    const isTerminalTask = isTerminalTaskStatus(task.status);
    const isStreamingTask = isLogStreamTaskStatus(task.status);
    setTrackedTaskIds(prev => {
      if (prev.size >= 10) return prev; // Cap at 10 simultaneous tracked tasks
      const next = new Set(prev);
      next.add(task.id);
      return next;
    });
    if (isStreamingTask) {
      streamingTrackedTaskIdsRef.current.add(task.id);
    } else {
      streamingTrackedTaskIdsRef.current.delete(task.id);
    }
    if (isTerminalTask) {
      terminalTrackedRef.current.add(task.id);
    }
    appendTrackLog(task.id, 'system', `Tracking started — ${task.title || task.id}`);

    // Open WS if not already connected and task is active
    if (isStreamingTask && task.project_id && !hasActiveWsForProject(task.project_id)) {
      closeCurrentWs('Tracking task project');
      wsReconnectAttemptRef.current = 0;
      connectWs(task.project_id);
    }
  }, [closeCurrentWs, connectWs, appendTrackLog, hasActiveWsForProject]);

  const untrackTask = useCallback((taskId: string) => {
    setTrackedTaskIds(prev => {
      const next = new Set(prev);
      next.delete(taskId);
      return next;
    });
    setTrackedLogs(prev => {
      const next = new Map(prev);
      next.delete(taskId);
      return next;
    });
    terminalTrackedRef.current.delete(taskId);
    streamingTrackedTaskIdsRef.current.delete(taskId);
    // Close WS if no log viewer open and no more tracked tasks
    // Use setTimeout to read the updated set size after state settles
    setTimeout(() => {
      if (!hasActiveTrackedTasks() && !hasForegroundLogTask()) {
        restoreSelectedProjectStream('No tracked tasks');
      }
    }, 0);
  }, [hasActiveTrackedTasks, hasForegroundLogTask, restoreSelectedProjectStream]);

  const untrackAll = useCallback(() => {
    setTrackedTaskIds(new Set());
    setTrackedLogs(new Map());
    terminalTrackedRef.current.clear();
    streamingTrackedTaskIdsRef.current.clear();
    if (!hasForegroundLogTask()) {
      restoreSelectedProjectStream('Tracking cleared');
    }
  }, [hasForegroundLogTask, restoreSelectedProjectStream]);

  const gatedTaskTypes = [...gatedTypesMap.values()].sort((a, b) => a.task_type.localeCompare(b.task_type));

  return {
    logTask, logLines, logsConnecting, wsDisconnected, wsDisconnectReason,
    handleViewLogs, closeLogViewer,
    wsRef, wsConnected,
    pipelineThrottle,
    ceilingDeferredProjectIds,
    gatedTaskTypes,
    strategistActive,
    cycleProgress,
    lastCycleCompletion,
    projectStrategicState,
    // Strategic-session live transcript (appended from strategic_session events).
    strategicSessionTranscript,
    clearStrategicSessionTranscript: () => setStrategicSessionTranscript([]),
    // Tracking
    trackedTaskIds, trackedLogs, trackTask, untrackTask, untrackAll,
  };
}
