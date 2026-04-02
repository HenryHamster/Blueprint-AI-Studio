/* eslint-disable react-refresh/only-export-components */
import React, { useState, useEffect, useRef, memo } from 'react';
import { Square, CheckCircle, AlertTriangle, Clock, SplitSquareHorizontal, Server, Hash, Activity, ExternalLink, Terminal, RotateCcw, Pencil, X, ChevronRight, ChevronDown, ChevronUp, Search, GitFork, Microscope, Shield, Archive, BookOpen, ShieldAlert, TestTube, Package, ArrowUpDown, Wrench } from 'lucide-react';

export interface ResearchFinding {
  type?: string;
  title: string;
  description: string;
  prompt: string;
  scope: 'small' | 'medium' | 'large';
  risk: 'low' | 'medium' | 'high';
  priority: number;
  related_files: string[];
  direction_id?: string;
}

export function isSafeUrl(url: string): boolean {
  try {
    const parsed = new URL(url);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:';
  } catch {
    return false;
  }
}

export function linkifyStatusMessage(msg: string, prUrl?: string): React.ReactNode {
  // Derive base URL from pr_url, e.g. https://github.com/owner/repo/pull/42 -> https://github.com/owner/repo/pull/
  let baseUrl: string | undefined;
  if (prUrl) {
    const m = prUrl.match(/^(.+\/pull\/)\d+/);
    if (m) baseUrl = m[1];
  }
  if (!baseUrl) return msg;

  const regex = /PR #(\d+)/g;
  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let match: RegExpExecArray | null;

  while ((match = regex.exec(msg)) !== null) {
    if (lastIndex < match.index) parts.push(msg.slice(lastIndex, match.index));
    const url = `${baseUrl}${match[1]}`;
    if (isSafeUrl(url)) {
      parts.push(
        <a key={match.index} href={url} target="_blank" rel="noopener noreferrer" className="text-blue-400 hover:text-blue-300 underline">
          {match[0]}
        </a>
      );
    } else {
      parts.push(match[0]);
    }
    lastIndex = match.index + match[0].length;
  }

  if (parts.length === 0) return msg;
  if (lastIndex < msg.length) parts.push(msg.slice(lastIndex));
  return <>{parts}</>;
}

export type TaskStatus = 'QUEUED' | 'PROVISIONING' | 'IN_PROGRESS' | 'EVALUATING' | 'COMPLETED' | 'FAILED' | 'CANCELLED';
export type TaskType = 'create' | 'plan' | 'research' | 'review' | 'fix' | 'refactor' | 'docs' | 'security_audit' | 'qa' | 'deps' | 'migrate';

export interface Task {
  id: string;
  project_id?: string;
  title: string;
  description: string;
  status: TaskStatus;
  task_type?: TaskType;
  vm_instance_id?: string;
  latest_status_message?: string;
  status_log?: { t: string; msg: string }[];
  created_by: string;
  requires_approval: boolean;
  parent_task_id?: string;
  depends_on?: string[];
  plan_output?: Record<string, unknown>;
  created_at: string;
  started_at?: string;
  last_heartbeat?: string;
  attempt_count?: number;
  payloads?: Record<string, unknown>[];
  artifacts?: Array<{artifact_type: string; pr_url?: string; commit_hash?: string; repo_url?: string; logs?: string; iteration: number; created_at?: string}>;
  pr_url?: string;
  commit_hash?: string;
  repo_url?: string;
  error_logs?: string;
  error_logs_truncated?: boolean;
  research_findings?: ResearchFinding[] | null;
  context?: Record<string, unknown>;
  history?: { timestamp: string; action: string; details: Record<string, unknown> }[];
  child_task_ids?: string[];
}

