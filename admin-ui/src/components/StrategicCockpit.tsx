/* eslint-disable react-refresh/only-export-components */
import { useMemo } from 'react';
import {
  Activity,
  AlertTriangle,
  Archive,
  CheckCircle,
  Clock,
  Compass,
  ExternalLink,
  FileText,
  GitFork,
  GitPullRequest,
  Layers,
  MessageSquare,
  Plus,
  Radio,
  Search,
  ShieldCheck,
  Terminal,
} from 'lucide-react';
import type { Task, TaskStatus } from '../types/task';
import type { Alert } from '../types/alert';
import type { Direction } from './DirectionsPanel';
import type { StrategicCycleCompletion, StrategicCycleProgress } from '../hooks/useLogViewer';
import { getTaskTypeLabel } from '../taskCapabilities';
import { parseTimestamp, timeAgo } from '../utils/time';
import { taskStatusBadgeClasses } from '../utils/taskStatus';

type IconComponent = typeof Activity;
type WorkstreamKind = 'direction' | 'task_type' | 'project' | 'unassigned';
type EvidenceTone = 'success' | 'info' | 'warning' | 'neutral';

export interface StrategicCockpitDirection {
  id: string;
  text: string;
  priority?: Direction['priority'] | string | null;
  status?: Direction['status'] | string | null;
  category?: string | null;
  last_researched_at?: string | null;
  held?: boolean | null;
}

export interface StrategicCockpitProps {
  tasks: Task[];
  directions?: StrategicCockpitDirection[];
  alerts?: Alert[];
  selectedProjectId?: string | null;
  projectName?: string | null;
  tasksLoading?: boolean;
  tasksError?: string | null;
  directionsLoading?: boolean;
  directionsError?: string | null;
  strategistActive?: boolean;
  cycleProgress?: StrategicCycleProgress | null;
  lastCycleCompletion?: StrategicCycleCompletion | null;
  onOpenComposer?: () => void;
  onOpenDirections?: () => void;
  onOpenResearch?: (directionId?: string | null) => void;
  onCreateTask?: () => void;
  onViewTask?: (task: Task) => void;
  onOpenTask?: (task: Task) => void;
  onDirectionClick?: (directionId: string) => void;
  className?: string;
}

export interface StrategicCockpitWorkstream {
  key: string;
  kind: WorkstreamKind;
  label: string;
  detail: string;
  directionId: string | null;
  priority: string | null;
  directionStatus: string | null;
  total: number;
  active: number;
  queued: number;
  judgment: number;
  friction: number;
  completed: number;
  evidence: number;
  latestTask: Task | null;
  tasks: Task[];
}

export interface StrategicCockpitEvidenceItem {
  id: string;
  taskId: string;
  taskTitle: string;
  label: string;
  detail: string;
  href: string | null;
  tone: EvidenceTone;
  sortMs: number;
}

export interface StrategicCockpitModel {
  scopeLabel: string;
  counts: {
    total: number;
    active: number;
    queued: number;
    needsJudgment: number;
    friction: number;
    failed: number;
    blocked: number;
    parked: number;
    completed: number;
  };
  activeWork: Task[];
  queuedWork: Task[];
  judgmentQueue: Array<{ task: Task; reasons: string[] }>;
  frictionTasks: Task[];
  recentCompleted: Task[];
  workstreams: StrategicCockpitWorkstream[];
  evidence: StrategicCockpitEvidenceItem[];
  alertQueue: Alert[];
  directionSnapshot: {
    provided: boolean;
    total: number;
    active: number;
    proposed: number;
    staged: number;
    paused: number;
    held: number;
  };
}

const ACTIVE_STATUSES = new Set<TaskStatus>(['IN_PROGRESS', 'PROVISIONING', 'EVALUATING']);
const FRICTION_STATUSES = new Set<TaskStatus>(['FAILED', 'BLOCKED', 'PARKED']);
const TERMINAL_STATUSES = new Set<TaskStatus>(['COMPLETED', 'FAILED', 'CANCELLED']);

const STATUS_PRIORITY: Record<TaskStatus, number> = {
  BLOCKED: 0,
  PARKED: 1,
  FAILED: 2,
  EVALUATING: 3,
  IN_PROGRESS: 4,
  PROVISIONING: 5,
  QUEUED: 6,
  COMPLETED: 7,
  CANCELLED: 8,
};

const REASON_PRIORITY: Record<string, number> = {
  approval: 0,
  blocked: 1,
  parked: 2,
  evaluating: 3,
  'post-commit': 4,
  unverified: 5,
  'review changes': 6,
};

const ALERT_PRIORITY: Record<Alert['severity'], number> = {
  ERROR: 0,
  WARNING: 1,
  INFO: 2,
};

const DIRECTION_BACKLOG_LIMIT = 6;
const DIRECTION_STATUS_PRIORITY: Record<string, number> = {
  active: 0,
  staged: 1,
  proposed: 2,
  paused: 3,
  completed: 5,
  expired: 5,
};
const DIRECTION_PRIORITY: Record<string, number> = {
  high: 0,
  medium: 1,
  low: 2,
};

const statToneClasses: Record<EvidenceTone, { icon: string; value: string; border: string }> = {
  success: {
    icon: 'text-emerald-400',
    value: 'text-emerald-300',
    border: 'border-emerald-500/20 bg-emerald-500/10',
  },
  info: {
    icon: 'text-blue-400',
    value: 'text-blue-300',
    border: 'border-blue-500/20 bg-blue-500/10',
  },
  warning: {
    icon: 'text-amber-400',
    value: 'text-amber-300',
    border: 'border-amber-500/20 bg-amber-500/10',
  },
  neutral: {
    icon: 'text-neutral-400',
    value: 'text-neutral-200',
    border: 'border-neutral-800/70 bg-neutral-900/50',
  },
};

const evidenceToneClasses: Record<EvidenceTone, string> = {
  success: 'bg-emerald-500/10 border-emerald-500/20 text-emerald-300',
  info: 'bg-blue-500/10 border-blue-500/20 text-blue-300',
  warning: 'bg-amber-500/10 border-amber-500/20 text-amber-300',
  neutral: 'bg-neutral-800/70 border-neutral-700/60 text-neutral-300',
};

function asRecord(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  return value as Record<string, unknown>;
}

