/* eslint-disable react-hooks/refs, react-hooks/set-state-in-effect */
import { useState, useEffect, useMemo, useRef } from 'react';
import type { Task } from '../types/task';
import { API_BASE_URL, apiFetch, authHeaders, authGet, resetTaskIterationCap, triggerTaskRollup } from '../api';
import { STATUS_OPTIONS } from '../constants';
import { usePersistedState } from './usePersistedState';

function playCompletionSound() {
  try {
    const ctx = new AudioContext();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain);
    gain.connect(ctx.destination);
    osc.frequency.setValueAtTime(880, ctx.currentTime);
    osc.frequency.setValueAtTime(1100, ctx.currentTime + 0.1);
    gain.gain.setValueAtTime(0.3, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
    osc.start(ctx.currentTime);
    osc.stop(ctx.currentTime + 0.4);
  } catch { /* AudioContext not available */ }
}

interface ConfirmDialog {
  title: string;
  message: string;
  confirmLabel: string;
  onConfirm: () => void;
}

/**
 * Collaborators the tasks domain needs from sibling domain hooks. They are
 * passed in (rather than re-derived) so this hook stays behavior-identical to
 * the original monolith — same callback identities and timing at the call
 * sites that matter (selection bulk-actions, archive refresh, notifications).
 */
interface UseTasksStateDeps {
  addToast: (msg: string, type: 'success' | 'error') => void;
  setError: (msg: string | null) => void;
  statusFilter: string[];
  // Metrics sink for each completed fetch.
  addMetricsPoint: (activeCount: number | undefined, tasks: Task[], completedCount: number, failedCount: number) => void;
  // Notification preference refs (latest value, read inside async fetch).
  notifyRef: React.MutableRefObject<boolean>;
  notifyOnFailRef: React.MutableRefObject<boolean>;
  notifyOnApprovalNeededRef: React.MutableRefObject<boolean>;
  soundOnCompleteRef: React.MutableRefObject<boolean>;
  // Archive refresh (used after retry/unarchive paths).
  fetchArchiveTasks: () => Promise<void> | void;
  // Selection / confirm-dialog state owned by useUIState.
  selectedTaskIds: Set<string>;
  setSelectedTaskIds: (ids: Set<string>) => void;
  setSelectionMode: (mode: boolean) => void;
  setConfirmDialog: (dialog: ConfirmDialog | null) => void;
  // Shared ref the facade keeps pointed at the latest fetchTasks.
  fetchTasksRef: React.MutableRefObject<() => Promise<void>>;
  prevTasksRef: React.MutableRefObject<Task[]>;
  // Project scope — when set, task list and counts are scoped to this project.
  selectedProjectId: string | null;
  canOperateProject: boolean;
  // Direction filter — when set, only tasks belonging to this direction are shown.
  directionFilter?: string | null;
}