export const TASK_TYPE_STYLES: Record<string, { bg: string; text: string; icon: typeof Search; label: string }> = {
  plan: { bg: 'bg-purple-500/10 border-purple-500/20', text: 'text-purple-400', icon: GitFork, label: 'Plan' },
  research: { bg: 'bg-cyan-500/10 border-cyan-500/20', text: 'text-cyan-400', icon: Microscope, label: 'Research' },
  review: { bg: 'bg-amber-500/10 border-amber-500/20', text: 'text-amber-400', icon: Search, label: 'Review' },
  fix: { bg: 'bg-orange-500/10 border-orange-500/20', text: 'text-orange-400', icon: RotateCcw, label: 'Fix' },
  refactor: { bg: 'bg-teal-500/10 border-teal-500/20', text: 'text-teal-400', icon: Wrench, label: 'Refactor' },
  docs: { bg: 'bg-indigo-500/10 border-indigo-500/20', text: 'text-indigo-400', icon: BookOpen, label: 'Docs' },
  security_audit: { bg: 'bg-red-500/10 border-red-500/20', text: 'text-red-400', icon: ShieldAlert, label: 'Security Audit' },
  qa: { bg: 'bg-green-500/10 border-green-500/20', text: 'text-green-400', icon: TestTube, label: 'QA' },
  deps: { bg: 'bg-blue-500/10 border-blue-500/20', text: 'text-blue-400', icon: Package, label: 'Deps' },
  migrate: { bg: 'bg-teal-500/10 border-teal-500/20', text: 'text-teal-400', icon: ArrowUpDown, label: 'Migrate' },
};

interface TaskCardProps {
  task: Task;
  maxRetries?: number;
  actionLoading?: string;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onArchive: (id: string) => void;
  onViewLogs: (task: Task) => void;
}

function formatDuration(ms: number): string {
  const totalMinutes = Math.floor(ms / 60000);
  if (totalMinutes < 1) return '<1m';
  const hours = Math.floor(totalMinutes / 60);
  const minutes = totalMinutes % 60;
  if (hours === 0) return `${minutes}m`;
  return `${hours}h ${minutes}m`;
}

export function useElapsedTime(startIso: string, endIso?: string): string {
  const [now, setNow] = useState(() => Date.now());
  const intervalRef = useRef<ReturnType<typeof setInterval>>(undefined);
  const isLive = !endIso;

  useEffect(() => {
    if (!isLive) return;
    intervalRef.current = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(intervalRef.current);
  }, [isLive]);

  const start = new Date(startIso).getTime();
  const end = endIso ? new Date(endIso).getTime() : now;
  return formatDuration(Math.max(0, end - start));
}

export function getHeartbeatStatus(lastHeartbeat?: string) {
  if (!lastHeartbeat) return { color: 'bg-neutral-600', text: 'No heartbeat' };

  const diffMinutes = (new Date().getTime() - new Date(lastHeartbeat).getTime()) / 60000;
  if (diffMinutes < 2) return { color: 'bg-emerald-500', text: 'Healthy (< 2m)' };
  if (diffMinutes < 5) return { color: 'bg-amber-500', text: 'Delayed (2-5m)' };
  return { color: 'bg-red-500', text: 'Stale (> 5m)' };
}

export function TaskTypeBadge({ taskType }: { taskType?: TaskType }) {
  if (!taskType || !(taskType in TASK_TYPE_STYLES)) return null;
  const style = TASK_TYPE_STYLES[taskType];
  const Icon = style.icon;
  return (
    <span className={`${style.bg} ${style.text} border text-[10px] uppercase tracking-wider px-2 py-0.5 rounded font-semibold whitespace-nowrap flex items-center gap-1`}>
      <Icon className="w-3 h-3" />{style.label}
    </span>
  );
}

function ResearchFindings({ findings }: { findings: ResearchFinding[] }) {
  const [expanded, setExpanded] = useState(false);

  if (!Array.isArray(findings)) return null;
  const items = expanded ? findings : findings.slice(0, 5);
  return (
    <div>
      <div className={`space-y-1 ${expanded ? 'max-h-64 overflow-y-auto custom-scrollbar' : ''}`}>
        {items.map((f, i) => (
          <div key={i} className="text-neutral-300">
            <span className={`inline-block text-[9px] uppercase px-1 rounded mr-1 ${f.risk === 'high' ? 'bg-red-900/40 text-red-400' : f.risk === 'medium' ? 'bg-amber-900/40 text-amber-400' : 'bg-emerald-900/40 text-emerald-400'}`}>{f.scope}/{f.risk}</span>
            {f.title}
            {expanded && f.description ? (
              <div className="text-[10px] text-neutral-500 ml-1 mt-0.5">{f.description}</div>
            ) : null}
          </div>
        ))}
      </div>
      {findings.length > 5 ? (
        <button
          onClick={() => setExpanded(!expanded)}
          className="text-[10px] text-neutral-500 hover:text-neutral-300 mt-1 transition-colors"
        >
          {expanded ? 'Show less' : `Show all ${findings.length} findings`}
        </button>
      ) : null}
    </div>
  );
}