function readString(value: unknown): string | null {
  return typeof value === 'string' && value.trim().length > 0 ? value.trim() : null;
}

function readStringField(value: unknown, keys: readonly string[]): string | null {
  const record = asRecord(value);
  if (!record) return null;
  for (const key of keys) {
    const result = readString(record[key]);
    if (result) return result;
  }
  return null;
}

function safeTimestampMs(value: string | number | Date | null | undefined): number {
  const ms = parseTimestamp(value).getTime();
  return Number.isFinite(ms) ? ms : 0;
}

function latestTaskTimestampMs(task: Task): number {
  const values: Array<string | number | Date | null | undefined> = [
    task.created_at,
    task.started_at,
    task.last_heartbeat,
    task.blocked_at,
    task.parked_at,
    task.last_tool_use_at,
    task.api_call_summary?.last_updated_at,
  ];

  for (const entry of task.status_log ?? []) values.push(entry.t);
  for (const entry of task.history ?? []) values.push(entry.timestamp);
  for (const artifact of task.artifacts ?? []) values.push(artifact.created_at);

  return values.reduce<number>((max, value) => Math.max(max, safeTimestampMs(value)), 0);
}

function compareTasksByRecency(a: Task, b: Task): number {
  const diff = latestTaskTimestampMs(b) - latestTaskTimestampMs(a);
  return diff !== 0 ? diff : a.id.localeCompare(b.id);
}

function compareJudgmentItems(
  a: { task: Task; reasons: string[] },
  b: { task: Task; reasons: string[] },
): number {
  const aReason = Math.min(...a.reasons.map(reason => REASON_PRIORITY[reason] ?? 99));
  const bReason = Math.min(...b.reasons.map(reason => REASON_PRIORITY[reason] ?? 99));
  if (aReason !== bReason) return aReason - bReason;
  return compareTasksByRecency(a.task, b.task);
}

function compareFrictionTasks(a: Task, b: Task): number {
  const aPriority = STATUS_PRIORITY[a.status] ?? 99;
  const bPriority = STATUS_PRIORITY[b.status] ?? 99;
  if (aPriority !== bPriority) return aPriority - bPriority;
  return compareTasksByRecency(a, b);
}

function shortId(value: string): string {
  return value.length > 10 ? value.slice(0, 8) : value;
}

function formatTaskType(taskType: string | undefined): string {
  return taskType ? getTaskTypeLabel(taskType) : 'Task';
}

function humanizeToken(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, char => char.toUpperCase());
}

function safeHref(value: string | null | undefined): string | null {
  if (!value) return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? value : null;
  } catch {
    return null;
  }
}

function formatCycleSummary(completion?: StrategicCycleCompletion | null): string {
  if (!completion) return 'Strategic ready';
  if (completion.failure_reason) return completion.failure_reason;

  const parts = [
    completion.created_count ? `+${completion.created_count} directions` : null,
    completion.updated_count ? `${completion.updated_count} updated` : null,
    completion.staged_count ? `${completion.staged_count} staged` : null,
    completion.retired_count ? `${completion.retired_count} retired` : null,
    completion.tasks_spawned ? `${completion.tasks_spawned} tasks spawned` : null,
    completion.problem_model_changed ? 'problem model updated' : null,
    completion.running_update_changed ? 'running update written' : null,
  ].filter(Boolean);

  return completion.status_message ?? (parts.length > 0 ? parts.join(' / ') : 'Cycle complete');
}

export function extractTaskDirectionId(task: Task): string | null {
  const directionKeys = [
    'direction_id',
    'directionId',
    'source_direction_id',
    'sourceDirectionId',
    'target_direction_id',
    'targetDirectionId',
  ];
  const topLevel = readStringField(task as unknown, directionKeys);
  if (topLevel) return topLevel;

  const fromContext = readStringField(task.context, directionKeys);
  if (fromContext) return fromContext;

  const fromPlan = readStringField(task.plan_output, directionKeys);
  if (fromPlan) return fromPlan;

  for (const payload of task.payloads ?? []) {
    const fromPayload = readStringField(payload, directionKeys);
    if (fromPayload) return fromPayload;
  }

  const findingDirectionIds = new Set(
    (task.research_findings ?? [])
      .map(finding => readString(finding.direction_id))
      .filter((value): value is string => Boolean(value)),
  );
  return findingDirectionIds.size === 1 ? [...findingDirectionIds][0] : null;
}

export function getJudgmentReasons(task: Task): string[] {
  const reasons: string[] = [];
  const add = (reason: string) => {
    if (!reasons.includes(reason)) reasons.push(reason);
  };

  if (task.requires_approval && !TERMINAL_STATUSES.has(task.status)) add('approval');
  if (task.status === 'BLOCKED') add('blocked');
  if (task.status === 'PARKED') add('parked');
  if (task.status === 'EVALUATING') add('evaluating');
  if (task.post_commit_pending) add('post-commit');
  if (task.unverified_merge_reason) add('unverified');

  const verdict = `${task.evaluation_summary?.status ?? ''} ${task.evaluation_summary?.verdict ?? ''}`.toLowerCase();
  if (verdict.includes('request') || verdict.includes('changes')) add('review changes');

  return reasons;
}

function taskEvidenceCount(task: Task): number {
  return collectTaskEvidence(task).length;
}

function buildWorkstreamSeed(
  task: Task,
  directionLookup: Map<string, StrategicCockpitDirection>,
): Pick<StrategicCockpitWorkstream, 'key' | 'kind' | 'label' | 'detail' | 'directionId' | 'priority' | 'directionStatus'> {
  const directionId = extractTaskDirectionId(task);
  if (directionId) {
    const direction = directionLookup.get(directionId);
    return {
      key: `direction:${directionId}`,
      kind: 'direction',
      label: direction?.text ?? `Direction ${shortId(directionId)}`,
      detail: direction ? `${direction.priority} priority / ${direction.status}` : `Direction ${shortId(directionId)}`,
      directionId,
      priority: direction?.priority ?? null,
      directionStatus: direction?.status ?? null,
    };
  }

  if (task.task_type) {
    return {
      key: `task_type:${task.task_type}`,
      kind: 'task_type',
      label: formatTaskType(task.task_type),
      detail: 'Task type workstream',
      directionId: null,
      priority: null,
      directionStatus: null,
    };
  }

  if (task.project_id) {
    return {
      key: `project:${task.project_id}`,
      kind: 'project',
      label: `Project ${shortId(task.project_id)}`,
      detail: 'Project workstream',
      directionId: null,
      priority: null,
      directionStatus: null,
    };
  }

  return {
    key: 'unassigned',
    kind: 'unassigned',
    label: 'Unassigned Work',
    detail: 'No direction, type, or project metadata',
    directionId: null,
    priority: null,
    directionStatus: null,
  };
}

