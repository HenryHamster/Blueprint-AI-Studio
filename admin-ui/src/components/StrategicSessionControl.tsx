import {
  CheckCircle2,
  ChevronRight,
  Clock3,
  CreditCard,
  Cpu,
  Eye,
  Infinity,
  LoaderCircle,
  MessageSquare,
  MonitorCog,
  Server,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { StrategicWorkSessionLastCheckIn, StrategicWorkSessionSinceLastCheckIn } from '../strategicStateApi';

export type StrategicInteractionMode = 'chat' | 'check_in' | 'monitor' | 'continuous';

export interface StrategicLocalRunnerStatus {
  status?: string | null;
  location?: string | null;
  status_reason?: string | null;
  mode?: string | null;
  connected?: boolean | null;
  active?: boolean | null;
  label?: string | null;
  detail?: string | null;
  continuation?: {
    enabled?: boolean | null;
    fresh?: boolean | null;
    preferred_runner_id?: string | null;
    reason?: string | null;
    updated_at?: string | null;
    last_seen_at?: string | null;
    expires_at?: string | null;
    owner_user_id?: string | null;
    owner_session_started_at?: string | null;
    owned_by_current_operator?: boolean | null;
    ownership?: 'none' | 'unknown' | 'owned' | 'current_operator' | 'other_operator' | string;
    ttl_seconds?: number | null;
  } | null;
  deviceCount?: number | null;
  onlineDevices?: number | null;
  totalDevices?: number | null;
  strategic_runner_count?: number | null;
  live_runner_count?: number | null;
  eligible_live_count?: number | null;
  stale_runner_count?: number | null;
  busy_runner_count?: number | null;
  agent_unavailable_count?: number | null;
  active_runner_id?: string | null;
  active_task_pinned?: boolean | null;
  active_task_runtime?: {
    provider?: string | null;
    model?: string | null;
    account_id?: string | null;
    vm_instance_id?: string | null;
    runner_target_id?: string | null;
  } | null;
  runners?: Array<{
    id?: string | null;
    hostname?: string | null;
    is_preferred?: boolean | null;
    eligible_for_active_task?: boolean | null;
    blocked_reason?: string | null;
  }> | null;
  fallback_reason?: string | null;
  liveness_window_seconds?: number | null;
}

export interface StrategicRuntimeWorkStatus {
  status?: string | null;
  active?: boolean | null;
  label?: string | null;
  detail?: string | null;
  runningCount?: number | null;
  activeCount?: number | null;
  queuedCount?: number | null;
  totalCount?: number | null;
  compute_jobs?: {
    counts?: {
      active?: number | null;
      terminal_recent?: number | null;
      total?: number | null;
      queued?: number | null;
      starting?: number | null;
      running?: number | null;
      polling?: number | null;
      failed_recent?: number | null;
      timed_out_recent?: number | null;
    } | null;
  } | null;
  held_approvals?: {
    count?: number | null;
  } | null;
  local_runner_holds?: {
    count?: number | null;
    compute_count?: number | null;
    strategic_count?: number | null;
  } | null;
  parked_wakes?: {
    counts?: {
      active?: number | null;
      problem?: number | null;
    } | null;
  } | null;
}

export interface StrategicAccountSpendStatus {
  totalAccounts?: number | null;
  availableAccounts?: number | null;
  enabledAccounts?: number | null;
  activeAccountTasks?: number | null;
  cooldownAccounts?: number | null;
  cappedAccounts?: number | null;
  configuredCapAccounts?: number | null;
  disabledModelCount?: number | null;
  cumulativeCostUsd?: number | null;
  trackedSpendUsd?: number | null;
  dailyCostCeilingUsd?: number | null;
}

export interface StrategicSessionControlProps {
  currentMode?: StrategicInteractionMode;
  mode?: StrategicInteractionMode;
  saving?: boolean;
  localRunnerStatus?: StrategicLocalRunnerStatus | null;
  localRunner?: StrategicLocalRunnerStatus | null;
  runtimeWorkStatus?: StrategicRuntimeWorkStatus | null;
  runtimeWork?: StrategicRuntimeWorkStatus | null;
  accountSpend?: StrategicAccountSpendStatus | null;
  lastCheckIn?: StrategicWorkSessionLastCheckIn | null;
  sinceLastCheckIn?: StrategicWorkSessionSinceLastCheckIn | null;
  checkingIn?: boolean;
  onModeChange?: (mode: StrategicInteractionMode) => void;
  onCheckIn?: () => void;
  onOpenDevices?: () => void;
  onOpenCompute?: () => void;
  onOpenAccounts?: () => void;
  onOpenSpend?: () => void;
  showMonitoringControls?: boolean;
  className?: string;
}

type Tone = 'neutral' | 'success' | 'warning' | 'danger' | 'info';

const MODE_OPTIONS: Array<{ value: StrategicInteractionMode; label: string; Icon: LucideIcon }> = [
  { value: 'chat', label: 'Chat', Icon: MessageSquare },
  { value: 'check_in', label: 'Check-in', Icon: Clock3 },
  { value: 'monitor', label: 'Monitor', Icon: Eye },
  { value: 'continuous', label: 'Workday', Icon: Infinity },
];

const toneClasses: Record<Tone, { border: string; bg: string; icon: string; text: string; dot: string }> = {
  neutral: {
    border: 'border-neutral-800',
    bg: 'bg-neutral-950/70',
    icon: 'text-neutral-500',
    text: 'text-neutral-300',
    dot: 'bg-neutral-500',
  },
  success: {
    border: 'border-neutral-800',
    bg: 'bg-neutral-950/70',
    icon: 'text-emerald-400',
    text: 'text-emerald-300',
    dot: 'bg-emerald-400',
  },
  warning: {
    border: 'border-amber-500/30',
    bg: 'bg-amber-500/10',
    icon: 'text-amber-400',
    text: 'text-amber-300',
    dot: 'bg-amber-400',
  },
  danger: {
    border: 'border-red-500/30',
    bg: 'bg-red-500/10',
    icon: 'text-red-400',
    text: 'text-red-300',
    dot: 'bg-red-400',
  },
  info: {
    border: 'border-neutral-800',
    bg: 'bg-neutral-950/70',
    icon: 'text-cyan-400',
    text: 'text-cyan-300',
    dot: 'bg-cyan-400',
  },
};

const WARNING_FALLBACK_REASONS = new Set([
  'agent_unavailable',
  'at_capacity',
  'capability_mismatch',
  'runner_claim_pending',
  'runner_unavailable',
]);

function cleanText(value: string | null | undefined): string | null {
  const trimmed = value?.trim();
  return trimmed ? trimmed : null;
}

function normalizedStatus(value: string | null | undefined): string {
  return cleanText(value)?.toLowerCase().replace(/\s+/g, '_') ?? 'unknown';
}

function humanizeStatus(value: string): string {
  return value
    .replace(/[_-]+/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
    .replace(/\b\w/g, char => char.toUpperCase());
}

function firstCount(...values: Array<number | null | undefined>): number | null {
  for (const value of values) {
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) return value;
  }
  return null;
}

function finiteNonNegativeNumber(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
}

function pluralize(count: number, singular: string): string {
  return `${count} ${singular}${count === 1 ? '' : 's'}`;
}

function formatUsd(value: number | null | undefined): string | null {
  const amount = finiteNonNegativeNumber(value);
  if (amount == null) return null;
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    maximumFractionDigits: 2,
  }).format(amount);
}

