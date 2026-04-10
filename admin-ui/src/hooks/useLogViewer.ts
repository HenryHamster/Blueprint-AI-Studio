import { useState, useEffect, useRef, useCallback } from 'react';
import type { Task, TrackLogEntry } from '../types/task';
import type { DeployState } from '../types/settings';
import { API_BASE_URL, authHeaders, fetchTask } from '../api';

interface UseLogViewerDeps {
  fetchTasksRef: React.MutableRefObject<() => void>;
  setDeployState: (ds: DeployState) => void;
}

let _trackLogIdCounter = 0;

export function useLogViewer({ fetchTasksRef, setDeployState }: UseLogViewerDeps) {
  const [logTask, setLogTask] = useState<Task | null>(null);
  const [logLines, setLogLines] = useState<string[]>([]);
  const [logsConnecting, setLogsConnecting] = useState(false);
  const [wsDisconnected, setWsDisconnected] = useState(false);
  const [wsDisconnectReason, setWsDisconnectReason] = useState<string | null>(null);
  const [wsConnected, setWsConnected] = useState(false);
  const [pipelineThrottle, setPipelineThrottle] = useState<{ reason: string; detail: string; recovery_seconds: number | null } | null>(null);
  const pipelineThrottleTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const wsReconnectAttemptRef = useRef<number>(0);
  const wsReconnectTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fetchTasksDebounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

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

  // Clean up WebSocket and timers on unmount
  useEffect(() => {
    return () => {
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
      if (pipelineThrottleTimerRef.current) {
        clearTimeout(pipelineThrottleTimerRef.current);
      }
    };
  }, []);

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
    } catch {
      // ignore parse errors
    }
  }, [appendTrackLog, setDeployState, fetchTasksRef]);

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
      let wasConnected = false;

      ws.onopen = () => {
        ws.send(JSON.stringify({ ticket }));
        wsReconnectAttemptRef.current = 0;
        setLogLines(prev => [...prev, '[Connected to live log stream]']);
        setWsConnected(true);
        setLogsConnecting(false);
        setWsDisconnected(false);
        wasConnected = true;
        // Add system log for all tracked tasks
        for (const tid of trackedTaskIdsRef.current) {
          appendTrackLog(tid, 'system', 'Stream connected');
        }
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

        if (!wasConnected) return;

        const isIntentional = e.code === 1000 || e.code === 1008;
        const attemptsExhausted = wsReconnectAttemptRef.current >= 5;

        if (!isIntentional && !attemptsExhausted) {
          const attempt = wsReconnectAttemptRef.current;
          wsReconnectAttemptRef.current = attempt + 1;
          const delay = Math.min(1000 * Math.pow(2, attempt), 30000);
          const delaySec = Math.round(delay / 1000);
          setWsDisconnected(true);
          setWsDisconnectReason(`Connection lost — reconnecting in ${delaySec}s (attempt ${attempt + 1}/5)...`);
          wsReconnectTimerRef.current = setTimeout(() => {
            wsReconnectTimerRef.current = null;
            setLogsConnecting(true);
            connectWs(projectId);
          }, delay);
        } else {
          setWsDisconnected(true);
          let reason: string;
          if (e.code === 1008) {
            reason = 'Authentication failed — please refresh the page';
          } else if (attemptsExhausted) {
            reason = 'Connection failed after 5 attempts — please refresh the page';
          } else {
            reason = `Disconnected (code ${e.code})`;
          }
          setWsDisconnectReason(reason);
        }
      };
    } catch (err) {
      setLogsConnecting(false);
      setLogLines(prev => [...prev, `[Connection error: ${err}]`]);
    }
  }, [handleWsMessage, appendTrackLog]);

  const handleViewLogs = useCallback((task: Task) => {
    if (wsReconnectTimerRef.current) {
      clearTimeout(wsReconnectTimerRef.current);
      wsReconnectTimerRef.current = null;
    }
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    wsReconnectAttemptRef.current = 0;
    setLogTask(task);
    setLogLines(task.error_logs ? [`[Error Log]\n${task.error_logs}`] : []);
    // Fetch fresh task data in the background; show stale list data immediately
    fetchTask(task.id).then(fresh => setLogTask(fresh)).catch(() => {/* keep stale data */});
    setWsDisconnected(false);

    if (task.status !== 'IN_PROGRESS' && task.status !== 'PROVISIONING') return;
    if (!task.project_id) return;

    setLogsConnecting(true);
    connectWs(task.project_id);
  }, [connectWs]);

  const closeLogViewer = useCallback(() => {
    if (wsReconnectTimerRef.current) {
      clearTimeout(wsReconnectTimerRef.current);
      wsReconnectTimerRef.current = null;
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
    // Tracking
    trackedTaskIds, trackedLogs, trackTask, untrackTask, untrackAll,
  };
}