function buildWorkstreams(
  tasks: readonly Task[],
  directions: readonly StrategicCockpitDirection[],
): StrategicCockpitWorkstream[] {
  const directionLookup = new Map(directions.map(direction => [direction.id, direction]));
  const groups = new Map<string, StrategicCockpitWorkstream>();

  for (const direction of directions) {
    groups.set(`direction:${direction.id}`, {
      key: `direction:${direction.id}`,
      kind: 'direction',
      label: direction.text,
      detail: `${direction.priority ?? 'medium'} priority / ${direction.status ?? 'active'}`,
      directionId: direction.id,
      priority: direction.priority ?? null,
      directionStatus: direction.status ?? null,
      total: 0,
      active: 0,
      queued: 0,
      judgment: 0,
      friction: 0,
      completed: 0,
      evidence: 0,
      latestTask: null,
      tasks: [],
    });
  }

  for (const task of tasks) {
    const seed = buildWorkstreamSeed(task, directionLookup);
    const existing = groups.get(seed.key);
    const stream = existing ?? {
      ...seed,
      total: 0,
      active: 0,
      queued: 0,
      judgment: 0,
      friction: 0,
      completed: 0,
      evidence: 0,
      latestTask: null,
      tasks: [],
    };

    stream.total += 1;
    if (ACTIVE_STATUSES.has(task.status)) stream.active += 1;
    if (task.status === 'QUEUED') stream.queued += 1;
    if (getJudgmentReasons(task).length > 0) stream.judgment += 1;
    if (FRICTION_STATUSES.has(task.status)) stream.friction += 1;
    if (task.status === 'COMPLETED') stream.completed += 1;
    stream.evidence += taskEvidenceCount(task);
    stream.tasks.push(task);
    if (!stream.latestTask || compareTasksByRecency(task, stream.latestTask) < 0) {
      stream.latestTask = task;
    }
    groups.set(seed.key, stream);
  }

  return [...groups.values()]
    .map(stream => ({
      ...stream,
      tasks: [...stream.tasks].sort(compareTasksByRecency),
    }))
    .sort((a, b) => {
      const scoreA = a.active * 8 + a.judgment * 5 + a.friction * 4 + a.queued * 2 + a.evidence;
      const scoreB = b.active * 8 + b.judgment * 5 + b.friction * 4 + b.queued * 2 + b.evidence;
      if (scoreA !== scoreB) return scoreB - scoreA;
      const aLatest = a.latestTask ? latestTaskTimestampMs(a.latestTask) : 0;
      const bLatest = b.latestTask ? latestTaskTimestampMs(b.latestTask) : 0;
      if (aLatest !== bLatest) return bLatest - aLatest;
      return a.label.localeCompare(b.label);
    });
}

function directionStatusRank(status: string | null): number {
  return DIRECTION_STATUS_PRIORITY[(status ?? 'active').toLowerCase()] ?? 4;
}

function directionPriorityRank(priority: string | null): number {
  return DIRECTION_PRIORITY[(priority ?? 'medium').toLowerCase()] ?? 3;
}

function isActiveDirectionOnlyStream(stream: StrategicCockpitWorkstream): boolean {
  return stream.kind === 'direction'
    && stream.total === 0
    && directionStatusRank(stream.directionStatus) === DIRECTION_STATUS_PRIORITY.active;
}

function isTerminalDirectionOnlyStream(stream: StrategicCockpitWorkstream): boolean {
  return stream.kind === 'direction'
    && stream.total === 0
    && directionStatusRank(stream.directionStatus) === DIRECTION_STATUS_PRIORITY.completed;
}

function compareDirectionBacklogStreams(a: StrategicCockpitWorkstream, b: StrategicCockpitWorkstream): number {
  const statusDelta = directionStatusRank(a.directionStatus) - directionStatusRank(b.directionStatus);
  if (statusDelta !== 0) return statusDelta;
  const priorityDelta = directionPriorityRank(a.priority) - directionPriorityRank(b.priority);
  if (priorityDelta !== 0) return priorityDelta;
  return a.label.localeCompare(b.label);
}

function selectDirectionBacklogStreams(workstreams: readonly StrategicCockpitWorkstream[]): StrategicCockpitWorkstream[] {
  const candidates = workstreams
    .filter(stream => stream.kind === 'direction' && stream.total === 0 && !isTerminalDirectionOnlyStream(stream))
    .sort(compareDirectionBacklogStreams);
  const active = candidates.filter(isActiveDirectionOnlyStream);
  const remainder = candidates.filter(stream => !isActiveDirectionOnlyStream(stream));
  return [
    ...active,
    ...remainder.slice(0, Math.max(0, DIRECTION_BACKLOG_LIMIT - active.length)),
  ];
}

