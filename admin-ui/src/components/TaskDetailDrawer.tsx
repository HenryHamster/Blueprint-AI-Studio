/* eslint-disable react-hooks/immutability, react-hooks/set-state-in-effect */
import { memo, useCallback, useState, useRef, useEffect } from 'react';
import { Terminal, X, Copy, Check, CheckCircle, Hash, Maximize2, Minimize2, Loader2, WifiOff, Download, Diff, ChevronDown, ChevronRight, Plus, Minus, CopyPlus } from 'lucide-react';
import { LogsTab } from './TaskDetailDrawerLogs';
import type { LogLevel } from './TaskDetailDrawerLogs';
import { StatusBadge, TaskTypeBadge } from './TaskCard';
import type { Task } from './TaskCard';
import { DetailsTab } from './TaskDetailDrawerMeta';
import { API_BASE_URL, authGet, fetchTask, fetchTaskPrFiles } from '../api';
import type { PrFileEntry } from '../api';
import { isCancellableTaskStatus, taskCancelConsequence } from './taskCancelCopy';

interface TaskDetailDrawerProps {
  task: Task;
  canOperateProject: boolean;
  tasks?: Task[];
  logLines: string[];
  logsConnecting?: boolean;
  wsDisconnected?: boolean;
  wsDisconnectReason?: string | null;
  actionLoading?: string;
  gcpProjectId?: string;
  onClose: () => void;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onNavigateToTask?: (taskId: string) => void;
  onWidthChange?: (width: number) => void;
  onDuplicate?: (task: Task) => void;
  onResetIterationCap?: (id: string) => void;
  onTriggerRollup?: (id: string) => void;
}

interface CancelImpactResponse {
  task_id?: string;
  status?: string;
  waiting_dependent_count: number;
  waiting_dependent_statuses?: string[];
  active_child_cascade_count: number;
  active_child_cascade_statuses?: string[];
  target_vm_instance_id?: string | null;
  target_has_vm_id: boolean;
  target_vm_provisioned?: boolean;
  active_child_vm_id_count?: number;
  active_child_vm_provisioned_count?: number;
  orphan_pr_cleanup_context_reference_count?: number;
  pull_request_artifact_task_count?: number;
}

type CancelImpactStatus = 'idle' | 'loading' | 'ready' | 'error';

// A task has a PR number in context if it carries review_loop.pr_number or rollup_pr_number.
// Used to determine whether "Reset Iteration Cap" is available.
function hasReviewPr(task: Task): boolean {
  const ctx = task.context as Record<string, unknown> | undefined;
  if (!ctx) return false;
  const reviewLoop = ctx['review_loop'] as Record<string, unknown> | undefined;
  return !!(reviewLoop?.['pr_number'] || ctx['rollup_pr_number']);
}

type Tab = 'details' | 'logs' | 'changes';

// Approval consequence wording differs by task type: a compute_experiment
// approval releases real spend (it is NOT a code merge), so the label must say
// so and surface the spend cap when the payload carries one. Mirrors the
// TaskCard approval button so the two surfaces never disagree.
function approveButtonLabel(task: Task): string {
  if ((task.task_type as string) === 'compute_experiment') {
    const cap = (task.payloads?.[0] as Record<string, unknown> | undefined)?.spend_cap_usd;
    return cap != null ? `Approve Spend ($${Number(cap).toFixed(2)})` : 'Approve Spend';
  }
  return 'Approve & Merge';
}

// Visible impact only: this uses the loaded task set passed to the drawer, not
// a global backend impact endpoint.
function countVisibleWaitingDependents(taskId: string, tasks?: Task[]): number {
  if (!tasks) return 0;
  return tasks.filter(
    t =>
      t.id !== taskId &&
      t.depends_on?.includes(taskId) &&
      (t.status === 'QUEUED' || t.status === 'BLOCKED' || t.status === 'PROVISIONING' || t.status === 'PARKED'),
  ).length;
}

function cancelConsequenceWithImpact(task: Task, impact: CancelImpactResponse | null): string {
  const base = taskCancelConsequence(task);
  if (
    impact?.target_has_vm_id &&
    !task.vm_instance_id &&
    task.status !== 'IN_PROGRESS' &&
    task.status !== 'PROVISIONING'
  ) {
    return `${base} A VM is recorded for this task; cancellation will request cleanup if it is still attached.`;
  }
  return base;
}

function hasMergedPr(task: Task): boolean {
  return !!(task.pr_url || task.artifacts?.some(a => a.pr_url));
}

function isAutonomousMerge(task: Task): boolean {
  if ((task.created_by ?? '').toUpperCase() === 'USER') return false;
  const ctx = task.context ?? {};
  if (ctx['review_dismissed'] || ctx['user_overrode_review']) return false;
  return true;
}