function formatRelativeTime(timestamp?: string | null): string | null {
  if (!timestamp) return null;
  const date = new Date(timestamp);
  if (Number.isNaN(date.getTime())) return null;
  const diffMs = Date.now() - date.getTime();
  if (diffMs < 60_000) return 'just now';
  const minutes = Math.floor(diffMs / 60_000);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString();
}

function checkInChangeCount(since?: StrategicWorkSessionSinceLastCheckIn | null): number | null {
  if (!since?.available) return null;
  return (
    (since.messages?.total ?? 0)
    + (since.tasks?.updated ?? 0)
    + (since.alerts?.raised ?? 0)
    + (since.strategic?.cycles_completed ?? 0)
  );
}

function checkInDetail(
  lastCheckIn?: StrategicWorkSessionLastCheckIn | null,
  sinceLastCheckIn?: StrategicWorkSessionSinceLastCheckIn | null,
): string {
  if (!lastCheckIn?.created_at) return 'Never checked';
  const relative = formatRelativeTime(lastCheckIn.created_at);
  const changeCount = checkInChangeCount(sinceLastCheckIn);
  const changeLabel = changeCount == null
    ? null
    : changeCount === 0
      ? '0 new'
      : `${changeCount} new`;
  return [relative ? `Last ${relative}` : null, changeLabel].filter(Boolean).join(' / ') || 'Checked';
}

function shortId(value?: string | null): string | null {
  const trimmed = cleanText(value);
  return trimmed ? trimmed.slice(0, 8) : null;
}

function preferredRunnerId(status: StrategicLocalRunnerStatus): string | null {
  return cleanText(status.continuation?.preferred_runner_id);
}

function preferredRunnerLabel(status: StrategicLocalRunnerStatus): string | null {
  const id = preferredRunnerId(status);
  const preferredCandidate = id
    ? status.runners?.find(runner => cleanText(runner.id) === id)
    : status.runners?.find(runner => runner.is_preferred === true);
  const candidateName = cleanText(preferredCandidate?.hostname);
  const compactId = shortId(preferredCandidate?.id ?? id);
  if (candidateName && compactId) return `${candidateName} ${compactId}`;
  return candidateName ?? compactId;
}

function runnerStatusValue(status: StrategicLocalRunnerStatus): string {
  return normalizedStatus(status.status ?? status.mode);
}

function continuationOwnedByAnotherOperator(status: StrategicLocalRunnerStatus): boolean {
  const continuation = status.continuation;
  return (
    continuation?.enabled === true
    && continuation.fresh === true
    && continuation.owned_by_current_operator === false
    && (
      continuation.ownership === 'other_operator'
      || cleanText(continuation.owner_user_id) != null
    )
  );
}