function collectTaskEvidence(task: Task): StrategicCockpitEvidenceItem[] {
  const items: StrategicCockpitEvidenceItem[] = [];
  const seen = new Set<string>();
  const sortMs = latestTaskTimestampMs(task);

  const push = (
    suffix: string,
    label: string,
    detail: string,
    tone: EvidenceTone,
    href: string | null = null,
  ) => {
    const id = `${task.id}:${suffix}`;
    if (seen.has(id)) return;
    seen.add(id);
    items.push({
      id,
      taskId: task.id,
      taskTitle: task.title,
      label,
      detail,
      href,
      tone,
      sortMs,
    });
  };

  if (task.pr_url) {
    push('task-pr', 'Pull request', task.pr_url.split('/').slice(-2).join('/'), 'success', safeHref(task.pr_url));
  }
  if (task.commit_hash) {
    push('task-commit', 'Commit', task.commit_hash.slice(0, 10), 'success');
  }
  if (task.evaluation_summary) {
    const summary = task.evaluation_summary;
    const detail = [summary.status, summary.verdict].filter(Boolean).join(' / ');
    push('evaluation', 'Evaluation', detail || 'Evaluation summary present', 'info');
  }
  if (task.context?.code_acceptance_verdict) {
    const verdict = task.context.code_acceptance_verdict;
    const label = verdict.proven ? 'Acceptance proven' : verdict.satisfied ? 'Acceptance satisfied' : 'Acceptance pending';
    const detail = [verdict.kind, verdict.reason].filter(Boolean).join(' / ');
    push('acceptance', label, detail || 'Acceptance verdict present', verdict.proven ? 'success' : 'warning');
  }
  if (task.research_findings && task.research_findings.length > 0) {
    const highPriority = task.research_findings.filter(finding => finding.priority === 'high').length;
    const detail = highPriority > 0
      ? `${task.research_findings.length} findings, ${highPriority} high priority`
      : `${task.research_findings.length} findings`;
    push('research-findings', 'Research findings', detail, 'info');
  }
  if (task.edges_considered && task.edges_considered.length > 0) {
    const handled = task.edges_considered.filter(edge => edge.handled).length;
    push('edges', 'Edge cases', `${handled}/${task.edges_considered.length} handled`, handled === task.edges_considered.length ? 'success' : 'warning');
  }

  for (const [index, artifact] of (task.artifacts ?? []).entries()) {
    const typeLabel = humanizeToken(artifact.artifact_type || 'Artifact');
    if (artifact.pr_url) {
      push(`artifact-${index}-pr`, typeLabel, artifact.pr_url.split('/').slice(-2).join('/'), 'success', safeHref(artifact.pr_url));
    } else if (artifact.commit_hash) {
      push(`artifact-${index}-commit`, typeLabel, artifact.commit_hash.slice(0, 10), 'success');
    } else if (artifact.storage_uri) {
      push(`artifact-${index}-storage`, typeLabel, artifact.storage_uri, 'neutral');
    } else if (artifact.logs) {
      push(`artifact-${index}-logs`, typeLabel, 'Logs captured', 'neutral');
    } else {
      push(`artifact-${index}`, typeLabel, `Iteration ${artifact.iteration}`, 'neutral');
    }
  }

  if (task.fix_loop_summary && task.fix_loop_summary.length > 0) {
    const nonSuccess = task.fix_loop_summary.filter(entry => entry.non_success).length;
    const iterations = task.fix_loop_summary.reduce((sum, entry) => sum + entry.iterations, 0);
    push('fix-loop', 'Fix loop', `${iterations} iterations, ${nonSuccess} non-success exits`, nonSuccess > 0 ? 'warning' : 'success');
  }

  return items;
}

function collectEvidence(tasks: readonly Task[]): StrategicCockpitEvidenceItem[] {
  return tasks
    .flatMap(collectTaskEvidence)
    .sort((a, b) => {
      if (a.sortMs !== b.sortMs) return b.sortMs - a.sortMs;
      return a.id.localeCompare(b.id);
    });
}

function deriveScopeLabel(tasks: readonly Task[], selectedProjectId?: string | null): string {
  if (selectedProjectId) return `Project ${shortId(selectedProjectId)}`;
  const projectIds = new Set(
    tasks
      .map(task => task.project_id)
      .filter((value): value is string => Boolean(value)),
  );
  if (projectIds.size === 1) return `Project ${shortId([...projectIds][0])}`;
  if (projectIds.size > 1) return `${projectIds.size} Projects`;
  return 'All Work';
}

export function deriveStrategicCockpitModel(
  tasks: readonly Task[],
  directions?: readonly StrategicCockpitDirection[],
  alerts?: readonly Alert[],
  options: { selectedProjectId?: string | null } = {},
): StrategicCockpitModel {
  const directionList = directions ?? [];
  const alertList = alerts ?? [];

  const activeWork = tasks.filter(task => ACTIVE_STATUSES.has(task.status)).sort(compareTasksByRecency);
  const queuedWork = tasks.filter(task => task.status === 'QUEUED').sort(compareTasksByRecency);
  const judgmentQueue = tasks
    .map(task => ({ task, reasons: getJudgmentReasons(task) }))
    .filter(item => item.reasons.length > 0)
    .sort(compareJudgmentItems);
  const frictionTasks = tasks.filter(task => FRICTION_STATUSES.has(task.status)).sort(compareFrictionTasks);
  const recentCompleted = tasks.filter(task => task.status === 'COMPLETED').sort(compareTasksByRecency);
  const visibleAlerts = alertList
    .filter(alert => !alert.dismissed_at)
    .sort((a, b) => {
      const severityDiff = ALERT_PRIORITY[a.severity] - ALERT_PRIORITY[b.severity];
      if (severityDiff !== 0) return severityDiff;
      const timeDiff = safeTimestampMs(b.created_at) - safeTimestampMs(a.created_at);
      return timeDiff !== 0 ? timeDiff : a.id.localeCompare(b.id);
    });

  const directionSnapshot = {
    provided: directions !== undefined,
    total: directionList.length,
    active: directionList.filter(direction => direction.status === 'active').length,
    proposed: directionList.filter(direction => direction.status === 'proposed').length,
    staged: directionList.filter(direction => direction.status === 'staged').length,
    paused: directionList.filter(direction => direction.status === 'paused').length,
    held: directionList.filter(direction => direction.held).length,
  };

  return {
    scopeLabel: deriveScopeLabel(tasks, options.selectedProjectId),
    counts: {
      total: tasks.length,
      active: activeWork.length,
      queued: queuedWork.length,
      needsJudgment: judgmentQueue.length,
      friction: frictionTasks.length,
      failed: tasks.filter(task => task.status === 'FAILED').length,
      blocked: tasks.filter(task => task.status === 'BLOCKED').length,
      parked: tasks.filter(task => task.status === 'PARKED').length,
      completed: recentCompleted.length,
    },
    activeWork,
    queuedWork,
    judgmentQueue,
    frictionTasks,
    recentCompleted,
    workstreams: buildWorkstreams(tasks, directionList),
    evidence: collectEvidence(tasks),
    alertQueue: visibleAlerts,
    directionSnapshot,
  };
}

function SectionHeader({ icon: Icon, title, meta }: { icon: IconComponent; title: string; meta?: string }) {
  return (
    <div className="flex min-w-0 items-center justify-between gap-3">
      <div className="flex items-center gap-2 min-w-0">
        <Icon className="h-4 w-4 shrink-0 text-neutral-400" />
        <h2 className="break-words text-sm font-semibold text-neutral-200">{title}</h2>
      </div>
      {meta && <span className="shrink-0 text-ui-md text-neutral-500 font-mono">{meta}</span>}
    </div>
  );
}

