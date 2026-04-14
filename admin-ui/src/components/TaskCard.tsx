/* eslint-disable react-refresh/only-export-components */
import React, { useState, useEffect, useRef, memo, forwardRef } from 'react';
import { CheckCircle, AlertTriangle, Clock, Hash, Activity, ExternalLink, Terminal, RotateCcw, Pencil, X, XCircle, RefreshCw, ChevronRight, ChevronDown, ChevronUp, Search, GitFork, Microscope, Shield, Archive, BookOpen, ShieldAlert, TestTube, Package, ArrowUpDown, Wrench, MoreHorizontal, Radio, Layers, Gauge, Compass, FileText } from 'lucide-react';
import { formatDurationMs, parseTimestamp } from '../utils/time';
import { authGet, API_BASE_URL } from '../api';

/** Format seconds as compact "30s", "5m", "1h" for HB age / queue wait labels. */
function formatCompact(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  if (m < 60) return `${m}m`;
  return `${Math.floor(m / 60)}h`;
}

export type { ResearchFinding } from '../types/task';
import type { ResearchFinding } from '../types/task';

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

export type { Task, TaskStatus, TaskType } from '../types/task';
import type { Task, TaskStatus, TaskType } from '../types/task';

export const TASK_TYPE_STYLES: Record<string, { bg: string; text: string; icon: typeof Search; label: string }> = {
  create: { bg: 'bg-amber-500/10 border-amber-500/20', text: 'text-amber-400', icon: Terminal, label: 'Create' },
  plan: { bg: 'bg-purple-500/10 border-purple-500/20', text: 'text-purple-400', icon: GitFork, label: 'Plan' },
  research: { bg: 'bg-cyan-500/10 border-cyan-500/20', text: 'text-cyan-400', icon: Microscope, label: 'Research' },
  review: { bg: 'bg-sky-500/10 border-sky-500/20', text: 'text-sky-400', icon: Search, label: 'Review' },
  fix: { bg: 'bg-orange-500/10 border-orange-500/20', text: 'text-orange-400', icon: RotateCcw, label: 'Fix' },
  refactor: { bg: 'bg-teal-500/10 border-teal-500/20', text: 'text-teal-400', icon: Wrench, label: 'Refactor' },
  docs: { bg: 'bg-indigo-500/10 border-indigo-500/20', text: 'text-indigo-400', icon: BookOpen, label: 'Docs' },
  security_audit: { bg: 'bg-red-500/10 border-red-500/20', text: 'text-red-400', icon: ShieldAlert, label: 'Security Audit' },
  qa: { bg: 'bg-green-500/10 border-green-500/20', text: 'text-green-400', icon: TestTube, label: 'QA' },
  deps: { bg: 'bg-blue-500/10 border-blue-500/20', text: 'text-blue-400', icon: Package, label: 'Deps' },
  migrate: { bg: 'bg-teal-500/10 border-teal-500/20', text: 'text-teal-400', icon: ArrowUpDown, label: 'Migrate' },
  investigate: { bg: 'bg-yellow-500/10 border-yellow-500/20', text: 'text-yellow-400', icon: Search, label: 'Investigate' },
  debug: { bg: 'bg-rose-500/10 border-rose-500/20', text: 'text-rose-400', icon: AlertTriangle, label: 'Debug' },
  smoketest: { bg: 'bg-lime-500/10 border-lime-500/20', text: 'text-lime-400', icon: Activity, label: 'Smoketest' },
  rollup: { bg: 'bg-violet-500/10 border-violet-500/20', text: 'text-violet-400', icon: Layers, label: 'Rollup' },
  perf: { bg: 'bg-pink-500/10 border-pink-500/20', text: 'text-pink-400', icon: Gauge, label: 'Perf' },
  strategic_review: { bg: 'bg-emerald-500/10 border-emerald-500/20', text: 'text-emerald-400', icon: Compass, label: 'Strategic Review' },
  design_doc_update: { bg: 'bg-slate-500/10 border-slate-500/20', text: 'text-slate-400', icon: FileText, label: 'Design Doc' },
};

function parsePhase(statusMessage: string): string {
  // Strip '[XXmb]' heap prefix if present
  const msg = statusMessage.replace(/^\[\d+(\.\d+)?MB\]\s*/i, '');
  if (/cloning/i.test(msg)) return 'Cloning repo';
  if (/running claude/i.test(msg)) return 'Working';
  const toolMatch = msg.match(/using tool:\s*(\S+)/i);
  if (toolMatch) return `Using ${toolMatch[1]}`;
  if (/pushing/i.test(msg)) return 'Pushing changes';
  if (/creating pull request/i.test(msg)) return 'Creating PR';
  if (/running test|running lint|running pre-commit/i.test(msg)) return 'Running checks';
  if (/rebasing/i.test(msg)) return 'Rebasing';
  if (/spawning/i.test(msg)) return 'Spawning subtasks';
  if (/addressing review/i.test(msg)) return 'Fixing review feedback';
  if (/strategic/i.test(msg)) return 'Strategic analysis';
  return msg.slice(0, 40).trim();
}