export function StatusBadge({ status }: { status: TaskStatus }) {
  if (status === 'PROVISIONING') {
    return (
      <span className="inline-flex items-center gap-1 bg-blue-500/10 text-blue-400 border border-blue-500/20 text-[10px] uppercase tracking-wider px-2 py-0.5 rounded font-semibold whitespace-nowrap">
        <Activity className="w-3 h-3 animate-spin" />Provisioning…
      </span>
    );
  }
  if (status === 'EVALUATING') {
    return (
      <span className="inline-flex items-center gap-1 bg-blue-500/10 text-blue-400 border border-blue-500/20 text-[10px] uppercase tracking-wider px-2 py-0.5 rounded font-semibold whitespace-nowrap">
        <Clock className="w-3 h-3" />Evaluating subtasks
      </span>
    );
  }
  if (status === 'CANCELLED') {
    return (
      <span className="inline-flex items-center gap-1 bg-neutral-500/10 text-neutral-500 border border-neutral-500/20 text-[10px] uppercase tracking-wider px-2 py-0.5 rounded font-semibold whitespace-nowrap">
        <X className="w-3 h-3" />Cancelled
      </span>
    );
  }
  return null;
}

function PlanPreview({ planOutput }: { planOutput: Record<string, unknown> }) {
  const subtasks = planOutput.subtasks as Record<string, unknown>[] | undefined;
  if (!subtasks) return null;
  return (
    <div className="bg-purple-950/20 text-purple-300 text-[11px] p-2.5 rounded-lg mb-3 border border-purple-900/30 max-h-32 overflow-y-auto">
      <span className="font-semibold block mb-1 text-purple-400 font-sans">Plan ({subtasks.length} subtasks):</span>
      {subtasks.map((s, i) => (
        <div key={i} className="mb-0.5 text-neutral-300 font-mono">
          {(s.depends_on_indices as number[] || []).length > 0 ? '  \u2514\u2500 ' : ''}{String(s.title)}
        </div>
      ))}
    </div>
  );
}