function statusColor(status: string): string {
  switch (status) {
    case 'added': return 'text-emerald-400';
    case 'removed': return 'text-red-400';
    case 'renamed': return 'text-blue-400';
    case 'modified': return 'text-amber-400';
    default: return 'text-neutral-400';
  }
}

interface ChangesTabContentProps {
  prFiles: PrFileEntry[] | null;
  prNumber: number | null;
  loading: boolean;
  error: string | null;
  includePatch: boolean;
  expandedPatches: Set<string>;
  setExpandedPatches: (patches: Set<string>) => void;
  onLoadWithDiff: () => void;
  onReload: () => void;
}

function ChangesTabContent({
  prFiles,
  prNumber,
  loading,
  error,
  includePatch,
  expandedPatches,
  setExpandedPatches,
  onLoadWithDiff,
  onReload,
}: ChangesTabContentProps) {
  const togglePatch = (filename: string) => {
    const next = new Set(expandedPatches);
    if (next.has(filename)) next.delete(filename);
    else next.add(filename);
    setExpandedPatches(next);
  };

  if (loading) {
    return (
      <div className="flex-1 flex items-center justify-center text-neutral-500 gap-2">
        <Loader2 className="w-4 h-4 animate-spin" />
        <span className="text-sm">Loading changed files…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex-1 flex flex-col items-center justify-center gap-3 p-6">
        <p className="text-sm text-red-400">{error}</p>
        <button
          onClick={onReload}
          className="px-3 py-1.5 rounded-lg bg-workbench-panel hover:bg-workbench-hover text-neutral-300 text-sm transition-colors"
        >
          Retry
        </button>
      </div>
    );
  }

  if (prFiles === null) {
    return (
      <div className="flex-1 flex items-center justify-center text-neutral-600 text-sm">
        No PR found for this task.
      </div>
    );
  }

  if (prFiles.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center text-neutral-600 text-sm">
        No files changed in PR #{prNumber}.
      </div>
    );
  }

  const totalAdditions = prFiles.reduce((s, f) => s + f.additions, 0);
  const totalDeletions = prFiles.reduce((s, f) => s + f.deletions, 0);

  return (
    <div className="flex-1 overflow-y-auto">
      <div className="px-4 py-2 border-b border-neutral-800 flex items-center justify-between gap-3">
        <div className="flex items-center gap-3 text-xs text-neutral-500">
          <span>{prFiles.length} file{prFiles.length !== 1 ? 's' : ''}</span>
          <span className="text-emerald-400">+{totalAdditions}</span>
          <span className="text-red-400">−{totalDeletions}</span>
          {prNumber && (
            <span className="text-neutral-600">PR #{prNumber}</span>
          )}
        </div>
        {!includePatch && (
          <button
            onClick={onLoadWithDiff}
            className="text-xs text-blue-400 hover:text-blue-300 transition-colors"
          >
            Load diffs
          </button>
        )}
      </div>
      <div className="divide-y divide-neutral-800/50">
        {prFiles.map(file => (
          <div key={file.filename} className="text-xs">
            <button
              onClick={() => file.patch ? togglePatch(file.filename) : undefined}
              className={`w-full flex items-center gap-2 px-4 py-2.5 hover:bg-workbench-hover/50 transition-colors text-left ${file.patch ? 'cursor-pointer' : 'cursor-default'}`}
            >
              {file.patch ? (
                expandedPatches.has(file.filename)
                  ? <ChevronDown className="w-3 h-3 text-neutral-500 shrink-0" />
                  : <ChevronRight className="w-3 h-3 text-neutral-500 shrink-0" />
              ) : (
                <span className="w-3 h-3 shrink-0" />
              )}
              <span className={`shrink-0 w-14 ${statusColor(file.status)}`}>{file.status}</span>
              <span className="flex-1 text-neutral-200 font-mono truncate">{file.filename}</span>
              <span className="shrink-0 flex items-center gap-1.5 text-neutral-500">
                {file.additions > 0 && (
                  <span className="flex items-center gap-0.5 text-emerald-500">
                    <Plus className="w-2.5 h-2.5" />{file.additions}
                  </span>
                )}
                {file.deletions > 0 && (
                  <span className="flex items-center gap-0.5 text-red-500">
                    <Minus className="w-2.5 h-2.5" />{file.deletions}
                  </span>
                )}
              </span>
            </button>
            {file.patch && expandedPatches.has(file.filename) && (
              <div className="px-4 pb-3 bg-workbench-shell">
                <pre className="text-ui-md font-mono overflow-x-auto leading-relaxed whitespace-pre">
                  {file.patch.split('\n').map((line, i) => (
                    <div
                      key={i}
                      className={
                        line.startsWith('+') && !line.startsWith('+++')
                          ? 'bg-emerald-950/30 text-emerald-300'
                          : line.startsWith('-') && !line.startsWith('---')
                          ? 'bg-red-950/30 text-red-300'
                          : line.startsWith('@@')
                          ? 'text-blue-400'
                          : 'text-neutral-500'
                      }
                    >
                      {line || ' '}
                    </div>
                  ))}
                </pre>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

export const TaskDetailDrawer = memo(function TaskDetailDrawer({
  task,
  canOperateProject = false,
  tasks,
  logLines,
  logsConnecting = false,
  wsDisconnected = false,
  wsDisconnectReason = null,
  actionLoading,
  gcpProjectId,
  onClose,
  onApprove,
  onRequestChanges,
  onCancel,
  onRetry,
  onEditPrompt,
  onDismiss,
  onNavigateToTask,
  onWidthChange,
  onDuplicate,
  onResetIterationCap,
  onTriggerRollup,
}: TaskDetailDrawerProps) {
  const [activeTab, setActiveTab] = useState<Tab>('details');
  const [logFilter, setLogFilter] = useState('');
  const [logLevel, setLogLevel] = useState<LogLevel>('all');
  const [showLineNumbers, setShowLineNumbers] = useState(false);
  const [copiedLogs, setCopiedLogs] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const [editingPrompt, setEditingPrompt] = useState(false);
  const [editPromptValue, setEditPromptValue] = useState(() => {
    const payload = task.payloads?.[0] as Record<string, unknown> | undefined;
    return (payload?.prompt as string) || task.description;
  });
  const [drawerWidth, setDrawerWidth] = useState(680);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [isMobile, setIsMobile] = useState(false);
  const dragCleanupRef = useRef<(() => void) | null>(null);
  const [prFiles, setPrFiles] = useState<PrFileEntry[] | null>(null);
  const [prFilesLoading, setPrFilesLoading] = useState(false);
  const [prFilesError, setPrFilesError] = useState<string | null>(null);
  const [prNumber, setPrNumber] = useState<number | null>(null);
  const [prFilesLoaded, setPrFilesLoaded] = useState(false);
  const [expandedPatches, setExpandedPatches] = useState<Set<string>>(new Set());
  const [includePatch, setIncludePatch] = useState(false);
  const [cancelImpact, setCancelImpact] = useState<CancelImpactResponse | null>(null);
  const [cancelImpactStatus, setCancelImpactStatus] = useState<CancelImpactStatus>('idle');
  const [detailTask, setDetailTask] = useState<Task | null>(null);

  // Stable callbacks replacing inline arrow functions to minimize child re-renders.
  const handleApprove = useCallback(() => { if (canOperateProject) onApprove(task.id); }, [canOperateProject, onApprove, task.id]);
  const handleCancel = useCallback(() => { if (canOperateProject) onCancel(task.id); }, [canOperateProject, onCancel, task.id]);
  const handleRetry = useCallback(() => { if (canOperateProject) onRetry(task.id); }, [canOperateProject, onRetry, task.id]);
  const handleDismiss = useCallback(() => { if (canOperateProject) onDismiss(task.id); }, [canOperateProject, onDismiss, task.id]);
  const handleDuplicate = useCallback(() => { if (canOperateProject) onDuplicate?.(task); }, [canOperateProject, onDuplicate, task]);
  const handleResetIterationCap = useCallback(() => { if (canOperateProject) onResetIterationCap?.(task.id); }, [canOperateProject, onResetIterationCap, task.id]);
  const handleTriggerRollup = useCallback(() => { if (canOperateProject) onTriggerRollup?.(task.id); }, [canOperateProject, onTriggerRollup, task.id]);

  // Fetch full task detail (status_log, plan_output) which are omitted from the list endpoint.
  useEffect(() => {
    setDetailTask(null);
    let cancelled = false;
    fetchTask(task.id).then(t => {
      if (!cancelled) setDetailTask(t);
    }).catch(() => {});
    return () => { cancelled = true; };
  }, [task.id]);

  // Merge list task with detail task: detail fields overlay list fields when available.
  const richTask: Task = detailTask ? { ...task, ...detailTask } : task;

  // Detect mobile viewport (< 640px, Tailwind sm: breakpoint)
  useEffect(() => {
    const handleResize = () => {
      setIsMobile(window.innerWidth < 640);
    };
    handleResize();
    window.addEventListener('resize', handleResize);
    return () => window.removeEventListener('resize', handleResize);
  }, []);

  // Report drawer width to parent so sibling panels can offset
  useEffect(() => {
    const width = isMobile ? window.innerWidth : (isFullscreen ? window.innerWidth : drawerWidth);
    onWidthChange?.(width);
  }, [drawerWidth, isFullscreen, isMobile, onWidthChange]);

  // Reset log filter when task changes
  useEffect(() => {
    setLogFilter('');
    setLogLevel('all');
  }, [task.id]);

  // Clean up drag listeners on unmount
  useEffect(() => {
    return () => { dragCleanupRef.current?.(); };
  }, []);

  // Reset changes tab state when task changes
  useEffect(() => {
    setPrFiles(null);
    setPrFilesLoading(false);
    setPrFilesError(null);
    setPrNumber(null);
    setPrFilesLoaded(false);
    setExpandedPatches(new Set());
    setIncludePatch(false);
  }, [task.id]);

  // Reset all transient form state when task changes
  useEffect(() => {
    setEditingPrompt(false);
    const payload = task.payloads?.[0] as Record<string, unknown> | undefined;
    setEditPromptValue((payload?.prompt as string) || task.description);
    setConfirmCancel(false);
    setShowChangesForm(false);
    setChangesFeedback('');
    setActiveTab('details');
    setCancelImpact(null);
    setCancelImpactStatus('idle');
  }, [task.id, task.payloads, task.description]);

  useEffect(() => {
    setCancelImpact(null);
    if (!confirmCancel) {
      setCancelImpactStatus('idle');
      return;
    }

    let cancelled = false;
    setCancelImpactStatus('loading');
    authGet(`${API_BASE_URL}/api/admin/tasks/${encodeURIComponent(task.id)}/cancel-impact`)
      .then(async response => {
        if (!response.ok) throw new Error(`Failed to fetch cancel impact: ${response.status}`);
        return response.json() as Promise<CancelImpactResponse>;
      })
      .then(data => {
        if (!cancelled) {
          setCancelImpact(data);
          setCancelImpactStatus('ready');
        }
      })
      .catch(() => {
        if (!cancelled) {
          setCancelImpact(null);
          setCancelImpactStatus('error');
        }
      });

    return () => { cancelled = true; };
  }, [confirmCancel, task.id]);

  const loadPrFiles = async (withPatch: boolean) => {
    setPrFilesLoading(true);
    setPrFilesError(null);
    try {
      const data = await fetchTaskPrFiles(task.id, withPatch);
      setPrFiles(data.files);
      setPrNumber(data.pr_number ?? null);
      setPrFilesLoaded(true);
      setIncludePatch(withPatch);
    } catch (e) {
      setPrFilesError(e instanceof Error ? e.message : 'Failed to load PR files');
    } finally {
      setPrFilesLoading(false);
    }
  };

  const handleChangesTabActivate = () => {
    setActiveTab('changes');
    if (!prFilesLoaded && !prFilesLoading) {
      loadPrFiles(false);
    }
  };

  // Keyboard shortcut: f to toggle fullscreen
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'f' && !e.ctrlKey && !e.metaKey && !e.altKey) {
        const target = e.target as HTMLElement;
        if (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA') return;
        setIsFullscreen(prev => !prev);
      }
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, []);

  const handleDragMouseDown = (e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startWidth = drawerWidth;

    const onMouseMove = (moveE: MouseEvent) => {
      const delta = startX - moveE.clientX;
      setDrawerWidth(Math.min(1400, Math.max(400, startWidth + delta)));
    };

    const cleanup = () => {
      document.removeEventListener('mousemove', onMouseMove);
      document.removeEventListener('mouseup', cleanup);
      dragCleanupRef.current = null;
    };

    dragCleanupRef.current = cleanup;
    document.addEventListener('mousemove', onMouseMove);
    document.addEventListener('mouseup', cleanup);
  };

  const filteredLogLines = logFilter
    ? logLines.filter(line => line.toLowerCase().includes(logFilter.toLowerCase()))
    : logLines;

  const handleCopyLogs = () => {
    navigator.clipboard.writeText(filteredLogLines.join('\n'));
    setCopiedLogs(true);
    setTimeout(() => setCopiedLogs(false), 2000);
  };

  const handleDownloadLogs = () => {
    const blob = new Blob([logLines.join('\n')], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `task-${task.id}-logs.txt`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const showApproval = canOperateProject && task.requires_approval && !['FAILED', 'CANCELLED'].includes(task.status);
  const showCancel = canOperateProject && isCancellableTaskStatus(task.status) && !task.requires_approval;
  const showFailedActions = canOperateProject && task.status === 'FAILED';
  const showResetCap = canOperateProject && !!onResetIterationCap && hasReviewPr(task);
  const showTriggerRollup = canOperateProject && !!onTriggerRollup && task.status === 'EVALUATING';
  const [showChangesForm, setShowChangesForm] = useState(false);
  const [changesFeedback, setChangesFeedback] = useState('');

  return (
    <div className="fixed inset-0 z-50 flex">
      <div className="flex-1 bg-black/40" onClick={onClose} />
      <div
        className="relative bg-workbench-shell border-l border-neutral-800 flex flex-col shadow-2xl"
        style={isMobile || isFullscreen ? { width: '100%' } : { width: drawerWidth }}
      >
        {/* Drag handle */}
        {!isFullscreen && !isMobile && (
          <div
            className="absolute left-0 top-0 bottom-0 w-1 cursor-col-resize hover:bg-blue-500/40 transition-colors z-10"
            onMouseDown={handleDragMouseDown}
          />
        )}

        {/* Header */}
        <div className="p-4 border-b border-neutral-800 shrink-0">
          <div className="flex items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 mb-2">
                <Terminal className="w-4 h-4 text-neutral-400 shrink-0" />
                <h2 className="text-base font-semibold text-neutral-100 truncate">{task.title}</h2>
              </div>
              <div className="flex items-center gap-2 flex-wrap">
                <StatusBadge status={task.status} />
                <TaskTypeBadge taskType={task.task_type} />
                {hasMergedPr(task) && (
                  isAutonomousMerge(task)
                    ? <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-emerald-500/15 text-emerald-400 border border-emerald-500/25">Autonomous merge</span>
                    : <span className="px-2 py-0.5 rounded-full text-xs font-medium bg-blue-500/15 text-blue-400 border border-blue-500/25">Operator-assisted merge</span>
                )}
              </div>
            </div>
            <div className="flex items-center gap-1 shrink-0">
              {canOperateProject && onDuplicate && (
                <button
                  onClick={handleDuplicate}
                  title="Duplicate task"
                  className="p-2.5 md:p-1.5 rounded-lg hover:bg-workbench-hover text-neutral-400 hover:text-neutral-200 transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                >
                  <CopyPlus className="w-5 h-5" />
                </button>
              )}
              <button
                onClick={() => setIsFullscreen(prev => !prev)}
                title={isFullscreen ? 'Exit fullscreen (f)' : 'Fullscreen (f)'}
                className="p-2.5 md:p-1.5 rounded-lg hover:bg-workbench-hover text-neutral-400 hover:text-neutral-200 transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
              >
                {isFullscreen ? <Minimize2 className="w-5 h-5" /> : <Maximize2 className="w-5 h-5" />}
              </button>
              <button
                onClick={onClose}
                className="p-2.5 md:p-1.5 rounded-lg hover:bg-workbench-hover text-neutral-400 hover:text-neutral-200 transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
              >
                <X className="w-5 h-5" />
              </button>
            </div>
          </div>
        </div>

        {/* Tabs */}
        <div className="flex items-center border-b border-neutral-800 shrink-0">
          <button
            onClick={() => setActiveTab('details')}
            className={`px-4 py-2.5 min-h-[44px] text-sm font-medium transition-colors ${
              activeTab === 'details'
                ? 'text-neutral-100 border-b-2 border-blue-500'
                : 'text-neutral-500 hover:text-neutral-300'
            }`}
          >
            Details
          </button>
          <button
            onClick={() => setActiveTab('logs')}
            className={`px-4 py-2.5 min-h-[44px] text-sm font-medium transition-colors ${
              activeTab === 'logs'
                ? 'text-neutral-100 border-b-2 border-blue-500'
                : 'text-neutral-500 hover:text-neutral-300'
            }`}
          >
            Logs
            {logLines.length > 0 && (
              <span className="ml-1.5 text-xs text-neutral-500">({logLines.length})</span>
            )}
          </button>
          <button
            onClick={handleChangesTabActivate}
            className={`px-4 py-2.5 min-h-[44px] text-sm font-medium transition-colors flex items-center gap-1.5 ${
              activeTab === 'changes'
                ? 'text-neutral-100 border-b-2 border-blue-500'
                : 'text-neutral-500 hover:text-neutral-300'
            }`}
          >
            <Diff className="w-3.5 h-3.5" />
            Changes
          </button>
          {activeTab === 'logs' && (
            <div className="ml-auto mr-2 flex items-center gap-1">
              <button
                onClick={() => setShowLineNumbers(v => !v)}
                title="Toggle line numbers"
                className={`p-2.5 md:p-1.5 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center ${
                  showLineNumbers
                    ? 'text-blue-400 bg-blue-500/10'
                    : 'text-neutral-500 hover:text-neutral-300'
                }`}
              >
                <Hash className="w-3.5 h-3.5" />
              </button>
              <button
                onClick={handleCopyLogs}
                disabled={logLines.length === 0}
                title="Copy logs"
                className="p-2.5 md:p-1.5 rounded transition-colors text-neutral-500 hover:text-neutral-300 disabled:text-neutral-700 disabled:cursor-not-allowed min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
              >
                {copiedLogs ? <Check className="w-3.5 h-3.5 text-emerald-400" /> : <Copy className="w-3.5 h-3.5" />}
              </button>
              <button
                onClick={handleDownloadLogs}
                disabled={logLines.length === 0}
                title="Download logs"
                className="p-2.5 md:p-1.5 rounded transition-colors text-neutral-500 hover:text-neutral-300 disabled:text-neutral-700 disabled:cursor-not-allowed min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
              >
                <Download className="w-3.5 h-3.5" />
              </button>
            </div>
          )}
        </div>

        {/* Content */}
        {wsDisconnected && activeTab !== 'changes' && (
          <div className="flex items-center gap-2 px-4 py-2 bg-amber-950/40 border-b border-amber-800/50 text-amber-400 text-xs shrink-0">
            <WifiOff className="w-3.5 h-3.5 shrink-0" />
            Log stream disconnected
          </div>
        )}
        {activeTab === 'details' ? (
          <DetailsTab task={richTask} tasks={tasks} gcpProjectId={gcpProjectId} onNavigateToTask={onNavigateToTask} />
        ) : activeTab === 'changes' ? (
          <ChangesTabContent
            prFiles={prFiles}
            prNumber={prNumber}
            loading={prFilesLoading}
            error={prFilesError}
            includePatch={includePatch}
            expandedPatches={expandedPatches}
            setExpandedPatches={setExpandedPatches}
            onLoadWithDiff={() => loadPrFiles(true)}
            onReload={() => loadPrFiles(includePatch)}
          />
        ) : (
          <>
            {wsDisconnected && (
              <div className="flex items-center gap-2 px-4 py-2 bg-amber-950/40 border-b border-amber-800/50 text-amber-400 text-xs shrink-0">
                <WifiOff className="w-3.5 h-3.5 shrink-0" />
                {wsDisconnectReason ?? 'Log stream disconnected'}
              </div>
            )}
            <LogsTab
              logLines={logLines}
              task={task}
              logFilter={logFilter}
              setLogFilter={setLogFilter}
              logLevel={logLevel}
              setLogLevel={setLogLevel}
              showLineNumbers={showLineNumbers}
              logsConnecting={logsConnecting}
            />
          </>
        )}

        {/* Action Footer */}
        {(showApproval || showCancel || showFailedActions || showResetCap || showTriggerRollup) && (
          <div className="border-t border-neutral-800 p-4 shrink-0 flex items-center gap-2 flex-wrap">
            {showApproval && (
              <div className="flex flex-col gap-2 w-full">
                <div className="flex flex-wrap gap-2">
                  <button
                    onClick={handleApprove}
                    disabled={!!actionLoading}
                    className="flex-1 px-6 py-3 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-semibold transition-colors disabled:opacity-50 flex items-center justify-center gap-2"
                  >
                    {actionLoading === 'approve' ? <Loader2 className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />} {approveButtonLabel(task)}
                  </button>
                  <button
                    onClick={() => setShowChangesForm(!showChangesForm)}
                    className="px-4 py-3 rounded-lg bg-workbench-panel hover:bg-workbench-hover text-neutral-300 text-sm font-medium transition-colors border border-neutral-700 flex items-center justify-center gap-1.5"
                  >
                    Request Changes
                  </button>
                  <button
                    onClick={handleCancel}
                    disabled={!!actionLoading}
                    className="px-3 py-3 rounded-lg bg-workbench-panel hover:bg-red-900/50 text-neutral-500 hover:text-red-400 text-sm transition-colors disabled:opacity-50 border border-neutral-700"
                  >
                    Reject
                  </button>
                </div>
                {showChangesForm && (
                  <div className="flex flex-col gap-2">
                    <textarea
                      value={changesFeedback}
                      onChange={e => setChangesFeedback(e.target.value)}
                      placeholder="Describe what needs to change..."
                      className="w-full bg-workbench-base border border-neutral-700 rounded-lg text-sm text-neutral-200 p-3 resize-none placeholder-neutral-600 workbench-focus"
                      rows={3}
                    />
                    <div className="flex gap-2">
                      <button
                        onClick={() => { if (!canOperateProject) return; onRequestChanges(task.id, changesFeedback, 'fix'); setShowChangesForm(false); setChangesFeedback(''); }}
                        disabled={!changesFeedback.trim() || !!actionLoading}
                        className="flex-1 bg-amber-500/10 hover:bg-amber-500/20 text-amber-400 border border-amber-500/20 py-2 min-h-[44px] rounded-lg text-sm font-medium transition-colors disabled:opacity-30 disabled:cursor-not-allowed flex items-center justify-center gap-1.5"
                      >
                        {actionLoading === 'request-changes' ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null} Send Fix
                      </button>
                      <button
                        onClick={() => { if (!canOperateProject) return; onRequestChanges(task.id, changesFeedback || 'Re-review requested', 'review'); setShowChangesForm(false); setChangesFeedback(''); }}
                        disabled={!!actionLoading}
                        className="flex-1 bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/20 py-2 min-h-[44px] rounded-lg text-sm font-medium transition-colors flex items-center justify-center gap-1.5"
                      >
                        Re-review
                      </button>
                    </div>
                  </div>
                )}
              </div>
            )}

            {showCancel && !confirmCancel && (
              <button
                onClick={() => setConfirmCancel(true)}
                className="px-4 py-2 min-h-[44px] rounded-lg bg-red-600/20 hover:bg-red-600/30 text-red-400 text-sm font-medium transition-colors border border-red-600/30"
              >
                Cancel Task
              </button>
            )}
            {showCancel && confirmCancel && (() => {
              const visibleWaiting = countVisibleWaitingDependents(task.id, tasks);
              const cancelImpactPending = cancelImpactStatus === 'idle' || cancelImpactStatus === 'loading';
              const cancelImpactError = cancelImpactStatus === 'error';
              const waiting = cancelImpact?.waiting_dependent_count ?? (cancelImpactError ? visibleWaiting : 0);
              const activeChildren = cancelImpact?.active_child_cascade_count ?? 0;
              const childVmIds = cancelImpact?.active_child_vm_id_count ?? 0;
              const childVmProvisioned = cancelImpact?.active_child_vm_provisioned_count ?? 0;
              const prContextReferences = cancelImpact?.orphan_pr_cleanup_context_reference_count ?? 0;
              const prArtifactTasks = cancelImpact?.pull_request_artifact_task_count ?? 0;
              const vmImpactParts = [
                cancelImpact?.target_has_vm_id
                  ? `target VM ${cancelImpact.target_vm_provisioned ? 'provisioned' : 'recorded'}`
                  : null,
                childVmIds > 0
                  ? `${childVmIds} child VM${childVmIds > 1 ? 's' : ''} recorded`
                  : null,
                childVmProvisioned > 0
                  ? `${childVmProvisioned} child VM${childVmProvisioned > 1 ? 's' : ''} provisioned`
                  : null,
              ].filter((part): part is string => Boolean(part));
              return (
                <div className="flex flex-col gap-1.5 w-full">
                  <span className="text-sm text-neutral-300 font-medium">Cancel this task?</span>
                  <span className="text-xs text-neutral-500">
                    {cancelConsequenceWithImpact(task, cancelImpact)}
                  </span>
                  {cancelImpactPending && !cancelImpact && (
                    <span className="text-xs text-neutral-600" data-testid="cancel-impact-loading">
                      Checking full cancel impact...
                    </span>
                  )}
                  {cancelImpactError && (
                    <span className="text-xs text-amber-300" data-testid="cancel-impact-fallback">
                      Full impact check failed; showing visible loaded-task dependents only.
                    </span>
                  )}
                  {waiting > 0 && (
                    <span className="text-xs text-amber-400" data-testid="cancel-blast-radius">
                      {cancelImpact
                        ? `${waiting} waiting task${waiting > 1 ? 's' : ''} depend${waiting > 1 ? '' : 's'} on this and may be stranded.`
                        : `${waiting} visible waiting task${waiting > 1 ? 's' : ''} depend${waiting > 1 ? '' : 's'} on this in the loaded task set and may be stranded.`}
                    </span>
                  )}
                  {activeChildren > 0 && (
                    <span className="text-xs text-amber-400" data-testid="cancel-child-cascade">
                      {activeChildren} active child task{activeChildren > 1 ? 's' : ''} may be cancelled with this task.
                    </span>
                  )}
                  {vmImpactParts.length > 0 && (
                    <span className="text-xs text-amber-400" data-testid="cancel-vm-cleanup">
                      VM cleanup: {vmImpactParts.join('; ')}. Cleanup will be requested where still attached.
                    </span>
                  )}
                  {(prContextReferences > 0 || prArtifactTasks > 0) && (
                    <span className="text-xs text-amber-400" data-testid="cancel-pr-cleanup">
                      PR cleanup: {prContextReferences} task{prContextReferences === 1 ? '' : 's'} with PR context and {prArtifactTasks} PR artifact task{prArtifactTasks === 1 ? '' : 's'} are in the cancel scope.
                    </span>
                  )}
                  <div className="flex items-center gap-2 mt-0.5">
                    <button
                      onClick={() => {
                        if (cancelImpactPending) return;
                        onCancel(task.id);
                        setConfirmCancel(false);
                      }}
                      disabled={!!actionLoading || cancelImpactPending}
                      className="px-3 py-1.5 min-h-[44px] rounded-lg bg-red-600 hover:bg-red-500 text-white text-sm font-medium transition-colors disabled:opacity-50 flex items-center gap-1.5"
                    >
                      {(actionLoading === 'cancel' || cancelImpactPending) && <Loader2 className="w-3 h-3 animate-spin" />}Confirm cancel
                    </button>
                    <button
                      onClick={() => setConfirmCancel(false)}
                      className="px-3 py-1.5 min-h-[44px] rounded-lg bg-workbench-panel hover:bg-workbench-hover text-neutral-300 text-sm font-medium transition-colors"
                    >
                      Keep running
                    </button>
                  </div>
                </div>
              );
            })()}

            {showFailedActions && (
              <>
                <button
                  onClick={handleRetry}
                  disabled={!!actionLoading}
                  className="px-4 py-2 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium transition-colors disabled:opacity-50 flex items-center gap-1.5"
                >
                  {actionLoading === 'retry' && <Loader2 className="w-3 h-3 animate-spin" />}Retry
                </button>
                {!editingPrompt ? (
                  <button
                    onClick={() => setEditingPrompt(true)}
                    className="px-4 py-2 rounded-lg bg-workbench-panel hover:bg-workbench-hover text-neutral-200 text-sm font-medium transition-colors border border-neutral-700"
                  >
                    Edit
                  </button>
                ) : (
                  <div className="flex-1 flex items-center gap-2">
                    <input
                      type="text"
                      value={editPromptValue}
                      onChange={(e) => setEditPromptValue(e.target.value)}
                      className="flex-1 px-3 py-1.5 rounded-lg bg-workbench-base border border-neutral-700 text-sm text-neutral-200 workbench-focus"
                    />
                    <button
                      onClick={() => { if (!canOperateProject) return; onEditPrompt(task.id, editPromptValue); setEditingPrompt(false); }}
                      disabled={!!actionLoading}
                      className="px-3 py-1.5 rounded-lg bg-blue-600 hover:bg-blue-500 text-white text-sm font-medium transition-colors disabled:opacity-50 flex items-center gap-1.5"
                    >
                      {actionLoading === 'edit' && <Loader2 className="w-3 h-3 animate-spin" />}Save
                    </button>
                    <button
                      onClick={() => { setEditingPrompt(false); setEditPromptValue(task.description); }}
                      className="px-3 py-1.5 rounded-lg bg-workbench-panel hover:bg-workbench-hover text-neutral-300 text-sm transition-colors"
                    >
                      Cancel
                    </button>
                  </div>
                )}
                <button
                  onClick={handleDismiss}
                  disabled={!!actionLoading}
                  className="px-4 py-2 rounded-lg bg-workbench-panel hover:bg-workbench-hover text-neutral-400 text-sm font-medium transition-colors disabled:opacity-50 flex items-center gap-1.5"
                >
                  {actionLoading === 'dismiss' && <Loader2 className="w-3 h-3 animate-spin" />}Dismiss
                </button>
              </>
            )}

            {showResetCap && (
              <button
                onClick={handleResetIterationCap}
                disabled={!!actionLoading}
                title="Archive failed review/fix tasks on this PR and reset the iteration counter"
                className="px-4 py-2 rounded-lg bg-amber-600/20 hover:bg-amber-600/30 text-amber-400 text-sm font-medium transition-colors disabled:opacity-50 border border-amber-600/30 flex items-center gap-1.5"
              >
                {actionLoading === 'reset-iteration-cap' && <Loader2 className="w-3 h-3 animate-spin" />}Reset Review Cap
              </button>
            )}

            {showTriggerRollup && (
              <button
                onClick={handleTriggerRollup}
                disabled={!!actionLoading}
                title="Force-create the rollup PR for this EVALUATING parent task"
                className="px-4 py-2 rounded-lg bg-blue-600/20 hover:bg-blue-600/30 text-blue-400 text-sm font-medium transition-colors disabled:opacity-50 border border-blue-600/30 flex items-center gap-1.5"
              >
                {actionLoading === 'trigger-rollup' && <Loader2 className="w-3 h-3 animate-spin" />}Force Rollup
              </button>
            )}
          </div>
        )}
      </div>
    </div>
  );
});