function runnerTone(status: StrategicLocalRunnerStatus): Tone {
  const value = runnerStatusValue(status);
  const fallbackReason = normalizedStatus(status.fallback_reason);
  const hasStaleContinuation = status.continuation?.enabled === true && status.continuation.fresh === false;
  const hasUnknownContinuation = status.continuation?.enabled === true && status.continuation.fresh == null;
  const hasForeignContinuation = continuationOwnedByAnotherOperator(status);
  if (value.includes('error') || value.includes('fail')) return 'danger';
  if (hasStaleContinuation || hasUnknownContinuation || hasForeignContinuation || WARNING_FALLBACK_REASONS.has(fallbackReason)) return 'warning';
  if (value.includes('warn')) return 'warning';
  if (status.connected === true || status.active === true) return 'success';
  if ((firstCount(status.eligible_live_count) ?? 0) > 0) return 'success';
  if (['online', 'connected', 'ready', 'running', 'active', 'local', 'local_ready'].includes(value)) return 'success';
  if (value.includes('start') || value.includes('sync') || value === 'pending_local') return 'info';
  if (['offline', 'disconnected', 'stopped', 'disabled'].includes(value)) return 'neutral';
  return 'info';
}

function runnerLabel(status: StrategicLocalRunnerStatus): string {
  const explicit = cleanText(status.label);
  if (explicit) return explicit;
  const value = runnerStatusValue(status);
  if (value === 'local') return 'Local';
  if (value === 'local_ready') return 'Ready';
  if (value === 'pending_local') return 'Waiting';
  if (value === 'cloud') return 'Cloud';
  if (status.connected === true || status.active === true) return 'Online';
  if (status.connected === false || status.active === false) return 'Offline';
  return humanizeStatus(value);
}

function runnerDetail(status: StrategicLocalRunnerStatus): string | null {
  const explicit = cleanText(status.detail);
  if (explicit) return explicit;
  const statusReason = cleanText(status.status_reason);
  const activeRunner = shortId(status.active_runner_id);
  const preferredRunner = preferredRunnerLabel(status);
  const runtimeModel = cleanText(status.active_task_runtime?.model);
  const value = runnerStatusValue(status);
  if (value === 'local') {
    const parts = [
      activeRunner ? `runner ${activeRunner}` : null,
      runtimeModel,
    ].filter(Boolean);
    return parts.length > 0 ? `Running on ${parts.join(' / ')}` : 'Current cycle pinned locally';
  }
  if (status.active_task_pinned === true) {
    return activeRunner ? `Pinned to runner ${activeRunner}` : 'Pinned to local runner';
  }
  const online = firstCount(status.onlineDevices);
  const total = firstCount(status.totalDevices);
  const count = firstCount(status.deviceCount);
  const ready = firstCount(status.eligible_live_count);
  const busy = firstCount(status.busy_runner_count);
  const live = firstCount(status.live_runner_count);
  const enrolled = firstCount(status.strategic_runner_count);
  const fallbackReason = normalizedStatus(status.fallback_reason);
  if (fallbackReason === 'no_runner') return 'No local Strategic runner enrolled';
  if (fallbackReason === 'runner_offline' && enrolled != null) return `${pluralize(enrolled, 'runner')} enrolled, none live`;
  if (fallbackReason === 'runner_offline') return 'Local Strategic runner offline';
  if (fallbackReason === 'agent_unavailable') return 'Runner live, agent unavailable';
  if (fallbackReason === 'at_capacity') return 'Runner busy with active work';
  if (fallbackReason === 'cloud_dispatch') return 'Current cycle dispatched to cloud';
  if (fallbackReason === 'capability_mismatch') return 'Runner cannot handle this cycle';
  if (fallbackReason === 'runner_claim_pending') return 'Waiting for runner claim';
  if (fallbackReason === 'runner_unavailable') return 'Runner unavailable';
  if (fallbackReason === 'local_required_cloud_dispatch') return 'Local Strategic work is running in cloud';
  if (statusReason) return humanizeStatus(statusReason);
  if (busy != null && busy > 0 && (ready == null || ready === 0)) return `${busy} busy`;
  if (status.continuation?.enabled === true) {
    if (continuationOwnedByAnotherOperator(status)) {
      return 'Workday owned by another operator';
    }
    if (status.continuation.fresh === false) {
      return preferredRunner
        ? `Stale continuation, preferred runner ${preferredRunner}`
        : 'Stale continuation';
    }
    if (status.continuation.fresh !== true) {
      return preferredRunner
        ? `Continuation status unknown, preferred runner ${preferredRunner}`
        : 'Continuation status unknown';
    }
    if (preferredRunner) return `Fresh continuation, preferred runner ${preferredRunner}`;
    if (ready != null) return `Fresh continuation, ${ready} eligible live`;
    return 'Fresh continuation';
  }
  if (online != null && total != null) return `${online}/${total} devices`;
  if (count != null) return `${count} device${count === 1 ? '' : 's'}`;
  if (ready != null) return `${ready} eligible live`;
  if (busy != null && busy > 0) return `${busy} busy`;
  if (live != null && enrolled != null) return `${live}/${enrolled} live`;
  if (enrolled != null) return pluralize(enrolled, 'runner');
  return null;
}