const FAILURE_CLASS_CONFIG: Record<string, { label: string; color: string; hint: string; suggestion: string; retryLabel: string }> = {
  CODE_ERROR: { label: 'Code Error', color: 'bg-red-500/20 text-red-400', hint: 'Application code error — review error logs for root cause', suggestion: 'Review error logs before retrying', retryLabel: 'Retry' },
  PROVISION_FAILURE: { label: 'Infra', color: 'bg-orange-500/20 text-orange-400', hint: 'VM startup failed — check zone health. Auto-retries on different zone', suggestion: 'VM startup failed — will use alternate zone on retry', retryLabel: 'Retry' },
  AGENT_TIMEOUT: { label: 'Timeout', color: 'bg-yellow-500/20 text-yellow-400', hint: 'Agent died or timed out — possible OOM or stuck execution', suggestion: 'Agent process died — check for OOM or stuck execution', retryLabel: 'Retry' },
  EXTERNAL_API: { label: 'API Error', color: 'bg-blue-500/20 text-blue-400', hint: 'External service failure — typically transient, auto-retries', suggestion: 'Transient service error — typically auto-retries', retryLabel: 'Retry' },
  RATE_LIMIT: { label: 'Rate Limit', color: 'bg-purple-500/20 text-purple-400', hint: 'API quota exhausted — auto-retries with different account', suggestion: 'API quota exhausted — auto-retries with different account', retryLabel: 'Retry' },
};

export { FAILURE_CLASS_CONFIG };

function HighlightText({ text, query }: { text: string; query?: string }) {
  if (!query || !text) return <>{text}</>;
  const idx = text.toLowerCase().indexOf(query.toLowerCase());
  if (idx === -1) return <>{text}</>;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="bg-amber-500/30 text-amber-200 rounded-sm px-0.5">{text.slice(idx, idx + query.length)}</mark>
      {text.slice(idx + query.length)}
    </>
  );
}

interface TaskCardProps {
  task: Task;
  maxRetries?: number;
  actionLoading?: string;
  compact?: boolean;
  selectionMode?: boolean;
  onToggleTaskSelection?: (id: string) => void;
  onCtrlClick?: (taskId: string) => void;
  colIdx?: number;
  cardIdx?: number;
  onNavigate?: (colIdx: number, cardIdx: number, direction: 'up' | 'down' | 'left' | 'right') => void;
  onApprove: (id: string) => void;
  onRequestChanges: (id: string, feedback: string, action: 'fix' | 'review') => void;
  onCancel: (id: string) => void;
  onRetry: (id: string) => void;
  onEditPrompt: (id: string, newPrompt: string) => void;
  onDismiss: (id: string) => void;
  onArchive: (id: string) => void;
  onViewLogs: (task: Task) => void;
  onTrack?: (task: Task) => void;
  isTracked?: boolean;
  blockingCount?: number;
  searchQuery?: string;
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

  const start = parseTimestamp(startIso).getTime();
  const end = endIso ? parseTimestamp(endIso).getTime() : now;
  return formatDurationMs(Math.max(0, end - start));
}

type DurationStats = Record<string, { p50_seconds: number; sample_count: number }>;
const _durationStatsCache: Record<string, { data: DurationStats; fetchedAt: number }> = {};
const DURATION_STATS_TTL_MS = 5 * 60 * 1000;

export function useDurationStats(projectId?: string): DurationStats {
  const cacheKey = projectId ?? 'global';
  const [stats, setStats] = useState<DurationStats>(() => {
    const cached = _durationStatsCache[cacheKey];
    return (cached && Date.now() - cached.fetchedAt < DURATION_STATS_TTL_MS) ? cached.data : {};
  });
  useEffect(() => {
    const cached = _durationStatsCache[cacheKey];
    if (cached && Date.now() - cached.fetchedAt < DURATION_STATS_TTL_MS) {
      setStats(cached.data);
      return;
    }
    const url = projectId
      ? `${API_BASE_URL}/api/admin/task-duration-stats?project_id=${projectId}`
      : `${API_BASE_URL}/api/admin/task-duration-stats`;
    authGet(url)
      .then(r => r.json())
      .then(data => {
        const d = (data?.stats ?? {}) as DurationStats;
        _durationStatsCache[cacheKey] = { data: d, fetchedAt: Date.now() };
        setStats(d);
      })
      .catch(() => {});
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cacheKey]);
  return stats;
}