export function useTasksState(deps: UseTasksStateDeps) {
  const {
    addToast, setError, statusFilter, addMetricsPoint,
    notifyRef, notifyOnFailRef, notifyOnApprovalNeededRef, soundOnCompleteRef,
    fetchArchiveTasks,
    selectedTaskIds, setSelectedTaskIds, setSelectionMode, setConfirmDialog,
    fetchTasksRef, prevTasksRef,
    selectedProjectId, canOperateProject, directionFilter,
  } = deps;
  const canOperateProjectRef = useRef(canOperateProject);
  canOperateProjectRef.current = canOperateProject;
  const selectedProjectIdRef = useRef(selectedProjectId);
  selectedProjectIdRef.current = selectedProjectId;

  const [tasks, setTasks] = useState<Task[]>([]);
  const [totalTasks, setTotalTasks] = useState(0);
  const [globalActiveCount, setGlobalActiveCount] = useState(0);
  const [globalEvaluatingCount, setGlobalEvaluatingCount] = useState(0);
  const [globalQueuedCount, setGlobalQueuedCount] = useState(0);
  const [scopedActiveCount, setScopedActiveCount] = useState<number | null>(null);
  const [scopedEvaluatingCount, setScopedEvaluatingCount] = useState<number | null>(null);
  const [scopedQueuedCount, setScopedQueuedCount] = useState<number | null>(null);
  const [initialLoading, setInitialLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [fetchEpoch, setFetchEpoch] = useState(0);
  const [page, setPage] = useState(1);
  const [rawLimit, setLimit] = usePersistedState<number>('pageSize', 50);
  // Backend supports up to 500 rows, but the UI caps at 200 — stale
  // localStorage values (e.g. a previously saved 500) are clamped so the
  // steady-state poll stays cheap and pagination stays consistent.
  const limit = Math.min(rawLimit, 200);
  const [refreshInterval, setRefreshInterval] = usePersistedState<number>('refreshInterval', 30000);

  const [actionLoadingMap, setActionLoadingMap] = useState<Record<string, string>>({});

  // Refresh indicator + status-bar state driven by the task fetch.
  const [lastRefresh, setLastRefresh] = useState<Date | null>(null);
  const [apiHealthy, setApiHealthy] = useState(true);
  const [lastFetchTime, setLastFetchTime] = useState<number>(0);

  // Reset page when status or direction filter changes
  useEffect(() => { setPage(1); }, [statusFilter]);
  useEffect(() => { setPage(1); }, [directionFilter]);

  // Reset page and selection when project scope changes
  useEffect(() => {
    setPage(1);
    setTasks([]);
    prevTasksRef.current = [];
    setInitialLoading(false);
    setRefreshing(false);
    setSelectedTaskIds(new Set());
    setSelectionMode(false);
    setScopedActiveCount(null);
    setScopedEvaluatingCount(null);
    setScopedQueuedCount(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedProjectId]);

  // A viewer may keep reading the same project, but any selection created
  // while operate access existed must not survive a downgrade.
  useEffect(() => {
    if (canOperateProject) return;
    setSelectedTaskIds(new Set());
    setSelectionMode(false);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [canOperateProject]);

  const fetchTasks = async () => {
    const requestProjectId = selectedProjectId;
    const requestIsCurrent = () => selectedProjectIdRef.current === requestProjectId;
    if (tasks.length === 0) {
      setInitialLoading(true);
    } else {
      setRefreshing(true);
    }
    try {
      const offset = (page - 1) * limit;
      let url = `${API_BASE_URL}/api/admin/tasks?limit=${limit}&offset=${offset}&archived=false`;
      if (statusFilter.length < STATUS_OPTIONS.length) {
        statusFilter.forEach(s => { url += `&status=${s}`; });
      }
      if (selectedProjectId) {
        url += `&project_id=${encodeURIComponent(selectedProjectId)}`;
      }
      if (directionFilter) {
        url += `&direction_id=${encodeURIComponent(directionFilter)}`;
      }
      const res = await authGet(url);
      if (!requestIsCurrent()) return;
      if (res.ok) {
        const data = await res.json();
        if (!requestIsCurrent()) return;
        let newTasks: Task[];
        if (Array.isArray(data)) {
          newTasks = data;
          setTotalTasks(data.length);
        } else {
          newTasks = data.tasks || [];
          setTotalTasks(data.total ?? newTasks.length);
          if (data.active_count !== undefined) setGlobalActiveCount(data.active_count);
          if (data.evaluating_count !== undefined) setGlobalEvaluatingCount(data.evaluating_count);
          if (data.queued_count !== undefined) setGlobalQueuedCount(data.queued_count);
          if (data.scoped_active_count !== undefined) setScopedActiveCount(data.scoped_active_count);
          else setScopedActiveCount(null);
          if (data.scoped_evaluating_count !== undefined) setScopedEvaluatingCount(data.scoped_evaluating_count);
          else setScopedEvaluatingCount(null);
          if (data.scoped_queued_count !== undefined) setScopedQueuedCount(data.scoped_queued_count);
          else setScopedQueuedCount(null);
        }

        // Browser notifications for completed/failed tasks
        if (prevTasksRef.current.length > 0) {
          const prevMap = new Map(prevTasksRef.current.map(t => [t.id, t.status]));
          const prevApprovalMap = new Map(prevTasksRef.current.map(t => [t.id, t.requires_approval]));
          let didTransition = false;
          for (const t of newTasks) {
            const prev = prevMap.get(t.id);
            if (prev && prev !== t.status && (t.status === 'COMPLETED' || t.status === 'FAILED')) {
              if (t.status === 'COMPLETED' && notifyRef.current && Notification.permission === 'granted') {
                new Notification('Task completed', {
                  body: t.title || t.id,
                });
              }
              if (t.status === 'FAILED' && notifyOnFailRef.current && Notification.permission === 'granted') {
                new Notification('Task failed', {
                  body: t.title || t.id,
                });
              }
              didTransition = true;
            }
            // Approval-needed notification: task newly requires approval and is not in a terminal state
            const prevApproval = prevApprovalMap.get(t.id);
            const nonTerminal = t.status !== 'COMPLETED' && t.status !== 'FAILED' && t.status !== 'CANCELLED';
            if (!prevApproval && t.requires_approval && nonTerminal) {
              if (notifyOnApprovalNeededRef.current && Notification.permission === 'granted') {
                new Notification('Task requires approval', {
                  body: t.title || t.id,
                });
              }
            }
          }
          if (didTransition && soundOnCompleteRef.current) {
            playCompletionSound();
          }
        }
        // Count incremental completed/failed transitions since last snapshot
        let completedCount = 0;
        let failedCount = 0;
        if (prevTasksRef.current.length > 0) {
          const prevMap = new Map(prevTasksRef.current.map(t => [t.id, t.status]));
          for (const t of newTasks) {
            const prev = prevMap.get(t.id);
            if (prev && prev !== t.status) {
              if (t.status === 'COMPLETED') completedCount++;
              else if (t.status === 'FAILED') failedCount++;
            }
          }
        }

        prevTasksRef.current = newTasks;

        setTasks(newTasks);
        setLastRefresh(new Date());
        setError(null);
        setApiHealthy(true);
        setLastFetchTime(Date.now());

        // Record metrics data point
        addMetricsPoint(data.active_count, newTasks, completedCount, failedCount);
      } else {
        setError(`Failed to fetch tasks: ${res.statusText}`);
        setApiHealthy(false);
      }
    } catch (err) {
      if (!requestIsCurrent()) return;
      console.error('Failed to fetch tasks:', err);
      setError('Failed to connect to backend server.');
      setApiHealthy(false);
    } finally {
      if (requestIsCurrent()) {
        setInitialLoading(false);
        setRefreshing(false);
        setFetchEpoch(epoch => epoch + 1);
      }
    }
  };
  fetchTasksRef.current = fetchTasks;

  const fetchCounts = async () => {
    const requestProjectId = selectedProjectId;
    const requestIsCurrent = () => selectedProjectIdRef.current === requestProjectId;
    let url = `${API_BASE_URL}/api/admin/tasks/counts`;
    if (selectedProjectId) {
      url += `?project_id=${encodeURIComponent(selectedProjectId)}`;
    }
    try {
      const res = await authGet(url);
      if (!requestIsCurrent()) return;
      if (!res.ok) return;
      const data = await res.json();
      if (!requestIsCurrent()) return;
      if (data.active_count !== undefined) setGlobalActiveCount(data.active_count);
      if (data.evaluating_count !== undefined) setGlobalEvaluatingCount(data.evaluating_count);
      if (data.queued_count !== undefined) setGlobalQueuedCount(data.queued_count);
      if (data.scoped_active_count !== undefined) setScopedActiveCount(data.scoped_active_count);
      else setScopedActiveCount(null);
      if (data.scoped_evaluating_count !== undefined) setScopedEvaluatingCount(data.scoped_evaluating_count);
      else setScopedEvaluatingCount(null);
      if (data.scoped_queued_count !== undefined) setScopedQueuedCount(data.scoped_queued_count);
      else setScopedQueuedCount(null);
    } catch { /* ignore transient errors */ }
  };

  const handleApprove = async (taskId: string) => {
    if (!canOperateProjectRef.current) return;
    const original = tasks.find(t => t.id === taskId);
    setTasks(prev => prev.map(t => t.id === taskId ? { ...t, requires_approval: false, status: 'IN_PROGRESS' } : t));
    setActionLoadingMap(m => ({ ...m, [taskId]: 'approve' }));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ requires_approval: false }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        if (original) setTasks(prev => prev.map(t => t.id === taskId ? original : t));
        addToast(data.detail ? `Failed to approve task: ${data.detail}` : 'Failed to approve task', 'error');
        return;
      }
      fetchTasks();
      addToast('Task approved', 'success');
    } catch (error) {
      console.error('Failed to approve task:', error);
      if (original) setTasks(prev => prev.map(t => t.id === taskId ? original : t));
      addToast('Failed to approve task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleRequestChanges = async (taskId: string, feedback: string, action: 'fix' | 'review' = 'fix') => {
    if (!canOperateProjectRef.current) return;
    setActionLoadingMap(m => ({ ...m, [taskId]: 'request-changes' }));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}/request-changes`, {
        method: 'POST',
        headers: await authHeaders(),
        body: JSON.stringify({ feedback, action }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        addToast(data.detail || 'Failed to request changes', 'error');
        return;
      }
      fetchTasks();
      addToast(action === 'fix' ? 'Fix task spawned' : 'Re-review spawned', 'success');
    } catch {
      addToast('Failed to request changes', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleCancel = async (taskId: string) => {
    if (!canOperateProjectRef.current) return;
    const original = tasks.find(t => t.id === taskId);
    setTasks(prev => prev.map(t => t.id === taskId ? { ...t, status: 'CANCELLED' } : t));
    setActionLoadingMap(m => ({ ...m, [taskId]: 'cancel' }));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ status: 'CANCELLED' }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        if (original) setTasks(prev => prev.map(t => t.id === taskId ? original : t));
        addToast(data.detail ? `Failed to cancel task: ${data.detail}` : 'Failed to cancel task', 'error');
        return;
      }
      fetchTasks();
      addToast('Task cancelled', 'success');
    } catch (error) {
      console.error('Failed to cancel task:', error);
      if (original) setTasks(prev => prev.map(t => t.id === taskId ? original : t));
      addToast('Failed to cancel task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleRetry = async (taskId: string) => {
    if (!canOperateProjectRef.current) return;
    const original = tasks.find(t => t.id === taskId);
    setTasks(prev => prev.map(t => t.id === taskId ? { ...t, status: 'QUEUED' } : t));
    setActionLoadingMap(m => ({ ...m, [taskId]: 'retry' }));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ status: 'QUEUED' }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        if (original) setTasks(prev => prev.map(t => t.id === taskId ? original : t));
        addToast(data.detail ? `Failed to retry task: ${data.detail}` : 'Failed to retry task', 'error');
        return;
      }
      fetchTasks();
      fetchArchiveTasks();
      addToast('Task queued for retry', 'success');
    } catch (error) {
      console.error('Failed to retry task:', error);
      if (original) setTasks(prev => prev.map(t => t.id === taskId ? original : t));
      addToast('Failed to retry task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleEditPrompt = async (taskId: string, newPrompt: string) => {
    if (!canOperateProjectRef.current) return;
    setActionLoadingMap(m => ({ ...m, [taskId]: 'edit' }));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ payloads: [{ prompt: newPrompt }] }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        addToast(data.detail ? `Failed to edit task: ${data.detail}` : 'Failed to edit task', 'error');
        return;
      }
      fetchTasks();
      addToast('Prompt updated', 'success');
    } catch (error) {
      console.error('Failed to edit task:', error);
      addToast('Failed to edit task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleDismiss = async (taskId: string) => {
    if (!canOperateProjectRef.current) return;
    setActionLoadingMap(m => ({ ...m, [taskId]: 'dismiss' }));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}`, {
        method: 'PATCH',
        headers: await authHeaders(),
        body: JSON.stringify({ status: 'CANCELLED' }),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        addToast(data.detail ? `Failed to dismiss task: ${data.detail}` : 'Failed to dismiss task', 'error');
        return;
      }
      fetchTasks();
      addToast('Task dismissed', 'success');
    } catch (error) {
      console.error('Failed to dismiss task:', error);
      addToast('Failed to dismiss task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleArchiveTask = async (taskId: string) => {
    if (!canOperateProjectRef.current) return;
    setActionLoadingMap(m => ({ ...m, [taskId]: 'archive' }));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}/archive`, {
        method: 'POST',
        headers: await authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        addToast(data.detail ? `Failed to archive task: ${data.detail}` : 'Failed to archive task', 'error');
        return;
      }
      fetchTasks();
      addToast('Task archived', 'success');
    } catch (error) {
      console.error('Failed to archive task:', error);
      addToast('Failed to archive task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleUnarchiveTask = async (taskId: string) => {
    if (!canOperateProjectRef.current) return;
    setActionLoadingMap(m => ({ ...m, [taskId]: 'unarchive' }));
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/${taskId}/unarchive`, {
        method: 'POST',
        headers: await authHeaders(),
      });
      if (!res.ok) {
        const data = await res.json().catch(() => ({}));
        addToast(data.detail ? `Failed to unarchive task: ${data.detail}` : 'Failed to unarchive task', 'error');
        return;
      }
      fetchArchiveTasks();
      // Also refresh the main task list so pipeline/kanban pick up the
      // now-unarchived task (used by pipeline-view "Unarchive parent").
      fetchTasks();
      addToast('Task unarchived', 'success');
    } catch (error) {
      console.error('Failed to unarchive task:', error);
      addToast('Failed to unarchive task', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleBulkArchive = async (filter: 'completed' | 'terminal') => {
    if (!canOperateProjectRef.current) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/bulk-archive`, {
        method: 'POST',
        headers: { ...(await authHeaders()), 'Content-Type': 'application/json' },
        body: JSON.stringify({ filter }),
      });
      if (!res.ok) throw new Error('Request failed');
      const data = await res.json();
      fetchTasks();
      addToast(`Archived ${data.archived_count} task${data.archived_count !== 1 ? 's' : ''}`, 'success');
    } catch (error) {
      console.error('Failed to bulk archive tasks:', error);
      addToast('Failed to archive tasks', 'error');
    }
    setConfirmDialog(null);
  };

  const handleBulkAction = async (action: 'approve' | 'retry', taskIds: string[]) => {
    if (!canOperateProjectRef.current || taskIds.length === 0) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/bulk-action`, {
        method: 'POST',
        headers: { ...(await authHeaders()), 'Content-Type': 'application/json' },
        body: JSON.stringify({ action, task_ids: taskIds }),
      });
      if (!res.ok) throw new Error('Request failed');
      const data = await res.json();
      fetchTasks();
      if (data.error_count === 0) {
        addToast(`${action === 'approve' ? 'Approved' : 'Retried'} ${data.success_count} task${data.success_count !== 1 ? 's' : ''}`, 'success');
      } else {
        addToast(`${data.success_count} succeeded, ${data.error_count} failed`, 'error');
      }
    } catch (error) {
      console.error(`Failed to bulk ${action} tasks:`, error);
      addToast(`Failed to ${action} tasks`, 'error');
    }
  };

  const handleBulkArchiveSelected = async () => {
    if (!canOperateProjectRef.current) return;
    const ids = Array.from(selectedTaskIds);
    if (ids.length === 0) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/bulk-archive`, {
        method: 'POST',
        headers: { ...(await authHeaders()), 'Content-Type': 'application/json' },
        body: JSON.stringify({ task_ids: ids }),
      });
      if (!res.ok) throw new Error('Request failed');
      const data = await res.json();
      setSelectedTaskIds(new Set());
      setSelectionMode(false);
      fetchTasks();
      addToast(`Archived ${data.archived_count} task${data.archived_count !== 1 ? 's' : ''}`, 'success');
    } catch (error) {
      console.error('Failed to archive selected tasks:', error);
      addToast('Failed to archive tasks', 'error');
    }
    setConfirmDialog(null);
  };

  const handleBulkArchiveColumn = async (taskIds: string[]) => {
    if (!canOperateProjectRef.current || taskIds.length === 0) return;
    // Chunk to avoid large single requests that can hit Cloud Run request timeouts.
    const CHUNK_SIZE = 100;
    try {
      let totalArchived = 0;
      for (let i = 0; i < taskIds.length; i += CHUNK_SIZE) {
        const chunk = taskIds.slice(i, i + CHUNK_SIZE);
        const res = await apiFetch('/api/admin/tasks/bulk-archive', {
          method: 'POST',
          body: JSON.stringify({ task_ids: chunk }),
        });
        if (!res.ok) throw new Error(`Request failed (${res.status})`);
        const data = await res.json();
        totalArchived += data.archived_count ?? 0;
      }
      fetchTasks();
      addToast(`Archived ${totalArchived} task${totalArchived !== 1 ? 's' : ''}`, 'success');
    } catch (error) {
      console.error('Failed to archive column tasks:', error);
      addToast('Failed to archive tasks', 'error');
    }
    setConfirmDialog(null);
  };

  const handleResetIterationCap = async (taskId: string) => {
    if (!canOperateProjectRef.current) return;
    setActionLoadingMap(m => ({ ...m, [taskId]: 'reset-iteration-cap' }));
    try {
      const data = await resetTaskIterationCap(taskId);
      fetchTasks();
      addToast(`Iteration cap reset (archived ${data.archived_count} task${data.archived_count !== 1 ? 's' : ''})`, 'success');
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Failed to reset iteration cap', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleTriggerRollup = async (taskId: string) => {
    if (!canOperateProjectRef.current) return;
    setActionLoadingMap(m => ({ ...m, [taskId]: 'trigger-rollup' }));
    try {
      const data = await triggerTaskRollup(taskId);
      fetchTasks();
      const prNum = data.rollup_pr_number ?? data.pr_number;
      addToast(prNum ? `Rollup PR #${prNum} triggered` : 'Rollup triggered', 'success');
    } catch (error) {
      addToast(error instanceof Error ? error.message : 'Failed to trigger rollup', 'error');
    } finally {
      setActionLoadingMap(m => { const { [taskId]: _unused, ...rest } = m; void _unused; return rest; });
    }
  };

  const handleBulkCancel = async (taskIds: string[]) => {
    if (!canOperateProjectRef.current || taskIds.length === 0) return;
    try {
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks/bulk-action`, {
        method: 'POST',
        headers: { ...(await authHeaders()), 'Content-Type': 'application/json' },
        body: JSON.stringify({ action: 'cancel', task_ids: taskIds }),
      });
      if (!res.ok) throw new Error('Request failed');
      const data = await res.json();
      setSelectedTaskIds(new Set());
      setSelectionMode(false);
      fetchTasks();
      if (data.error_count === 0) {
        addToast(`Cancelled ${data.success_count} task${data.success_count !== 1 ? 's' : ''}`, 'success');
      } else {
        addToast(`${data.success_count} succeeded, ${data.error_count} failed`, 'error');
      }
    } catch (error) {
      console.error('Failed to bulk cancel tasks:', error);
      addToast('Failed to cancel tasks', 'error');
    }
    setConfirmDialog(null);
  };

  const needsAttentionCount = useMemo(() => {
    return tasks.filter(t =>
      t.status === 'FAILED' ||
      (t.requires_approval && t.status !== 'COMPLETED' && t.status !== 'CANCELLED')
    ).length;
  }, [tasks]);

  return {
    tasks, setTasks, totalTasks,
    globalActiveCount, globalEvaluatingCount, globalQueuedCount,
    scopedActiveCount, scopedEvaluatingCount, scopedQueuedCount,
    initialLoading, refreshing, fetchEpoch,
    page, setPage, limit, setLimit, refreshInterval, setRefreshInterval,
    actionLoadingMap,
    lastRefresh, apiHealthy, lastFetchTime,
    needsAttentionCount,
    fetchTasks, fetchCounts,
    handleApprove, handleRequestChanges, handleCancel, handleRetry,
    handleEditPrompt, handleDismiss, handleArchiveTask, handleUnarchiveTask,
    handleBulkArchive, handleBulkArchiveSelected, handleBulkArchiveColumn,
    handleBulkAction, handleBulkCancel,
    handleResetIterationCap, handleTriggerRollup,
  };
}
