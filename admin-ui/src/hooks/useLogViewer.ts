import { useState, useEffect, useRef, useCallback } from 'react';
import type { Task } from '../types/task';
import type { DeployState } from '../types/settings';
import { API_BASE_URL, authHeaders, fetchTask } from '../api';

interface UseLogViewerDeps {
  fetchTasksRef: React.MutableRefObject<() => void>;
  setDeployState: (ds: DeployState) => void;
}

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

    const connectWs = async () => {
      try {
        const ticketRes = await fetch(`${API_BASE_URL}/api/projects/${task.project_id}/ws-ticket`, {
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
        const ws = new WebSocket(`${wsUrl}/api/projects/${task.project_id}/ws`);
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
        };

        ws.onmessage = (event) => {
          try {
            const msg = JSON.parse(event.data);
            if (msg.event === 'agent_log' && msg.data?.task_id === task.id) {
              setLogLines(prev => [...prev, msg.data.log]);
            } else if (msg.event === 'task_updated' && msg.data?.task_id === task.id) {
              setLogLines(prev => [...prev, `[Status: ${msg.data.status}]`]);
            }
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
        };

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
              connectWs();
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
    };

    connectWs();
  }, [setDeployState]);

  const closeLogViewer = () => {
    if (wsReconnectTimerRef.current) {
      clearTimeout(wsReconnectTimerRef.current);
      wsReconnectTimerRef.current = null;
    }
    wsReconnectAttemptRef.current = 0;
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }
    setWsConnected(false);
    setLogsConnecting(false);
    setWsDisconnected(false);
    setWsDisconnectReason(null);
    setLogTask(null);
    setLogLines([]);
  };

  return {
    logTask, logLines, logsConnecting, wsDisconnected, wsDisconnectReason,
    handleViewLogs, closeLogViewer,
    wsRef, wsConnected,
    pipelineThrottle,
  };
}