export function getHeartbeatStatus(lastHeartbeat?: string) {
  if (!lastHeartbeat) return { color: 'bg-neutral-600', text: 'No heartbeat' };

  const diffMinutes = (new Date().getTime() - parseTimestamp(lastHeartbeat).getTime()) / 60000;
  if (diffMinutes < 2) return { color: 'bg-emerald-500', text: 'Healthy (< 2m)' };
  if (diffMinutes < 5) return { color: 'bg-amber-500', text: 'Delayed (2-5m)' };
  return { color: 'bg-red-500', text: 'Stale (> 5m)' };
}

export function TaskTypeBadge({ taskType, expanded }: { taskType?: TaskType; expanded?: boolean }) {
  if (!taskType) return null;
  const style = TASK_TYPE_STYLES[taskType] ?? { bg: 'bg-neutral-500/10 border-neutral-500/20', text: 'text-neutral-400', icon: Hash, label: taskType };
  const Icon = style.icon;
  return (
    <span
      className={`group/badge ${style.bg} ${style.text} border text-[10px] uppercase tracking-wider rounded font-semibold whitespace-nowrap inline-flex items-center gap-0 hover:gap-1 p-1 hover:px-2 hover:py-0.5 transition-all duration-150 ${expanded ? 'gap-1 px-2 py-0.5' : ''}`}
      title={style.label}
    >
      <Icon className="w-3 h-3 shrink-0" />
      <span className={`max-w-0 overflow-hidden group-hover/badge:max-w-[80px] transition-all duration-150 ${expanded ? 'max-w-[80px]' : ''}`}>
        {style.label}
      </span>
    </span>
  );
}