function runnerMeta(status: StrategicLocalRunnerStatus, primaryDetail: string | null): string[] {
  const online = firstCount(status.onlineDevices);
  const totalDevices = firstCount(status.totalDevices);
  const deviceCount = firstCount(status.deviceCount);
  const live = firstCount(status.live_runner_count);
  const enrolled = firstCount(status.strategic_runner_count);
  const ready = firstCount(status.eligible_live_count);
  const busy = firstCount(status.busy_runner_count);
  const stale = firstCount(status.stale_runner_count);
  const unavailable = firstCount(status.agent_unavailable_count);
  const continuation = status.continuation;
  const preferredRunner = preferredRunnerLabel(status);
  const parts = [
    online != null && totalDevices != null ? `${online}/${totalDevices} devices` : null,
    online == null && deviceCount != null ? pluralize(deviceCount, 'device') : null,
    live != null && enrolled != null ? `${live}/${enrolled} live` : null,
    ready != null ? `${ready} eligible live` : null,
    busy != null && busy > 0 ? `${busy} busy` : null,
    stale != null && stale > 0 ? `${stale} stale` : null,
    unavailable != null && unavailable > 0 ? `${unavailable} unavailable` : null,
    preferredRunner ? `preferred runner ${preferredRunner}` : null,
    status.active_task_pinned === true ? 'pinned' : null,
    continuationOwnedByAnotherOperator(status) ? 'owned by another operator' : null,
    continuation?.enabled === true
      ? `${continuation.fresh === true ? 'fresh' : continuation.fresh === false ? 'stale' : 'unknown'} continuation`
      : null,
  ];
  return parts.filter((part): part is string => {
    if (!part) return false;
    if (part === primaryDetail) return false;
    return !primaryDetail?.toLowerCase().includes(part.toLowerCase());
  });
}

function runtimeComputeCounts(status: StrategicRuntimeWorkStatus): NonNullable<StrategicRuntimeWorkStatus['compute_jobs']>['counts'] {
  return status.compute_jobs?.counts ?? null;
}

function runtimeHeldApprovalCount(status: StrategicRuntimeWorkStatus): number {
  return firstCount(status.held_approvals?.count) ?? 0;
}

function runtimeLocalRunnerHoldCount(status: StrategicRuntimeWorkStatus): number {
  const explicit = firstCount(status.local_runner_holds?.count);
  if (explicit != null) return explicit;
  return (
    (firstCount(status.local_runner_holds?.strategic_count) ?? 0)
    + (firstCount(status.local_runner_holds?.compute_count) ?? 0)
  );
}

function runtimeStrategicLocalRunnerHoldCount(status: StrategicRuntimeWorkStatus): number {
  return firstCount(status.local_runner_holds?.strategic_count) ?? 0;
}

function runtimeLocalRunnerHoldParts(status: StrategicRuntimeWorkStatus): string[] {
  const total = runtimeLocalRunnerHoldCount(status);
  if (total === 0) return [];
  const strategic = runtimeStrategicLocalRunnerHoldCount(status);
  const compute = firstCount(status.local_runner_holds?.compute_count);
  const parts = [
    strategic > 0 ? pluralize(strategic, 'Strategic hold') : null,
    compute != null && compute > 0 ? pluralize(compute, 'compute hold') : null,
  ].filter((part): part is string => Boolean(part));
  if (parts.length > 0) {
    const covered = strategic + (compute ?? 0);
    if (covered < total) parts.push(pluralize(total - covered, 'local runner hold'));
    return parts;
  }
  return [pluralize(total, 'local runner hold')];
}

function runtimeParkedWakeActiveCount(status: StrategicRuntimeWorkStatus): number {
  return firstCount(status.parked_wakes?.counts?.active) ?? 0;
}

function runtimeParkedWakeProblemCount(status: StrategicRuntimeWorkStatus): number {
  return firstCount(status.parked_wakes?.counts?.problem) ?? 0;
}

function runtimeRecentFailureCount(status: StrategicRuntimeWorkStatus): number {
  const counts = runtimeComputeCounts(status);
  return (firstCount(counts?.failed_recent) ?? 0) + (firstCount(counts?.timed_out_recent) ?? 0);
}

function runtimeTone(status: StrategicRuntimeWorkStatus): Tone {
  const value = normalizedStatus(status.status);
  if (runtimeRecentFailureCount(status) > 0) return 'danger';
  if (runtimeHeldApprovalCount(status) > 0 || runtimeLocalRunnerHoldCount(status) > 0 || runtimeParkedWakeProblemCount(status) > 0) {
    return 'warning';
  }
  if (value.includes('error') || value.includes('fail')) return 'danger';
  if (value.includes('block') || value.includes('queued') || value.includes('wait')) return 'warning';
  if (runtimeIsUseful(status)) return 'success';
  if (value === 'idle' || value === 'none' || value === 'unknown') return 'neutral';
  return 'info';
}

function runtimeActiveCount(status: StrategicRuntimeWorkStatus): number {
  const counts = runtimeComputeCounts(status);
  const computeActive = firstCount(status.runningCount, status.activeCount, counts?.active)
    ?? (firstCount(counts?.running) ?? 0) + (firstCount(counts?.polling) ?? 0);
  return computeActive + runtimeParkedWakeActiveCount(status);
}