export const TaskCard = memo(function TaskCard({ task, maxRetries, actionLoading, onApprove, onRequestChanges, onCancel, onRetry, onEditPrompt, onDismiss, onArchive, onViewLogs }: TaskCardProps) {
  const [editing, setEditing] = useState(false);
  const [editPrompt, setEditPrompt] = useState(task.description || '');
  const [descExpanded, setDescExpanded] = useState(false);
  const [statusExpanded, setStatusExpanded] = useState(false);
  const [historyExpanded, setHistoryExpanded] = useState(false);
  const [confirmAction, setConfirmAction] = useState<'cancel' | 'dismiss' | 'archive' | null>(null);
  const [confirmCountdown, setConfirmCountdown] = useState(3);
  const [showChangesForm, setShowChangesForm] = useState(false);
  const [changesFeedback, setChangesFeedback] = useState('');
  const confirmTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const confirmIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
      if (confirmIntervalRef.current) clearInterval(confirmIntervalRef.current);
    };
  }, []);

  const handleDestructiveClick = (action: 'cancel' | 'dismiss' | 'archive', callback: () => void) => {
    if (confirmAction === action) {
      if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
      if (confirmIntervalRef.current) clearInterval(confirmIntervalRef.current);
      setConfirmAction(null);
      setConfirmCountdown(3);
      callback();
    } else {
      if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
      if (confirmIntervalRef.current) clearInterval(confirmIntervalRef.current);
      setConfirmAction(action);
      setConfirmCountdown(3);
      let remaining = 3;
      confirmIntervalRef.current = setInterval(() => {
        remaining -= 1;
        setConfirmCountdown(remaining);
        if (remaining <= 0) clearInterval(confirmIntervalRef.current!);
      }, 1000);
      confirmTimeoutRef.current = setTimeout(() => {
        setConfirmAction(null);
        setConfirmCountdown(3);
      }, 3000);
    }
  };
  const isChild = !!task.parent_task_id;
  const heartbeat = getHeartbeatStatus(task.last_heartbeat);
  const isActive = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';
  const isActiveForDuration = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING' || task.status === 'EVALUATING';
  const isTerminal = task.status === 'COMPLETED' || task.status === 'FAILED' || task.status === 'CANCELLED';
  const endTime = isTerminal && task.history && task.history.length > 0
    ? task.history[task.history.length - 1].timestamp
    : isTerminal ? task.created_at : undefined;
  const duration = useElapsedTime(task.created_at, isActiveForDuration ? undefined : endTime);

  return (
    <div className={`bg-neutral-900 p-4 rounded-xl border transition-shadow ${task.status === 'CANCELLED' ? 'opacity-60' : ''} ${isChild ? 'border-l-4 border-l-neutral-500 border-y-neutral-800 border-r-neutral-800 shadow-sm' : 'border-neutral-800 shadow-sm hover:shadow-md hover:border-neutral-700'}`}>
      <div className="flex justify-between items-start mb-3">
        <div className="flex items-center gap-2 pr-2 overflow-hidden">
          {isChild && <span title="Child Task" className="shrink-0 flex items-center"><SplitSquareHorizontal className="w-4 h-4 text-neutral-500" /></span>}
          <h3 className={`font-medium text-sm truncate ${task.status === 'CANCELLED' ? 'text-neutral-500 line-through' : 'text-neutral-100'}`} title={task.title}>{task.title}</h3>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <StatusBadge status={task.status} />
          {isActive && task.last_heartbeat && (
            <div className={`w-2.5 h-2.5 rounded-full shrink-0 ${heartbeat.color}`} title={heartbeat.text} />
          )}
          <TaskTypeBadge taskType={task.task_type} />
          {task.requires_approval && (
            <span className="bg-amber-500/10 text-amber-500 border border-amber-500/20 text-[10px] uppercase tracking-wider px-2 py-0.5 rounded font-semibold whitespace-nowrap flex items-center gap-1">
              <Shield className="w-3 h-3" />Approval
            </span>
          )}
          {(task.attempt_count ?? 0) > 0 && (
            <span className="bg-neutral-800 text-neutral-400 text-[10px] px-1.5 py-0.5 rounded font-mono">
              #{(task.attempt_count ?? 0) + 1}
            </span>
          )}
        </div>
      </div>

      <div className="flex flex-col gap-1 mb-3 text-[10px] text-neutral-500 font-mono">
        <div className="flex items-center gap-1.5" title="Task ID">
          <Hash className="w-3 h-3" />
          <span className="truncate">{task.id}</span>
        </div>
        {task.vm_instance_id ? (
          <div className="flex items-center gap-1.5" title="VM Instance ID">
            <Server className="w-3 h-3" />
            <span className="truncate">{task.vm_instance_id}</span>
          </div>
        ) : null}
      </div>

      {isActive && heartbeat.text.startsWith('Stale') && (
        <div className="bg-amber-50 border border-amber-200 text-amber-700 rounded px-2 py-1 text-xs flex items-center gap-1.5 mb-3">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0" style={{ width: 14, height: 14 }} />
          No heartbeat for 5+ minutes — task may be stuck
        </div>
      )}

      <div className="mb-3">
        <p className={`text-xs text-neutral-400 ${descExpanded ? '' : 'line-clamp-2'}`}>{task.description}</p>
        {task.description.length > 120 && (
          <button
            onClick={(e) => { e.stopPropagation(); setDescExpanded(!descExpanded); }}
            className="text-xs text-gray-400 hover:text-gray-600 cursor-pointer flex items-center gap-0.5 mt-0.5"
          >
            {descExpanded ? <>Show less <ChevronUp className="w-3 h-3" /></> : <>Show more <ChevronDown className="w-3 h-3" /></>}
          </button>
        )}
      </div>

      {task.latest_status_message ? (
        <div className="bg-neutral-950 text-neutral-400 text-[11px] rounded-lg mb-3 border border-neutral-800/50 font-mono">
          <button
            onClick={() => setStatusExpanded(!statusExpanded)}
            className="w-full flex items-center gap-1.5 p-2.5 text-left hover:text-neutral-300 transition-colors"
          >
            <ChevronRight className={`w-3 h-3 shrink-0 text-neutral-500 transition-transform ${statusExpanded ? 'rotate-90' : ''}`} />
            <span className="font-semibold text-neutral-500 font-sans text-[11px]">Agent Status:</span>
            {!statusExpanded && (
              <span className="truncate ml-1">&gt; {task.latest_status_message.split('\n')[0].slice(0, 80)}</span>
            )}
          </button>
          {statusExpanded && (
            <div className="px-2.5 pb-2.5 whitespace-pre-wrap break-words">
              &gt; {linkifyStatusMessage(task.latest_status_message, task.pr_url)}
            </div>
          )}
        </div>
      ) : null}

      {(task.status === 'COMPLETED' || task.status === 'EVALUATING') && task.pr_url && isSafeUrl(task.pr_url) ? (
        <a
          href={task.pr_url}
          target="_blank"
          rel="noopener noreferrer"
          className="flex items-center gap-1.5 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-xs px-3 py-2 rounded-lg mb-3 hover:bg-emerald-500/20 transition-colors font-medium"
        >
          <ExternalLink className="w-3.5 h-3.5" />
          View Pull Request
        </a>
      ) : (task.task_type === 'review' || task.task_type === 'fix') && task.repo_url && task.payloads?.[0]?.pr_number && isSafeUrl(`${task.repo_url}/pull/${task.payloads[0].pr_number}`) ? (
        <a
          href={`${task.repo_url}/pull/${task.payloads[0].pr_number}`}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-neutral-400 hover:text-neutral-200 text-[11px] mb-3 transition-colors"
        >
          <ExternalLink className="w-3 h-3" />
          PR #{task.payloads[0].pr_number as number}
        </a>
      ) : null}

      {task.status === 'FAILED' && task.error_logs ? (
        <div className="bg-red-950/30 text-red-400 text-[11px] p-2.5 rounded-lg mb-3 border border-red-900/30 font-mono max-h-24 overflow-y-auto">
          <span className="font-semibold block mb-1 text-red-500 font-sans">Error:</span>
          {task.error_logs.slice(0, 500)}
        </div>
      ) : null}

      {task.research_findings ? (
        <div className="bg-cyan-950/20 text-cyan-300 text-[11px] p-2.5 rounded-lg mb-3 border border-cyan-900/30 font-mono max-h-32 overflow-y-auto">
          <span className="font-semibold block mb-1 text-cyan-400 font-sans">Research Findings:</span>
          <ResearchFindings findings={task.research_findings} />
        </div>
      ) : null}

      {task.plan_output ? <PlanPreview planOutput={task.plan_output} /> : null}

      {task.depends_on && task.depends_on.length > 0 ? (
        <div className="text-[10px] text-neutral-500 mb-3 font-mono flex items-center gap-1">
          <GitFork className="w-3 h-3" />
          Waiting on {task.depends_on.length} task{task.depends_on.length > 1 ? 's' : ''}
        </div>
      ) : null}

      {/* History Section */}
      {((task.attempt_count ?? 0) > 0 || (task.history && task.history.length > 0)) && (
        <div className="bg-neutral-950 text-neutral-400 text-[11px] rounded-lg mb-3 border border-neutral-800/50 font-mono">
          <button
            onClick={() => setHistoryExpanded(!historyExpanded)}
            className="w-full flex items-center gap-1.5 p-2.5 text-left hover:text-neutral-300 transition-colors"
          >
            <ChevronRight className={`w-3 h-3 shrink-0 text-neutral-500 transition-transform ${historyExpanded ? 'rotate-90' : ''}`} />
            <span className="font-semibold text-neutral-500 font-sans text-[11px]">History</span>
            {(task.attempt_count ?? 0) > 0 && (
              <span className="ml-auto text-[10px] text-neutral-500 font-sans">
                Attempt {(task.attempt_count ?? 0) + 1}{maxRetries != null ? ` of ${maxRetries + 1}` : ''}
              </span>
            )}
          </button>
          {historyExpanded && (
            <div className="px-2.5 pb-2.5 space-y-1.5">
              {task.history && task.history.length > 0 ? (
                task.history.map((entry, i) => (
                  <div key={i} className="flex gap-2 text-[10px]">
                    <span className="text-neutral-600 shrink-0 tabular-nums">
                      {new Date(entry.timestamp).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}
                    </span>
                    <span className="text-neutral-400 font-semibold shrink-0">{entry.action}</span>
                    <span className="text-neutral-500 truncate">
                      {entry.details ? Object.entries(entry.details).map(([k, v]) => `${k}: ${typeof v === 'object' ? JSON.stringify(v) : v}`).join(', ').slice(0, 120) : ''}
                    </span>
                  </div>
                ))
              ) : (
                <span className="text-neutral-600 text-[10px]">No history entries</span>
              )}
            </div>
          )}
        </div>
      )}

      <div className="flex items-center justify-between text-[11px] text-neutral-500 mt-auto pt-3 border-t border-neutral-800">
        <div className="flex items-center gap-1.5">
          <Clock className="w-3 h-3" />
          {new Date(task.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          <span className="text-neutral-400 font-mono">
            {task.status === 'QUEUED' ? 'Waiting' : duration}
          </span>
        </div>

        <div className="flex items-center gap-2">
          {isActive ? (
            <div className="flex items-center gap-1.5" title={heartbeat.text}>
              <Activity className="w-3 h-3" />
              <div className={`w-1.5 h-1.5 rounded-full ${heartbeat.color}`} />
            </div>
          ) : null}

          {task.status !== 'QUEUED' ? (
            <button
              onClick={() => onViewLogs(task)}
              className="flex items-center gap-1 text-neutral-400 hover:text-neutral-200 transition-colors"
              title="View logs"
            >
              <Terminal className="w-3 h-3" />
            </button>
          ) : null}
        </div>
      </div>

      {task.requires_approval && (task.status === 'QUEUED' || task.status === 'EVALUATING') ? (
        <div className="mt-4 flex flex-col gap-2">
          <button onClick={() => onApprove(task.id)} disabled={!!actionLoading} className={`w-full bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-400 border border-emerald-500/30 py-2.5 rounded-md text-sm font-semibold transition-colors flex items-center justify-center gap-2 ${actionLoading && actionLoading !== 'approve' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`}>
            {actionLoading === 'approve' ? <Activity className="w-4 h-4 animate-spin" /> : <CheckCircle className="w-4 h-4" />} Approve & Merge
          </button>
          {!showChangesForm ? (
            <div className="flex gap-2">
              <button onClick={() => setShowChangesForm(true)} className="flex-1 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border border-neutral-700 py-2 rounded-md text-xs font-medium transition-colors flex items-center justify-center gap-1.5">
                <RotateCcw className="w-3.5 h-3.5" /> Request Changes
              </button>
              <button onClick={() => handleDestructiveClick('cancel', () => onCancel(task.id))} disabled={!!actionLoading} className={`px-3 py-2 rounded-md text-xs font-medium transition-all duration-300 flex items-center justify-center gap-1 border ${confirmAction === 'cancel' ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'bg-neutral-800/50 hover:bg-neutral-700 text-neutral-500 border-neutral-700'}`}>
                {confirmAction === 'cancel' ? `Confirm (${confirmCountdown}s)` : 'Reject'}
              </button>
            </div>
          ) : (
            <div className="flex flex-col gap-2 bg-neutral-800/50 rounded-md p-2 border border-neutral-700">
              <textarea
                value={changesFeedback}
                onChange={e => setChangesFeedback(e.target.value)}
                placeholder="Describe what needs to change..."
                className="w-full bg-neutral-900 border border-neutral-700 rounded text-xs text-neutral-200 p-2 resize-none focus:outline-none focus:border-neutral-500 placeholder-neutral-600"
                rows={3}
              />
              <div className="flex gap-2">
                <button
                  onClick={() => { onRequestChanges(task.id, changesFeedback, 'fix'); setShowChangesForm(false); setChangesFeedback(''); }}
                  disabled={!changesFeedback.trim() || !!actionLoading}
                  className="flex-1 bg-amber-500/10 hover:bg-amber-500/20 text-amber-400 border border-amber-500/20 py-1.5 rounded text-xs font-medium transition-colors disabled:opacity-30 disabled:cursor-not-allowed flex items-center justify-center gap-1.5"
                >
                  {actionLoading === 'request-changes' ? <Activity className="w-3 h-3 animate-spin" /> : <RotateCcw className="w-3 h-3" />} Send Fix
                </button>
                <button
                  onClick={() => { onRequestChanges(task.id, changesFeedback || 'Re-review requested', 'review'); setShowChangesForm(false); setChangesFeedback(''); }}
                  disabled={!!actionLoading}
                  className="flex-1 bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/20 py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5"
                >
                  <Search className="w-3 h-3" /> Re-review
                </button>
                <button onClick={() => { setShowChangesForm(false); setChangesFeedback(''); }} className="px-2 py-1.5 text-neutral-500 hover:text-neutral-300 text-xs">
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      ) : null}

      {!task.requires_approval && isActive ? (
        <div className="mt-3 flex gap-2">
          <button onClick={() => handleDestructiveClick('cancel', () => onCancel(task.id))} disabled={!!actionLoading} className={`flex-1 py-1.5 rounded text-xs font-medium transition-all duration-300 flex items-center justify-center gap-1.5 border ${confirmAction === 'cancel' ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'} ${actionLoading ? 'cursor-not-allowed' : ''}`}>
            {actionLoading === 'cancel' ? <Activity className="w-3.5 h-3.5 animate-spin" /> : <Square className="w-3.5 h-3.5" />} {confirmAction === 'cancel' ? `Click again to cancel (${confirmCountdown}s)` : 'Cancel'}
          </button>
        </div>
      ) : null}

      {isTerminal ? (
        <div className="mt-3">
          <button onClick={() => handleDestructiveClick('archive', () => onArchive(task.id))} disabled={!!actionLoading} className={`w-full py-1.5 rounded text-xs font-medium transition-all duration-300 flex items-center justify-center gap-1.5 border ${confirmAction === 'archive' ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-500 border-neutral-700'} ${actionLoading && actionLoading !== 'archive' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`}>
            {actionLoading === 'archive' ? <Activity className="w-3.5 h-3.5 animate-spin" /> : <Archive className="w-3.5 h-3.5" />}
            {confirmAction === 'archive' ? `Click again to archive (${confirmCountdown}s)` : 'Archive'}
          </button>
        </div>
      ) : null}

      {task.status === 'FAILED' ? (
        <div className="mt-3 space-y-2">
          {editing ? (
            <div className="space-y-2">
              <textarea
                value={editPrompt}
                onChange={e => setEditPrompt(e.target.value)}
                className={`w-full bg-neutral-950 text-neutral-100 rounded px-2 py-1.5 text-xs resize-y focus:outline-none focus:ring-1 focus:ring-blue-500/50 min-h-[120px] font-mono border ${editPrompt.trim() === '' ? 'border-red-500/70' : 'border-neutral-700'}`}
              />
              <div className="text-[10px] text-neutral-500">{editPrompt.length} characters</div>
              <div className="flex gap-2">
                <button onClick={() => { onEditPrompt(task.id, editPrompt); setEditing(false); }}
                  disabled={!!actionLoading || editPrompt.trim() === ''}
                  className={`flex-1 border py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5 ${editPrompt.trim() === '' ? 'bg-neutral-800 text-neutral-600 border-neutral-700 cursor-not-allowed' : 'bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border-blue-500/20'} ${actionLoading ? 'opacity-50 cursor-not-allowed' : ''}`}>
                  {actionLoading === 'edit' && <Activity className="w-3.5 h-3.5 animate-spin" />}Save
                </button>
                <button onClick={() => { setEditing(false); setEditPrompt(task.description || ''); }}
                  className="px-3 bg-neutral-800 hover:bg-neutral-700 text-neutral-400 border border-neutral-700 py-1.5 rounded text-xs transition-colors">
                  Cancel
                </button>
              </div>
            </div>
          ) : (
            <div className="flex gap-2">
              <button onClick={() => onRetry(task.id)} disabled={!!actionLoading} className={`flex-1 bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/20 py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5 ${actionLoading && actionLoading !== 'retry' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`}>
                {actionLoading === 'retry' ? <Activity className="w-3.5 h-3.5 animate-spin" /> : <RotateCcw className="w-3.5 h-3.5" />} Retry
              </button>
              <button onClick={() => setEditing(true)} disabled={!!actionLoading} className={`flex-1 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border border-neutral-700 py-1.5 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1.5 ${actionLoading ? 'opacity-50 pointer-events-none' : ''}`}>
                <Pencil className="w-3.5 h-3.5" /> Edit
              </button>
              <button onClick={() => handleDestructiveClick('dismiss', () => onDismiss(task.id))} disabled={!!actionLoading} className={`py-1.5 px-2.5 rounded text-xs transition-all duration-300 flex items-center justify-center gap-1 border ${confirmAction === 'dismiss' ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-500 border-neutral-700'} ${actionLoading && actionLoading !== 'dismiss' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`} title="Dismiss">
                {actionLoading === 'dismiss' ? <Activity className="w-3.5 h-3.5 animate-spin" /> : <X className="w-3.5 h-3.5" />}{confirmAction === 'dismiss' ? ` (${confirmCountdown}s)` : ''}
              </button>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
});