export function ResearchFindings({ findings }: { findings: ResearchFinding[] }) {
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
  const configs: Partial<Record<TaskStatus, { bg: string; text: string; icon: typeof Activity; label: string; spin?: boolean }>> = {
    PROVISIONING: { bg: 'bg-blue-500/10 border-blue-500/20', text: 'text-blue-400', icon: Activity, label: 'Provisioning…', spin: true },
    EVALUATING: { bg: 'bg-blue-500/10 border-blue-500/20', text: 'text-blue-400', icon: Clock, label: 'Evaluating' },
    CANCELLED: { bg: 'bg-neutral-500/10 border-neutral-500/20', text: 'text-neutral-500', icon: X, label: 'Cancelled' },
  };
  const cfg = configs[status];
  if (!cfg) return null;
  const Icon = cfg.icon;
  return (
    <span
      className={`group/status inline-flex items-center gap-0 hover:gap-1 ${cfg.bg} ${cfg.text} border text-[10px] uppercase tracking-wider p-1 hover:px-2 hover:py-0.5 rounded font-semibold whitespace-nowrap transition-all duration-150`}
      title={cfg.label}
    >
      <Icon className={`w-3 h-3 shrink-0 ${cfg.spin ? 'animate-spin' : ''}`} />
      <span className="max-w-0 overflow-hidden group-hover/status:max-w-[120px] transition-all duration-150">
        {cfg.label}
      </span>
    </span>
  );
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

export const TaskCard = memo(forwardRef<HTMLDivElement, TaskCardProps>(function TaskCard({ task, actionLoading, compact, blockingCount, selectionMode, onToggleTaskSelection, onCtrlClick, colIdx, cardIdx, onNavigate, onApprove, onRequestChanges, onCancel, onRetry, onEditPrompt, onDismiss, onArchive, onViewLogs, onTrack, isTracked, searchQuery }, ref) {
  const [editing, setEditing] = useState(false);
  const [editPrompt, setEditPrompt] = useState(task.description || '');
  const [descExpanded, setDescExpanded] = useState(false);
  const [statusExpanded, setStatusExpanded] = useState(false);
  const [findingsExpanded, setFindingsExpanded] = useState(false);
  const [confirmAction, setConfirmAction] = useState<'cancel' | 'dismiss' | 'archive' | null>(null);
  const [confirmCountdown, setConfirmCountdown] = useState(3);
  const [showChangesForm, setShowChangesForm] = useState(false);
  const [changesFeedback, setChangesFeedback] = useState('');
  const [showOverflowMenu, setShowOverflowMenu] = useState(false);
  const overflowMenuRef = useRef<HTMLDivElement>(null);
  const confirmTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const confirmIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    return () => {
      if (confirmTimeoutRef.current) clearTimeout(confirmTimeoutRef.current);
      if (confirmIntervalRef.current) clearInterval(confirmIntervalRef.current);
    };
  }, []);

  useEffect(() => {
    if (!showOverflowMenu) return;
    function handleClick(e: MouseEvent) {
      if (overflowMenuRef.current && !overflowMenuRef.current.contains(e.target as Node)) {
        setShowOverflowMenu(false);
      }
    }
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [showOverflowMenu]);

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
  const [now30, setNow30] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow30(Date.now()), 30000);
    return () => clearInterval(id);
  }, []);

  const isChild = !!task.parent_task_id;
  const heartbeat = getHeartbeatStatus(task.last_heartbeat);
  const isActive = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING';

  const hbAgeSec = (task.status === 'IN_PROGRESS' || task.status === 'EVALUATING') && task.last_heartbeat
    ? (now30 - parseTimestamp(task.last_heartbeat).getTime()) / 1000
    : null;
  const hbAgeText = hbAgeSec !== null ? `HB ${formatCompact(hbAgeSec)}` : null;
  const hbAgeColor = hbAgeSec === null ? '' : hbAgeSec < 60 ? 'text-emerald-400' : hbAgeSec < 300 ? 'text-amber-400' : 'text-red-400';

  const queueWaitSec = task.status === 'QUEUED'
    ? (now30 - parseTimestamp(task.created_at).getTime()) / 1000
    : null;
  const queueWaitText = queueWaitSec !== null ? `Queued ${formatCompact(queueWaitSec)}` : null;
  const isActiveForDuration = task.status === 'IN_PROGRESS' || task.status === 'PROVISIONING' || task.status === 'EVALUATING';
  const isTerminal = task.status === 'COMPLETED' || task.status === 'FAILED' || task.status === 'CANCELLED';
  const endTime = isTerminal && task.history && task.history.length > 0
    ? task.history[task.history.length - 1].timestamp
    : isTerminal ? task.created_at : undefined;
  const duration = useElapsedTime(task.created_at, isActiveForDuration ? undefined : endTime);

  const durationStats = useDurationStats(task.project_id);
  const taskType = task.task_type ?? ((task.payloads?.[0] as Record<string, unknown>)?.type as string) ?? 'create';
  const etaLabel = (() => {
    if (task.status !== 'IN_PROGRESS' && task.status !== 'PROVISIONING') return null;
    if (!task.started_at) return null;
    const typeStat = durationStats[taskType];
    if (!typeStat || typeStat.sample_count < 3) return null;
    const elapsedSec = (now30 - parseTimestamp(task.started_at).getTime()) / 1000;
    const remaining = typeStat.p50_seconds - elapsedSec;
    if (remaining > 0) return `~${Math.ceil(remaining / 60)}m left`;
    return 'longer than usual';
  })();

  const handleRootKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    if (e.target !== e.currentTarget) return;
    switch (e.key) {
      case 'Enter':
        e.preventDefault();
        onViewLogs(task);
        break;
      case ' ':
        e.preventDefault();
        if (selectionMode && onToggleTaskSelection) {
          onToggleTaskSelection(task.id);
        } else {
          onViewLogs(task);
        }
        break;
      case 'Escape':
        e.preventDefault();
        (e.currentTarget as HTMLElement).blur();
        break;
      case 'ArrowDown':
        e.preventDefault();
        if (colIdx !== undefined && cardIdx !== undefined) onNavigate?.(colIdx, cardIdx, 'down');
        break;
      case 'ArrowUp':
        e.preventDefault();
        if (colIdx !== undefined && cardIdx !== undefined) onNavigate?.(colIdx, cardIdx, 'up');
        break;
      case 'ArrowRight':
        e.preventDefault();
        if (colIdx !== undefined && cardIdx !== undefined) onNavigate?.(colIdx, cardIdx, 'right');
        break;
      case 'ArrowLeft':
        e.preventDefault();
        if (colIdx !== undefined && cardIdx !== undefined) onNavigate?.(colIdx, cardIdx, 'left');
        break;
    }
  };

  const taskTypeBorderColor: Record<string, string> = {
    create: 'border-l-amber-500',
    plan: 'border-l-purple-400',
    research: 'border-l-cyan-400',
    review: 'border-l-sky-400',
    fix: 'border-l-orange-400',
    refactor: 'border-l-teal-400',
    docs: 'border-l-indigo-400',
    security_audit: 'border-l-red-400',
    qa: 'border-l-green-400',
    deps: 'border-l-blue-400',
    migrate: 'border-l-teal-400',
    investigate: 'border-l-yellow-400',
    debug: 'border-l-rose-400',
    smoketest: 'border-l-lime-400',
    rollup: 'border-l-violet-400',
    perf: 'border-l-pink-400',
    strategic_review: 'border-l-emerald-400',
    design_doc_update: 'border-l-sky-400',
  };
  const borderColor = task.task_type ? (taskTypeBorderColor[task.task_type] ?? 'border-l-neutral-700') : 'border-l-neutral-700';

  return (
    <div
      ref={ref}
      tabIndex={0}
      role="article"
      onKeyDown={handleRootKeyDown}
      onClick={(e) => {
        if ((e.ctrlKey || e.metaKey) && onCtrlClick) {
          e.preventDefault();
          e.stopPropagation();
          onCtrlClick(task.id);
        }
      }}
      className={`relative group bg-neutral-900 rounded-lg border border-l-4 ${borderColor} transition-shadow outline-none focus-visible:ring-2 focus-visible:ring-amber-400/50 ${compact ? 'p-2' : 'px-3 py-2.5'} ${task.status === 'CANCELLED' ? 'opacity-60' : ''} ${isChild ? 'border-y-neutral-800 border-r-neutral-800 shadow-sm' : 'border-y-neutral-800 border-r-neutral-800 shadow-sm hover:shadow-md hover:border-y-neutral-700 hover:border-r-neutral-700'}`}
    >
      <div className="flex items-start gap-2 mb-1.5">
        <div className="flex-1 min-w-0">
          <h3 className={`font-medium text-sm ${task.status === 'CANCELLED' ? 'text-neutral-500 line-through' : 'text-neutral-100'}`}><HighlightText text={task.title} query={searchQuery} /></h3>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          {/* Hover quick-action toolbar */}
          <div className="flex items-center gap-0.5 opacity-0 group-hover:opacity-100 transition-opacity duration-150">
            {task.requires_approval && (task.status === 'QUEUED' || task.status === 'EVALUATING') && (
              <button
                title="Approve"
                onClick={(e) => { e.stopPropagation(); onApprove(task.id); }}
                disabled={!!actionLoading}
                className="w-6 h-6 flex items-center justify-center rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-400 hover:text-neutral-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <CheckCircle className="w-3.5 h-3.5" />
              </button>
            )}
            {(task.status === 'IN_PROGRESS' || task.status === 'QUEUED') && (
              <button
                title={confirmAction === 'cancel' ? `Confirm cancel (${confirmCountdown}s)` : 'Cancel'}
                onClick={(e) => { e.stopPropagation(); handleDestructiveClick('cancel', () => onCancel(task.id)); }}
                disabled={!!actionLoading}
                className={`w-6 h-6 flex items-center justify-center rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${confirmAction === 'cancel' ? 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-400 hover:text-neutral-100'}`}
              >
                <XCircle className="w-3.5 h-3.5" />
              </button>
            )}
            {task.status === 'FAILED' && (
              <button
                title="Retry"
                onClick={(e) => { e.stopPropagation(); onRetry(task.id); }}
                disabled={!!actionLoading}
                className="w-6 h-6 flex items-center justify-center rounded bg-neutral-800 hover:bg-neutral-700 text-neutral-400 hover:text-neutral-100 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
              >
                <RefreshCw className="w-3.5 h-3.5" />
              </button>
            )}
            {(task.status === 'COMPLETED' || task.status === 'CANCELLED' || task.status === 'FAILED') && (
              <button
                title={confirmAction === 'archive' ? `Confirm archive (${confirmCountdown}s)` : 'Archive'}
                onClick={(e) => { e.stopPropagation(); handleDestructiveClick('archive', () => onArchive(task.id)); }}
                disabled={!!actionLoading}
                className={`w-6 h-6 flex items-center justify-center rounded transition-colors disabled:opacity-50 disabled:cursor-not-allowed ${confirmAction === 'archive' ? 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-400 hover:text-neutral-100'}`}
              >
                <Archive className="w-3.5 h-3.5" />
              </button>
            )}
          </div>
          <StatusBadge status={task.status} />
          {isActive && task.last_heartbeat && (
            <div className="flex items-center gap-0.5" title={heartbeat.text}>
              <div className={`w-2.5 h-2.5 rounded-full shrink-0 ${heartbeat.color}`} />
              {hbAgeText && <span className={`text-[10px] font-mono ${hbAgeColor}`}>{hbAgeText}</span>}
            </div>
          )}
        </div>
      </div>
      <div className="flex items-center gap-1.5 flex-wrap mb-1.5">
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
        {(blockingCount ?? 0) > 0 && (task.status === 'FAILED' || task.status === 'CANCELLED') && (
          <span className="inline-flex items-center gap-1 text-[10px] font-medium bg-red-500/10 border border-red-500/30 text-red-400 px-1.5 py-0.5 rounded">
            <AlertTriangle className="w-3 h-3" />
            Blocking {blockingCount} task{blockingCount! > 1 ? 's' : ''}
          </span>
        )}
      </div>

      {isActive && heartbeat.text.startsWith('Stale') && (
        <div className="bg-amber-950/40 border border-amber-900/50 text-amber-400 rounded px-2 py-1 text-xs flex items-center gap-1.5 mb-2">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0" style={{ width: 14, height: 14 }} />
          No heartbeat for 5+ minutes — task may be stuck
        </div>
      )}

      {task.status === 'IN_PROGRESS' && task.latest_status_message && (
        <div className="flex items-center gap-1.5 mb-1.5">
          <span className="relative flex h-1.5 w-1.5 shrink-0">
            <span className="animate-ping absolute inline-flex h-full w-full rounded-full bg-neutral-400 opacity-75" />
            <span className="relative inline-flex rounded-full h-1.5 w-1.5 bg-neutral-500" />
          </span>
          <span className="text-[11px] text-neutral-400 truncate">{parsePhase(task.latest_status_message)}</span>
        </div>
      )}

      {!compact && (
        <div className="mb-2">
          <p className={`text-xs text-neutral-400 ${descExpanded ? '' : 'line-clamp-1'}`}><HighlightText text={task.description} query={searchQuery} /></p>
          {task.description.length > 80 && (
            <button
              onClick={(e) => { e.stopPropagation(); setDescExpanded(!descExpanded); }}
              className="text-xs text-gray-400 hover:text-gray-600 cursor-pointer flex items-center gap-0.5 mt-0.5"
            >
              {descExpanded ? <>Show less <ChevronUp className="w-3 h-3" /></> : <>Show more <ChevronDown className="w-3 h-3" /></>}
            </button>
          )}
        </div>
      )}

      {!compact && task.latest_status_message ? (
        <div className="bg-neutral-950 text-neutral-400 text-[11px] rounded-lg mb-2 border border-neutral-800/50 font-mono">
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
          className="flex items-center gap-1.5 bg-emerald-500/10 text-emerald-400 border border-emerald-500/20 text-xs px-3 py-1.5 rounded mb-2 hover:bg-emerald-500/20 transition-colors font-medium"
        >
          <ExternalLink className="w-3.5 h-3.5" />
          View Pull Request
        </a>
      ) : (task.task_type === 'review' || task.task_type === 'fix') && task.repo_url && task.payloads?.[0]?.pr_number && isSafeUrl(`${task.repo_url}/pull/${task.payloads[0].pr_number}`) ? (
        <a
          href={`${task.repo_url}/pull/${task.payloads[0].pr_number}`}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-neutral-400 hover:text-neutral-200 text-[11px] mb-2 transition-colors"
        >
          <ExternalLink className="w-3 h-3" />
          PR #{task.payloads[0].pr_number as number}
        </a>
      ) : null}

      {task.status === 'FAILED' && task.failure_class && FAILURE_CLASS_CONFIG[task.failure_class] ? (
        <div className="mb-2">
          <span
            className={`inline-block text-[10px] px-1.5 py-0.5 rounded font-semibold ${FAILURE_CLASS_CONFIG[task.failure_class].color}`}
            title={FAILURE_CLASS_CONFIG[task.failure_class].hint}
          >
            {FAILURE_CLASS_CONFIG[task.failure_class].label}
          </span>
          <p className="text-[10px] text-neutral-500 mt-0.5">{FAILURE_CLASS_CONFIG[task.failure_class].suggestion}</p>
          {(() => {
            const retryAfter = task.context?.retry_after as string | undefined;
            if (!retryAfter) return null;
            const retryTime = new Date(retryAfter).getTime();
            const isFuture = retryTime > now30;
            if (isFuture) {
              const mins = Math.ceil((retryTime - now30) / 60000);
              return <p className="text-[10px] text-emerald-400 mt-0.5">Auto-retrying in {mins}m</p>;
            }
            return <p className="text-[10px] text-amber-400 mt-0.5">Auto-retry pending</p>;
          })()}
        </div>
      ) : null}

      {task.status === 'FAILED' && task.error_logs ? (
        <div className="bg-red-950/30 text-red-400 text-[11px] p-2 rounded mb-2 border border-red-900/30 font-mono max-h-20 overflow-y-auto">
          <span className="font-semibold block mb-1 text-red-500 font-sans">Error:</span>
          {task.error_logs.slice(0, 500)}
        </div>
      ) : null}

      {task.research_findings ? (
        <div className="bg-cyan-950/20 text-cyan-300 text-[11px] p-2 rounded mb-2 border border-cyan-900/30 font-mono">
          <span
            onClick={(e) => { e.stopPropagation(); setFindingsExpanded(!findingsExpanded); }}
            className="flex items-center gap-1 text-cyan-400 font-sans cursor-pointer mb-1"
          >
            <ChevronRight className={`w-3 h-3 shrink-0 transition-transform ${findingsExpanded ? 'rotate-90' : ''}`} />
            {task.research_findings.length} findings
          </span>
          {findingsExpanded && (
            <div className="max-h-32 overflow-y-auto">
              <ResearchFindings findings={task.research_findings} />
            </div>
          )}
        </div>
      ) : null}

      {task.plan_output ? <PlanPreview planOutput={task.plan_output} /> : null}

      {task.depends_on && task.depends_on.length > 0 ? (
        <div className="text-[10px] text-neutral-500 mb-2 font-mono flex items-center gap-1">
          <GitFork className="w-3 h-3" />
          Waiting on {task.depends_on.length} task{task.depends_on.length > 1 ? 's' : ''}
        </div>
      ) : null}

      <div className="flex items-center justify-between text-[10px] text-neutral-500 mt-auto pt-2 border-t border-neutral-800">
        <div className="flex items-center gap-1.5">
          <Clock className="w-3 h-3" />
          {parseTimestamp(task.created_at).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })}
          <span className="text-neutral-400 font-mono">
            {queueWaitText ?? duration}
          </span>
          {etaLabel && (
            <span className={`font-mono ${etaLabel === 'longer than usual' ? 'text-amber-400' : 'text-neutral-400'}`}>
              · {etaLabel}
            </span>
          )}
        </div>

        <div className="flex items-center gap-2">
          {isActive ? (
            <div className="flex items-center gap-1.5" title={heartbeat.text}>
              <Activity className="w-3 h-3" />
              <div className={`w-1.5 h-1.5 rounded-full ${heartbeat.color}`} />
              {hbAgeText && <span className={`text-[10px] font-mono ${hbAgeColor}`}>{hbAgeText}</span>}
            </div>
          ) : null}

          {task.status !== 'QUEUED' && onTrack ? (
            <button
              onClick={(e) => { e.stopPropagation(); onTrack(task); }}
              className={`flex items-center gap-1 px-1.5 py-0.5 rounded transition-colors text-[10px] ${isTracked ? 'bg-blue-500/20 text-blue-400 hover:bg-blue-500/30' : 'text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800'}`}
              title={isTracked ? 'Tracking live' : 'Track task'}
            >
              <Radio className="w-3 h-3" />
              {isTracked ? 'Live' : 'Track'}
            </button>
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

      {task.requires_approval && !['FAILED', 'CANCELLED'].includes(task.status) ? (
        <div className={`flex flex-col gap-2 ${compact ? 'mt-2' : 'mt-4'}`}>
          <button onClick={() => onApprove(task.id)} disabled={!!actionLoading} className={`w-full bg-emerald-500/20 hover:bg-emerald-500/30 text-emerald-400 border border-emerald-500/30 rounded-md font-semibold transition-colors flex items-center justify-center gap-2 ${compact ? 'py-1.5 text-xs' : 'py-2.5 text-sm'} ${actionLoading && actionLoading !== 'approve' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`}>
            {actionLoading === 'approve' ? <Activity className={compact ? 'w-3.5 h-3.5 animate-spin' : 'w-4 h-4 animate-spin'} /> : <CheckCircle className={compact ? 'w-3.5 h-3.5' : 'w-4 h-4'} />} Approve & Merge
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

      {/* Cancel/Archive for active and terminal-non-failed tasks handled via hover toolbar + drawer */}

      {task.status === 'FAILED' ? (
        compact ? (
          editing ? (
            <div className="mt-2 space-y-2">
              <textarea
                value={editPrompt}
                onChange={e => setEditPrompt(e.target.value)}
                className={`w-full bg-neutral-950 text-neutral-100 rounded px-2 py-1.5 text-xs resize-y focus:outline-none focus:ring-1 focus:ring-blue-500/50 min-h-[80px] font-mono border ${editPrompt.trim() === '' ? 'border-red-500/70' : 'border-neutral-700'}`}
              />
              <div className="flex gap-1.5">
                <button onClick={() => { onEditPrompt(task.id, editPrompt); setEditing(false); }}
                  disabled={!!actionLoading || editPrompt.trim() === ''}
                  className={`flex-1 border py-1 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1 ${editPrompt.trim() === '' ? 'bg-neutral-800 text-neutral-600 border-neutral-700 cursor-not-allowed' : 'bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border-blue-500/20'} ${actionLoading ? 'opacity-50 cursor-not-allowed' : ''}`}>
                  {actionLoading === 'edit' && <Activity className="w-3 h-3 animate-spin" />}Save
                </button>
                <button onClick={() => { setEditing(false); setEditPrompt(task.description || ''); }}
                  className="px-2 bg-neutral-800 hover:bg-neutral-700 text-neutral-400 border border-neutral-700 py-1 rounded text-xs transition-colors">
                  Cancel
                </button>
              </div>
            </div>
          ) : (
          <div className="mt-2" ref={overflowMenuRef}>
            <button
              onClick={() => setShowOverflowMenu(v => !v)}
              className="p-1 rounded text-neutral-500 hover:text-neutral-300 hover:bg-neutral-800 transition-colors"
              title="More actions"
            >
              <MoreHorizontal className="w-3.5 h-3.5" />
            </button>
            {showOverflowMenu && (
              <div className="mt-1 flex gap-1.5">
                <button onClick={() => { if (task.failure_class === 'CODE_ERROR') { setEditing(true); setShowOverflowMenu(false); } else { onRetry(task.id); setShowOverflowMenu(false); } }} disabled={!!actionLoading} className={`flex-1 bg-blue-500/10 hover:bg-blue-500/20 text-blue-400 border border-blue-500/20 py-1 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1 ${actionLoading && actionLoading !== 'retry' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`}>
                  {actionLoading === 'retry' ? <Activity className="w-3 h-3 animate-spin" /> : <RotateCcw className="w-3 h-3" />} {task.failure_class && FAILURE_CLASS_CONFIG[task.failure_class] ? FAILURE_CLASS_CONFIG[task.failure_class].retryLabel : 'Retry'}
                </button>
                <button onClick={() => { setEditing(true); setShowOverflowMenu(false); }} disabled={!!actionLoading} className={`py-1 px-2 rounded text-xs font-medium transition-colors flex items-center justify-center gap-1 bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border border-neutral-700 ${actionLoading ? 'opacity-50 pointer-events-none cursor-not-allowed' : ''}`} title="Edit prompt">
                  <Pencil className="w-3 h-3" />
                </button>
                <button onClick={() => { handleDestructiveClick('dismiss', () => { onDismiss(task.id); setShowOverflowMenu(false); }); }} disabled={!!actionLoading} className={`py-1 px-2 rounded text-xs transition-all duration-300 flex items-center justify-center gap-1 border ${confirmAction === 'dismiss' ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-500 border-neutral-700'} ${actionLoading && actionLoading !== 'dismiss' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`} title="Dismiss">
                  {actionLoading === 'dismiss' ? <Activity className="w-3 h-3 animate-spin" /> : <X className="w-3 h-3" />}{confirmAction === 'dismiss' ? ` (${confirmCountdown}s)` : ''}
                </button>
                <button onClick={() => { handleDestructiveClick('archive', () => { onArchive(task.id); setShowOverflowMenu(false); }); }} disabled={!!actionLoading} className={`py-1 px-2 rounded text-xs transition-all duration-300 flex items-center justify-center gap-1 border ${confirmAction === 'archive' ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-500 border-neutral-700'} ${actionLoading && actionLoading !== 'archive' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`} title="Archive">
                  {actionLoading === 'archive' ? <Activity className="w-3 h-3 animate-spin" /> : <Archive className="w-3 h-3" />}{confirmAction === 'archive' ? ` (${confirmCountdown}s)` : ''}
                </button>
              </div>
            )}
          </div>
          )
        ) : (
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
              <>
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
                <button onClick={() => handleDestructiveClick('archive', () => onArchive(task.id))} disabled={!!actionLoading} className={`w-full py-1.5 rounded text-xs font-medium transition-all duration-300 flex items-center justify-center gap-1.5 border ${confirmAction === 'archive' ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-500 border-neutral-700'} ${actionLoading && actionLoading !== 'archive' ? 'opacity-50 pointer-events-none' : ''} ${actionLoading ? 'cursor-not-allowed' : ''}`}>
                  {actionLoading === 'archive' ? <Activity className="w-3.5 h-3.5 animate-spin" /> : <Archive className="w-3.5 h-3.5" />}
                  {confirmAction === 'archive' ? `Click again to archive (${confirmCountdown}s)` : 'Archive'}
                </button>
              </>
            )}
          </div>
        )
      ) : null}
    </div>
  );
}));