function runtimeIsUseful(status: StrategicRuntimeWorkStatus): boolean {
  const value = normalizedStatus(status.status);
  return Boolean(
    status.active === true
      || runtimeActiveCount(status) > 0
      || (firstCount(status.queuedCount, runtimeComputeCounts(status)?.queued) ?? 0) > 0
      || runtimeHeldApprovalCount(status) > 0
      || runtimeLocalRunnerHoldCount(status) > 0
      || runtimeParkedWakeProblemCount(status) > 0
      || runtimeRecentFailureCount(status) > 0
      || (firstCount(runtimeComputeCounts(status)?.terminal_recent) ?? 0) > 0
      || ['active', 'running', 'queued', 'blocked', 'error', 'failed', 'provisioning', 'evaluating'].includes(value),
  );
}

function runtimeLabel(status: StrategicRuntimeWorkStatus): string {
  const explicit = cleanText(status.label);
  if (explicit) return explicit;
  if (runtimeHeldApprovalCount(status) > 0 || runtimeLocalRunnerHoldCount(status) > 0 || runtimeParkedWakeProblemCount(status) > 0) {
    return 'Needs check';
  }
  if (runtimeRecentFailureCount(status) > 0) return 'Recent failure';
  const active = runtimeActiveCount(status);
  if (active > 0) return 'Active';
  const queued = firstCount(status.queuedCount, runtimeComputeCounts(status)?.queued) ?? 0;
  if (queued > 0) return 'Queued';
  if ((firstCount(runtimeComputeCounts(status)?.terminal_recent) ?? 0) > 0) return 'Recent result';
  return humanizeStatus(normalizedStatus(status.status));
}

function runtimeDetail(status: StrategicRuntimeWorkStatus): string | null {
  const explicit = cleanText(status.detail);
  if (explicit) return explicit;
  const active = runtimeActiveCount(status);
  const queued = firstCount(status.queuedCount, runtimeComputeCounts(status)?.queued) ?? 0;
  const parts = [
    active > 0 ? `${active} active` : null,
    queued > 0 ? `${queued} queued` : null,
    ...runtimeLocalRunnerHoldParts(status),
    runtimeHeldApprovalCount(status) > 0 ? pluralize(runtimeHeldApprovalCount(status), 'approval') : null,
    runtimeParkedWakeProblemCount(status) > 0 ? pluralize(runtimeParkedWakeProblemCount(status), 'wake issue') : null,
    runtimeRecentFailureCount(status) > 0 ? pluralize(runtimeRecentFailureCount(status), 'recent failure') : null,
  ].filter(Boolean);
  if (parts.length > 0) return parts.join(' / ');
  const terminal = firstCount(runtimeComputeCounts(status)?.terminal_recent) ?? 0;
  if (terminal > 0) return pluralize(terminal, 'recent result');
  return null;
}

function accountSpendTone(status: StrategicAccountSpendStatus): Tone {
  const available = firstCount(status.availableAccounts);
  const total = firstCount(status.totalAccounts);
  if (total === 0) return 'warning';
  if (available === 0 && (total == null || total > 0)) return 'danger';
  if (
    (firstCount(status.cooldownAccounts) ?? 0) > 0
    || (firstCount(status.disabledModelCount) ?? 0) > 0
    || (available != null && total != null && available < total)
  ) {
    return 'warning';
  }
  if (available != null && available > 0) return 'success';
  if ((finiteNonNegativeNumber(status.trackedSpendUsd ?? status.cumulativeCostUsd) ?? 0) > 0) return 'info';
  return 'neutral';
}

function accountSpendLabel(status: StrategicAccountSpendStatus): string {
  const available = firstCount(status.availableAccounts);
  const enabled = firstCount(status.enabledAccounts);
  const total = firstCount(status.totalAccounts);
  if (total === 0) return 'No accounts';
  if (available === 0 && (total == null || total > 0)) return 'Blocked';
  if ((firstCount(status.cooldownAccounts) ?? 0) > 0) return 'Cooldowns';
  if ((firstCount(status.disabledModelCount) ?? 0) > 0) return 'Cooldowns';
  if (available != null && total != null && available < total) return 'Partial';
  if (available != null && total != null) return `${available}/${total} available`;
  if (available != null) return pluralize(available, 'available account');
  if (enabled != null && total != null) return `${enabled}/${total} enabled`;
  if (enabled != null) return pluralize(enabled, 'enabled account');
  if ((finiteNonNegativeNumber(status.trackedSpendUsd ?? status.cumulativeCostUsd) ?? 0) > 0) return 'Spend tracked';
  return 'Available';
}

