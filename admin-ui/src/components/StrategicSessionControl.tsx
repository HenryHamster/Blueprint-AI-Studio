import {
  AlertTriangle,
  ChevronRight,
  CreditCard,
  Cpu,
  Infinity as InfinityIcon,
  LoaderCircle,
  MessageSquare,
  MonitorCog,
  Play,
  Server,
  Square,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

// The strategic interaction vocabulary is collapsed to two modes: conversational
// "Chat" and the resident "Workday" (``continuous``) session. The retired
// ``check_in`` / ``monitor`` modes are no longer offered here; a legacy value read
// off old data is coerced to ``chat`` by ``normalizeStrategicInteractionMode``.
export type StrategicInteractionMode = 'chat' | 'continuous';

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

/**
 * One device-picker option for the resident (Workday) session (mirrors an entry
 * of `build_resident_runner_options` in `strategic_resident_session.py`).
 */
export interface StrategicResidentRunnerOption {
  id: string;
  name: string;
  hostname?: string | null;
  online: boolean;
  agent_ready: boolean;
  eligible: boolean;
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
  /**
   * Resident-session controls. The container passes `onStartSession` only when
   * resident sessions are enabled (the `strategic_session_enabled` feature flag)
   * and keeps `onStopSession` wired unconditionally so a live session can always
   * be reclaimed. `sessionActive` reflects whether a resident session is
   * currently live and `sessionStatus` is its status string (`active` or
   * `ended`), used for the live-status chip. `sessionBusy` disables the button
   * while a start/stop request is in flight. The Start/Stop affordance only
   * renders in `continuous` ("Workday") mode.
   */
  sessionActive?: boolean;
  sessionStatus?: string | null;
  /**
   * Legible resident-session state from the read model's
   * `resident_session_availability`: `dark` (feature off) / `ready` (Workday +
   * runner live, auto-starting) / `active` / `blocked` (e.g. no runner) /
   * `error` (last auto-start failed). Drives the status chip in Workday mode so
   * the operator sees why a session isn't running instead of a bare Start button.
   */
  sessionState?: string | null;
  /**
   * Reason a Workday session isn't running (`sessions_disabled` / `no_runner` /
   * `runner_offline` / `start_failed: <msg>`), shown as the status chip's detail.
   * Always surfaced when the session can't start. `runner_offline` is the
   * mid-session block-on-offline verdict — the pinned device went offline and the
   * operator must restart to pick another (no auto-migration).
   */
  sessionReason?: string | null;
  sessionBusy?: boolean;
  /**
   * Multi-device picker: the project's enrolled runners (auto-pick + override
   * dropdown), the currently selected/pinned device id, and the change handler.
   * `onSelectRunner(null)` selects auto-pick. Passing `sessionRunners` (with an
   * `onSelectRunner`) renders the device dropdown in Workday mode.
   */
  sessionRunners?: StrategicResidentRunnerOption[] | null;
  sessionSelectedRunnerId?: string | null;
  /**
   * Operator-decision capture telemetry for the live resident session (mirrors
   * `operator_decision_summary` on the session snapshot). When present, a small
   * "op-decision capture: N/M (X%)" chip renders in Workday mode so the operator
   * can see whether operator corrections are reaching the durable decisions
   * register; a chronically-low rate is toned as a warning. Absent/null hides
   * the chip entirely (no operator turns processed / legacy session).
   */
  operatorDecisionSummary?: {
    total: number;
    captured: number;
    missed: number;
    capture_rate: number | null;
  } | null;
  onSelectRunner?: (runnerId: string | null) => void;
  onStartSession?: () => void;
  onStopSession?: () => void;
  onModeChange?: (mode: StrategicInteractionMode) => void;
  onOpenDevices?: () => void;
  onOpenCompute?: () => void;
  onOpenAccounts?: () => void;
  onOpenSpend?: () => void;
  showMonitoringControls?: boolean;
  density?: 'default' | 'compact';
  /**
   * Collapse the detail/session-status chips below the `md` breakpoint while
   * keeping the Chat/Workday mode toggle visible. Lets the toggle render on
   * mobile (where the full control is otherwise too wide) without the chips
   * overflowing the narrow header.
   */
  hideDetailsOnMobile?: boolean;
  className?: string;
}

type Tone = 'neutral' | 'success' | 'warning' | 'danger' | 'info';

const MODE_OPTIONS: Array<{ value: StrategicInteractionMode; label: string; Icon: LucideIcon }> = [
  { value: 'chat', label: 'Chat', Icon: MessageSquare },
  { value: 'continuous', label: 'Workday', Icon: InfinityIcon },
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

// The chip renders for a live (`active`) session, but map the full backend
// vocabulary (`active`, `ended`, null/none) explicitly so the label is accurate.
function sessionStatusLabel(status: string | null | undefined): string {
  const value = normalizedStatus(status);
  if (value === 'ended') return 'Ended';
  if (value === 'active') return 'Active';
  const trimmed = cleanText(status);
  return trimmed ? humanizeStatus(value) : 'Active';
}

// Turn a backend resident-session reason code into operator-readable detail.
// `start_failed: <msg>` carries the real cause; strip the code prefix and show
// the message. `no_runner` / `sessions_disabled` map to plain English.
function humanizeResidentReason(
  reason: string | null | undefined,
  selectedDeviceName?: string | null,
): string | null {
  const trimmed = cleanText(reason);
  if (!trimmed) return null;
  if (trimmed === 'sessions_disabled') return 'Resident sessions disabled';
  if (trimmed === 'no_runner') return 'No live local runner';
  if (trimmed === 'cycle_in_flight') return 'Waiting for the current strategic cycle to finish…';
  if (trimmed === 'runner_offline') {
    // Block-on-offline: name the pinned device and tell the operator to restart
    // (Workday does not auto-migrate to another device).
    const device = cleanText(selectedDeviceName);
    return device
      ? `Device ${device} offline — restart the session to pick another device`
      : 'Device offline — restart the session to pick another device';
  }
  const startFailed = /^start_failed\s*:?\s*(.*)$/i.exec(trimmed);
  if (startFailed) {
    const msg = cleanText(startFailed[1]);
    return msg ? `Start failed: ${msg}` : 'Start failed';
  }
  return humanizeStatus(normalizedStatus(trimmed));
}

// Dropdown label for one device: name plus an offline/busy suffix so the
// operator sees each device's status inline while picking. An offline device is
// still selectable (an explicit override) — the backend blocks the start with a
// clear reason rather than silently choosing another.
function runnerOptionLabel(option: StrategicResidentRunnerOption): string {
  const name =
    cleanText(option.name) ?? cleanText(option.hostname) ?? shortId(option.id) ?? 'device';
  if (!option.online) return `${name} — offline`;
  if (!option.eligible) return `${name} — busy`;
  return name;
}

interface ResidentSessionView {
  tone: Tone;
  Icon: LucideIcon;
  label: string;
  detail: string | null;
  showStop: boolean;
  showStart: boolean;
  // Fix-it affordance: `blocked:no_runner` surfaces an "Add runner" link that
  // routes the operator to Devices to enroll a local runner.
  showAddRunner: boolean;
}

// Map the resident-session state (+ reason) to a single legible status chip.
// Entering Workday auto-starts the session, so the chip — not a Start button —
// is the primary affordance across the formal state machine:
//   off      — feature flag off (no controls).
//   ready    — Workday on + live runner, auto-starting on entry.
//   starting — session created / task pinning, agent not yet streaming.
//   active   — runner attached / replying (Stop only).
//   blocked  — enabled but can't run; `no_runner` shows an "Add runner" fix-it.
//   error    — the last auto-start failed (reason surfaced).
// Stop stays on a live/starting session; Start remains only as an explicit
// fallback.
function residentSessionView(
  state: string | null | undefined,
  reason: string | null | undefined,
  sessionActive: boolean,
  sessionStatus: string | null | undefined,
  hasStart: boolean,
  hasStop: boolean,
  hasAddRunner: boolean,
  selectedDeviceName?: string | null,
): ResidentSessionView {
  const normalized = normalizedStatus(state);
  const reasonCode = normalizedStatus(reason);
  // Block-on-offline: the pinned device went offline mid-session. The blob is
  // still `active`, but the read model reports blocked/runner_offline. Prompt a
  // RESTART (Stop, then re-pick a device and Start) — no auto-migration.
  const runnerOffline = normalized === 'blocked' && reasonCode === 'runner_offline';
  // Transient block: a batch strategic cycle is in flight. The session will
  // auto-start once the cycle completes — show a calm waiting state, not an error.
  const cycleInFlight = normalized === 'blocked' && reasonCode === 'cycle_in_flight';
  const active = (sessionActive || normalized === 'active') && !runnerOffline && !cycleInFlight;
  const detail = humanizeResidentReason(reason, selectedDeviceName);
  if (cycleInFlight) {
    return {
      tone: 'info',
      Icon: LoaderCircle,
      label: 'Waiting',
      detail: detail ?? 'The current strategic cycle is running. The session will start automatically when it finishes.',
      showStop: false,
      showStart: false,
      showAddRunner: false,
    };
  }
  if (runnerOffline) {
    return {
      tone: 'warning',
      Icon: AlertTriangle,
      label: 'Device offline',
      detail: detail ?? 'Device offline — restart the session to pick another device',
      // Keep Stop so the operator can restart (Stop → re-pick device → Start).
      showStop: hasStop,
      showStart: false,
      showAddRunner: false,
    };
  }
  if (active) {
    return {
      tone: 'success',
      Icon: InfinityIcon,
      label: sessionStatusLabel(sessionStatus),
      detail: 'Runner attached / replying',
      showStop: hasStop,
      showStart: false,
      showAddRunner: false,
    };
  }
  if (normalized === 'starting') {
    return {
      tone: 'info',
      Icon: LoaderCircle,
      label: 'Starting',
      detail: detail ?? 'Bringing up session, agent not yet streaming',
      showStop: hasStop,
      showStart: false,
      showAddRunner: false,
    };
  }
  if (normalized === 'error') {
    return {
      tone: 'danger',
      Icon: AlertTriangle,
      label: 'Start failed',
      detail: detail ?? 'Resident session could not start',
      showStop: false,
      showStart: hasStart,
      showAddRunner: false,
    };
  }
  if (normalized === 'blocked') {
    const noRunner = normalizedStatus(reason) === 'no_runner';
    return {
      tone: 'warning',
      Icon: AlertTriangle,
      label: 'Not running',
      detail: detail ?? 'Resident session unavailable',
      showStop: false,
      showStart: hasStart,
      showAddRunner: noRunner && hasAddRunner,
    };
  }
  // `off` (feature flag off) — also tolerate the legacy `dark` value.
  if (normalized === 'off' || normalized === 'dark') {
    return {
      tone: 'neutral',
      Icon: InfinityIcon,
      label: 'Sessions off',
      detail: detail ?? 'Resident sessions disabled',
      showStop: false,
      showStart: false,
      showAddRunner: false,
    };
  }
  // `ready` (or unknown): Workday on with a live runner, auto-starting on entry.
  return {
    tone: 'info',
    Icon: InfinityIcon,
    label: 'Ready',
    detail: 'Starts on Workday entry',
    showStop: false,
    showStart: hasStart,
    showAddRunner: false,
  };
}

/**
 * capture_rate (integer percent) below this is toned as a warning in the chip.
 * Mirrors the backend enforcement floor (`OPERATOR_DECISION_CAPTURE_LOW_RATE_PCT`
 * in `strategic_resident_session.py`) so the operator-visible signal and the
 * strategist-internal notice agree on what "chronically low" means.
 */
const OPERATOR_DECISION_LOW_RATE_PCT = 60;

interface OperatorDecisionCaptureView {
  tone: Tone;
  label: string;
  detail: string;
}

/**
 * Build the operator-decision capture chip view from the session summary, or
 * `null` when there is nothing to show (absent summary, or no operator turns
 * measured yet). The chip is warning-toned when the rate is at/below the low
 * floor and neutral otherwise.
 */
function operatorDecisionCaptureView(
  summary:
    | { total: number; captured: number; missed: number; capture_rate: number | null }
    | null
    | undefined,
): OperatorDecisionCaptureView | null {
  if (!summary || typeof summary.total !== 'number' || summary.total <= 0) return null;
  const { total, captured, capture_rate } = summary;
  const rate = typeof capture_rate === 'number' ? capture_rate : null;
  const low = rate !== null && rate < OPERATOR_DECISION_LOW_RATE_PCT;
  const ratePart = rate !== null ? ` (${rate}%)` : '';
  return {
    tone: low ? 'warning' : 'neutral',
    label: `Op-decision capture: ${captured}/${total}${ratePart}`,
    detail: low
      ? 'Capture chronically low — operator corrections may not be reaching the durable decisions register'
      : 'Operator corrections captured into the durable decisions register',
  };
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
  sessionActive = false,
  sessionStatus = null,
  sessionState = null,
  sessionReason = null,
  sessionBusy = false,
  sessionRunners = null,
  sessionSelectedRunnerId = null,
  operatorDecisionSummary = null,
  onSelectRunner,
  onStartSession,
  onStopSession,
  onModeChange,
  onOpenDevices,
  onOpenCompute,
  onOpenAccounts,
  onOpenSpend,
  showMonitoringControls = true,
  density = 'default',
  hideDetailsOnMobile = false,
  className = '',
}: StrategicSessionControlProps) {
  const compact = density === 'compact';
  const selectedMode = currentMode ?? mode ?? 'chat';
  const runnerStatus = localRunnerStatus ?? localRunner;
  const workStatus = runtimeWorkStatus ?? runtimeWork;
  const openAccountSpend = onOpenAccounts ?? onOpenSpend;
  const accountSpendOpenLabel = onOpenAccounts ? 'Open accounts' : 'Open spend';

  // Resident session status: only meaningful in the "Workday" (continuous) mode.
  // Entering Workday is the on-switch (the backend auto-starts the session), so
  // the primary affordance is a legible status chip — off / ready / starting /
  // active / blocked / error — that always shows the reason when it can't start.
  // Stop stays on a live session; a manual Start remains only as an explicit
  // fallback, and `blocked:no_runner` surfaces an "Add runner" fix-it link.
  // Shown when the container wired session info (callbacks / active / explicit
  // state); absent that, this stays inert so callers that don't use resident
  // sessions render exactly as before.
  // Multi-device picker state. `runnerOptions` are the enrolled devices; the
  // selected/pinned device drives the name shown in the block-on-offline prompt
  // and the dropdown's current value. Auto-pick is the empty selection.
  const runnerOptions = Array.isArray(sessionRunners) ? sessionRunners : [];
  const selectedRunnerId = cleanText(sessionSelectedRunnerId);
  const selectedRunner = selectedRunnerId
    ? runnerOptions.find(option => cleanText(option.id) === selectedRunnerId) ?? null
    : null;
  const selectedDeviceName = selectedRunner
    ? cleanText(selectedRunner.name)
    : null;
  const showDevicePicker =
    selectedMode === 'continuous' && Boolean(onSelectRunner) && runnerOptions.length > 0;

  const sessionView = residentSessionView(
    sessionState,
    sessionReason,
    sessionActive,
    sessionStatus,
    Boolean(onStartSession),
    Boolean(onStopSession),
    Boolean(onOpenDevices),
    selectedDeviceName,
  );
  const showSessionControl = selectedMode === 'continuous' && (
    Boolean(onStartSession) || Boolean(onStopSession) || sessionActive || Boolean(cleanText(sessionState)) || showDevicePicker
  );
  const SessionIcon = sessionView.Icon;
  const captureView = operatorDecisionCaptureView(operatorDecisionSummary);

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
      className={`flex min-w-0 text-neutral-100 ${compact ? 'flex-row items-center justify-end gap-1' : 'flex-col gap-1.5 sm:flex-row sm:items-center sm:justify-end'} ${className}`}
      data-testid="strategic-session-control"
    >
      <div
        className={`${compact ? 'flex w-auto' : 'grid min-w-0 grid-cols-2 sm:flex sm:w-auto'} gap-0.5 rounded border border-neutral-800 bg-neutral-950/80 p-0.5`}
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
              className={`inline-flex min-w-0 items-center justify-center gap-1.5 rounded text-xs font-semibold transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-cyan-400 ${
                compact ? 'h-7 w-7 px-0 py-0' : 'min-h-11 px-2 py-1.5 sm:min-h-8 sm:px-2'
              } ${
                selected
                  ? 'bg-neutral-800 text-neutral-100 ring-1 ring-neutral-700'
                  : 'text-neutral-400 hover:bg-neutral-900 hover:text-neutral-100'
              } disabled:cursor-not-allowed disabled:opacity-50`}
            >
              <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
              <span className={compact ? 'sr-only' : 'truncate'}>{option.label}</span>
            </button>
          );
        })}
      </div>

      <div className={`${hideDetailsOnMobile ? 'max-md:hidden ' : ''}${compact ? 'flex flex-row items-center justify-end gap-1' : 'flex min-w-0 flex-col gap-1.5 sm:flex-row sm:flex-wrap sm:items-center sm:justify-end'}`}>
        {showSessionControl && (
          <>
            <span
              className={`inline-flex min-w-0 shrink-0 items-center gap-1.5 rounded border px-2 ${
                compact ? 'h-7 py-0' : 'min-h-11 py-1.5 sm:min-h-8'
              } ${toneClasses[sessionView.tone].border} ${toneClasses[sessionView.tone].bg}`}
              role="status"
              aria-label={`Resident Strategic session: ${sessionView.label}${
                sessionView.detail ? `, ${sessionView.detail}` : ''
              }`}
              title={
                sessionView.detail
                  ? `Resident Strategic session: ${sessionView.label} — ${sessionView.detail}`
                  : `Resident Strategic session: ${sessionView.label}`
              }
            >
              <SessionIcon className={`h-3.5 w-3.5 shrink-0 ${toneClasses[sessionView.tone].icon}`} aria-hidden="true" />
              <StatusDot tone={sessionView.tone} />
              {!compact && (
                <span className="flex min-w-0 flex-col leading-tight">
                  <span className={`truncate text-xs font-semibold ${toneClasses[sessionView.tone].text}`}>
                    {sessionView.label}
                  </span>
                  {sessionView.detail && (
                    <span className="truncate text-ui-sm text-neutral-400">{sessionView.detail}</span>
                  )}
                </span>
              )}
            </span>
            {captureView && (
              <span
                className={`inline-flex min-w-0 shrink-0 items-center gap-1.5 rounded border px-2 ${
                  compact ? 'h-7 py-0' : 'min-h-11 py-1.5 sm:min-h-8'
                } ${toneClasses[captureView.tone].border} ${toneClasses[captureView.tone].bg}`}
                role="status"
                aria-label={`${captureView.label} — ${captureView.detail}`}
                title={`${captureView.label} — ${captureView.detail}`}
              >
                <StatusDot tone={captureView.tone} />
                {!compact && (
                  <span className={`truncate text-xs font-semibold ${toneClasses[captureView.tone].text}`}>
                    {captureView.label}
                  </span>
                )}
              </span>
            )}
            {showDevicePicker && (
              <label
                className={`inline-flex min-w-0 shrink-0 items-center gap-1.5 rounded border border-neutral-800 bg-neutral-950/70 px-2 ${
                  compact ? 'h-7 py-0' : 'min-h-11 py-1.5 sm:min-h-8'
                }`}
                title={
                  selectedRunner
                    ? `Workday device: ${selectedRunner.name}${selectedRunner.online ? '' : ' (offline)'}`
                    : 'Workday device: auto-pick an available device'
                }
              >
                <Server
                  className={`h-3.5 w-3.5 shrink-0 ${
                    selectedRunner
                      ? selectedRunner.online
                        ? 'text-emerald-400'
                        : 'text-amber-400'
                      : 'text-neutral-500'
                  }`}
                  aria-hidden="true"
                />
                {!compact && (
                  <span className="shrink-0 font-mono text-ui-sm font-semibold uppercase text-neutral-500">
                    Device
                  </span>
                )}
                <select
                  aria-label="Workday device"
                  value={selectedRunnerId ?? ''}
                  disabled={sessionBusy || !onSelectRunner}
                  onChange={event =>
                    onSelectRunner?.(event.target.value ? event.target.value : null)
                  }
                  className="min-w-0 max-w-[9rem] truncate bg-transparent text-xs font-semibold text-neutral-200 focus:outline-none disabled:cursor-not-allowed disabled:opacity-60"
                >
                  <option value="">Auto (recommended)</option>
                  {runnerOptions.map(option => (
                    <option key={option.id} value={option.id}>
                      {runnerOptionLabel(option)}
                    </option>
                  ))}
                </select>
              </label>
            )}
            {sessionView.showStop && (
              <button
                type="button"
                onClick={onStopSession}
                disabled={!onStopSession || sessionBusy}
                className={`group inline-flex min-w-0 shrink-0 items-center gap-2 rounded border border-red-500/30 bg-red-500/10 text-left transition-colors hover:border-red-400/50 hover:bg-red-500/15 focus-visible:outline focus-visible:outline-2 focus-visible:outline-cyan-400 disabled:cursor-not-allowed disabled:opacity-60 ${
                  compact ? 'h-7 px-2 py-0' : 'min-h-11 px-2 py-1.5 sm:min-h-8'
                }`}
                aria-label="Stop resident Strategic session"
                title="Stop resident Strategic session"
              >
                {sessionBusy ? (
                  <LoaderCircle className="h-3.5 w-3.5 shrink-0 animate-spin text-red-300" aria-hidden="true" />
                ) : (
                  <Square className="h-3.5 w-3.5 shrink-0 text-red-400" aria-hidden="true" />
                )}
                <span className={compact ? 'sr-only' : 'truncate text-xs font-semibold text-red-300'}>Stop</span>
              </button>
            )}
            {sessionView.showStart && (
              <button
                type="button"
                onClick={onStartSession}
                disabled={!onStartSession || sessionBusy}
                className={`group inline-flex min-w-0 shrink-0 items-center gap-2 rounded border border-emerald-500/30 bg-emerald-500/10 text-left transition-colors hover:border-emerald-400/50 hover:bg-emerald-500/15 focus-visible:outline focus-visible:outline-2 focus-visible:outline-cyan-400 disabled:cursor-not-allowed disabled:opacity-60 ${
                  compact ? 'h-7 px-2 py-0' : 'min-h-11 px-2 py-1.5 sm:min-h-8'
                }`}
                aria-label="Start resident Strategic session"
                title="Start resident Strategic session"
              >
                {sessionBusy ? (
                  <LoaderCircle className="h-3.5 w-3.5 shrink-0 animate-spin text-emerald-300" aria-hidden="true" />
                ) : (
                  <Play className="h-3.5 w-3.5 shrink-0 text-emerald-400" aria-hidden="true" />
                )}
                <span className={compact ? 'sr-only' : 'truncate text-xs font-semibold text-emerald-300'}>Start session</span>
              </button>
            )}
            {sessionView.showAddRunner && (
              <button
                type="button"
                onClick={onOpenDevices}
                className={`group inline-flex min-w-0 shrink-0 items-center gap-2 rounded border border-amber-500/30 bg-amber-500/10 text-left transition-colors hover:border-amber-400/50 hover:bg-amber-500/15 focus-visible:outline focus-visible:outline-2 focus-visible:outline-cyan-400 ${
                  compact ? 'h-7 px-2 py-0' : 'min-h-11 px-2 py-1.5 sm:min-h-8'
                }`}
                aria-label="Add a local runner: open Devices"
                title="Add a local runner: open Devices to enroll one"
              >
                <Server className="h-3.5 w-3.5 shrink-0 text-amber-400" aria-hidden="true" />
                <span className={compact ? 'sr-only' : 'truncate text-xs font-semibold text-amber-300'}>Add runner</span>
              </button>
            )}
          </>
        )}

        {saving && (
          <span
            className={`inline-flex min-w-0 items-center gap-2 rounded border border-cyan-500/25 bg-neutral-950/70 font-mono text-ui-sm font-semibold uppercase text-cyan-300 ${
              compact ? 'h-7 px-2 py-0' : 'min-h-11 px-2 py-1.5 sm:min-h-8'
            }`}
            role="status"
            aria-label="Saving Strategic session mode"
            title="Saving Strategic session mode"
          >
            <LoaderCircle className="h-3.5 w-3.5 shrink-0 animate-spin" aria-hidden="true" />
            <span className={compact ? 'sr-only' : undefined}>Saving</span>
          </span>
        )}

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
