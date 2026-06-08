import { useState, useEffect, useRef, useCallback } from 'react';
import type { Task, TrackLogEntry } from '../types/task';
import type { DeployState } from '../types/settings';
import { API_BASE_URL, authHeaders, fetchTask } from '../api';

interface UseLogViewerDeps {
  fetchTasksRef: React.MutableRefObject<() => void>;
  setDeployState: (ds: DeployState) => void;
  fetchDirectionsRef?: React.MutableRefObject<() => void>;
  selectedProjectId?: string | null;
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
}

let _trackLogIdCounter = 0;

export function useLogViewer({ fetchTasksRef, setDeployState, fetchDirectionsRef, selectedProjectId }: UseLogViewerDeps) {
  const [logTask, setLogTask] = useState<Task | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [logsConnecting, setLogsConnecting] = useState(false);
  const [wsDisconnected, setWsDisconnected] = useState(false);
  const [wsDisconnectReason, setWsDisconnectReason] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [pipelineThrottle, setPipelineThrottle] = useState<{ reason: string; detail: string; recovery_seconds: number | null } | null>(null);
  // Lightweight "strategist is working" indicator, toggled by strategic_cycle_started/completed events.
  const [strategistActive, setStrategistActive] = useState(false);
  // Phase-level progress from the current running cycle (null when idle).
  const [cycleProgress, setCycleProgress] = useState<StrategicCycleProgress | null>(null);
  // Outcome receipt from the most recently completed cycle (null until first completion).
  const [lastCycleCompletion, setLastCycleCompletion] = useState<StrategicCycleCompletion | null>(null);
  const pipelineThrottleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const wsReconnectAttemptRef = useRef<number>(0);
  const wsReconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fetchTasksDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fetchDirectionsDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wsPingIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const lastPongRef = useRef<number>(0);
  const wsPongTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // --- Task Tracking State ---
  const [trackedTaskIds, setTrackedTaskIds] = useState<Set<string>>(new Set());
  const [trackedLogs, setTrackedLogs] = useState<Map<string, TrackLogEntry[]>>(new Map());
  const trackedTaskIdsRef = useRef<Set<string>>(trackedTaskIds);
  const logTaskRef = useRef<Task | null>(null);
  // Keep a ref to the current project_id for WS connections opened for tracking
  const trackingProjectIdRef = useRef<string | null>(null);
  // Track which tracked tasks have reached terminal state (for WS auto-close)
  const terminalTrackedRef = useRef<Set<string>>(new Set());

  // Sync refs
  useEffect(() => { trackedTaskIdsRef.current = trackedTaskIds; }, [trackedTaskIds]);
  useEffect(() => { logTaskRef.current = logTask; }, [logTask]);

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
    };
  }, []);

  // Keep a stable ref to connectWs for the online listener (avoids re-registering on each render)
  const connectWsRef = useRef<((projectId: string) => Promise<void>) | null>(null);

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

  // Shared WS message handler — dispatches to log drawer AND tracked tasks
  const handleWsMessage = useCallback((event: MessageEvent) => {
    try {
      const msg = JSON.parse(event.data);

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
        setLogLines(prev => [...prev, `[Status: ${msg.data.status}]`]);
      }

      // Tracked task events
      if (taskId && trackedTaskIdsRef.current.has(taskId)) {
        if (msg.event === 'agent_log') {
          appendTrackLog(taskId, 'log', msg.data.log);
        } else if (msg.event === 'task_updated') {
          const status = msg.data.status;
          const statusMsg = msg.data.message;
          appendTrackLog(taskId, 'status', statusMsg ? `[${status}] ${statusMsg}` : `Status: ${status}`);
          // When a tracked task reaches a terminal state, close WS if no active tracked tasks remain
          if (['COMPLETED', 'FAILED', 'CANCELLED'].includes(status)) {
            terminalTrackedRef.current.add(taskId);
            const allTerminal = [...trackedTaskIdsRef.current].every(id => terminalTrackedRef.current.has(id));
            if (allTerminal && !logTaskRef.current && wsRef.current) {
              // All tracked tasks finished and no log drawer open — close WS to free server resources
              wsRef.current.close();
              wsRef.current = null;
              setWsConnected(false);
            }
          }
        } else if (msg.event === 'artifact_created') {
          const aType = msg.data.artifact_type || 'artifact';
          const prUrl = msg.data.pr_url;
          appendTrackLog(taskId, 'artifact', prUrl ? `${aType}: ${prUrl}` : aType);
        }
      }

      // Global events (deploy state, pipeline throttle, task list refresh)
      if (msg.event === 'deploy_state') {
        setDeployState(msg.data);
      }
      if (msg.event === 'pipeline_throttle') {
        setPipelineThrottle(msg.data);
        if (pipelineThrottleTimerRef.current) clearTimeout(pipelineThrottleTimerRef.current);
        const clearMs = (msg.data.recovery_seconds ?? 60) * 1000;
        pipelineThrottleTimerRef.current = setTimeout(() => setPipelineThrottle(null), clearMs);
      }
      if (msg.event === 'task_updated' || msg.event === 'artifact_created') {
        if (fetchTasksDebounceRef.current) clearTimeout(fetchTasksDebounceRef.current);
        fetchTasksDebounceRef.current = setTimeout(() => { fetchTasksRef.current(); }, 500);
      }

      // Strategist activity events — drive a live refresh of the directions view.
      // These only trigger refetches (the data still comes from the REST endpoints);
      // event payloads are never applied directly as state.
      const refetchDirections = () => {
        if (fetchDirectionsDebounceRef.current) clearTimeout(fetchDirectionsDebounceRef.current);
        fetchDirectionsDebounceRef.current = setTimeout(() => { fetchDirectionsRef?.current?.(); }, 500);
      };
      if (msg.event === 'strategic_updated') {
        refetchDirections();
      } else if (msg.event === 'strategic_cycle_started') {
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
          setLastCycleCompletion(msg.data.completion as StrategicCycleCompletion);
        }
        refetchDirections();
      }
    } catch {
      // ignore parse errors
    }
  }, [appendTrackLog, setDeployState, fetchTasksRef, fetchDirectionsRef]);

  const connectWs = useCallback(async (projectId: string) => {
    try {
      const ticketRes = await fetch(`${API_BASE_URL}/api/projects/${projectId}/ws-ticket`, {
        method: 'POST',
        headers: await authHeaders(),
      });
      if (!ticketRes.ok) {
        setLogsConnecting(false);
        setLogLines(prev => [...prev, '[Failed to get WebSocket ticket]']);
        return;
      }
      const { ticket } = await ticketRes.json();

      const wsUrl = (API_BASE_URL || window.location.origin).replace(/^http/, 'ws');
      const ws = new WebSocket(`${wsUrl}/api/projects/${projectId}/ws`);
      wsRef.current = ws;
      trackingProjectIdRef.current = projectId;
      let wasConnected = false;

      ws.onopen = () => {
        ws.send(JSON.stringify({ ticket }));
        wsReconnectAttemptRef.current = 0;
        setLogLines(prev => [...prev, '[Connected to live log stream]']);
        setWsConnected(true);
        setLogsConnecting(false);
        setWsDisconnected(false);
        wasConnected = true;
        // Client-side safety net: clear any stale "strategist working" state on
        // (re)connect. If a strategic_cycle_completed was dropped while the WS
        // was down, the indicator would otherwise stick on; a fresh cycle will
        // re-emit strategic_cycle_started.
        setStrategistActive(false);
        setCycleProgress(null);
        // Add system log for all tracked tasks
        for (const tid of trackedTaskIdsRef.current) {
          appendTrackLog(tid, 'system', 'Stream connected');
        }

        // Start application-level ping/pong heartbeat (30s interval)
        let lastPingTime = 0;
        wsPingIntervalRef.current = setInterval(() => {
          if (ws.readyState === WebSocket.OPEN) {
            lastPingTime = Date.now();
            ws.send(JSON.stringify({ type: 'ping' }));

            // Expect a pong within 10 seconds
            if (wsPongTimeoutRef.current) clearTimeout(wsPongTimeoutRef.current);
            wsPongTimeoutRef.current = setTimeout(() => {
              if (lastPongRef.current < lastPingTime && ws.readyState === WebSocket.OPEN) {
                // No pong received — connection is silently dead, force reconnect
                ws.close(4001, 'Pong timeout');
              }
            }, 10000);
          }
        }, 30000);

        // Refresh task list to catch up on any missed state changes during disconnect
        setLogLines(prev => [...prev, '[Refreshing task list after reconnect]']);
        fetchTasksRef.current();
      };

      ws.onmessage = handleWsMessage;

      ws.onerror = (e) => {
        console.error('[WS] error:', e);
        setLogLines(prev => [...prev, `[WebSocket error — check browser console (F12)]`]);
        setLogsConnecting(false);
        if (wasConnected) setWsDisconnected(true);
      };

      ws.onclose = (e) => {
        setLogLines(prev => [...prev, `[Stream disconnected: code=${e.code} reason=${e.reason || 'none'}]`]);
        setWsConnected(false);
        setLogsConnecting(false);

        // Clean up ping heartbeat for this connection
        if (wsPingIntervalRef.current) {
          clearInterval(wsPingIntervalRef.current);
          wsPingIntervalRef.current = null;
        }
        if (wsPongTimeoutRef.current) {
          clearTimeout(wsPongTimeoutRef.current);
          wsPongTimeoutRef.current = null;
        }

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
          setWsDisconnected(true);
          const reason = e.code === 1008
            ? 'Authentication failed — please refresh the page'
            : `Disconnected (code ${e.code})`;
          setWsDisconnectReason(reason);
        }
      };
    } catch (err) {
      setLogsConnecting(false);
      setLogLines(prev => [...prev, `[Connection error: ${err}]`]);
    }
  }, [handleWsMessage, appendTrackLog, fetchTasksRef]);

  // Keep connectWsRef in sync so the online listener can call the latest version
  useEffect(() => { connectWsRef.current = connectWs; }, [connectWs]);

  const handleViewLogs = useCallback((task: Task) => {
    // Update drawer state immediately (non-destructive)
    setLogTask(task);
    setLogLines(task.error_logs ? [`[Error Log]\n${task.error_logs}`] : []);
    fetchTask(task.id).then(fresh => setLogTask(fresh)).catch(() => {/* keep stale data */});
    setWsDisconnected(false);

    if (task.status !== 'IN_PROGRESS' && task.status !== 'PROVISIONING') return;
    if (!task.project_id) return;

    // If WS is already connected to the same project, reuse it
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN
        && trackingProjectIdRef.current === task.project_id) {
      return;
    }

    // Different project or no WS — tear down and reconnect
    if (wsReconnectTimerRef.current) {
      clearTimeout(wsReconnectTimerRef.current);
      wsReconnectTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    wsReconnectAttemptRef.current = 0;
    setLogsConnecting(true);
    connectWs(task.project_id);
  }, [connectWs]);

  const closeLogViewer = useCallback(() => {
    if (wsReconnectTimerRef.current) {
      clearTimeout(wsReconnectTimerRef.current);
      wsReconnectTimerRef.current = null;
    }
    if (wsPingIntervalRef.current) {
      clearInterval(wsPingIntervalRef.current);
      wsPingIntervalRef.current = null;
    }
    if (wsPongTimeoutRef.current) {
      clearTimeout(wsPongTimeoutRef.current);
      wsPongTimeoutRef.current = null;
    }
    wsReconnectAttemptRef.current = 0;
    // Only close WS if no tracked tasks need it
    if (trackedTaskIdsRef.current.size === 0) {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      setWsConnected(false);
    }
    setLogsConnecting(false);
    setWsDisconnected(false);
    setWsDisconnectReason(null);
    setLogTask(null);
    setLogLines([]);
  }, []);

  // --- Track / Untrack ---
  const trackTask = useCallback((task: Task) => {
    setTrackedTaskIds(prev => {
      if (prev.size >= 10) return prev; // Cap at 10 simultaneous tracked tasks
      const next = new Set(prev);
      next.add(task.id);
      return next;
    });
    if (task.project_id) trackingProjectIdRef.current = task.project_id;
    appendTrackLog(task.id, 'system', `Tracking started — ${task.title || task.id}`);

    // Open WS if not already connected and task is active
    if (!wsRef.current && task.project_id) {
      connectWs(task.project_id);
    }
  }, [connectWs, appendTrackLog]);

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
    // Close WS if no log viewer open and no more tracked tasks
    // Use setTimeout to read the updated set size after state settles
    setTimeout(() => {
      if (trackedTaskIdsRef.current.size === 0 && !logTaskRef.current && wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
        setWsConnected(false);
      }
    }, 0);
  }, []);

  const untrackAll = useCallback(() => {
    setTrackedTaskIds(new Set());
    setTrackedLogs(new Map());
    terminalTrackedRef.current.clear();
    if (!logTaskRef.current && wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
      setWsConnected(false);
    }
  }, []);

  return {
    logTask, logLines, logsConnecting, wsDisconnected, wsDisconnectReason,
    handleViewLogs, closeLogViewer,
    wsRef, wsConnected,
    pipelineThrottle,
    strategistActive,
    cycleProgress,
    lastCycleCompletion,
    // Tracking
    trackedTaskIds, trackedLogs, trackTask, untrackTask, untrackAll,
  };
}