function StatCard({
  icon: Icon,
  label,
  value,
  detail,
  tone,
}: {
  icon: IconComponent;
  label: string;
  value: number | string;
  detail: string;
  tone: EvidenceTone;
}) {
  const toneClass = statToneClasses[tone];
  return (
    <div className={`min-w-0 rounded-lg border p-3 ${toneClass.border}`}>
      <div className="flex items-center justify-between gap-2">
        <span className="text-ui-md text-neutral-500">{label}</span>
        <Icon className={`h-4 w-4 shrink-0 ${toneClass.icon}`} />
      </div>
      <div className={`mt-2 font-mono text-2xl font-semibold ${toneClass.value}`}>{value}</div>
      <p className="mt-1 min-h-4 break-words text-ui-md text-neutral-500">{detail}</p>
    </div>
  );
}

function StatusPill({ status }: { status: TaskStatus }) {
  return (
    <span className={`shrink-0 rounded border px-1.5 py-0.5 text-ui-sm font-medium ${taskStatusBadgeClasses(status)}`}>
      {status.toLowerCase().replace(/_/g, ' ')}
    </span>
  );
}

function ReasonPills({ reasons }: { reasons: string[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {reasons.map(reason => (
        <span key={reason} className="rounded border border-amber-500/20 bg-amber-500/10 px-1.5 py-0.5 text-ui-sm font-medium text-amber-300">
          {reason}
        </span>
      ))}
    </div>
  );
}

function TaskMiniRow({
  task,
  onViewTask,
  reasons,
}: {
  task: Task;
  onViewTask?: (task: Task) => void;
  reasons?: string[];
}) {
  const body = (
    <>
      <div className="flex min-w-0 items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="break-words text-ui-md font-medium text-neutral-200">{task.title}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-1.5 text-ui-sm text-neutral-500">
            <span>{formatTaskType(task.task_type)}</span>
            <span className="font-mono">{shortId(task.id)}</span>
            <span>{timeAgo(task.created_at)}</span>
          </div>
        </div>
        <StatusPill status={task.status} />
      </div>
      {reasons && reasons.length > 0 && <div className="mt-2"><ReasonPills reasons={reasons} /></div>}
    </>
  );

  if (onViewTask) {
    return (
      <button
        type="button"
        onClick={() => onViewTask(task)}
        className="w-full min-w-0 min-h-11 rounded-md border border-neutral-800/70 bg-neutral-950/40 p-2 text-left transition-colors hover:border-neutral-700 hover:bg-neutral-900/80"
      >
        {body}
      </button>
    );
  }

  return (
    <div className="min-w-0 min-h-11 rounded-md border border-neutral-800/70 bg-neutral-950/40 p-2">
      {body}
    </div>
  );
}

function EmptyState({ children }: { children: string }) {
  return (
    <div className="rounded-md border border-dashed border-neutral-800 bg-neutral-950/30 px-3 py-4 text-center text-ui-md text-neutral-500">
      {children}
    </div>
  );
}

function WorkdayLane({
  icon,
  label,
  tasks,
  onViewTask,
  reasonsByTaskId,
}: {
  icon: IconComponent;
  label: string;
  tasks: Task[];
  onViewTask?: (task: Task) => void;
  reasonsByTaskId?: Map<string, string[]>;
}) {
  const Icon = icon;
  return (
    <div className="min-w-0 rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-3">
      <div className="mb-2 flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <Icon className="h-3.5 w-3.5 text-neutral-400" />
          <h3 className="text-ui-md font-semibold text-neutral-300">{label}</h3>
        </div>
        <span className="font-mono text-ui-md text-neutral-500">{tasks.length}</span>
      </div>
      <div className="space-y-2">
        {tasks.slice(0, 3).map(task => (
          <TaskMiniRow
            key={task.id}
            task={task}
            onViewTask={onViewTask}
            reasons={reasonsByTaskId?.get(task.id)}
          />
        ))}
        {tasks.length === 0 && <EmptyState>Nothing in this lane.</EmptyState>}
      </div>
    </div>
  );
}

function WorkstreamProgress({ stream }: { stream: StrategicCockpitWorkstream }) {
  const total = stream.active + stream.queued + stream.judgment + stream.friction + stream.completed;
  if (total === 0) return <div className="h-1.5 rounded-full bg-neutral-800" />;

  const segments = [
    { key: 'active', value: stream.active, className: 'bg-emerald-400' },
    { key: 'queued', value: stream.queued, className: 'bg-blue-400' },
    { key: 'judgment', value: stream.judgment, className: 'bg-amber-400' },
    { key: 'friction', value: stream.friction, className: 'bg-red-400' },
    { key: 'completed', value: stream.completed, className: 'bg-neutral-500' },
  ].filter(segment => segment.value > 0);

  return (
    <div className="flex h-1.5 overflow-hidden rounded-full bg-neutral-800">
      {segments.map(segment => (
        <span
          key={segment.key}
          className={segment.className}
          style={{ width: `${(segment.value / total) * 100}%` }}
        />
      ))}
    </div>
  );
}

function WorkstreamCard({
  stream,
  onDirectionClick,
}: {
  stream: StrategicCockpitWorkstream;
  onDirectionClick?: (directionId: string) => void;
}) {
  const clickable = Boolean(stream.directionId && onDirectionClick);
  const inner = (
    <>
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <Compass className="h-3.5 w-3.5 shrink-0 text-cyan-400" />
            <h3 className="min-w-0 break-words text-sm font-semibold text-neutral-100">{stream.label}</h3>
          </div>
          <p className="mt-1 break-words text-ui-md text-neutral-500">{stream.detail}</p>
        </div>
        <span className="shrink-0 rounded border border-neutral-700 bg-neutral-800/70 px-1.5 py-0.5 font-mono text-ui-sm text-neutral-300">
          {stream.total}
        </span>
      </div>
      <div className="mt-3">
        <WorkstreamProgress stream={stream} />
      </div>
      <div className="mt-3 grid grid-cols-5 gap-1 text-center">
        <MetricChip label="now" value={stream.active} tone="success" />
        <MetricChip label="queue" value={stream.queued} tone="info" />
        <MetricChip label="judge" value={stream.judgment} tone="warning" />
        <MetricChip label="risk" value={stream.friction} tone={stream.friction > 0 ? 'warning' : 'neutral'} />
        <MetricChip label="proof" value={stream.evidence} tone={stream.evidence > 0 ? 'success' : 'neutral'} />
      </div>
      <div className="mt-3 rounded-md bg-neutral-950/40 p-2">
        <p className="text-ui-sm text-neutral-500">Focus</p>
        <p className="mt-0.5 break-words text-ui-md text-neutral-300">
          {stream.latestTask?.title ?? 'No task activity in scope'}
        </p>
      </div>
    </>
  );

  if (clickable && stream.directionId) {
    return (
      <button
        type="button"
        onClick={() => onDirectionClick?.(stream.directionId as string)}
        className="w-full min-w-0 min-h-11 overflow-hidden rounded-lg border border-neutral-800/70 bg-neutral-900/50 p-3 text-left transition-colors hover:border-neutral-700 hover:bg-neutral-900"
      >
        {inner}
      </button>
    );
  }

  return (
    <div className="w-full min-w-0 overflow-hidden rounded-lg border border-neutral-800/70 bg-neutral-900/50 p-3">
      {inner}
    </div>
  );
}

function MetricChip({ label, value, tone }: { label: string; value: number | string; tone: EvidenceTone }) {
  const cls = evidenceToneClasses[tone];
  return (
    <div className={`min-w-0 rounded border px-1 py-1 ${cls}`}>
      <div className="font-mono text-ui-md font-semibold">{value}</div>
      <div className="text-ui-sm opacity-80">{label}</div>
    </div>
  );
}

function JudgmentQueue({
  items,
  onViewTask,
}: {
  items: StrategicCockpitModel['judgmentQueue'];
  onViewTask?: (task: Task) => void;
}) {
  return (
    <section className="rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-3">
      <SectionHeader icon={Search} title="Judgment Queue" meta={`${items.length}`} />
      <div className="mt-3 space-y-2">
        {items.slice(0, 6).map(item => (
          <TaskMiniRow key={item.task.id} task={item.task} onViewTask={onViewTask} reasons={item.reasons} />
        ))}
        {items.length === 0 && <EmptyState>No tasks currently require operator judgment.</EmptyState>}
      </div>
    </section>
  );
}

function EvidenceBundle({ items }: { items: StrategicCockpitEvidenceItem[] }) {
  return (
    <section className="rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-3">
      <SectionHeader icon={ShieldCheck} title="Evidence Bundle" meta={`${items.length}`} />
      <div className="mt-3 space-y-2">
        {items.slice(0, 8).map(item => (
          <div key={item.id} className="rounded-md border border-neutral-800/70 bg-neutral-950/40 p-2">
            <div className="flex items-start justify-between gap-2">
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className={`rounded border px-1.5 py-0.5 text-ui-sm font-medium ${evidenceToneClasses[item.tone]}`}>
                    {item.label}
                  </span>
                  <span className="font-mono text-ui-sm text-neutral-500">{shortId(item.taskId)}</span>
                </div>
                <p className="mt-1 break-words text-ui-md text-neutral-200">{item.taskTitle}</p>
                <p className="mt-0.5 break-words text-ui-md text-neutral-500">{item.detail}</p>
              </div>
              {item.href && (
                <a
                  href={item.href}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="flex min-h-11 min-w-11 shrink-0 items-center justify-center rounded-md text-neutral-400 transition-colors hover:bg-neutral-800 hover:text-neutral-100"
                  aria-label={`Open ${item.label} evidence for ${item.taskTitle}`}
                  title={`Open ${item.label} evidence for ${item.taskTitle}`}
                >
                  <ExternalLink className="h-3.5 w-3.5" />
                </a>
              )}
            </div>
          </div>
        ))}
        {items.length === 0 && <EmptyState>No evidence in this view.</EmptyState>}
      </div>
    </section>
  );
}

function FrictionWatch({
  tasks,
  counts,
  onViewTask,
}: {
  tasks: Task[];
  counts: StrategicCockpitModel['counts'];
  onViewTask?: (task: Task) => void;
}) {
  return (
    <section className="rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-3">
      <SectionHeader icon={AlertTriangle} title="Friction Watch" meta={`${counts.friction}`} />
      <div className="mt-3 grid grid-cols-3 gap-2">
        <MetricChip label="failed" value={counts.failed} tone={counts.failed > 0 ? 'warning' : 'neutral'} />
        <MetricChip label="blocked" value={counts.blocked} tone={counts.blocked > 0 ? 'warning' : 'neutral'} />
        <MetricChip label="parked" value={counts.parked} tone={counts.parked > 0 ? 'warning' : 'neutral'} />
      </div>
      <div className="mt-3 space-y-2">
        {tasks.slice(0, 5).map(task => (
          <TaskMiniRow key={task.id} task={task} onViewTask={onViewTask} />
        ))}
        {tasks.length === 0 && <EmptyState>No blocked, parked, or failed tasks in scope.</EmptyState>}
      </div>
    </section>
  );
}

function StrategicPanel({
  model,
  directionsLoading,
  directionsError,
  onOpenComposer,
  onOpenDirections,
  onOpenResearch,
}: {
  model: StrategicCockpitModel;
  directionsLoading?: boolean;
  directionsError?: string | null;
  onOpenComposer?: () => void;
  onOpenDirections?: () => void;
  onOpenResearch?: (directionId?: string | null) => void;
}) {
  const snapshot = model.directionSnapshot;
  const directionCountValue = directionsError ? '!' : directionsLoading ? '...' : snapshot.total;
  const directionCountTone: EvidenceTone = directionsError
    ? 'warning'
    : snapshot.provided
      ? 'info'
      : 'neutral';
  return (
    <section className="rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-3">
      <SectionHeader icon={FileText} title="Strategic Thread" meta={model.alertQueue.length > 0 ? `${model.alertQueue.length} alerts` : undefined} />
      <div className="mt-3 space-y-2">
        {model.alertQueue.slice(0, 3).map(alert => (
          <div key={alert.id} className={`rounded-md border p-2 ${alert.severity === 'ERROR' ? 'border-red-500/20 bg-red-500/10' : alert.severity === 'WARNING' ? 'border-amber-500/20 bg-amber-500/10' : 'border-blue-500/20 bg-blue-500/10'}`}>
            <div className="flex items-center justify-between gap-2">
              <span className="text-ui-sm font-semibold text-neutral-300">{alert.severity.toLowerCase()}</span>
              <span className="font-mono text-ui-sm text-neutral-500">{timeAgo(alert.created_at)}</span>
            </div>
            <p className="mt-1 break-words text-ui-md text-neutral-200">{alert.title}</p>
            <p className="mt-0.5 break-words text-ui-md text-neutral-500">{alert.message}</p>
          </div>
        ))}
        {model.alertQueue.length === 0 && <EmptyState>No active strategic alerts.</EmptyState>}
      </div>

      <div className="mt-4 rounded-lg border border-neutral-800/70 bg-neutral-950/40 p-3">
        <div className="mb-2 flex items-center gap-2">
          <Terminal className="h-3.5 w-3.5 text-neutral-400" />
          <h3 className="text-ui-md font-semibold text-neutral-300">Steer Panel</h3>
        </div>
        <p className="mb-3 text-ui-md text-neutral-500">
          Open the Strategic thread to talk through intent, scope, reviews, and next moves.
        </p>
        <div className="grid grid-cols-2 gap-2">
          <MetricChip label="directions" value={directionCountValue} tone={directionCountTone} />
          <MetricChip label="active" value={snapshot.active} tone="success" />
          <MetricChip label="proposed" value={snapshot.proposed + snapshot.staged} tone={snapshot.proposed + snapshot.staged > 0 ? 'warning' : 'neutral'} />
          <MetricChip label="held" value={snapshot.held + snapshot.paused} tone={snapshot.held + snapshot.paused > 0 ? 'warning' : 'neutral'} />
        </div>
        <div className="mt-3 grid grid-cols-3 gap-2">
          <button
            type="button"
            onClick={onOpenComposer}
            disabled={!onOpenComposer}
            className="min-h-11 rounded-md border border-neutral-700 bg-neutral-900/70 px-2 text-ui-sm text-neutral-300 transition-colors hover:border-neutral-500 disabled:border-neutral-800 disabled:text-neutral-600"
          >
            Thread
          </button>
          <button
            type="button"
            onClick={onOpenDirections}
            disabled={!onOpenDirections}
            className="min-h-11 rounded-md border border-neutral-700 bg-neutral-900/70 px-2 text-ui-sm text-neutral-300 transition-colors hover:border-neutral-500 disabled:border-neutral-800 disabled:text-neutral-600"
          >
            Directions
          </button>
          <button
            type="button"
            onClick={() => onOpenResearch?.(null)}
            disabled={!onOpenResearch}
            className="min-h-11 rounded-md border border-neutral-700 bg-neutral-900/70 px-2 text-ui-sm text-neutral-300 transition-colors hover:border-neutral-500 disabled:border-neutral-800 disabled:text-neutral-600"
          >
            Research
          </button>
        </div>
      </div>
    </section>
  );
}

export function StrategicCockpit({
  tasks,
  directions,
  alerts,
  selectedProjectId,
  projectName,
  tasksLoading = false,
  tasksError = null,
  directionsLoading = false,
  directionsError = null,
  strategistActive = false,
  cycleProgress = null,
  lastCycleCompletion = null,
  onOpenComposer,
  onOpenDirections,
  onOpenResearch,
  onCreateTask,
  onViewTask,
  onOpenTask,
  onDirectionClick,
  className = '',
}: StrategicCockpitProps) {
  const model = useMemo(
    () => deriveStrategicCockpitModel(tasks, directions, alerts, { selectedProjectId }),
    [tasks, directions, alerts, selectedProjectId],
  );
  const reasonsByTaskId = useMemo(
    () => new Map(model.judgmentQueue.map(item => [item.task.id, item.reasons])),
    [model.judgmentQueue],
  );
  const strategicStatus = strategistActive
    ? `Strategic working${cycleProgress?.phase_label ? `: ${cycleProgress.phase_label}` : ''}`
    : formatCycleSummary(lastCycleCompletion);
  const handleViewTask = onOpenTask ?? onViewTask;
  const handleDirectionClick = onDirectionClick ?? ((directionId: string) => onOpenResearch?.(directionId));
  const activeMissionStreams = model.workstreams.filter(stream => stream.total > 0).slice(0, 6);
  const directionOnlyStreams = selectDirectionBacklogStreams(model.workstreams);
  const directionBacklogTotal = model.workstreams.filter(
    stream => stream.kind === 'direction' && stream.total === 0 && !isTerminalDirectionOnlyStream(stream),
  ).length;
  const dataStatus = tasksError || directionsError
    ? 'Degraded snapshot'
    : tasksLoading || directionsLoading
      ? 'Refreshing snapshot'
      : 'Broad task snapshot';

  return (
    <div className={`flex-1 min-w-0 w-full overflow-auto bg-neutral-950 text-neutral-100 custom-scrollbar ${className}`} data-testid="strategic-cockpit">
      <div className="min-w-0 space-y-4 p-3 sm:p-4">
        <header className="min-w-0 rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-3">
          <div className="flex flex-col gap-3 lg:flex-row lg:items-start lg:justify-between">
            <div className="min-w-0">
              <div className="flex items-center gap-2">
                <Compass className="h-5 w-5 text-cyan-400" />
                <h1 className="text-lg font-semibold text-neutral-50">Strategic Cockpit</h1>
              </div>
              <p className="mt-1 max-w-3xl break-words text-sm text-neutral-400">
                {strategicStatus}
              </p>
            </div>
            <div className="flex min-w-0 flex-wrap items-center gap-2">
              {projectName && (
                <span className="rounded-md border border-neutral-800 bg-neutral-950/60 px-2 py-1 text-ui-md text-neutral-300">
                  {projectName}
                </span>
              )}
              <span
                className={`rounded-md border px-2 py-1 text-ui-md ${
                  tasksError || directionsError
                    ? 'border-amber-500/30 bg-amber-500/10 text-amber-300'
                    : 'border-neutral-800 bg-neutral-950/60 text-neutral-400'
                }`}
                title={[tasksError, directionsError].filter(Boolean).join(' / ') || 'Cockpit uses a 500-task project snapshot plus live directions.'}
              >
                {dataStatus}
              </span>
              <span className="rounded-md border border-neutral-800 bg-neutral-950/60 px-2 py-1 font-mono text-ui-md text-neutral-400">
                {model.scopeLabel}
              </span>
              <span className="rounded-md border border-neutral-800 bg-neutral-950/60 px-2 py-1 font-mono text-ui-md text-neutral-400">
                {model.counts.total} tasks
              </span>
              {onOpenComposer && (
                <button
                  type="button"
                  onClick={onOpenComposer}
                  className="inline-flex min-h-11 items-center gap-1.5 rounded-md bg-cyan-400 px-3 py-2 text-sm font-semibold text-neutral-950 transition-colors hover:bg-cyan-300"
                >
                  <MessageSquare className="h-4 w-4" />
                  Open Strategic
                </button>
              )}
              {onOpenDirections && (
                <button
                  type="button"
                  onClick={onOpenDirections}
                  className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-neutral-700 bg-neutral-950/60 px-3 py-2 text-sm font-medium text-neutral-200 transition-colors hover:border-neutral-500"
                >
                  <Compass className="h-4 w-4" />
                  Directions
                </button>
              )}
              {onOpenResearch && (
                <button
                  type="button"
                  onClick={() => onOpenResearch(null)}
                  className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-neutral-700 bg-neutral-950/60 px-3 py-2 text-sm font-medium text-neutral-200 transition-colors hover:border-neutral-500"
                >
                  <Search className="h-4 w-4" />
                  Research
                </button>
              )}
              {onCreateTask && (
                <button
                  type="button"
                  onClick={onCreateTask}
                  className="inline-flex min-h-11 items-center gap-1.5 rounded-md border border-neutral-700 bg-neutral-950/60 px-3 py-2 text-sm font-medium text-neutral-200 transition-colors hover:border-neutral-500"
                >
                  <Plus className="h-4 w-4" />
                  Task
                </button>
              )}
            </div>
          </div>
        </header>

        <section className="grid min-w-0 grid-cols-2 gap-2 lg:grid-cols-4">
          <StatCard
            icon={Radio}
            label="Active Work"
            value={model.counts.active}
            detail={`${model.counts.queued} queued behind it`}
            tone={model.counts.active > 0 ? 'success' : 'neutral'}
          />
          <StatCard
            icon={Search}
            label="Needs Judgment"
            value={model.counts.needsJudgment}
            detail="Approvals, reviews, blockers"
            tone={model.counts.needsJudgment > 0 ? 'warning' : 'neutral'}
          />
          <StatCard
            icon={AlertTriangle}
            label="Friction"
            value={model.counts.friction}
            detail={`${model.counts.failed} failed / ${model.counts.blocked} blocked / ${model.counts.parked} parked`}
            tone={model.counts.friction > 0 ? 'warning' : 'neutral'}
          />
          <StatCard
            icon={CheckCircle}
            label="Completed"
            value={model.counts.completed}
            detail={`${model.evidence.length} evidence items`}
            tone={model.counts.completed > 0 ? 'info' : 'neutral'}
          />
        </section>

        <div className="grid min-w-0 gap-4 xl:grid-cols-3">
          <main className="min-w-0 space-y-4 xl:col-span-2">
            <section className="min-w-0 rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-3">
              <SectionHeader icon={GitFork} title="Mission Cards" meta={`${model.workstreams.length}`} />
              <div className="mt-3 grid min-w-0 gap-3 lg:grid-cols-2">
                {activeMissionStreams.map(stream => (
                  <WorkstreamCard key={stream.key} stream={stream} onDirectionClick={handleDirectionClick} />
                ))}
                {activeMissionStreams.length === 0 && directionOnlyStreams.length === 0 && <EmptyState>No workstreams in scope.</EmptyState>}
              </div>
              {directionOnlyStreams.length > 0 && (
                <div className="mt-4 border-t border-neutral-800/70 pt-3">
                  <SectionHeader icon={Compass} title="Direction Backlog" meta={`${directionOnlyStreams.length}/${directionBacklogTotal}`} />
                  <div className="mt-3 grid min-w-0 gap-3 lg:grid-cols-2">
                    {directionOnlyStreams.map(stream => (
                      <WorkstreamCard key={stream.key} stream={stream} onDirectionClick={handleDirectionClick} />
                    ))}
                  </div>
                </div>
              )}
            </section>

            <section className="min-w-0 rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-3">
              <SectionHeader icon={Clock} title="Workday Flow" />
              <div className="mt-3 grid min-w-0 gap-3 lg:grid-cols-2 2xl:grid-cols-4">
                <WorkdayLane icon={Activity} label="Now" tasks={model.activeWork} onViewTask={handleViewTask} />
                <WorkdayLane icon={Layers} label="Next" tasks={model.queuedWork} onViewTask={handleViewTask} />
                <WorkdayLane icon={Search} label="Waiting" tasks={model.judgmentQueue.map(item => item.task)} onViewTask={handleViewTask} reasonsByTaskId={reasonsByTaskId} />
                <WorkdayLane icon={Archive} label="Done" tasks={model.recentCompleted} onViewTask={handleViewTask} />
              </div>
            </section>

            <JudgmentQueue items={model.judgmentQueue} onViewTask={handleViewTask} />
            <EvidenceBundle items={model.evidence} />
          </main>

          <aside className="min-w-0 space-y-4">
            <FrictionWatch tasks={model.frictionTasks} counts={model.counts} onViewTask={handleViewTask} />
            <StrategicPanel
              model={model}
              directionsLoading={directionsLoading}
              directionsError={directionsError}
              onOpenComposer={onOpenComposer}
              onOpenDirections={onOpenDirections}
              onOpenResearch={onOpenResearch}
            />
            <section className="rounded-lg border border-neutral-800/70 bg-neutral-900/40 p-3">
              <SectionHeader icon={GitPullRequest} title="Recent Completions" meta={`${model.recentCompleted.length}`} />
              <div className="mt-3 space-y-2">
                {model.recentCompleted.slice(0, 5).map(task => (
                  <TaskMiniRow key={task.id} task={task} onViewTask={handleViewTask} />
                ))}
                {model.recentCompleted.length === 0 && <EmptyState>No completed tasks in scope.</EmptyState>}
              </div>
            </section>
          </aside>
        </div>
      </div>
    </div>
  );
}