function accountSpendDetail(status: StrategicAccountSpendStatus): string | null {
  const available = firstCount(status.availableAccounts);
  const enabled = firstCount(status.enabledAccounts);
  const total = firstCount(status.totalAccounts);
  const active = firstCount(status.activeAccountTasks) ?? 0;
  const cooldowns = firstCount(status.cooldownAccounts) ?? 0;
  const configuredCaps = firstCount(status.configuredCapAccounts, status.cappedAccounts) ?? 0;
  const disabledModels = firstCount(status.disabledModelCount) ?? 0;
  const costValue = finiteNonNegativeNumber(status.trackedSpendUsd ?? status.cumulativeCostUsd);
  const ceilingValue = finiteNonNegativeNumber(status.dailyCostCeilingUsd);
  const hasCeiling = ceilingValue != null && ceilingValue > 0;
  const shouldShowSpend = costValue != null && costValue > 0;
  const trackedSpend = shouldShowSpend ? formatUsd(costValue) : null;
  const ceiling = hasCeiling ? formatUsd(ceilingValue) : null;
  const parts = [
    total === 0 ? 'No accounts' : null,
    available != null && total != null && total > 0 ? `${available}/${total} available` : null,
    enabled != null && total != null && total > 0 && enabled !== available ? pluralize(enabled, 'enabled account') : null,
    enabled != null && total == null ? pluralize(enabled, 'enabled account') : null,
    active > 0 ? `${active} active` : null,
    cooldowns > 0 ? `${cooldowns} cooldown` : null,
    configuredCaps > 0 ? `${configuredCaps} with caps` : null,
    disabledModels > 0 ? pluralize(disabledModels, 'disabled model') : null,
    trackedSpend ? `tracked ${trackedSpend}` : null,
    ceiling ? `daily ceiling ${ceiling}` : null,
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(' / ') : null;
}

function StatusDot({ tone }: { tone: Tone }) {
  return <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${toneClasses[tone].dot}`} aria-hidden="true" />;
}

export function StrategicSessionControl({
  currentMode,
  mode,
  saving = false,
  localRunnerStatus = null,
  localRunner = null,
  runtimeWorkStatus = null,
  runtimeWork = null,
  accountSpend = null,
  lastCheckIn = null,
  sinceLastCheckIn = null,
  checkingIn = false,
  onModeChange,
  onCheckIn,
  onOpenDevices,
  onOpenCompute,
  onOpenAccounts,
  onOpenSpend,
  showMonitoringControls = true,
  className = '',
}: StrategicSessionControlProps) {
  const selectedMode = currentMode ?? mode ?? 'chat';
  const runnerStatus = localRunnerStatus ?? localRunner;
  const workStatus = runtimeWorkStatus ?? runtimeWork;
  const openAccountSpend = onOpenAccounts ?? onOpenSpend;
  const accountSpendOpenLabel = onOpenAccounts ? 'Open accounts' : 'Open spend';
  const checkInSummary = checkInDetail(lastCheckIn, sinceLastCheckIn);
  const checkInChanges = checkInChangeCount(sinceLastCheckIn);

  const runner = runnerStatus
    ? (() => {
        const detail = runnerDetail(runnerStatus);
        return {
          tone: runnerTone(runnerStatus),
          label: runnerLabel(runnerStatus),
          detail,
          meta: runnerMeta(runnerStatus, detail),
        };
      })()
    : null;

  const runtime = workStatus
    ? {
        tone: runtimeTone(workStatus),
        label: runtimeLabel(workStatus),
        detail: runtimeDetail(workStatus),
        useful: runtimeIsUseful(workStatus),
      }
    : null;

  const accountSpendControl = accountSpend
    ? {
        tone: accountSpendTone(accountSpend),
        label: accountSpendLabel(accountSpend),
        detail: accountSpendDetail(accountSpend),
      }
    : null;

  return (
    <div
      className={`flex min-w-0 flex-col gap-1.5 text-neutral-100 sm:flex-row sm:items-center sm:justify-end ${className}`}
      data-testid="strategic-session-control"
    >
      <div
        className="grid min-w-0 grid-cols-2 gap-0.5 rounded border border-neutral-800 bg-neutral-950/80 p-0.5 sm:flex sm:w-auto"
        role="group"
        aria-label="Strategic interaction mode"
      >
        {MODE_OPTIONS.map(option => {
          const selected = option.value === selectedMode;
          const Icon = option.Icon;
          return (
            <button
              key={option.value}
              type="button"
              disabled={saving || !onModeChange}
              aria-pressed={selected}
              aria-label={`Switch Strategic mode to ${option.label}`}
              title={`Strategic mode: ${option.label}`}
              onClick={() => {
                if (!selected) onModeChange?.(option.value);
              }}
              className={`inline-flex min-h-11 min-w-0 items-center justify-center gap-1.5 rounded px-2 py-1.5 text-xs font-semibold transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-cyan-400 sm:min-h-8 sm:px-2 ${
                selected
                  ? 'bg-neutral-800 text-neutral-100 ring-1 ring-neutral-700'
                  : 'text-neutral-400 hover:bg-neutral-900 hover:text-neutral-100'
              } disabled:cursor-not-allowed disabled:opacity-50`}
            >
              <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span className="truncate">{option.label}</span>
            </button>
          );
        })}
      </div>

      <div className="flex min-w-0 flex-col gap-1.5 sm:flex-row sm:flex-wrap sm:items-center sm:justify-end">
        {saving && (
          <span
            className="inline-flex min-h-11 min-w-0 items-center gap-2 rounded border border-cyan-500/25 bg-neutral-950/70 px-2 py-1.5 font-mono text-ui-sm font-semibold uppercase text-cyan-300 sm:min-h-8"
            role="status"
            aria-label="Saving Strategic session mode"
            title="Saving Strategic session mode"
          >
            <LoaderCircle className="h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden="true" />
            Saving
          </span>
        )}

        <button
          type="button"
          onClick={onCheckIn}
          disabled={!onCheckIn || checkingIn}
          className="group inline-grid min-h-11 w-full min-w-0 grid-cols-[auto,minmax(0,1fr),auto] items-center gap-2 rounded border border-neutral-800 bg-neutral-950/70 px-2 py-1.5 text-left transition-colors hover:border-neutral-600 hover:bg-neutral-900/80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-cyan-400 disabled:cursor-not-allowed disabled:opacity-60 sm:min-h-8 sm:w-auto sm:max-w-[16rem]"
          aria-label={`Mark Strategic session checked: ${checkInSummary}`}
          title={`Mark Strategic session checked: ${checkInSummary}`}
        >
          {checkingIn ? (
            <LoaderCircle className="h-3.5 w-3.5 shrink-0 animate-spin text-cyan-300" aria-hidden="true" />
          ) : (
            <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-neutral-500 transition-colors group-hover:text-neutral-200" aria-hidden="true" />
          )}
          <span className="min-w-0 flex-1">
            <span className="block truncate text-xs font-semibold text-neutral-200">Mark checked</span>
            <span className="block truncate text-ui-sm text-neutral-400">{checkInSummary}</span>
          </span>
          {checkInChanges != null && checkInChanges > 0 && (
            <span className="shrink-0 rounded border border-cyan-500/25 bg-cyan-500/10 px-1.5 py-0.5 font-mono text-ui-sm font-semibold text-cyan-300">
              {checkInChanges}
            </span>
          )}
        </button>

        {showMonitoringControls && (
          <>
            {runner && (
              <button
                type="button"
                onClick={onOpenDevices}
                disabled={!onOpenDevices}
                className={`group inline-grid min-h-11 w-full min-w-0 grid-cols-[auto,minmax(0,1fr),auto] items-center gap-2 rounded border px-2 py-1.5 text-left transition-colors hover:border-neutral-500 hover:bg-neutral-900/80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-cyan-400 sm:min-h-8 sm:w-auto sm:max-w-[22rem] ${
                  toneClasses[runner.tone].border
                } ${toneClasses[runner.tone].bg} disabled:cursor-not-allowed disabled:opacity-60`}
                aria-label={`Open Devices: runner ${runner.label}${runner.detail ? `, ${runner.detail}` : ''}${
                  runner.meta.length > 0 ? `, ${runner.meta.join(', ')}` : ''
                }`}
                title={runner.detail ? `Open Devices: ${runner.detail}` : 'Open Devices'}
              >
                <Server className={`h-3.5 w-3.5 shrink-0 ${toneClasses[runner.tone].icon}`} aria-hidden="true" />
                <span className="min-w-0 flex-1">
                  <span className="flex min-w-0 items-center gap-1.5 leading-tight">
                    <span className="shrink-0 font-mono text-ui-sm font-semibold uppercase text-neutral-500">Devices</span>
                    <StatusDot tone={runner.tone} />
                    <span className={`truncate text-xs font-semibold ${toneClasses[runner.tone].text}`}>{runner.label}</span>
                  </span>
                  <span className="block truncate text-ui-sm text-neutral-400">{runner.detail ?? 'Open device and Strategic runner state'}</span>
                </span>
                <span className="hidden min-w-0 items-center gap-1 lg:flex">
                  {runner.meta.slice(0, 2).map(item => (
                    <span
                      key={item}
                      className="max-w-24 truncate rounded border border-neutral-800 bg-neutral-950/60 px-1.5 py-0.5 font-mono text-ui-sm text-neutral-400"
                    >
                      {item}
                    </span>
                  ))}
                </span>
                <ChevronRight className="h-3.5 w-3.5 shrink-0 text-neutral-600 transition-colors group-hover:text-neutral-300" aria-hidden="true" />
              </button>
            )}

        {accountSpendControl ? (
          openAccountSpend ? (
            <button
              type="button"
              onClick={openAccountSpend}
              className={`group inline-grid min-h-11 w-full min-w-0 grid-cols-[auto,minmax(0,1fr),auto] items-center gap-2 rounded border px-2 py-1.5 text-left transition-colors hover:border-neutral-500 hover:bg-neutral-900/80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-cyan-400 sm:min-h-8 sm:w-auto sm:max-w-[20rem] ${
                toneClasses[accountSpendControl.tone].border
              } ${toneClasses[accountSpendControl.tone].bg}`}
              aria-label={`${accountSpendOpenLabel}: accounts ${accountSpendControl.label}${
                accountSpendControl.detail ? `, ${accountSpendControl.detail}` : ''
              }`}
              title={accountSpendControl.detail ? `${accountSpendOpenLabel}: ${accountSpendControl.detail}` : accountSpendOpenLabel}
            >
              <CreditCard
                className={`h-3.5 w-3.5 shrink-0 ${toneClasses[accountSpendControl.tone].icon}`}
                aria-hidden="true"
              />
              <span className="min-w-0 flex-1">
                <span className="flex min-w-0 items-center gap-1.5 leading-tight">
                  <span className="shrink-0 font-mono text-ui-sm font-semibold uppercase text-neutral-500">Accounts</span>
                  <StatusDot tone={accountSpendControl.tone} />
                  <span className={`truncate text-xs font-semibold ${toneClasses[accountSpendControl.tone].text}`}>
                    {accountSpendControl.label}
                  </span>
                </span>
                <span className="block truncate text-ui-sm text-neutral-400">
                  {accountSpendControl.detail ?? 'Open account and spend state'}
                </span>
              </span>
              <ChevronRight
                className="h-3.5 w-3.5 shrink-0 text-neutral-600 transition-colors group-hover:text-neutral-300"
                aria-hidden="true"
              />
            </button>
          ) : (
            <span
              className={`inline-grid min-h-11 w-full min-w-0 grid-cols-[auto,minmax(0,1fr)] items-center gap-2 rounded border px-2 py-1.5 text-left sm:min-h-8 sm:w-auto sm:max-w-[20rem] ${
                toneClasses[accountSpendControl.tone].border
              } ${toneClasses[accountSpendControl.tone].bg}`}
              aria-label={`Accounts ${accountSpendControl.label}${accountSpendControl.detail ? `, ${accountSpendControl.detail}` : ''}`}
              title="Accounts"
            >
              <CreditCard
                className={`h-3.5 w-3.5 shrink-0 ${toneClasses[accountSpendControl.tone].icon}`}
                aria-hidden="true"
              />
              <span className="min-w-0 flex-1">
                <span className="flex min-w-0 items-center gap-1.5 leading-tight">
                  <span className="shrink-0 font-mono text-ui-sm font-semibold uppercase text-neutral-500">Accounts</span>
                  <StatusDot tone={accountSpendControl.tone} />
                  <span className={`truncate text-xs font-semibold ${toneClasses[accountSpendControl.tone].text}`}>
                    {accountSpendControl.label}
                  </span>
                </span>
                <span className="block truncate text-ui-sm text-neutral-400">
                  {accountSpendControl.detail ?? 'Account and spend state'}
                </span>
              </span>
            </span>
          )
        ) : null}

        {runtime && runtime.useful && onOpenCompute ? (
          <button
            type="button"
            onClick={onOpenCompute}
            className={`group inline-grid min-h-11 w-full min-w-0 grid-cols-[auto,minmax(0,1fr),auto] items-center gap-2 rounded border px-2 py-1.5 text-left transition-colors hover:border-neutral-500 hover:bg-neutral-900/80 focus-visible:outline focus-visible:outline-2 focus-visible:outline-cyan-400 sm:min-h-8 sm:w-auto sm:max-w-[20rem] ${
              toneClasses[runtime.tone].border
            } ${toneClasses[runtime.tone].bg}`}
            aria-label={`Open compute: runtime work ${runtime.label}${runtime.detail ? `, ${runtime.detail}` : ''}`}
            title={runtime.detail ? `Open compute: ${runtime.detail}` : 'Open compute'}
          >
            <Cpu className={`h-3.5 w-3.5 shrink-0 ${toneClasses[runtime.tone].icon}`} aria-hidden="true" />
            <span className="min-w-0 flex-1">
              <span className="flex min-w-0 items-center gap-1.5 leading-tight">
                <span className="shrink-0 font-mono text-ui-sm font-semibold uppercase text-neutral-500">Compute</span>
                <StatusDot tone={runtime.tone} />
                <span className={`truncate text-xs font-semibold ${toneClasses[runtime.tone].text}`}>{runtime.label}</span>
              </span>
              <span className="block truncate text-ui-sm text-neutral-400">{runtime.detail ?? 'Open runtime work'}</span>
            </span>
            <ChevronRight className="h-3.5 w-3.5 shrink-0 text-neutral-600 transition-colors group-hover:text-neutral-300" aria-hidden="true" />
          </button>
        ) : runtime ? (
          <span
            className={`inline-grid min-h-11 w-full min-w-0 grid-cols-[auto,minmax(0,1fr)] items-center gap-2 rounded border px-2 py-1.5 text-left sm:min-h-8 sm:w-auto sm:max-w-[20rem] ${
              toneClasses[runtime.tone].border
            } ${toneClasses[runtime.tone].bg}`}
            aria-label={`Runtime work ${runtime.label}${runtime.detail ? `, ${runtime.detail}` : ''}`}
            title="Runtime work"
          >
            <MonitorCog className={`h-3.5 w-3.5 shrink-0 ${toneClasses[runtime.tone].icon}`} aria-hidden="true" />
            <span className="min-w-0 flex-1">
              <span className="flex min-w-0 items-center gap-1.5 leading-tight">
                <span className="shrink-0 font-mono text-ui-sm font-semibold uppercase text-neutral-500">Compute</span>
                <StatusDot tone={runtime.tone} />
                <span className={`truncate text-xs font-semibold ${toneClasses[runtime.tone].text}`}>{runtime.label}</span>
              </span>
              <span className="block truncate text-ui-sm text-neutral-400">{runtime.detail ?? 'No active runtime work'}</span>
            </span>
          </span>
        ) : null}
          </>
        )}
      </div>
    </div>
  );
}

export default StrategicSessionControl;
