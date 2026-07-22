/* eslint-disable react-hooks/refs, react-hooks/set-state-in-effect */
import { memo, useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { ConversationUtilityBar } from './ConversationUtilityBar';
import { useChatChrome } from '../hooks/useChatChrome';
import { auth } from '../firebase';
import { currentUiSurface, reportUiLoadTiming } from '../uiLoadTiming';
import { apiFetch, API_BASE_URL, authHeaders, type StrategicRefreshMarker, acceptPendingConfirmation, dismissPendingConfirmation } from '../api';
import type { StrategicCycleProgress, StrategicCycleCompletion, StrategicSessionDelta } from '../hooks/useLogViewer';
import { parseTimestamp, timeAgo } from '../utils/time';
import { type EventPref, saveEventPrefs, effectiveSignal, getToastSeverity, countHighSignalMessages } from './eventPrefs';
import { type TemporaryMutes } from './temporaryMutes';
import { loadLastSeen, saveLastSeen } from './lastSeen';
import { saveServerNoisePrefs } from './serverSync';
import type { MessageAttachment, ProjectMessage } from '../types/messages';
import { computeStrategicStreamingTurn } from '../utils/strategicSessionLive';
import { StrategicStreamingBubble } from './StrategicLiveTurn';
import { AlertsBanner } from './AlertsBanner';
import type { Alert } from '../types/alert';
import { SaveViewDialog } from './SaveViewDialog';
import { useMessageList } from './useMessageList';
import { useMessagesPanelFilters } from './useMessagesPanelFilters';
import { MessagesListView, type ConversationListImperativeApi } from './MessagesListView';
import { MessagesPanelHeader, type MessagesPanelFilterChrome, type WebhookStats } from './MessagesPanelHeader';
import { MessagesComposer, type PendingAttachment } from './MessagesComposer';
import { armingToastCopy, parseArmingOutcome, conversationAcceptanceToastCopy, type ArmingOutcome, WORKDAY_CAPABILITY_HINT, WORKDAY_STARTER_PROMPTS } from './messageArming';
import { MessagesBatchActionBar } from './MessagesBatchActionBar';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { generateIdempotencyKey } from '../utils/idempotencyKey';
import { StrategicChallengeChip } from './StrategicChallengeChip';
import { useDirectionForm } from '../hooks/useDirectionForm';
import { useConversationSend } from '../hooks/useConversationSend';
import { compareConversationMessages, useConversation } from '../hooks/useConversation';
import { useStrategistStatus } from '../hooks/useStrategistStatus';
import { StrategistStatusChip } from './StrategistStatusChip';
import { DirectionsAddForm } from './DirectionsAddForm';
import {
  createStrategicSessionMessage,
  deleteMessageAttachmentUpload,
  findStrategicMessageByClientIdentity,
  StrategicSessionMessageCreateError,
  type StrategicContextPackageDescriptor,
  type StrategicMessageTarget,
  type StrategicStateResponse,
  type StrategicWorkbenchState,
} from '../strategicStateApi';
import { buildStrategicContextDescriptor, SESSION_IDLE_TTL_MS } from '../utils/strategicContextDescriptor';
import { messageHasClientIdentity, mergeCanonicalConversationMessage } from '../utils/conversationMessageIdentity';
import { clearComposerDraftIfCurrent, loadComposerDraft, saveComposerDraft } from '../utils/conversationDraftState';
import { finalizeLastAttachmentRemoval, type ConversationComposerIntent, updateConversationComposerIntent } from '../utils/conversationAttachmentDraftFinalization';
import { ComposerRequestOwnershipGuard } from '../utils/composerRequestOwnership';
import { ConversationAttachmentUploadError, uploadConversationAttachments } from '../utils/conversationAttachmentUpload';
import {
  clearPendingAttachments as clearStoredPendingAttachments,
  deletePendingAttachment as deleteStoredPendingAttachment,
  listPendingAttachments as listStoredPendingAttachments,
  putPendingAttachment as putStoredPendingAttachment,
  retryPendingAttachmentCleanups,
} from '../utils/pendingAttachmentStore';
const DEFAULT_VIEW_ID = '__default__';
const MESSAGE_CONTEXT_SELECTED_REF_DESCRIPTOR_LIMIT = 8;
// Mirrors MAX_IMAGE_DOWNLOAD_BYTES in the agent's strategic image handling.
// Images above this threshold are uploaded but the agent silently skips them.
const MAX_AGENT_IMAGE_BYTES = Math.floor(13_000_000 * 3 / 4);
// Receipt shown for at most 2 hours after a steer fires, preventing stale "picked up"
// messages that grow without bound, and blocking stale receipts from resurfacing after
// an operator cancels a fresh steer.
const STEER_RECEIPT_MAX_AGE_MS = 7_200_000;
const OPTIMISTIC_STRATEGIC_REFRESH_MAX_AGE_MS = 45_000;
const TRIAGE_PAGE_SIZE = 30;

function countStrategicReplies(conversationMessages: ProjectMessage[]): number {
  let n = 0;
  for (const msg of conversationMessages) {
    const ft = (msg.from_type || '').toLowerCase();
    if (ft === 'strategic' || ft === 'strategist') n += 1;
  }
  return n;
}
function artifactRefIdentity(ref: Record<string, unknown>): string {
  return String(ref.artifact_ref_id || ref.id || '');
}

function mergeArtifactRefs(
  ...groups: Array<Array<Record<string, unknown>>>
): Array<Record<string, unknown>> {
  const merged = new Map<string, Record<string, unknown>>();
  for (const ref of groups.flat()) {
    const identity = artifactRefIdentity(ref);
    if (identity) merged.set(identity, ref);
  }
  return [...merged.values()];
}
function getCompletionReceiptTone(status?: string | null): {
  banner: string;
  dot: string;
  text: string;
  message: string;
  label: string;
} {
  const normalizedStatus = (status || 'completed').toLowerCase();
  if (normalizedStatus === 'deferred') {
    return {
      banner: 'bg-amber-950/30',
      dot: 'bg-amber-400',
      text: 'text-amber-300',
      message: 'text-amber-400/80',
      label: 'Cycle deferred',
    };
  }
  if (normalizedStatus === 'failed') {
    return {
      banner: 'bg-red-950/30',
      dot: 'bg-red-400',
      text: 'text-red-300',
      message: 'text-red-400/80',
      label: 'Cycle failed',
    };
  }
  if (normalizedStatus === 'cancelled') {
    return {
      banner: 'bg-red-950/30',
      dot: 'bg-red-400',
      text: 'text-red-300',
      message: 'text-red-400/80',
      label: 'Cycle cancelled',
    };
  }
  return {
    banner: 'bg-emerald-950/30',
    dot: 'bg-emerald-400',
    text: 'text-emerald-300',
    message: 'text-emerald-400/80',
    label: 'Cycle complete',
  };
}
function viewSummaryLine(query: string, prefs: Record<string, EventPref>, sort: 'importance' | 'recent', showResolved: boolean, showOnlyNew?: boolean): string {
  const parts: string[] = [];
  parts.push(query.trim() ? `Search: ${query.trim().slice(0, 40)}` : 'No search');
  const muted = Object.values(prefs).filter(v => v === 'mute').length;
  const surfaced = Object.values(prefs).filter(v => v === 'surfaced').length;
  if (muted > 0) parts.push(`${muted} muted`);
  if (surfaced > 0) parts.push(`${surfaced} surfaced`);
  if (showOnlyNew) parts.push('only new');
  parts.push(`sort: ${sort}`);
  parts.push(showResolved ? 'resolved shown' : 'resolved hidden');
  return parts.join(' · ');
}
function contextPackageTextTone(status?: string | null): string {
  if (status === 'building') return 'text-cyan-300';
  if (status === 'stale') return 'text-amber-300';
  return 'text-emerald-300';
}
function workbenchModeLabel(mode?: string | null): string {
  if (mode === 'check_in') return 'Check-in';
  if (mode === 'monitor') return 'Monitor';
  if (mode === 'continuous') return 'Workday';
  return 'Chat';
}
function attentionTextTone(level?: string | null): string {
  if (level === 'needs_operator') return 'text-amber-300';
  if (level === 'watch') return 'text-neutral-300';
  return 'text-emerald-300';
}
/** Human-readable short label for an attention item's lifecycle.operator_action. */
function lifecycleOperatorActionLabel(operatorAction?: string | null): string {
  if (!operatorAction) return '';
  const labels: Record<string, string> = {
    reply_or_resolve_message: 'Reply or resolve',
    inspect_or_resolve_message: 'Inspect or resolve',
    approve_or_reject_task: 'Approve or reject',
    hand_merge_or_archive_task: 'Hand-merge or archive',
    connect_or_select_local_runner: 'Connect or select runner',
    recover_runner_or_retarget_task: 'Recover runner or retarget',
    recover_or_remove_runner: 'Recover or remove runner',
    inspect_runtime_or_requeue_task: 'Inspect runtime or requeue',
    inspect_dependency_or_requeue_task: 'Inspect dependency or requeue',
    remap_or_attest_criterion: 'Remap or attest',
    update_direction: 'Update direction',
    provide_credentials_or_dismiss: 'Provide credentials',
    grant_access_or_dismiss: 'Grant access',
    confirm_intent_change_or_dismiss: 'Confirm intent',
    answer_decision_or_dismiss: 'Answer decision',
    remap_direction_or_attest_criterion: 'Remap or attest',
    monitor_active_direction_progress: 'Monitor progress',
    register_criteria_or_correct_referenced_keys: 'Register criteria',
    re_attest_or_provide_evidence: 'Re-attest or provide evidence',
    re_attest_or_provide_machine_evidence: 'Re-attest or provide evidence',
    re_attest_or_provide_evidence_or_retry: 'Re-attest or retry',
    link_task_or_resolve_debt: 'Link or resolve debt',
    dismiss_or_fix_alert_source: 'Dismiss or fix',
    inspect_blocked_task: 'Inspect blocked task',
    inspect_parked_task: 'Inspect parked task',
    inspect_failed_task: 'Inspect failed task',
    inspect_experiment_result: 'Inspect result',
    inspect_source: 'Inspect source',
  };
  return labels[operatorAction] ?? operatorAction.replace(/_/g, ' ');
}
/** Human-readable label for an attention item's lifecycle.acknowledge_effect. */
function lifecycleAcknowledgeEffectLabel(effect?: string | null): string {
  if (!effect || effect === 'none') return 'Acknowledge';
  if (effect === 'clears_unread_only') return 'Mark as seen — removes from needs-action strip until next update';
  return `Acknowledge (${effect.replace(/_/g, ' ')})`;
}
/** Human-readable label for an attention item's lifecycle.clears_when. */
function lifecycleClearsLabel(clearsWhen?: string | null): string {
  if (!clearsWhen) return '';
  const labels: Record<string, string> = {
    message_resolved_or_deleted: 'Clears when the message is resolved or deleted',
    approval_resolved_or_task_terminal_or_archived: 'Clears when the approval is resolved or the task ends',
    task_archived_or_pr_merged_or_closed: 'Clears when the task is archived or the PR merges',
    eligible_runner_available_or_task_dispatches_expires_terminal_or_archived: 'Clears when an eligible runner is available or the task dispatches',
    runner_recovers_task_unpins_dispatches_terminal_or_archived: 'Clears when the runner recovers or the task dispatches',
    runner_heartbeats_back_to_live_or_is_deleted: 'Clears when the runner heartbeats or is removed',
    task_claims_healthy_runtime_recovers_terminal_or_archived: 'Clears when the task claims a healthy runtime',
    dependency_recovers_task_leaves_queue_terminal_or_archived: 'Clears when the dependency resolves or the task leaves queue',
    criterion_mapped_to_active_direction_or_operator_attested: 'Clears when the criterion is mapped or attested',
    confirmation_accepted_dismissed_or_credential_provided: 'Clears when accepted, dismissed, or credential provided',
    confirmation_accepted_dismissed_or_access_granted: 'Clears when accepted, dismissed, or access granted',
    confirmation_accepted_or_dismissed: 'Clears when accepted or dismissed',
    alert_dismissed_or_resolved_at_source: 'Clears when the alert is dismissed or resolved',
    task_unblocked_terminal_or_archived: 'Clears when the task is unblocked or ends',
    task_unparked_terminal_or_archived: 'Clears when the task is unparked or ends',
    task_requeued_archived_or_otherwise_resolved: 'Clears when the task is requeued or resolved',
    task_archived_or_outside_6h_window: 'Clears when the task is archived or outside the 6h window',
    criterion_covered_or_version_closed_or_attested: 'Clears when the criterion is covered, version closes, or attested',
    criterion_covered_or_version_closed: 'Clears when the criterion is covered or version closes',
    criteria_registered_or_prose_reconciled_or_version_closed: 'Clears when criteria are registered or version closes',
    criterion_covered_or_version_closed_or_re_attested: 'Clears when the criterion is covered, version closes, or re-attested',
    underlying_source_clears: 'Clears when the underlying source clears',
    direction_flag_cleared_or_direction_inactive: 'Clears when the direction flag is cleared or the direction goes inactive',
    debt_resolved_dismissed_or_linked_to_work: 'Clears when the debt is resolved, dismissed, or linked to work',
  };
  return labels[clearsWhen] ?? `Clears: ${clearsWhen.replace(/_/g, ' ')}`;
}
function attentionItemIcon(kind?: string): string {
  if (kind === 'message') return '💬';
  if (kind === 'confirmation') return '✋';
  if (kind === 'task') return '📋';
  if (kind === 'alert') return '🚨';
  if (kind === 'direction') return '🧭';
  if (kind === 'runner') return '🖥';
  return '📌';
}
function isMessageNeedingOperatorAttention(
  message: ProjectMessage,
  attention: StrategicStateResponse['attention'] | null | undefined,
): boolean {
  return attention?.items.some(item => (
    item.kind === 'message'
    && item.severity === 'needs_operator'
    && item.ref?.id === message.id
  )) ?? false;
}
function messageReplyFromLabel(msg: ProjectMessage): string {
  const fromType = (msg.from_type || '').toLowerCase();
  if (fromType === 'strategic' || fromType === 'strategist') return 'Strategic';
  if (fromType === 'agent') return 'Agent';
  if (fromType === 'system') return 'System';
  return 'Operator';
}
function canPrimeStrategicChatReply(msg: ProjectMessage): boolean {
  const fromType = (msg.from_type || '').toLowerCase();
  return (
    msg.status === 'open'
    && (fromType === 'agent' || fromType === 'strategic' || fromType === 'strategist')
  );
}
function isConversationMessage(
  msg: ProjectMessage,
  eventPrefs: Record<string, EventPref>,
  tempMutes: TemporaryMutes,
): boolean {
  const fromType = (msg.from_type || '').toLowerCase();
  const eventType = msg.context?.event_type;
  const strategicOrigin = fromType === 'strategic' || fromType === 'strategist';
  if (fromType === 'user' || fromType === 'operator') return true;
  if (strategicOrigin && !eventType) return true;
  if (msg.message_type === 'QUESTION' || msg.message_type === 'BLOCKER' || msg.message_type === 'BUG_REPORT' || msg.message_type === 'FEEDBACK') return true;
  if (eventType === 'question_answer' || eventType === 'feedback_acknowledgment') return true;
  return effectiveSignal(msg, eventPrefs, tempMutes) === 'high';
}
function buildMessageWorkbenchContext(
  state?: StrategicStateResponse | null,
  workbenchOverride?: StrategicWorkbenchState | null,
): Record<string, unknown> | null {
  const session = state?.work_session ?? null;
  const workbench = workbenchOverride ?? state?.workbench ?? null;
  if (!session && !workbench && !state?.context_package?.id) return null;
  const selectedRefs = workbench?.selected_refs ?? session?.selected_refs ?? [];
  const interactionMode = workbench?.interaction_mode ?? session?.mode ?? 'chat';
  const activeSurface = workbench?.active_surface ?? session?.active_surface ?? 'strategic';
  const sessionStartedAt = workbench?.session_started_at ?? session?.session_started_at ?? null;
  const lastInteractionAt = workbench?.last_interaction_at ?? session?.last_interaction_at ?? null;
  const contextPackageId = session?.context_package_id ?? state?.context_package?.id ?? null;
  const sessionFreshTs = lastInteractionAt ?? sessionStartedAt;
  const sessionIsFresh = sessionFreshTs != null && (() => {
    const parsed = parseTimestamp(sessionFreshTs);
    return !isNaN(parsed.getTime()) && Date.now() - parsed.getTime() <= SESSION_IDLE_TTL_MS;
  })();
  if (!sessionIsFresh) {
    return contextPackageId ? { version: 1, context_package_id: contextPackageId } : null;
  }
  const selectedRefKeys = new Set(
    selectedRefs
      .map(ref => `${ref.kind}:${ref.id}`)
      .filter(key => key.length > 1),
  );
  const selectedRefDescriptors = (session?.selected_ref_descriptors ?? [])
    .filter(ref => selectedRefKeys.has(`${ref.kind}:${ref.id}`))
    .slice(0, MESSAGE_CONTEXT_SELECTED_REF_DESCRIPTOR_LIMIT)
    .map(ref => ({
      kind: ref.kind,
      id: ref.id,
      label: ref.label,
      ...(ref.summary ? { summary: ref.summary } : {}),
      ...(ref.status ? { status: ref.status } : {}),
      ...(ref.target ? { target: ref.target } : {}),
    }));
  const contextPackage = state?.context_package ?? null;
  const contextPackageDescriptor = contextPackage
    ? {
        id: contextPackage.id,
        title: contextPackage.title,
        ...(contextPackage.label ? { label: contextPackage.label } : {}),
        ...(contextPackage.status ? { status: contextPackage.status } : {}),
        ...(contextPackage.summary ? { summary: contextPackage.summary } : {}),
      }
    : null;
  return {
    version: 1,
    interaction_mode: interactionMode,
    active_surface: activeSurface,
    ...(workbench && Object.prototype.hasOwnProperty.call(workbench, 'preferred_runner_id')
      ? { preferred_runner_id: workbench.preferred_runner_id ?? null }
      : {}),
    selected_refs: selectedRefs,
    ...(selectedRefDescriptors.length ? { selected_ref_descriptors: selectedRefDescriptors } : {}),
    session_started_at: sessionStartedAt,
    context_package_id: contextPackageId,
    ...(contextPackageDescriptor ? { context_package_descriptor: contextPackageDescriptor } : {}),
  };
}
function strategicRefreshFromReadModel(
  state?: StrategicStateResponse | null,
): StrategicRefreshMarker | null {
  if (!state) return null;
  const refresh = state.strategic.refresh;
  if (refresh.pending) return refresh.pending;
  if (refresh.last_fired_at && refresh.last_fired_origin === 'operator') {
    const ageMs = Date.now() - parseTimestamp(refresh.last_fired_at).getTime();
    if (!Number.isNaN(ageMs) && ageMs <= STEER_RECEIPT_MAX_AGE_MS) {
      return {
        last_fired_at: refresh.last_fired_at,
        last_fired_origin: refresh.last_fired_origin,
      };
    }
  }
  return null;
}
export type MessagesPanelVariant = 'modal' | 'page';
export interface MessagesPanelProps {
  selectedProjectId: string | null;
  githubRepo?: string | null;
  defaultBranch?: string;
  onClose?: () => void;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  onMessageCountChange?: (count: number) => void;
  onOpenTask?: (taskId: string) => void;
  notifyOnHighSignalMessage?: boolean;
  /**
   * `modal` preserves the legacy full header when `onClose` is supplied.
   * `page` makes MessagesPanel suitable as a primary page: no close button by
   * default and compact filter/saved-view chrome unless overridden below.
   */
  variant?: MessagesPanelVariant;
  /** Controls whether filter/search/saved-view tools are always visible or collapsed behind a header toggle. */
  filterChrome?: MessagesPanelFilterChrome;
  /** Overrides the close button default derived from `variant` and `onClose`. */
  showCloseButton?: boolean;
  /** Live signal: whether a strategic cycle is currently running for this project. */
  strategistActive?: boolean;
  /** Phase-level detail about the currently running cycle, if any. */
  cycleProgress?: StrategicCycleProgress | null;
  /** Outcome receipt from the most recently completed cycle, shown until the next cycle begins. */
  lastCycleCompletion?: StrategicCycleCompletion | null;
  /** Incrementing signal from the workbench shell to focus the Strategic composer. */
  focusComposerSignal?: number;
  /** Incrementing signal from realtime events to refresh messages immediately. */
  realtimeRefreshSignal?: number;
  /** Canonical message projection received over realtime, reconciled immediately by sequence/client identity. */
  realtimeCanonicalMessage?: ProjectMessage | null;
  /** The realtime projection was transport-truncated and must be fetched by canonical message id. */
  realtimeCanonicalRefetchRequired?: boolean;
  /** Increments when the strategic-state poll completes with new data.
   *  When set, the conversation refresh piggybacks on the strategic-state
   *  poll cadence instead of running its own independent timer. */
  strategicPollEpoch?: number;
  /** Compact Strategic context descriptor, shown only in the minimal chat surface. */
  contextPackage?: StrategicContextPackageDescriptor | null;
  /** Strategic workbench read model, used by the minimal chat surface. */
  strategicState?: StrategicStateResponse | null;
  /** Optimistic workbench session state from the shell; overrides stale read-model workbench refs. */
  strategicWorkbenchState?: StrategicWorkbenchState | null;
  /** One-shot target from the Strategic rail, used to reveal/reply to a specific message. */
  targetMessage?: StrategicMessageTarget | null;
  /** Called once the panel has consumed targetMessage. */
  onTargetMessageHandled?: () => void;
  /** Refresh the Strategic read model after a chat action changes Strategic state. */
  onStrategicStateRefresh?: () => void | Promise<void>;
  /** Record a successful operator interaction with Strategic workbench state. */
  onStrategicInteraction?: () => void | Promise<void>;
  /** Open the Strategic context workspace from the minimal Strategic composer. */
  onOpenStrategicContextWorkspace?: () => void;
  /** Open the global command palette from the Strategic composer. */
  onOpenCommandPalette?: () => void;
  /** Open the project Activity inbox from the calm operation status. */
  onOpenActivityFeed?: () => void;
  /** Backwards-compatible alias for older call sites. */
  onOpenStrategicContextCommands?: () => void;
  /** Legacy refresh callback kept for callers that still wire the old prop name. */
  onStrategicRefresh?: () => void;
  /** Ephemeral Workday transcript deltas for the in-flight Strategic reply. */
  strategicSessionTranscript?: StrategicSessionDelta[];
  /** Active alerts surfaced inline at the top of the panel, replacing the separate AlertsPopover. */
  alerts?: Alert[];
  onDismissAlert?: (id: string) => void;
  onDismissAllAlerts?: () => void;
  onViewAlertTask?: (taskId: string) => void;
}
async function fetchBackendLastSeen(projectId: string): Promise<number | null> {
  try {
    const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/last-seen?project_id=${encodeURIComponent(projectId)}`);
    if (!res.ok) return null;
    const data = await res.json() as { last_seen_at?: number | null };
    const val = data.last_seen_at;
    return typeof val === 'number' && val > 0 ? val : null;
  } catch {
    return null;
  }
}
async function postBackendLastSeen(projectId: string, timestamp: number): Promise<void> {
  try {
    await apiFetch(`${API_BASE_URL}/api/admin/messages/last-seen`, {
      method: 'PUT',
      body: JSON.stringify({ project_id: projectId, timestamp }),
    });
  } catch { /* ignore — localStorage already saved */ }
}
function messagesEqual(a: ProjectMessage, b: ProjectMessage): boolean {
  return a.id === b.id
    && a.project_id === b.project_id
    && a.message_type === b.message_type
    && a.from_type === b.from_type
    && a.from_task_id === b.from_task_id
    && a.content === b.content
    && a.status === b.status
    && a.resolution === b.resolution
    && a.resolved_at === b.resolved_at
    && a.created_at === b.created_at
    && a.updated_at === b.updated_at
    && a.actor_user_id === b.actor_user_id
    && a.sequence === b.sequence
    && a.surface === b.surface
    && a.handling_state === b.handling_state
    && a.handled_by_task_id === b.handled_by_task_id
    && a.reply_to_message_id === b.reply_to_message_id
    && a.client_idempotency_key === b.client_idempotency_key
    && a.idempotency_key === b.idempotency_key
    && JSON.stringify(a.context ?? {}) === JSON.stringify(b.context ?? {});
}
function messageListsEqual(a: ProjectMessage[], b: ProjectMessage[]): boolean {
  return a.length === b.length && a.every((message, i) => messagesEqual(message, b[i]));
}
function revokePendingAttachmentPreviews(attachments: PendingAttachment[]): void {
  for (const attachment of attachments) {
    if (attachment.previewUrl && typeof URL.revokeObjectURL === 'function') {
      URL.revokeObjectURL(attachment.previewUrl);
    }
  }
}
// Build a local object-URL preview for image files small enough to render
// inline; larger files and non-images get no preview (null).
function makePendingPreviewUrl(file: File): string | null {
  if (file.type?.startsWith('image/') && file.size < 10_000_000 && typeof URL.createObjectURL === 'function') {
    try {
      return URL.createObjectURL(file);
    } catch {
      return null;
    }
  }
  return null;
}
export const MessagesPanel = memo(function MessagesPanel({ selectedProjectId, githubRepo, defaultBranch, onClose, addToast, onMessageCountChange, onOpenTask, notifyOnHighSignalMessage, variant, filterChrome, showCloseButton, strategistActive, cycleProgress, lastCycleCompletion, focusComposerSignal, realtimeRefreshSignal, realtimeCanonicalMessage, realtimeCanonicalRefetchRequired, strategicPollEpoch, contextPackage, strategicState, strategicWorkbenchState, targetMessage, onTargetMessageHandled, onStrategicStateRefresh, onStrategicInteraction, onOpenStrategicContextWorkspace, onOpenCommandPalette, onOpenStrategicContextCommands, onStrategicRefresh, strategicSessionTranscript, alerts, onDismissAlert, onDismissAllAlerts, onViewAlertTask }: MessagesPanelProps) {
  const resolvedVariant = variant ?? (onClose ? 'modal' : 'page');
  const resolvedFilterChrome = filterChrome ?? (resolvedVariant === 'page' ? 'compact' : 'full');
  const resolvedShowCloseButton = showCloseButton ?? (resolvedVariant !== 'page' && Boolean(onClose));
  const conversationMode = resolvedFilterChrome === 'minimal';
  const currentActorUserId = auth.currentUser?.uid ?? auth.currentUser?.email ?? null;
  const currentStrategicState = strategicState?.project_id === selectedProjectId ? strategicState : null;
  const usesStrategicReadModel = currentStrategicState != null;
  const effectiveContextPackage = contextPackage ?? currentStrategicState?.context_package ?? null;
  const workbenchState = strategicWorkbenchState ?? currentStrategicState?.workbench ?? null;
  const strategicAttention = currentStrategicState?.attention ?? null;
  const strategicAttentionRef = useRef(strategicAttention);
  // eslint-disable-next-line react-hooks/refs
  strategicAttentionRef.current = strategicAttention;
  const refreshStrategicState = onStrategicStateRefresh ?? onStrategicRefresh;
  const recordStrategicInteraction = useCallback(() => {
    if (!onStrategicInteraction) return;
    Promise.resolve(onStrategicInteraction()).catch(() => {});
  }, [onStrategicInteraction]);
  const selectedProjectIdRef = useRef(selectedProjectId);
  // eslint-disable-next-line react-hooks/refs
  selectedProjectIdRef.current = selectedProjectId;
  const currentActorUserIdRef = useRef(currentActorUserId);
  currentActorUserIdRef.current = currentActorUserId;
  const conversationModeRef = useRef(conversationMode);
  // eslint-disable-next-line react-hooks/refs
  conversationModeRef.current = conversationMode;
  const targetMessageId = targetMessage?.messageId ?? null;
  const targetMessageIdRef = useRef<string | null>(targetMessageId);
  // eslint-disable-next-line react-hooks/refs
  targetMessageIdRef.current = targetMessageId;
  const strategicSessionPinnedTargetRef = useRef<{ projectId: string; messageId: string } | null>(null);
  const usesStrategicReadModelRef = useRef(usesStrategicReadModel);
  // eslint-disable-next-line react-hooks/refs
  usesStrategicReadModelRef.current = usesStrategicReadModel;
  const attentionNeeds = strategicAttention?.counts.needs_operator ?? 0;
  // Needs-action items derived from the canonical attention model — these replace
  // the client-side effectiveSignal re-derivation for the needs-action set.
  const attentionNeedsActionItems = useMemo(
    () => strategicAttention?.items.filter(item => item.severity === 'needs_operator') ?? [],
    [strategicAttention?.items],
  );
  // Set of message IDs that the server marks needs_operator. Used to converge
  // the list high-signal split onto the same canonical source as the badge and
  // toast surfaces so all four surfaces agree without client-side re-derivation.
  const needsOperatorMessageIds = useMemo(
    () => new Set(
      attentionNeedsActionItems
        .filter(item => item.kind === 'message')
        .map(item => item.ref.id),
    ),
    [attentionNeedsActionItems],
  );
  const [messages, setMessages] = useState<ProjectMessage[]>([]);
  const messagesRef = useRef<ProjectMessage[]>([]);
  messagesRef.current = messages;
  const [loading, setLoading] = useState(true);
  // Triage pagination: small initial page (~30) with load-older-on-demand
  const loadingOlderTriageRef = useRef(false);
  const triageOffsetRef = useRef(0);
  const [expandedConfirmationItemId, setExpandedConfirmationItemId] = useState<string | null>(null);
  const [resolvedConfirmationIds, setResolvedConfirmationIds] = useState<Set<string>>(() => new Set());
  const visibleAttentionNeedsActionItems = useMemo(
    () => attentionNeedsActionItems.filter(item => !resolvedConfirmationIds.has(item.id)),
    [attentionNeedsActionItems, resolvedConfirmationIds],
  );
  const [chatChromeExpanded, persistChatChrome] = useChatChrome();
  // Compose — initialized from the per-project draft (if any) so a reload restores it.
  const [newType, setNewType] = useState(() => loadComposerDraft(currentActorUserId, selectedProjectId)?.type ?? 'FEEDBACK');
  const [newContent, setNewContent] = useState(() => loadComposerDraft(currentActorUserId, selectedProjectId)?.content ?? '');
  const [draftSourceRefs, setDraftSourceRefs] = useState<Array<Record<string, unknown>>>(() => loadComposerDraft(currentActorUserId, selectedProjectId)?.source_refs ?? []);
  const [draftArtifactRefs, setDraftArtifactRefs] = useState<Array<Record<string, unknown>>>(() => loadComposerDraft(currentActorUserId, selectedProjectId)?.artifact_refs ?? []);
  const composerDraftWriteIdRef = useRef(loadComposerDraft(currentActorUserId, selectedProjectId)?.write_id ?? null);
  const [submitting, setSubmitting] = useState(false);
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const pendingAttachmentsRef = useRef<PendingAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  useEffect(() => {
    pendingAttachmentsRef.current = pendingAttachments;
  }, [pendingAttachments]);
  useEffect(() => () => {
    revokePendingAttachmentPreviews(pendingAttachmentsRef.current);
    pendingAttachmentsRef.current = [];
  }, []);
  // Synchronous in-flight guard. `submitting` state updates asynchronously, so
  // two rapid keypresses (double-Enter) can both read the pre-update value and
  // each fire a request before React re-renders. This ref is set synchronously
  // at the very top of handleSubmit and cleared in its finally, closing that
  // race window so a burst collapses to a single POST on the client side (the
  // server idempotency key is the backstop for retries that do reach it).
  const submittingGuardRef = useRef(new ComposerRequestOwnershipGuard());
  const attachmentMutationGuardRef = useRef(new ComposerRequestOwnershipGuard());
  const attachmentMutationGenerationRef = useRef(0);
  const [attachmentMutating, setAttachmentMutating] = useState(false);
  const pendingAttachmentRestoreRequestRef = useRef(0);
  const [restoringPendingAttachments, setRestoringPendingAttachments] = useState(false);
  const [replyTo, setReplyTo] = useState<{ id: string; content: string; from: string; message_type: string; status: string } | null>(() => loadComposerDraft(currentActorUserId, selectedProjectId)?.reply_to ?? null);
  const [highlightedMessageId, setHighlightedMessageId] = useState<string | null>(null);
  // INSTANT ACK — the operator's just-sent message, rendered optimistically at
  // the tail of the chat the moment they hit Send (before the POST round-trips),
  // then dropped once the durable message lands via fetchMessages. Reply-only /
  // resolve paths do not use it (those messages are the operator's answer to an
  // existing thread, not a new chat turn).
  // On mount, restore any in-flight optimistic bubble that was serialised to
  // sessionStorage before a tab switch unmounted this component. It is retained
  // until canonical acceptance so an unconfirmed send can be safely recovered.
  const conversationSend = useConversationSend(
    conversationMode ? selectedProjectId : null,
    conversationMode ? currentActorUserId : null,
  );
  const optimisticSend = conversationSend.pending;
  const sendState = conversationSend.sendState;
  const sendWakeupState = conversationSend.wakeupState;
  // "Workday's on it" working state: set the instant the operator sends, cleared
  // when a fresh strategist reply lands (count vs baseline — clock-skew-proof), a
  // `done` lifecycle frame arrives, or a safety timeout. Drives the live activity
  // strip so there is zero dead time between send and visible progress.
  const [workingSince, setWorkingSince] = useState<number | null>(null);
  const strategicReplyBaselineRef = useRef<number>(0);
  // Real arming outcome the server reported for the last send (null before any
  // send, or after the operator changes the message type so the composer reverts
  // to a fresh type-based prediction).
  const [lastArming, setLastArming] = useState<ArmingOutcome | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const focusComposerTextarea = useCallback(() => {
    window.setTimeout(() => {
      const el = textareaRef.current;
      if (!el) return;
      el.focus();
      if (!el.disabled) {
        try {
          el.setSelectionRange(el.value.length, el.value.length);
        } catch {
          // Ignore selection failures in non-browser test environments.
        }
      }
    }, 0);
  }, []);
  const effectiveNewType = conversationMode ? 'FEEDBACK' : newType;
  const composerIntentRef = useRef<ConversationComposerIntent>({ type: effectiveNewType, content: newContent, reply_to: replyTo, source_refs: draftSourceRefs, artifact_refs: draftArtifactRefs, attachment_send_key: conversationSend.identity });
  const updateComposerIntent = useCallback((patch: Partial<ConversationComposerIntent>) => (composerIntentRef.current = updateConversationComposerIntent(composerIntentRef.current, patch)), []);
  const saveComposerDraftSnapshot = useCallback((
    actorUserId: string,
    projectId: string,
    intent: ConversationComposerIntent,
  ) => {
    const previousWriteId = currentActorUserIdRef.current === actorUserId
      && selectedProjectIdRef.current === projectId
      ? composerDraftWriteIdRef.current
      : null;
    const result = saveComposerDraft(actorUserId, projectId, intent, undefined, previousWriteId);
    if (
      result !== false
      && currentActorUserIdRef.current === actorUserId
      && selectedProjectIdRef.current === projectId
    ) composerDraftWriteIdRef.current = result;
    return result !== false;
  }, []);
  const clearSubmittedComposerDraft = useCallback((
    actorUserId: string | null,
    projectId: string,
    writeId: string | null | undefined,
  ) => {
    if (!actorUserId) return false;
    const cleared = clearComposerDraftIfCurrent(actorUserId, projectId, writeId);
    if (
      cleared
      && currentActorUserIdRef.current === actorUserId
      && selectedProjectIdRef.current === projectId
      && composerDraftWriteIdRef.current === writeId
    ) composerDraftWriteIdRef.current = null;
    return cleared;
  }, []);
  const resetComposerIntent = useCallback(() => {
    updateComposerIntent({ content: '', reply_to: null, source_refs: [], artifact_refs: [], operator_intent: undefined, attachment_send_key: null });
    setNewContent(''); setDraftSourceRefs([]); setDraftArtifactRefs([]); setReplyTo(null);
  }, [updateComposerIntent]);
  const captureCurrentDraftRefs = useCallback(() => {
    // Context belongs to the message draft, never to whichever workbench surface
    // happens to be visible. Keep the exact explicit chips already in the
    // composer; merely viewing/selecting a workbench surface attaches nothing.
    const sourceRefs = composerIntentRef.current.source_refs;
    const explicitToolRefs = composerIntentRef.current.artifact_refs.filter(
      ref => typeof ref.artifact_ref_id === 'string',
    );
    return {
      sourceRefs,
      artifactRefs: mergeArtifactRefs(
        sourceRefs.filter(ref => ref.kind === 'artifact'),
        explicitToolRefs,
      ),
    };
  }, []);
  const persistComposerEnvelope = useCallback((overrides: {
    content?: string;
    type?: string;
    replyTo?: typeof replyTo;
    sourceRefs?: Array<Record<string, unknown>>;
    artifactRefs?: Array<Record<string, unknown>>;
    attachmentSendKey?: string | null;
  } = {}) => {
    if (!currentActorUserId || !selectedProjectId) return;
    const current = composerIntentRef.current;
    const next = updateComposerIntent({
      type: overrides.type ?? current.type, content: overrides.content ?? current.content,
      reply_to: overrides.replyTo === undefined ? current.reply_to : overrides.replyTo,
      source_refs: overrides.sourceRefs ?? current.source_refs, artifact_refs: overrides.artifactRefs ?? current.artifact_refs,
      operator_intent: current.operator_intent,
      attachment_send_key: overrides.attachmentSendKey === undefined ? current.attachment_send_key : overrides.attachmentSendKey,
    });
    saveComposerDraftSnapshot(currentActorUserId, selectedProjectId, next);
  }, [currentActorUserId, saveComposerDraftSnapshot, selectedProjectId, updateComposerIntent]);
  // Idempotency key for the in-flight draft. Generated once per draft and
  // reused across retries / a rapid double-submit so the backend collapses
  // them to one logical message; cleared after a successful send so the next
  // distinct message gets a fresh key.
  const idempotencyKeyRef = useRef<string | null>(null);
  // Per-message idempotency identity for the feedback→direction conversion.
  // Keyed by source message id so a retried (or batch-replayed) convert of the
  // same message reuses one key and the backend collapses it to a single
  // direction; cleared only after the message is resolved/created so a failed
  // attempt keeps its key for the retry.
  const directionIdempotencyKeysRef = useRef<Map<string, string>>(new Map());
  // Form instance for the convert-to-direction modal
  const convertForm = useDirectionForm();
  const [pendingConvertMsg, setPendingConvertMsg] = useState<ProjectMessage | null>(null);
  const [convertSubmitting, setConvertSubmitting] = useState(false);
  // Per-message action state
  const [resolvingId, setResolvingId] = useState<string | null>(null);
  const [resolveInput, setResolveInput] = useState('');
  const [deletingIds, setDeletingIds] = useState<Set<string>>(new Set());
  const [creatingDirectionIds, setCreatingDirectionIds] = useState<Set<string>>(new Set());
  const [investigatingId, setInvestigatingId] = useState<string | null>(null);
  const [investigatingIds, setInvestigatingIds] = useState<Set<string>>(new Set());
  // Batch selection
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [batchCreating, setBatchCreating] = useState(false);
  // Scroll state
  const scrollRef = useRef<HTMLDivElement>(null);
  const conversationListRef = useRef<ConversationListImperativeApi | null>(null);
  const [showScrollDown, setShowScrollDown] = useState(false);
  const prevMessageCountRef = useRef(0);
  const fetchMessagesRequestSeqRef = useRef(0);
  const lastRealtimeRefreshSignalRef = useRef<number | null>(null);
  const lastStrategicSessionTargetFetchRef = useRef<string | null>(null);
  const getStrategicSessionIncludeMessageId = useCallback((projectId: string | null, currentTargetMessageId: string | null): string | null => {
    if (!projectId) return null;
    if (currentTargetMessageId) return currentTargetMessageId;
    const pinnedTarget = strategicSessionPinnedTargetRef.current;
    return pinnedTarget?.projectId === projectId ? pinnedTarget.messageId : null;
  }, []);
  const {
    showResolved, setShowResolved,
    routineExpanded, setRoutineExpanded,
    criticalOnly, setCriticalOnly,
    expandedGroupKeys, setExpandedGroupKeys,
    eventPrefs, setEventPrefs,
    showPrefsPanel, setShowPrefsPanel,
    sortMode, setSortMode,
    searchQuery, setSearchQuery,
    muteWindow, setMuteWindowPersisted,
    tempMutes,
    showTempMuteMenu, setShowTempMuteMenu,
    savedViews,
    activeViewId,
    showSaveDialog,
    saveDialogMode,
    saveDialogName, setSaveDialogName,
    saveDialogDescription, setSaveDialogDescription,
    saveDialogError, setSaveDialogError,
    openKebabId, setOpenKebabId,
    kebabRef,
    showOnlyNew, setShowOnlyNew,
    needsActionOnly, setNeedsActionOnly,
    activeTabModified,
    updatePref,
    addTemporaryMute,
    applyView,
    openSaveDialog,
    openRenameDialog,
    closeSaveDialog,
    commitSaveDialog,
    deleteView,
  } = useMessagesPanelFilters(selectedProjectId);
  const [webhookStats, setWebhookStats] = useState<WebhookStats | null>(null);
  // Pending strategic-refresh marker (project.vision._strategic_refresh): set when
  // operator steering arms a cycle, cleared backend-side once the autonomous loop
  // consumes it. Drives the "strategic cycle pending" badge until then.
  const [strategicRefresh, setStrategicRefresh] = useState<StrategicRefreshMarker | null>(null);
  const optimisticStrategicRefreshRef = useRef<StrategicRefreshMarker | null>(null);
  const completionReceiptTone = lastCycleCompletion ? getCompletionReceiptTone(lastCycleCompletion.status) : null;
  const strategistStatus = useStrategistStatus({
    strategistActive,
    cycleProgress,
    lastCycleCompletion: undefined, // chip in MessagesPanel only covers active+pending; completion uses its own banner below
    pendingRefreshAt: strategicRefresh?.requested_at ?? null,
    lastFiredAt: !strategicRefresh?.requested_at ? (strategicRefresh?.last_fired_at ?? null) : null,
  });
  const showStrategistChip = !conversationMode && (strategistActive || Boolean(strategicRefresh));
  // In-flight guard for the "cancel armed steer" affordance on the pending badge.
  const [cancellingRefresh, setCancellingRefresh] = useState(false);
  const openMessageCommandSurface = useCallback(() => {
    focusComposerTextarea();
    if (conversationMode) onOpenCommandPalette?.();
  }, [conversationMode, focusComposerTextarea, onOpenCommandPalette]);
  // Last-seen tracking: dividerTime is the threshold; when operator scrolls past or timeout fires, we persist it
  const [dividerTime, setDividerTime] = useState<number | null>(null);
  const dividerTimeRef = useRef<number | null>(null);
  const dividerDismissedRef = useRef(false);
  const dismissDivider = useCallback(() => {
    if (!dividerDismissedRef.current && selectedProjectId) {
      const now = Date.now();
      setDividerTime(now);
      dividerTimeRef.current = now;
      saveLastSeen(auth.currentUser?.email, selectedProjectId, now);
      void postBackendLastSeen(selectedProjectId, now);
      dividerDismissedRef.current = true;
    }
  }, [selectedProjectId]);
  useEffect(() => {
    if (!usesStrategicReadModel) return;
    const readModelRefresh = strategicRefreshFromReadModel(currentStrategicState);
    const optimisticRefresh = optimisticStrategicRefreshRef.current;
    if (optimisticRefresh?.requested_at) {
      const optimisticAgeMs = Date.now() - parseTimestamp(optimisticRefresh.requested_at).getTime();
      const optimisticTime = parseTimestamp(optimisticRefresh.requested_at).getTime();
      const readModelTime = readModelRefresh?.requested_at
        ? parseTimestamp(readModelRefresh.requested_at).getTime()
        : readModelRefresh?.last_fired_at
          ? parseTimestamp(readModelRefresh.last_fired_at).getTime()
          : NaN;
      const keepFreshOptimistic =
        !Number.isNaN(optimisticAgeMs) &&
        optimisticAgeMs <= OPTIMISTIC_STRATEGIC_REFRESH_MAX_AGE_MS &&
        !Number.isNaN(optimisticTime) &&
        (Number.isNaN(readModelTime) || readModelTime < optimisticTime);
      if (keepFreshOptimistic) {
        setStrategicRefresh(optimisticRefresh);
        return;
      }
      optimisticStrategicRefreshRef.current = null;
    }
    setStrategicRefresh(readModelRefresh);
  }, [currentStrategicState, usesStrategicReadModel]);
  useEffect(() => {
    const optimisticRefresh = optimisticStrategicRefreshRef.current;
    if (!optimisticRefresh?.requested_at || strategicRefresh !== optimisticRefresh) return;
    const requestedAt = parseTimestamp(optimisticRefresh.requested_at).getTime();
    if (Number.isNaN(requestedAt)) {
      optimisticStrategicRefreshRef.current = null;
      setStrategicRefresh(strategicRefreshFromReadModel(currentStrategicState));
      return;
    }
    const delayMs = Math.max(
      0,
      requestedAt + OPTIMISTIC_STRATEGIC_REFRESH_MAX_AGE_MS - Date.now(),
    );
    const timer = window.setTimeout(() => {
      if (optimisticStrategicRefreshRef.current !== optimisticRefresh) return;
      optimisticStrategicRefreshRef.current = null;
      setStrategicRefresh(previous =>
        previous === optimisticRefresh
          ? strategicRefreshFromReadModel(currentStrategicState)
          : previous,
      );
    }, delayMs);
    return () => window.clearTimeout(timer);
  }, [currentStrategicState, strategicRefresh]);
  const handleShowOnlyNew = useCallback(() => {
    setShowOnlyNew(true);
    // Scroll to first new message on next render
    setTimeout(() => {
      const msgBubbles = scrollRef.current?.querySelectorAll('[data-testid="message-bubble"]');
      if (msgBubbles && msgBubbles.length > 0) {
        (msgBubbles[0] as HTMLElement).scrollIntoView({ behavior: 'smooth', block: 'start' });
      }
    }, 100);
  }, [setShowOnlyNew]);
  // Stable ref for addToast
  const addToastRef = useRef(addToast);
  // eslint-disable-next-line react-hooks/refs
  addToastRef.current = addToast;
  const getCurrentMessages = useCallback(() => messagesRef.current, []);
  const handleConversationError = useCallback((message: string) => {
    addToastRef.current(message, 'error');
  }, []);
  const scrollConversationMessageIntoView = useCallback(async (messageId: string): Promise<boolean> => {
    for (let attempt = 0; attempt < 20; attempt += 1) {
      const existing = scrollRef.current?.querySelector(`[data-message-id="${messageId}"]`);
      if (existing) {
        existing.scrollIntoView({ block: 'center', behavior: 'smooth' });
        return true;
      }
      if (!conversationModeRef.current) return false;
      if (await (conversationListRef.current?.scrollToMessage(messageId) ?? Promise.resolve(false))) return true;
      await new Promise<void>(resolve => window.setTimeout(resolve, 25));
    }
    return false;
  }, []);
  const conversationIncludeMessageId = getStrategicSessionIncludeMessageId(selectedProjectId, targetMessageId);
  const handleConversationTargetFetched = useCallback((messageId: string) => {
    lastStrategicSessionTargetFetchRef.current = messageId;
    if (strategicSessionPinnedTargetRef.current?.messageId === messageId) {
      strategicSessionPinnedTargetRef.current = null;
    }
  }, []);
  const {
    refresh: refreshConversation,
    loadOlder: loadOlderConversation,
    hasMore: conversationHasMore,
    loadingOlder: loadingOlderConversation,
  } = useConversation({
    enabled: conversationMode,
    projectId: selectedProjectId,
    actorUserId: currentActorUserId,
    includeMessageId: conversationIncludeMessageId,
    searchQuery,
    realtimeRefreshSignal,
    realtimeMessage: realtimeCanonicalMessage,
    realtimeMessageRequiresCanonicalRefetch: realtimeCanonicalRefetchRequired,
    strategicPollEpoch,
    getMessages: getCurrentMessages,
    setMessages,
    onLoadingChange: setLoading,
    onError: handleConversationError,
    onTargetFetched: handleConversationTargetFetched,
  });
  const notifyOnHighSignalMessageRef = useRef(notifyOnHighSignalMessage);
  // eslint-disable-next-line react-hooks/refs
  notifyOnHighSignalMessageRef.current = notifyOnHighSignalMessage;
  // Stable ref for the direction convert-form so the memoized convert handler
  // does not churn on the form hook's per-render object identity.
  const convertFormRef = useRef(convertForm);
  // eslint-disable-next-line react-hooks/refs
  convertFormRef.current = convertForm;
  // Track seen messages to detect new arrivals and avoid initial-load toast spam
  const seenMessageIdsRef = useRef<Set<string>>(new Set());
  const hasInitiallyLoadedRef = useRef(false);
  const eventPrefsRef = useRef(eventPrefs);
  // eslint-disable-next-line react-hooks/refs
  eventPrefsRef.current = eventPrefs;
  const tempMutesRef = useRef(tempMutes);
  // eslint-disable-next-line react-hooks/refs
  tempMutesRef.current = tempMutes;
  // Reset per-component state when switching projects so that the next
  // fetch is treated as an initial load for the new project (avoids a flood
  // of toasts for project B's pre-existing open BLOCKER/BUG/FEEDBACK items).
  // Filter/prefs state is reset by useMessagesPanelFilters.
  useEffect(() => {
    let active = true;
    seenMessageIdsRef.current = new Set();
    hasInitiallyLoadedRef.current = false;
    setWebhookStats(null);
    optimisticStrategicRefreshRef.current = null;
    if (!usesStrategicReadModelRef.current) setStrategicRefresh(null);
    // Reset triage pagination when switching projects
    loadingOlderTriageRef.current = false;
    triageOffsetRef.current = 0;
    const lastSeen = loadLastSeen(currentActorUserId, selectedProjectId);
    setDividerTime(lastSeen);
    dividerTimeRef.current = lastSeen;
    dividerDismissedRef.current = false;
    if (selectedProjectId) {
      // Prefer backend last-seen over localStorage — if backend has a more recent
      // timestamp (e.g. dismissed on another device), update the divider position.
      fetchBackendLastSeen(selectedProjectId).then(backendTs => {
        if (!active) return;
        if (backendTs !== null && backendTs > (dividerTimeRef.current ?? 0)) {
          setDividerTime(backendTs);
          dividerTimeRef.current = backendTs;
        } else if (dividerTimeRef.current === null) {
          // Cold-start bootstrap: operator has no prior last-seen (neither localStorage
          // nor backend). Record "now" so the since-last-visit feed activates on the
          // next visit. Does NOT fire if either source already has a baseline.
          const now = Date.now();
          setDividerTime(now);
          dividerTimeRef.current = now;
          saveLastSeen(currentActorUserId, selectedProjectId, now);
          void postBackendLastSeen(selectedProjectId, now);
        }
      });
    }
    // Reload composer draft for the new project — drafts are scoped per-project
    // so switching projects shows that project's in-progress draft, not another's.
    const draft = loadComposerDraft(currentActorUserId, selectedProjectId);
    const unresolvedSend = optimisticSend
      && (optimisticSend.state === 'sending' || optimisticSend.state === 'needs_retry')
      ? optimisticSend
      : null;
    const immutablePayload = unresolvedSend?.payload;
    const restoredContent = immutablePayload?.content ?? unresolvedSend?.content ?? draft?.content ?? '';
    const restoredType = immutablePayload?.message_type ?? unresolvedSend?.message_type ?? draft?.type ?? 'FEEDBACK';
    const restoredSourceRefs = immutablePayload?.source_refs ?? draft?.source_refs ?? [];
    const restoredArtifactRefs = immutablePayload?.artifact_refs ?? draft?.artifact_refs ?? [];
    const restoredOperatorIntent = immutablePayload?.operator_intent ?? draft?.operator_intent;
    const restoredReply = unresolvedSend?.reply_to ?? draft?.reply_to ?? null;
    const restoredAttachmentKey = unresolvedSend?.key ?? draft?.attachment_send_key ?? null;
    composerDraftWriteIdRef.current = unresolvedSend?.draft_write_id ?? draft?.write_id ?? null;
    updateComposerIntent({ type: restoredType, content: restoredContent, reply_to: restoredReply, source_refs: restoredSourceRefs, artifact_refs: restoredArtifactRefs, operator_intent: restoredOperatorIntent, attachment_send_key: restoredAttachmentKey });
    setNewContent(restoredContent);
    setNewType(restoredType);
    setDraftSourceRefs(restoredSourceRefs);
    setDraftArtifactRefs(restoredArtifactRefs);
    setReplyTo(restoredReply);
    conversationSend.adoptIdentity(restoredAttachmentKey);
    setMessages([]);
    messagesRef.current = [];
    setPendingAttachments(prev => {
      revokePendingAttachmentPreviews(prev);
      pendingAttachmentsRef.current = [];
      return [];
    });
    setUploading(false);
    setSubmitting(false);
    submittingGuardRef.current.reset();
    attachmentMutationGuardRef.current.reset();
    attachmentMutationGenerationRef.current += 1;
    setAttachmentMutating(false);
    pendingAttachmentRestoreRequestRef.current += 1;
    setRestoringPendingAttachments(false);
    return () => { active = false; };
  }, [currentActorUserId, selectedProjectId, updateComposerIntent]);
  useEffect(() => {
    if (!focusComposerSignal) return;
    focusComposerTextarea();
  }, [focusComposerSignal, focusComposerTextarea]);
  // Reset triage pagination offset when search query changes so the first page
  // reflects the newest matching messages.
  const prevSearchQueryRef = useRef(searchQuery);
  useEffect(() => {
    if (prevSearchQueryRef.current !== searchQuery) {
      prevSearchQueryRef.current = searchQuery;
      triageOffsetRef.current = 0;
      loadingOlderTriageRef.current = false;
    }
  }, [searchQuery]);
  // Memoized message-list derivation: open/resolved split, high-signal vs routine,
  // pinned blockers/questions, auto-promoted failing groups, visible-resolved, and
  // new-message counts — all behind one pure hook so the panel stays focused on
  // state, effects, and handlers.
  const {
    openMessages,
    linkedMessages,
    resolvedMessages,
    openFeedbackMessages,
    newHighSignalCount,
    newMessageCounts,
    totalNewCount,
    muteActive,
    highSignalOpen,
    routineOpen,
    pinnedBlockers,
    pinnedQuestions,
    otherHighSignal,
    promotedGroups,
    remainingRoutineRows,
    autoExpandedKeys,
    visibleRoutineCount,
    mutedRoutineCount,
    visibleResolved,
    hiddenResolvedCount,
  } = useMessageList({
    messages,
    eventPrefs,
    tempMutes,
    muteWindow,
    searchQuery,
    sortMode,
    criticalOnly,
    showResolved,
    showOnlyNew,
    dividerTime,
    needsOperatorIds: needsOperatorMessageIds,
    // Gate needs-action filter on strategic read model availability: the filter
    // narrows to needsOperatorIds, which is only populated from that model.
    needsActionOnly: needsActionOnly && usesStrategicReadModel,
  });
  // Derived on-demand in the effect below; no separate memo needed.
  const conversationMessages = useMemo(() => {
    return messages
      .filter(msg => msg.surface != null
        ? msg.surface === 'conversation'
        : isConversationMessage(msg, eventPrefs, tempMutes))
      .slice()
      .sort(compareConversationMessages);
  }, [messages, eventPrefs, tempMutes]);
  const streamingTurn = useMemo(
    () => conversationMode
      ? computeStrategicStreamingTurn(strategicSessionTranscript, conversationMessages)
      : null,
    [conversationMode, strategicSessionTranscript, conversationMessages],
  );
  // Render the operator's optimistic bubble until the durable copy lands. Client
  // identity, never message content, is the reconciliation key: identical text
  // sent intentionally must remain two distinct turns.
  const renderedConversationMessages = useMemo(() => {
    if (!conversationMode || !optimisticSend) return conversationMessages;
    const alreadyDurable = conversationMessages.some(m => messageHasClientIdentity(m, optimisticSend.key));
    if (alreadyDurable) return conversationMessages;
    const optimisticMessage = {
      id: `optimistic:${optimisticSend.key}`,
      project_id: selectedProjectId ?? '',
      message_type: optimisticSend.message_type as ProjectMessage['message_type'],
      from_type: 'user' as ProjectMessage['from_type'],
      from_task_id: null,
      content: optimisticSend.content,
      context: optimisticSend.context ?? {},
      status: 'open' as ProjectMessage['status'],
      resolution: null,
      resolved_at: null,
      delivered_at: null,
      created_at: optimisticSend.created_at,
      reply_to_message_id: optimisticSend.reply_to_message_id ?? null,
      idempotency_key: optimisticSend.key,
    } satisfies ProjectMessage;
    return [...conversationMessages, optimisticMessage];
  }, [conversationMode, optimisticSend, conversationMessages, selectedProjectId]);
  const starterPrompts = conversationMode && !loading && conversationMessages.length === 0 ? WORKDAY_STARTER_PROMPTS : null;
  // Clear the working state once a FRESH strategist reply lands (compare reply
  // COUNT to the baseline captured at send time — robust to client/server clock
  // skew, unlike a timestamp compare).
  useEffect(() => {
    if (workingSince == null) return;
    if (countStrategicReplies(conversationMessages) > strategicReplyBaselineRef.current) {
      setWorkingSince(null);
      conversationSend.clearPending();
    }
  }, [workingSince, conversationMessages, conversationSend]);
  // Safety net: never leave the working state stuck if a turn silently stalls.
  useEffect(() => {
    if (workingSince == null) return;
    const timer = window.setTimeout(() => setWorkingSince(null), 120_000);
    return () => window.clearTimeout(timer);
  }, [workingSince]);
  // Working state is ephemeral and always scoped to the selected project. The
  // durable pending-send hook independently restores that project's identity.
  useEffect(() => {
    setWorkingSince(null);
  }, [selectedProjectId]);
  const pendingAttachmentSendKey = conversationSend.identity ?? optimisticSend?.key ?? null;
  useEffect(() => {
    if (!currentActorUserId || !selectedProjectId) return;
    void retryPendingAttachmentCleanups(currentActorUserId, selectedProjectId).catch(() => undefined);
  }, [currentActorUserId, selectedProjectId]);
  useEffect(() => {
    if (!conversationMode || !currentActorUserId || !selectedProjectId || !pendingAttachmentSendKey) return;
    const actorUserId = currentActorUserId;
    const projectId = selectedProjectId;
    const sendKey = pendingAttachmentSendKey;
    const restoreRequest = pendingAttachmentRestoreRequestRef.current + 1;
    pendingAttachmentRestoreRequestRef.current = restoreRequest;
    setRestoringPendingAttachments(true);
    let cancelled = false;
    let retryTimer: number | null = null;
    let retryAttempt = 0;
    let failureNotified = false;
    const scopeIsCurrent = () => (
      !cancelled
      && pendingAttachmentRestoreRequestRef.current === restoreRequest
      && selectedProjectIdRef.current === projectId
      && (conversationSend.identity ?? optimisticSend?.key ?? null) === sendKey
    );
    const scheduleRestore = (delayMs: number) => {
      if (!scopeIsCurrent()) return;
      if (retryTimer != null) window.clearTimeout(retryTimer);
      retryTimer = window.setTimeout(() => {
        retryTimer = null;
        void restoreAttachments();
      }, delayMs);
    };
    const restoreAttachments = async () => {
      if (!scopeIsCurrent()) return;
      if (attachmentMutationGuardRef.current.active) {
        scheduleRestore(50);
        return;
      }
      const mutationGeneration = attachmentMutationGenerationRef.current;
      try {
        const stored = await listStoredPendingAttachments(actorUserId, projectId, sendKey);
        if (!scopeIsCurrent()) return;
        if (
          attachmentMutationGuardRef.current.active
          || attachmentMutationGenerationRef.current !== mutationGeneration
        ) {
          scheduleRestore(50);
          return;
        }
        setPendingAttachments(previous => {
          const previousById = new Map(previous.map(attachment => [attachment.id, attachment]));
          const restored = stored.map(attachment => {
            const existing = previousById.get(attachment.id);
            return existing ?? {
              ...attachment,
              previewUrl: makePendingPreviewUrl(attachment.file),
            };
          });
          revokePendingAttachmentPreviews(previous.filter(attachment => !restored.some(item => item.id === attachment.id)));
          pendingAttachmentsRef.current = restored;
          return restored;
        });
        setRestoringPendingAttachments(false);
      } catch {
        if (!scopeIsCurrent()) return;
        if (!failureNotified) {
          failureNotified = true;
          addToastRef.current('Could not restore pending attachments; retrying automatically', 'error');
        }
        const retryDelayMs = Math.min(250 * (2 ** retryAttempt), 5_000);
        retryAttempt += 1;
        scheduleRestore(retryDelayMs);
      }
    };
    void restoreAttachments();
    return () => {
      cancelled = true;
      if (retryTimer != null) window.clearTimeout(retryTimer);
      if (pendingAttachmentRestoreRequestRef.current === restoreRequest) {
        pendingAttachmentRestoreRequestRef.current += 1;
        setRestoringPendingAttachments(false);
      }
    };
  }, [
    conversationMode,
    conversationSend.identity,
    currentActorUserId,
    optimisticSend?.key,
    pendingAttachmentSendKey,
    selectedProjectId,
  ]);
  // Retain a canonical wake receipt while the message is merely pending. Clear
  // it only when handling advances to claim/reply/supersession.
  useEffect(() => {
    if (!optimisticSend) return;
    const landed = conversationMessages.find(m => messageHasClientIdentity(m, optimisticSend.key));
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (landed && ['claimed', 'responded', 'superseded'].includes(landed.handling_state ?? '')) {
      conversationSend.clearPending();
      if (currentActorUserId && selectedProjectId) {
        void clearStoredPendingAttachments(currentActorUserId, selectedProjectId, optimisticSend.key);
      }
    }
  }, [conversationMessages, optimisticSend]);
  const restoredSendRecoveryRef = useRef<Set<string>>(new Set());
  useEffect(() => {
    if (!conversationMode || !currentActorUserId || !selectedProjectId || !optimisticSend || !conversationSend.restored) return;
    const recoveryKeys = restoredSendRecoveryRef.current, recoveryKey = `${currentActorUserId}:${selectedProjectId}:${optimisticSend.key}`;
    if (recoveryKeys.has(recoveryKey)) return;
    recoveryKeys.add(recoveryKey);
    let cancelled = false, settled = false;
    void findStrategicMessageByClientIdentity(selectedProjectId, optimisticSend.key)
      .then(receipt => {
        if (cancelled || selectedProjectIdRef.current !== selectedProjectId || currentActorUserIdRef.current !== currentActorUserId) return;
        settled = true;
        if (!receipt) {
          if (optimisticSend.state === 'sending') conversationSend.transition('needs_retry');
          return;
        }
        const adopted = receipt.message;
        setMessages(previous => mergeCanonicalConversationMessage(previous, adopted));
        const wakeupState = receipt.wakeup_state ?? optimisticSend.wakeup_state ?? null;
        const claimed = adopted.handling_state === 'claimed';
        const terminal = claimed || adopted.handling_state === 'responded' || adopted.handling_state === 'superseded';
        if (terminal) conversationSend.clear();
        else conversationSend.transition(
          wakeupState === 'paused' || wakeupState === 'waiting_for_capacity' ? 'waiting' : 'accepted',
          wakeupState,
        );
        setWorkingSince(claimed || wakeupState === 'queued' || wakeupState === 'coalesced' ? Date.now() : null);
        resetComposerIntent();
        clearSubmittedComposerDraft(currentActorUserId, selectedProjectId, optimisticSend.draft_write_id);
        clearPendingAttachments(optimisticSend.key);
        if (textareaRef.current) textareaRef.current.style.height = 'auto';
        void refreshStrategicState?.();
      })
      .catch(() => {
        settled = !cancelled;
        if (!cancelled && selectedProjectIdRef.current === selectedProjectId && currentActorUserIdRef.current === currentActorUserId && optimisticSend.state === 'sending') conversationSend.transition('needs_retry');
      });
    return () => { cancelled = true; if (!settled) recoveryKeys.delete(recoveryKey); };
  }, [clearSubmittedComposerDraft, conversationMode, currentActorUserId, optimisticSend, resetComposerIntent, selectedProjectId]);
  const showStrategicStatusStrip = conversationMode && Boolean(
    strategistActive
      || strategicRefresh
      || (effectiveContextPackage?.status && effectiveContextPackage.status !== 'ready')
      || (strategicAttention?.level === 'needs_operator' && attentionNeeds > 0),
  );
  const strategicComposerContext = useMemo(
    () => conversationMode
      ? buildStrategicContextDescriptor(currentStrategicState, strategicWorkbenchState)
      : null,
    [conversationMode, currentStrategicState, strategicWorkbenchState],
  );
  const mutedCount = useMemo(
    () => Object.values(eventPrefs).filter(v => v === 'mute').length,
    [eventPrefs],
  );
  useEffect(() => {
    if (!selectedProjectId) {
      strategicSessionPinnedTargetRef.current = null;
      lastStrategicSessionTargetFetchRef.current = null;
      return;
    }
    if (strategicSessionPinnedTargetRef.current?.projectId !== selectedProjectId) {
      strategicSessionPinnedTargetRef.current = null;
    }
    if (targetMessageId) {
      strategicSessionPinnedTargetRef.current = { projectId: selectedProjectId, messageId: targetMessageId };
    }
    lastStrategicSessionTargetFetchRef.current = null;
  }, [selectedProjectId, targetMessageId]);
  useEffect(() => {
    if (conversationMode && targetMessageId && searchQuery.trim()) setSearchQuery('');
  }, [conversationMode, searchQuery, setSearchQuery, targetMessageId]);
  useEffect(() => {
    if (!targetMessageId) return;
    if (!conversationMode) {
      onTargetMessageHandled?.();
      return;
    }
    if (loading) return;
    const visibleTarget = conversationMessages.find(msg => msg.id === targetMessageId) ?? null;
    const target = visibleTarget ?? messages.find(msg => msg.id === targetMessageId) ?? null;
    if (!target) {
      if (!loading && lastStrategicSessionTargetFetchRef.current === targetMessageId) {
        onTargetMessageHandled?.();
      }
      return;
    }
    if (!visibleTarget) {
      if (targetMessage?.action === 'reply') {
        addToastRef.current('Reply not primed: target must be an open Agent or Strategic message.', 'info');
      }
      onTargetMessageHandled?.();
      return;
    }
    let cancelled = false;
    void scrollConversationMessageIntoView(target.id).then(revealed => {
      if (cancelled || !revealed) return;
      setHighlightedMessageId(target.id);
      if (targetMessage?.action === 'reply' && visibleTarget && canPrimeStrategicChatReply(visibleTarget)) {
        const primedReply = {
          id: visibleTarget.id,
          content: visibleTarget.content,
          from: messageReplyFromLabel(visibleTarget),
          message_type: visibleTarget.message_type,
          status: visibleTarget.status,
        };
        setReplyTo(primedReply);
        persistComposerEnvelope({ replyTo: primedReply });
        textareaRef.current?.focus();
      } else if (targetMessage?.action === 'reply') {
        addToastRef.current('Reply not primed: target must be an open Agent or Strategic message.', 'info');
      }
      onTargetMessageHandled?.();
    });
    return () => { cancelled = true; };
  }, [
    conversationMessages,
    conversationMode,
    loading,
    messages,
    onTargetMessageHandled,
    targetMessage?.action,
    targetMessageId,
    scrollConversationMessageIntoView,
    // `persistComposerEnvelope` changes when reply state changes; including it
    // would re-prime the same externally-held target forever.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  ]);
  useEffect(() => {
    if (!highlightedMessageId) return;
    const frame = window.requestAnimationFrame(() => {
      scrollRef.current
        ?.querySelector(`[data-message-id="${highlightedMessageId}"]`)
        ?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    });
    const timer = window.setTimeout(() => setHighlightedMessageId(null), 4000);
    return () => {
      window.cancelAnimationFrame(frame);
      window.clearTimeout(timer);
    };
  }, [highlightedMessageId]);
  const getTop3NoisyEventTypes = useCallback((): Array<[string, number]> => {
    if (routineOpen.length === 0) return [];
    const now = Date.now();
    const oneHourAgo = now - 3600000;
    const eventTypeCounts: Record<string, number> = {};
    for (const msg of routineOpen) {
      const et = msg.context.event_type as string | undefined;
      if (et && msg.created_at && parseTimestamp(msg.created_at).getTime() > oneHourAgo) {
        eventTypeCounts[et] = (eventTypeCounts[et] || 0) + 1;
      }
    }
    return Object.entries(eventTypeCounts).sort((a, b) => b[1] - a[1]).slice(0, 3);
  }, [routineOpen]);
  const fetchMessages = useCallback(async () => {
    const requestSeq = fetchMessagesRequestSeqRef.current + 1;
    fetchMessagesRequestSeqRef.current = requestSeq;
    if (!selectedProjectId) { setLoading(false); return; }
    const requestProjectId = selectedProjectId;
    const requestConversationMode = conversationMode;
    const isCurrentRequest = () => (
      fetchMessagesRequestSeqRef.current === requestSeq
      && selectedProjectIdRef.current === requestProjectId
      && conversationModeRef.current === requestConversationMode
    );
    try {
      if (conversationMode) {
        // When the strategic read model is available, useConversation piggybacks
        // on that poll cadence. Standalone/minimal callers still need an initial
        // direct conversation fetch.
        if (!usesStrategicReadModelRef.current) {
          await refreshConversation(false);
        }
        const sessionMessages = messagesRef.current;
        sessionMessages.forEach(message => seenMessageIdsRef.current.add(message.id));
        hasInitiallyLoadedRef.current = true;
        if (!usesStrategicReadModelRef.current) {
          onMessageCountChange?.(countHighSignalMessages(sessionMessages, eventPrefsRef.current, tempMutesRef.current));
        }
        return;
      }
      const params = new URLSearchParams({ project_id: selectedProjectId, status: 'all', limit: String(TRIAGE_PAGE_SIZE) });
      if (triageOffsetRef.current > 0) params.set('offset', String(triageOffsetRef.current));
      if (searchQuery.trim()) params.set('q', searchQuery.trim());
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (!isCurrentRequest()) return;
      const msgs: ProjectMessage[] = data.messages ?? [];
      // Detect new needs-action messages for toast alerts. The canonical
      // attention model (not local mute prefs) is the exclusive gate: if the
      // server marks a message needs_operator it must trigger a toast regardless
      // of any locally-muted event_type so all four notification surfaces agree.
      if (hasInitiallyLoadedRef.current) {
        const newNeedsOperator = msgs.filter(m =>
          m.status === 'open' &&
          isMessageNeedingOperatorAttention(m, strategicAttentionRef.current) &&
          !seenMessageIdsRef.current.has(m.id)
        );
        if (newNeedsOperator.length > 0) {
          const summary = newNeedsOperator.length === 1
            ? `New needs-action message: ${newNeedsOperator[0].content.slice(0, 80)}`
            : `${newNeedsOperator.length} new needs-action messages`;
          const toastSeverity = newNeedsOperator.some(m => getToastSeverity(m) === 'error') ? 'error' : 'info';
          addToastRef.current(summary, toastSeverity);
          if (notifyOnHighSignalMessageRef.current && 'Notification' in window && Notification.permission === 'granted') {
            for (const msg of newNeedsOperator) {
              new Notification(msg.message_type, { body: msg.content.slice(0, 100) });
            }
          }
        }
      }
      // Update tracking of seen message IDs
      msgs.forEach(m => seenMessageIdsRef.current.add(m.id));
      hasInitiallyLoadedRef.current = true;
      // Merge or replace messages depending on load mode
      if (loadingOlderTriageRef.current) {
        // Load-older: merge with existing, update in place for same IDs
        loadingOlderTriageRef.current = false;
        setMessages(prev => {
          const merged = new Map(prev.map(m => [m.id, m]));
          let changed = false;
          for (const m of msgs) {
            const existing = merged.get(m.id);
            if (!existing) {
              merged.set(m.id, m);
              changed = true;
            } else if (!messagesEqual(existing, m)) {
              merged.set(m.id, m);
              changed = true;
            }
          }
          if (!changed) return prev;
          return [...merged.values()];
        });
      } else {
        // Initial load or poller refresh: replace state with fetched messages
        setMessages(prev => {
          if (messageListsEqual(prev, msgs)) return prev;
          return msgs;
        });
      }
      if (!usesStrategicReadModelRef.current) {
        onMessageCountChange?.(countHighSignalMessages(msgs, eventPrefsRef.current, tempMutesRef.current));
      }
    } catch {
      if (isCurrentRequest()) addToastRef.current('Failed to load messages', 'error');
    } finally {
      if (isCurrentRequest()) setLoading(false);
    }
    // Secondary fetches off the critical path — fire without awaiting so the
    // loading flag clears as soon as the primary messages fetch resolves,
    // making the message list visible/interactive immediately.
    if (!conversationMode && isCurrentRequest()) {
      // Webhook delivery stats (non-critical — errors silently ignored)
      apiFetch(`${API_BASE_URL}/api/admin/notification-dispatch/stats?project_id=${selectedProjectId}`)
        .then(statsRes => {
          if (!statsRes.ok) return;
          statsRes.json().then(stats => {
            if (isCurrentRequest()) setWebhookStats(stats);
          }).catch(() => {});
        }).catch(() => {});
      // Pending strategic-refresh marker — surfaced as a "strategic cycle pending"
      // badge until the autonomous loop consumes it (when it clears backend-side).
      // After consumption, shows a receipt via last_fired_at only if it was operator-origin
      // AND fired within STEER_RECEIPT_MAX_AGE_MS (prevents stale receipts that grow without
      // bound, and prevents a just-cancelled steer from showing an old "picked up" receipt).
      // Non-critical: errors leave the prior marker state untouched.
      if (!usesStrategicReadModel) {
        apiFetch(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(selectedProjectId)}`)
          .then(visRes => {
            if (!visRes.ok) return;
            visRes.json().then(proj => {
              const p = proj as { vision?: { _strategic_refresh?: StrategicRefreshMarker | null; _strategic_refresh_last_fired_at?: string; _strategic_refresh_last_fired_origin?: string } };
              if (usesStrategicReadModelRef.current || selectedProjectIdRef.current !== selectedProjectId || !isCurrentRequest()) return;
              const marker = p.vision?._strategic_refresh;
              const lastFiredAt = p.vision?._strategic_refresh_last_fired_at;
              const lastFiredOrigin = p.vision?._strategic_refresh_last_fired_origin;
              if (marker && typeof marker === 'object') {
                setStrategicRefresh(marker);
              } else if (lastFiredAt && typeof lastFiredAt === 'string' && lastFiredOrigin === 'operator') {
                const ageMs = Date.now() - parseTimestamp(lastFiredAt).getTime();
                if (!isNaN(ageMs) && ageMs <= STEER_RECEIPT_MAX_AGE_MS) {
                  setStrategicRefresh({ last_fired_at: lastFiredAt, last_fired_origin: lastFiredOrigin });
                } else {
                  setStrategicRefresh(null);
                }
              } else {
                setStrategicRefresh(null);
              }
            }).catch(() => {});
          }).catch(() => {});
      }
    }
  }, [conversationMode, selectedProjectId, onMessageCountChange, refreshConversation, searchQuery, usesStrategicReadModel]);
  usePoller(fetchMessages, { intervalMs: DEFAULT_POLL_MS, deps: [fetchMessages] });
  useEffect(() => {
    if (conversationMode || !realtimeRefreshSignal || realtimeRefreshSignal === lastRealtimeRefreshSignalRef.current) return;
    lastRealtimeRefreshSignalRef.current = realtimeRefreshSignal;
    void fetchMessages();
  }, [conversationMode, fetchMessages, realtimeRefreshSignal]);
  // Record chat data-ready timing when conversation messages first render (once per page load)
  const initialLoadDoneRef = useRef(false);
  useEffect(() => {
    if (loading || initialLoadDoneRef.current || !conversationMode) return;
    initialLoadDoneRef.current = true;
    const ms = Math.round(performance.now());
    reportUiLoadTiming(currentUiSurface(), ms, 'chat_data_ready');
  }, [loading, conversationMode]);
  // Auto-scroll to bottom on initial load, when the operator sends their own
  // message (optimistic send), or when a new message arrives while near bottom.
  // Uses renderedConversationMessages (includes the optimistic bubble) so the
  // operator's own just-sent message fires the scroll before the server round-trip.
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const currentCount = conversationMode
      ? renderedConversationMessages.length
      : highSignalOpen.length + routineOpen.length + visibleResolved.length;
    if (conversationMode && targetMessageId) {
      prevMessageCountRef.current = currentCount;
      return;
    }
    const isInitialLoad = prevMessageCountRef.current === 0 && currentCount > 0;
    if (isInitialLoad) {
      // First load — jump to bottom instantly. Use the list's API when available
      // (react-window scrollHeight is an estimate that lands mid-history).
      if (conversationMode && conversationListRef.current?.scrollToBottom) {
        requestAnimationFrame(() => conversationListRef.current?.scrollToBottom());
      } else {
        requestAnimationFrame(() => el.scrollTo({ top: el.scrollHeight, behavior: 'instant' }));
      }
    } else if (currentCount > prevMessageCountRef.current) {
      // Operator's own just-sent message (optimistic bubble) — always scroll.
      // For incoming messages use a generous threshold (200 px) to absorb mobile
      // viewport shifts from the on-screen keyboard opening/closing.
      const alwaysScroll = conversationMode && optimisticSend != null;
      const nearBottom = alwaysScroll || el.scrollHeight - el.scrollTop - el.clientHeight < 200;
      if (nearBottom) {
        requestAnimationFrame(() => el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' }));
      }
    }
    // Don't scroll at all on polling refresh with same count — preserves position
    prevMessageCountRef.current = currentCount;
  }, [conversationMode, renderedConversationMessages.length, highSignalOpen, routineOpen, targetMessageId, visibleResolved, optimisticSend]);
  // Update the tab-title badge from the canonical attention read model whenever
  // available, including the full message-list view. This keeps it aligned with
  // the closed-panel count; legacy callers without that model retain the
  // high-signal fallback computed inline.
  useEffect(() => {
    if (usesStrategicReadModel) {
      onMessageCountChange?.(attentionNeeds);
      return;
    }
    onMessageCountChange?.(countHighSignalMessages(messages, eventPrefs, tempMutes));
  }, [messages, eventPrefs, tempMutes, onMessageCountChange, usesStrategicReadModel, attentionNeeds]);
  // Track scroll position for "scroll to bottom" button
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    function onScroll() {
      if (!el) return;
      const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
      setShowScrollDown(gap > 150);
    }
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, []);
  // Scroll-based divider dismissal: detect when user scrolls past the divider
  useEffect(() => {
    const el = scrollRef.current;
    if (!el || !dividerTime || dividerDismissedRef.current) return;
    function onScroll() {
      if (!el) return;
      const dividerEl = document.querySelector('[data-divider-marker]');
      if (!dividerEl) return;
      const dividerRect = dividerEl.getBoundingClientRect();
      // If divider is above the viewport, dismiss it
      if (dividerRect.bottom < 0) {
        dismissDivider();
      }
    }
    el.addEventListener('scroll', onScroll, { passive: true });
    return () => el.removeEventListener('scroll', onScroll);
  }, [dividerTime, selectedProjectId, dismissDivider]);
  function scrollToBottom() {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: 'smooth' });
  }
  function beginAttachmentMutation() {
    if (submittingGuardRef.current.active || attachmentMutationGuardRef.current.active || uploading || sendState === 'needs_retry' || !selectedProjectId) return null;
    const scope = { actorUserId: currentActorUserId, projectId: selectedProjectId };
    attachmentMutationGenerationRef.current += 1;
    const owner = attachmentMutationGuardRef.current.begin(scope.actorUserId, scope.projectId);
    setAttachmentMutating(true);
    return { ...scope, owner };
  }
  function finishAttachmentMutation(owner: ReturnType<ComposerRequestOwnershipGuard['begin']>) {
    if (attachmentMutationGuardRef.current.finish(owner)) {
      attachmentMutationGenerationRef.current += 1;
      setAttachmentMutating(false);
    }
  }
  async function handleAddAttachment(file: File) {
    if (file.size > 50 * 1024 * 1024) {
      addToast('File exceeds the 50 MB size limit and was skipped', 'error');
      return;
    }
    if (file.type?.startsWith('image/') && file.size > MAX_AGENT_IMAGE_BYTES) {
      addToast('This image exceeds ~9.75 MB and may not be visible to the strategist', 'info');
    }
    if (conversationMode && !currentActorUserId) {
      addToast('Sign in before attaching files', 'error');
      return;
    }
    const mutation = beginAttachmentMutation();
    if (!mutation) return;
    const id = `att_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
    const storedAttachment = { id, file, mime_type: file.type || 'application/octet-stream', name: file.name, size_bytes: file.size };
    const priorIdentity = conversationSend.identity, retainIdentityOnFailure = Boolean(priorIdentity || composerIntentRef.current.content.trim() || composerIntentRef.current.reply_to || pendingAttachmentsRef.current.length);
    let sendKey: string | null = null, freshIdentityMinted = false;
    try {
      if (conversationMode && mutation.actorUserId) {
        sendKey = conversationSend.ensureIdentity(generateIdempotencyKey);
        freshIdentityMinted = priorIdentity == null;
        await putStoredPendingAttachment(mutation.actorUserId, mutation.projectId, sendKey, storedAttachment);
        if (currentActorUserIdRef.current !== mutation.actorUserId || selectedProjectIdRef.current !== mutation.projectId) return;
        const captured = captureCurrentDraftRefs();
        setDraftSourceRefs(captured.sourceRefs);
        setDraftArtifactRefs(captured.artifactRefs);
        persistComposerEnvelope({
          sourceRefs: captured.sourceRefs,
          artifactRefs: captured.artifactRefs,
          attachmentSendKey: sendKey,
        });
      }
      if (currentActorUserIdRef.current !== mutation.actorUserId || selectedProjectIdRef.current !== mutation.projectId) return;
      const previewUrl = makePendingPreviewUrl(file);
      setPendingAttachments(prev => {
        const next = [...prev, { ...storedAttachment, previewUrl }];
        pendingAttachmentsRef.current = next;
        return next;
      });
    } catch {
      if (freshIdentityMinted && !retainIdentityOnFailure) conversationSend.clearIdentity();
      if (currentActorUserIdRef.current === mutation.actorUserId && selectedProjectIdRef.current === mutation.projectId) {
        addToast('Could not persist this attachment; it was not added', 'error');
      }
    } finally {
      finishAttachmentMutation(mutation.owner);
    }
  }
  async function handleRemoveAttachment(id: string) {
    const mutation = beginAttachmentMutation();
    if (!mutation) return;
    const sendKey = conversationSend.identity ?? optimisticSend?.key ?? null;
    try {
      if (conversationMode && mutation.actorUserId && sendKey) {
        await deleteStoredPendingAttachment(mutation.actorUserId, mutation.projectId, sendKey, id);
      }
      if (currentActorUserIdRef.current !== mutation.actorUserId || selectedProjectIdRef.current !== mutation.projectId) return;
      const next = pendingAttachmentsRef.current.filter(attachment => attachment.id !== id);
      setPendingAttachments(prev => {
        const att = prev.find(a => a.id === id);
        if (att?.previewUrl && typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(att.previewUrl);
        pendingAttachmentsRef.current = next;
        return next;
      });
      if (next.length === 0 && mutation.actorUserId) {
        const result = finalizeLastAttachmentRemoval(
          mutation.actorUserId,
          mutation.projectId,
          updateComposerIntent({ attachment_send_key: null }),
          conversationSend.clearIdentity,
          composerDraftWriteIdRef.current,
        );
        if (result !== false) composerDraftWriteIdRef.current = result;
      }
    } catch {
      if (currentActorUserIdRef.current === mutation.actorUserId && selectedProjectIdRef.current === mutation.projectId) {
        addToast('Could not remove the persisted attachment', 'error');
      }
    } finally {
      finishAttachmentMutation(mutation.owner);
    }
  }
  // Swap the File backing an existing pending attachment — used by the pre-send
  // preview for canvas-edited (rotate/crop) bytes and picker-based replace. The
  // old object-URL is revoked and a fresh one minted so the chip and any open
  // preview re-render from the new bytes.
  async function handleReplaceAttachmentFile(id: string, file: File) {
    if (file.size > 50 * 1024 * 1024) {
      addToast('File exceeds the 50 MB size limit and was skipped', 'error');
      return;
    }
    if (file.type?.startsWith('image/') && file.size > MAX_AGENT_IMAGE_BYTES) {
      addToast('This image exceeds ~9.75 MB and may not be visible to the strategist', 'info');
    }
    const sendKey = conversationSend.identity ?? optimisticSend?.key ?? null;
    if (conversationMode && (!currentActorUserId || !sendKey)) {
      addToast('Could not identify the pending attachment draft', 'error');
      return;
    }
    const mutation = beginAttachmentMutation();
    if (!mutation) return;
    const storedAttachment = {
      id,
      file,
      mime_type: file.type || 'application/octet-stream',
      name: file.name,
      size_bytes: file.size,
    };
    try {
      if (conversationMode && mutation.actorUserId && sendKey) {
        await putStoredPendingAttachment(mutation.actorUserId, mutation.projectId, sendKey, storedAttachment);
      }
      if (currentActorUserIdRef.current !== mutation.actorUserId || selectedProjectIdRef.current !== mutation.projectId) return;
      const previewUrl = makePendingPreviewUrl(file);
      setPendingAttachments(prev => {
        const next = prev.map(att => {
          if (att.id !== id) return att;
          if (att.previewUrl && typeof URL.revokeObjectURL === 'function') URL.revokeObjectURL(att.previewUrl);
          return { ...att, ...storedAttachment, previewUrl };
        });
        pendingAttachmentsRef.current = next;
        return next;
      });
    } catch {
      if (currentActorUserIdRef.current === mutation.actorUserId && selectedProjectIdRef.current === mutation.projectId) {
        addToast('Could not persist the replacement attachment', 'error');
      }
    } finally {
      finishAttachmentMutation(mutation.owner);
    }
  }
  function clearPendingAttachments(sendKey?: string | null) {
    setPendingAttachments(prev => {
      revokePendingAttachmentPreviews(prev);
      pendingAttachmentsRef.current = [];
      return [];
    });
    const durableSendKey = sendKey ?? conversationSend.identity ?? optimisticSend?.key ?? null;
    if (conversationMode && currentActorUserId && selectedProjectId && durableSendKey) {
      void clearStoredPendingAttachments(currentActorUserId, selectedProjectId, durableSendKey)
        .catch(() => addToastRef.current('Message sent, but local attachment cleanup needs a retry', 'info'));
    }
  }
  async function handleSubmit() {
    const submitProjectId = selectedProjectId;
    const submitActorUserId = currentActorUserId;
    const submitReplyTo = replyTo ? { ...replyTo } : null;
    const submitStillCurrent = () => (
      selectedProjectIdRef.current === submitProjectId
      && currentActorUserIdRef.current === submitActorUserId
    );
    const submittedContent = newContent.trim();
    const submittedDraftWriteId = composerDraftWriteIdRef.current;
    const submittedOperatorIntent = composerIntentRef.current.operator_intent;
    const attachmentsForSubmit = pendingAttachmentsRef.current;
    const retainedPayloadAttachments = optimisticSend?.payload?.attachments ?? [];
    let attachmentUploadFailed = false;
    if (!submitProjectId || (
      !submittedContent
      && attachmentsForSubmit.length === 0
      && retainedPayloadAttachments.length === 0
    )) return;
    // Synchronous guard first — reject re-entrant calls before the async
    // `setSubmitting` has a chance to re-render, so rapid double-Enter cannot
    // launch two requests.
    if (submittingGuardRef.current.active || attachmentMutationGuardRef.current.active || restoringPendingAttachments) return;
    const submitOwner = submittingGuardRef.current.begin(submitActorUserId, submitProjectId);
    setSubmitting(true);
    try {
      // The activity inbox retains its legacy operational-resolution endpoint.
      // Strategist conversation replies always use the durable idempotent message
      // POST below, whose reply contract resolves open questions atomically.
      if (!conversationMode && submitReplyTo && submitReplyTo.message_type === 'QUESTION' && submitReplyTo.status === 'open') {
        if (attachmentsForSubmit.length > 0) {
          addToast('Remove attachments before answering a question', 'error');
          return;
        }
        const workbenchContext = buildMessageWorkbenchContext(currentStrategicState, strategicWorkbenchState);
        const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/${submitReplyTo.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            resolution: submittedContent,
            ...(workbenchContext ? { workbench_context: workbenchContext } : {}),
          }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        if (!submitStillCurrent()) return;
        idempotencyKeyRef.current = null;
        resetComposerIntent();
        clearSubmittedComposerDraft(submitActorUserId, submitProjectId, submittedDraftWriteId);
        setLastArming(null);
        if (textareaRef.current) textareaRef.current.style.height = 'auto';
        addToast('Question answered', 'success');
        void refreshStrategicState?.();
        recordStrategicInteraction();
        await fetchMessages();
        return;
      }
      const isRetry = conversationMode && optimisticSend?.state === 'needs_retry';
      const sendIdentity = conversationMode
        ? (isRetry ? optimisticSend.key : conversationSend.ensureIdentity(generateIdempotencyKey))
        : (idempotencyKeyRef.current ?? generateIdempotencyKey());
      if (!conversationMode) idempotencyKeyRef.current = sendIdentity;
      // The continuous strategist conversation no longer inherits hidden
      // Chat/Workday workbench state. Activity-inbox writes retain the legacy
      // workbench snapshot until that surface is retired.
      const workbenchContext = conversationMode
        ? null
        : buildMessageWorkbenchContext(currentStrategicState, strategicWorkbenchState);
      const initialSourceRefs = isRetry
        ? (optimisticSend?.payload?.source_refs ?? [])
        : draftSourceRefs;
      const initialArtifactRefs = isRetry
        ? (optimisticSend?.payload?.artifact_refs ?? [])
        : (draftArtifactRefs.length > 0
            ? draftArtifactRefs
            : initialSourceRefs.filter(ref => ref.kind === 'artifact'));
      if (conversationMode && !isRetry) {
        const visibleAttachmentDescriptors: MessageAttachment[] = attachmentsForSubmit.map(att => ({
          type: att.mime_type?.startsWith('image/') ? 'image' : 'attachment',
          mime_type: att.mime_type,
          name: att.name,
          size_bytes: att.size_bytes,
          ...(att.previewUrl ? { url: att.previewUrl } : {}),
          _attachmentKey: att.id,
        }));
        conversationSend.begin({
          key: sendIdentity,
          content: submittedContent,
          created_at: new Date().toISOString(),
          message_type: effectiveNewType,
          state: 'sending',
          wakeup_state: null,
          reply_to_message_id: submitReplyTo?.id ?? null,
          reply_to: submitReplyTo,
          draft_write_id: submittedDraftWriteId,
          context: {
            ...(visibleAttachmentDescriptors.length ? { attachments: visibleAttachmentDescriptors } : {}),
            ...(submitReplyTo ? {
              reply_to: submitReplyTo.id,
              reply_preview: submitReplyTo.content.slice(0, 120),
            } : {}),
            source_refs: initialSourceRefs,
            artifact_refs: initialArtifactRefs,
            ...(workbenchContext ? { strategic_session: workbenchContext } : {}),
          },
          payload: {
            message_type: effectiveNewType,
            content: submittedContent,
            idempotency_key: sendIdentity,
            reply_to_message_id: submitReplyTo?.id ?? null,
            source_refs: initialSourceRefs,
            artifact_refs: initialArtifactRefs,
            ...(submittedOperatorIntent ? { operator_intent: submittedOperatorIntent } : {}),
            attachments: [],
            ...(workbenchContext ? { workbench_context: workbenchContext } : {}),
          },
        });
      }
      if (isRetry) {
        const receipt = await findStrategicMessageByClientIdentity(submitProjectId, sendIdentity);
        if (receipt) {
          const adopted = receipt.message;
          if (!submitStillCurrent()) return;
          setMessages(previous => mergeCanonicalConversationMessage(previous, adopted));
          const adoptedWorking = adopted.handling_state === 'claimed'
            || receipt.wakeup_state === 'queued'
            || receipt.wakeup_state === 'coalesced';
          setWorkingSince(adoptedWorking ? Date.now() : null);
          if (['claimed', 'responded', 'superseded'].includes(adopted.handling_state ?? '')) {
            conversationSend.clear();
          } else {
            conversationSend.transition(
              receipt.wakeup_state === 'paused' || receipt.wakeup_state === 'waiting_for_capacity' ? 'waiting' : 'accepted',
              receipt.wakeup_state,
            );
          }
          resetComposerIntent();
          clearSubmittedComposerDraft(submitActorUserId, submitProjectId, optimisticSend.draft_write_id);
          clearPendingAttachments(sendIdentity);
          if (textareaRef.current) textareaRef.current.style.height = 'auto';
          addToast('Message accepted', 'success');
          void fetchMessages().catch(() => undefined);
          void refreshStrategicState?.();
          recordStrategicInteraction();
          return;
        }
      }
      const retainedUploads = isRetry ? retainedPayloadAttachments : [];
      const uploadedAttachments: MessageAttachment[] = [...retainedUploads];
      const contextAttachments: MessageAttachment[] = isRetry && Array.isArray(optimisticSend?.context?.attachments)
        ? optimisticSend.context.attachments as MessageAttachment[]
        : [...retainedUploads];
      if (attachmentsForSubmit.length > 0 && retainedUploads.length === 0) {
        setUploading(true);
        try {
          const batch = await uploadConversationAttachments(submitProjectId, attachmentsForSubmit);
          uploadedAttachments.push(...batch.uploaded);
          contextAttachments.push(...batch.visible);
        } catch (error) {
          attachmentUploadFailed = true;
          const name = error instanceof ConversationAttachmentUploadError ? error.attachmentName : 'attachment';
          if (submitStillCurrent()) addToast(`Failed to upload ${name}`, 'error');
          throw error;
        } finally {
          if (submitStillCurrent()) setUploading(false);
        }
      }
      const context: Record<string, unknown> = {};
      if (workbenchContext) context.strategic_session = workbenchContext;
      if (contextAttachments.length > 0) {
        context.attachments = contextAttachments;
      }
      if (submitReplyTo) {
        context.reply_to = submitReplyTo.id;
        context.reply_preview = submitReplyTo.content.slice(0, 120);
      }
      if (conversationMode) {
        const sourceRefs = initialSourceRefs;
        const artifactRefs = initialArtifactRefs;
        let payload = isRetry && optimisticSend?.payload
          ? (uploadedAttachments.length === (optimisticSend.payload.attachments?.length ?? 0)
              ? optimisticSend.payload
              : { ...optimisticSend.payload, attachments: uploadedAttachments })
          : {
              message_type: effectiveNewType,
              content: submittedContent,
              idempotency_key: sendIdentity,
              reply_to_message_id: submitReplyTo?.id ?? null,
              source_refs: sourceRefs,
              artifact_refs: artifactRefs,
              ...(submittedOperatorIntent ? { operator_intent: submittedOperatorIntent } : {}),
              attachments: uploadedAttachments,
              ...(workbenchContext ? { workbench_context: workbenchContext } : {}),
            };
        const pendingSend = isRetry && optimisticSend
          ? optimisticSend
          : {
              key: sendIdentity,
              content: submittedContent,
              created_at: new Date().toISOString(),
              message_type: effectiveNewType,
              state: 'sending' as const,
              wakeup_state: null,
              reply_to_message_id: submitReplyTo?.id ?? null,
              reply_to: submitReplyTo,
              draft_write_id: submittedDraftWriteId,
              context: {
                ...context,
                source_refs: sourceRefs,
                artifact_refs: artifactRefs,
              },
              payload,
            };
        conversationSend.replace({
          ...pendingSend,
          context: {
            ...pendingSend.context,
            ...context,
            source_refs: sourceRefs,
            artifact_refs: artifactRefs,
          },
          payload,
        });
        // A retry reached here only after the identity lookup proved absence.
        let data;
        try {
          data = await createStrategicSessionMessage(submitProjectId, payload);
        } catch (error) {
          const staleAttachmentReceipt = isRetry
            && retainedUploads.length > 0
            && error instanceof StrategicSessionMessageCreateError
            && (error.status === 400 || error.status === 409)
            && /attachment upload/i.test(error.message);
          if (!staleAttachmentReceipt || !submitActorUserId) throw error;
          const durableAttachments = await listStoredPendingAttachments(submitActorUserId, submitProjectId, sendIdentity);
          if (durableAttachments.length === 0) {
            if (submitStillCurrent()) addToast('Attachment upload expired and the local file is unavailable. Abandon this pending send to continue.', 'error');
            throw error;
          }
          await Promise.allSettled(retainedUploads.flatMap(attachment => (
            attachment.attachment_id
              ? [deleteMessageAttachmentUpload(submitProjectId, attachment.attachment_id)]
              : []
          )));
          setUploading(true);
          let recovered;
          try {
            recovered = await uploadConversationAttachments(submitProjectId, durableAttachments);
          } catch (recoveryError) {
            attachmentUploadFailed = true;
            throw recoveryError;
          } finally {
            if (submitStillCurrent()) setUploading(false);
          }
          payload = { ...payload, attachments: recovered.uploaded };
          const recoveredContext = {
            ...pendingSend.context,
            ...context,
            attachments: recovered.visible,
            source_refs: sourceRefs,
            artifact_refs: artifactRefs,
          };
          conversationSend.replace({ ...pendingSend, context: recoveredContext, payload });
          data = await createStrategicSessionMessage(submitProjectId, payload);
        }
        const recoveredReceipt = data.message ? null : await findStrategicMessageByClientIdentity(submitProjectId, sendIdentity);
        const canonicalMessage = data.message ?? recoveredReceipt?.message ?? null;
        if (!canonicalMessage) throw new Error('Accepted message response omitted its canonical projection');
        if (!submitStillCurrent()) {
          const recoveredWakeup = data.wakeup_state ?? recoveredReceipt?.wakeup_state ?? null;
          conversationSend.transition(
            recoveredWakeup === 'paused' || recoveredWakeup === 'waiting_for_capacity' ? 'waiting' : 'accepted',
            recoveredWakeup,
          );
          clearSubmittedComposerDraft(submitActorUserId, submitProjectId, pendingSend.draft_write_id);
          if (submitActorUserId) {
            void clearStoredPendingAttachments(submitActorUserId, submitProjectId, sendIdentity)
              .catch(() => undefined);
          }
          return;
        }
        setMessages(previous => mergeCanonicalConversationMessage(previous, canonicalMessage));
        const wakeupState = data.wakeup_state ?? recoveredReceipt?.wakeup_state ?? null;
        const acceptedState = wakeupState === 'paused' || wakeupState === 'waiting_for_capacity'
          ? 'waiting'
          : 'accepted' as const;
        conversationSend.transition(acceptedState, wakeupState);
        if (wakeupState === 'queued' || wakeupState === 'coalesced') {
          strategicReplyBaselineRef.current = countStrategicReplies(conversationMessages);
          setWorkingSince(Date.now());
        } else {
          setWorkingSince(null);
        }

        const arming = parseArmingOutcome(data?.arming);
        // Clear only after durable acceptance. A lost/failed response preserves
        // text, reply, attachments, and source context for identity recovery.
        resetComposerIntent();
        clearSubmittedComposerDraft(submitActorUserId, submitProjectId, pendingSend.draft_write_id);
        setLastArming(arming);
        clearPendingAttachments(sendIdentity);
        if (textareaRef.current) textareaRef.current.style.height = 'auto';
        const receiptToast = conversationAcceptanceToastCopy(wakeupState, arming);
        addToast(receiptToast.message, receiptToast.severity);
        let didRefreshStrategicState = false;
        if (arming === 'steering') {
          const optimisticRefresh = {
            requested_at: new Date().toISOString(),
            origin: 'operator',
            reason: 'operator_message',
            count: 1,
            operator_count: 1,
            excerpt: submittedContent,
          };
          optimisticStrategicRefreshRef.current = optimisticRefresh;
          setStrategicRefresh(optimisticRefresh);
          void refreshStrategicState?.();
          didRefreshStrategicState = true;
        } else if (arming === 'duplicate') {
          void refreshStrategicState?.();
          didRefreshStrategicState = true;
        }
        await fetchMessages().catch(() => undefined);
        if (!didRefreshStrategicState) void refreshStrategicState?.();
        recordStrategicInteraction();
        return;
      }
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_id: selectedProjectId,
          message_type: effectiveNewType,
          content: submittedContent,
          idempotency_key: idempotencyKeyRef.current,
          ...(Object.keys(context).length > 0 ? { context } : {}),
          ...(workbenchContext ? { workbench_context: workbenchContext } : {}),
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      if (!submitStillCurrent()) return;
      // Read the server's real arming decision (additive, backward-compatible
      // field) so the toast and composer hint reflect what actually happened
      // rather than a type-only guess. Tolerate older responses without it.
      let arming: ArmingOutcome | null = null;
      try {
        const data = await res.json();
        if (!submitStillCurrent()) return;
        arming = parseArmingOutcome(data?.arming);
      } catch {
        // No/invalid JSON body — fall back to generic confirmation copy.
      }
      // Sent cleanly — the next distinct message gets a fresh key.
      idempotencyKeyRef.current = null;
      resetComposerIntent();
      clearSubmittedComposerDraft(submitActorUserId, submitProjectId, submittedDraftWriteId);
      setLastArming(arming);
      clearPendingAttachments();
      if (textareaRef.current) textareaRef.current.style.height = 'auto';
      // A genuine steering message the server could not arm (`not_armed`) is a
      // partial outcome — the message saved, but the strategist was not woken.
      // Surface it as info, not a green success, so the receipt is truthful.
      addToast(armingToastCopy(arming), arming === 'not_armed' ? 'info' : 'success');
      let didRefreshStrategicState = false;
      if (arming === 'steering') {
        const optimisticRefresh = {
          requested_at: new Date().toISOString(),
          origin: 'operator',
          reason: 'operator_message',
          count: 1,
          operator_count: 1,
          excerpt: submittedContent,
        };
        optimisticStrategicRefreshRef.current = optimisticRefresh;
        setStrategicRefresh(optimisticRefresh);
        void refreshStrategicState?.();
        didRefreshStrategicState = true;
      } else if (arming === 'duplicate') {
        void refreshStrategicState?.();
        didRefreshStrategicState = true;
      }
      await fetchMessages();
      if (!didRefreshStrategicState) void refreshStrategicState?.();
      recordStrategicInteraction();
    } catch {
      // The draft and identity remain intact. The next manual retry first queries
      // the canonical store, covering the response-lost-after-commit boundary.
      if (conversationMode) {
        conversationSend.transition('needs_retry');
        if (submitStillCurrent()) {
          setWorkingSince(null);
          addToast('Send not confirmed — retry to recover or resend safely', 'error');
        }
      } else {
        if (submitStillCurrent() && !attachmentUploadFailed) addToast('Failed to send message', 'error');
      }
    } finally {
      if (submittingGuardRef.current.finish(submitOwner)) {
        setSubmitting(false);
        setUploading(false);
      }
    }
  }

  // Retract an armed-but-not-yet-fired strategic steer. Idempotent server-side
  // (cancelling when nothing is armed is a no-op), so a late/duplicate click is
  // harmless. Clears the local pending marker immediately, then refetches to
  // confirm the badge cleared.
  async function handleCancelStrategicRefresh() {
    if (!selectedProjectId || cancellingRefresh) return;
    setCancellingRefresh(true);
    try {
      const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(selectedProjectId)}/cancel-strategic-refresh`, { method: 'POST' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      optimisticStrategicRefreshRef.current = null;
      setStrategicRefresh(null);
      void refreshStrategicState?.();
      addToast('Strategic steer cancelled', 'success');
      await fetchMessages();
    } catch {
      addToast('Failed to cancel strategic steer', 'error');
    } finally {
      setCancellingRefresh(false);
    }
  }

  const handleResolve = useCallback(async (msgId: string, resolution: string) => {
    setResolvingId(null);
    setResolveInput('');
    try {
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/${msgId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resolution }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await fetchMessages();
    } catch {
      addToastRef.current('Failed to resolve message', 'error');
    }
  }, [fetchMessages]);

  const handleDelete = useCallback(async (msgId: string) => {
    setDeletingIds(prev => new Set(prev).add(msgId));
    try {
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/${msgId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await fetchMessages();
    } catch {
      addToastRef.current('Failed to delete message', 'error');
    } finally {
      setDeletingIds(prev => { const next = new Set(prev); next.delete(msgId); return next; });
    }
  }, [fetchMessages]);

  async function handleCreateDirection(msg: ProjectMessage, silent = false): Promise<boolean> {
    if (!selectedProjectId) return false;
    setCreatingDirectionIds(prev => new Set(prev).add(msg.id));
    try {
      const text = msg.content.slice(0, 2000);
      // Reuse one idempotency identity per source message so a retry / batch
      // replay of the same conversion collapses to a single direction; a
      // duplicate replay returns the existing direction as a clean success.
      let idemKey = directionIdempotencyKeysRef.current.get(msg.id);
      if (!idemKey) {
        idemKey = generateIdempotencyKey();
        directionIdempotencyKeysRef.current.set(msg.id, idemKey);
      }
      const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(selectedProjectId)}/directions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, priority: 'medium', idempotency_key: idemKey }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const directionId: string = data.direction?.id ?? '';
      const resolveRes = await apiFetch(`${API_BASE_URL}/api/admin/messages/${msg.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resolution: `Direction created: ${directionId}` }),
      });
      if (!resolveRes.ok) throw new Error(`HTTP ${resolveRes.status}`);
      // Resolved/created cleanly — drop the per-message key so a genuinely
      // distinct future convert of the same message id mints a fresh identity.
      directionIdempotencyKeysRef.current.delete(msg.id);
      if (!silent) addToast('Direction created', 'success');
      await fetchMessages();
      void refreshStrategicState?.();
      return true;
    } catch {
      addToast('Failed to create direction', 'error');
      return false;
    } finally {
      setCreatingDirectionIds(prev => { const next = new Set(prev); next.delete(msg.id); return next; });
    }
  }

  const handleOpenConvertModal = useCallback((msg: ProjectMessage) => {
    convertFormRef.current.seedFromMessage(msg.content);
    setPendingConvertMsg(msg);
  }, []);

  async function handleSubmitConvertForm() {
    if (!selectedProjectId || !pendingConvertMsg || !convertForm.newText.trim()) return;
    setConvertSubmitting(true);
    const msg = pendingConvertMsg;
    try {
      let idemKey = directionIdempotencyKeysRef.current.get(msg.id);
      if (!idemKey) {
        idemKey = generateIdempotencyKey();
        directionIdempotencyKeysRef.current.set(msg.id, idemKey);
      }
      // eslint-disable-next-line react-hooks/immutability
      convertForm.addIdempotencyKeyRef.current = idemKey;
      const body: Record<string, unknown> = {
        text: convertForm.newText.trim(),
        priority: convertForm.newPriority,
        paths: convertForm.newPathTags,
        idempotency_key: idemKey,
      };
      if (convertForm.newCategory) body.category = convertForm.newCategory;
      body.completion_criteria = convertForm.newCompletionCriteria.trim() || null;
      body.success_evidence = convertForm.newSuccessEvidence.trim() || null;
      body.retirement_condition = convertForm.newRetirementCondition.trim() || null;
      const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(selectedProjectId)}/directions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const directionId: string = data.direction?.id ?? '';
      const resolveRes = await apiFetch(`${API_BASE_URL}/api/admin/messages/${msg.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resolution: `Direction created: ${directionId}` }),
      });
      if (!resolveRes.ok) throw new Error(`HTTP ${resolveRes.status}`);
      directionIdempotencyKeysRef.current.delete(msg.id);
      setPendingConvertMsg(null);
      convertForm.resetAddForm();
      addToast('Direction created', 'success');
      await fetchMessages();
      void refreshStrategicState?.();
    } catch {
      convertForm.markAddAttemptFailed();
      addToast('Failed to create direction', 'error');
    } finally {
      setConvertSubmitting(false);
    }
  }

  const handleCreateInvestigation = useCallback(async (msg: ProjectMessage): Promise<boolean> => {
    if (!selectedProjectId || !githubRepo) return false;
    setInvestigatingIds(prev => new Set(prev).add(msg.id));
    try {
      const lines = msg.content.split('\n');
      const title = lines[0].slice(0, 100);
      const prompt = `${msg.content}\n\nOriginal message: ${msg.id}`;
      const res = await fetch(`${API_BASE_URL}/api/admin/tasks`, {
        method: 'POST',
        headers: await authHeaders(),
        body: JSON.stringify({
          github_repo: githubRepo,
          title,
          prompt,
          source_message_id: msg.id,
          ...(defaultBranch ? { base_branch: defaultBranch } : {}),
          task_type: 'investigate',
        }),
      });
      if (!res.ok) {
        const errData = await res.json().catch(() => ({}));
        throw new Error(errData?.detail || errData?.error?.message || `HTTP ${res.status}`);
      }
      setInvestigatingId(null);
      addToastRef.current('Investigation task created', 'success');
      return true;
    } catch (err) {
      addToastRef.current(`Failed to create investigation: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
      return false;
    } finally {
      setInvestigatingIds(prev => { const next = new Set(prev); next.delete(msg.id); return next; });
    }
  }, [selectedProjectId, githubRepo, defaultBranch]);

  const KEEP_RESOLVED = 20;

  async function handleBulkDeleteResolved() {
    // Keep the 20 most recent resolved, delete the rest
    const sorted = [...resolvedMessages].sort((a, b) => {
      const ta = a.created_at ? parseTimestamp(a.created_at).getTime() : 0;
      const tb = b.created_at ? parseTimestamp(b.created_at).getTime() : 0;
      return ta - tb;
    });
    const toDelete = sorted.slice(0, -KEEP_RESOLVED);
    if (toDelete.length === 0) {
      addToast(`Only ${sorted.length} resolved — nothing to clear`, 'info');
      return;
    }
    let count = 0;
    for (const msg of toDelete) {
      try {
        const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/${msg.id}`, { method: 'DELETE' });
        if (res.ok) count++;
      } catch { /* skip */ }
    }
    if (count > 0) addToast(`Cleared ${count} old resolved (kept ${Math.min(sorted.length, KEEP_RESOLVED)} recent)`, 'success');
    await fetchMessages();
  }

  const toggleSelect = useCallback((msgId: string) => {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(msgId)) next.delete(msgId); else next.add(msgId);
      return next;
    });
  }, []);

  async function handleBatchCreateDirections() {
    const msgs = openFeedbackMessages.filter(m => selectedIds.has(m.id));
    if (msgs.length === 0) return;
    setBatchCreating(true);
    let count = 0;
    for (const msg of msgs) {
      const ok = await handleCreateDirection(msg, true);
      if (ok) count++;
    }
    setBatchCreating(false);
    setSelectedIds(new Set());
    setSelectMode(false);
    if (count > 0) addToast(`${count} direction${count > 1 ? 's' : ''} created`, 'success');
  }

  async function handleResolveGroup(msgs: ProjectMessage[]) {
    try {
      for (const m of msgs) {
        await apiFetch(`${API_BASE_URL}/api/admin/messages/${m.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ resolution: '' }),
        });
      }
      await fetchMessages();
    } catch {
      addToast('Failed to resolve messages', 'error');
    }
  }

  // Stable per-bubble handlers so `bubbleProps` keeps a stable identity across
  // renders (see React.memo on MessageBubble). Setters/refs are stable, so most
  // carry an empty dependency array; the rest depend only on their (already
  // memoized) target handler.
  const onCreateInvestigation = useCallback((msg: ProjectMessage) => { void handleCreateInvestigation(msg); }, [handleCreateInvestigation]);
  const onDelete = useCallback((msgId: string) => { void handleDelete(msgId); }, [handleDelete]);
  const onStartResolve = useCallback((msgId: string) => { setResolvingId(msgId); setResolveInput(''); }, []);
  const onCancelResolve = useCallback(() => setResolvingId(null), []);
  const onStartInvestigate = useCallback((msgId: string) => setInvestigatingId(msgId), []);
  const onCancelInvestigate = useCallback(() => setInvestigatingId(null), []);
  const onReply = useCallback((replyTo: { id: string; content: string; from: string; message_type: string; status: string }) => {
    setReplyTo(replyTo);
    const captured = captureCurrentDraftRefs();
    setDraftSourceRefs(captured.sourceRefs);
    setDraftArtifactRefs(captured.artifactRefs);
    persistComposerEnvelope({ replyTo, sourceRefs: captured.sourceRefs, artifactRefs: captured.artifactRefs });
    textareaRef.current?.focus();
  }, [captureCurrentDraftRefs, persistComposerEnvelope]);

  const bubbleProps = useMemo(() => ({
    actionMode: conversationMode ? 'reply-only' as const : 'full' as const,
    resolvingId,
    resolveInput,
    deletingIds,
    creatingDirectionIds,
    investigatingId,
    investigatingIds,
    selectMode,
    selectedIds,
    onOpenTask,
    onToggleSelect: toggleSelect,
    onCreateDirection: handleOpenConvertModal,
    onCreateInvestigation,
    onResolve: handleResolve,
    onDelete,
    onStartResolve,
    onCancelResolve,
    onSetResolveInput: setResolveInput,
    onStartInvestigate,
    onCancelInvestigate,
    onReply,
    onOpenStrategicContext: conversationMode ? (onOpenStrategicContextWorkspace ?? onOpenStrategicContextCommands) : undefined,
    currentActorUserId: auth.currentUser?.uid ?? null,
  }), [
    conversationMode,
    resolvingId,
    resolveInput,
    deletingIds,
    creatingDirectionIds,
    investigatingId,
    investigatingIds,
    selectMode,
    selectedIds,
    onOpenTask,
    toggleSelect,
    handleOpenConvertModal,
    onCreateInvestigation,
    handleResolve,
    onDelete,
    onStartResolve,
    onCancelResolve,
    onStartInvestigate,
    onCancelInvestigate,
    onReply,
    onOpenStrategicContextWorkspace,
    onOpenStrategicContextCommands,
  ]);

  // Stable callbacks for MessagesPanelHeader and MessagesComposer props —
  // extracted into useCallback so that memo-wrapped children (MessagesPanelHeader
  // and MessagesComposer) can skip re-renders when only these callback identities change.
  const handleResetAllPrefs = useCallback(() => {
    setEventPrefs({});
    saveEventPrefs(auth.currentUser?.email, selectedProjectId, {});
    if (selectedProjectId) void saveServerNoisePrefs(selectedProjectId, {});
  }, [selectedProjectId]);

  const handleToggleSort = useCallback(() => {
    setSortMode(m => m === 'importance' ? 'recent' : 'importance');
  }, []);

  const handleToggleCriticalOnly = useCallback(() => {
    setCriticalOnly(v => !v);
  }, []);

  const handleToggleShowOnlyNew = useCallback(() => {
    setShowOnlyNew(v => !v);
  }, []);

  const handleToggleSelectMode = useCallback(() => {
    if (selectMode) { setSelectMode(false); setSelectedIds(new Set()); }
    else { setSelectMode(true); setSelectedIds(new Set(openFeedbackMessages.map(m => m.id))); }
  }, [selectMode, openFeedbackMessages]);

  const handleOpenMutedPrefs = useCallback(() => {
    setShowPrefsPanel(true);
  }, []);

  const handleChangeType = useCallback((value: string) => {
    if (sendState === 'needs_retry') return;
    updateComposerIntent({ type: value });
    setNewType(value);
    setLastArming(null);
    if (newContent || replyTo || pendingAttachmentsRef.current.length > 0) persistComposerEnvelope({ type: value });
  }, [newContent, persistComposerEnvelope, replyTo, sendState, updateComposerIntent]);

  const handleChangeContent = useCallback((value: string) => {
    // Do not silently abandon an unresolved identity: the original POST may
    // already have committed. The operator must retry/recover it first.
    if (sendState === 'needs_retry' && optimisticSend && value !== optimisticSend.content) return;
    updateComposerIntent({ content: value });
    setNewContent(value);
    if (value) {
      const captured = captureCurrentDraftRefs();
      setDraftSourceRefs(captured.sourceRefs);
      setDraftArtifactRefs(captured.artifactRefs);
      persistComposerEnvelope({ content: value, sourceRefs: captured.sourceRefs, artifactRefs: captured.artifactRefs });
    } else if (replyTo || pendingAttachmentsRef.current.length > 0) {
      persistComposerEnvelope({ content: '' });
    } else persistComposerEnvelope({ content: '' });
  }, [captureCurrentDraftRefs, optimisticSend, persistComposerEnvelope, replyTo, sendState, updateComposerIntent]);

  const handleCancelReply = useCallback(() => {
    if (sendState !== 'needs_retry') {
      setReplyTo(null);
      persistComposerEnvelope({ replyTo: null });
    }
  }, [persistComposerEnvelope, sendState]);

  const handleRemoveArtifactRef = useCallback((identity: string) => {
    if (sendState === 'sending' || sendState === 'needs_retry') return;
    const current = composerIntentRef.current;
    const artifactRefs = current.artifact_refs.filter(ref => artifactRefIdentity(ref) !== identity);
    const sourceRefs = current.source_refs.filter(ref => artifactRefIdentity(ref) !== identity);
    const next = updateComposerIntent({
      artifact_refs: artifactRefs,
      source_refs: sourceRefs,
      operator_intent: undefined,
    });
    setDraftArtifactRefs(artifactRefs);
    setDraftSourceRefs(sourceRefs);
    if (currentActorUserId && selectedProjectId) {
      saveComposerDraftSnapshot(currentActorUserId, selectedProjectId, next);
    }
  }, [currentActorUserId, saveComposerDraftSnapshot, selectedProjectId, sendState, updateComposerIntent]);

  async function handleAbandonPendingSend() {
    if (!optimisticSend || optimisticSend.state !== 'needs_retry' || !selectedProjectId || !currentActorUserId || submittingGuardRef.current.active) return;
    const projectId = selectedProjectId;
    const actorUserId = currentActorUserId;
    const identity = optimisticSend.key;
    const submitOwner = submittingGuardRef.current.begin(actorUserId, projectId);
    setSubmitting(true);
    try {
      const receipt = await findStrategicMessageByClientIdentity(projectId, identity);
      if (selectedProjectIdRef.current !== projectId || currentActorUserIdRef.current !== actorUserId) return;
      if (receipt) {
        setMessages(previous => mergeCanonicalConversationMessage(previous, receipt.message));
        if (['claimed', 'responded', 'superseded'].includes(receipt.message.handling_state ?? '')) {
          conversationSend.clear();
        } else {
          conversationSend.transition(
            receipt.wakeup_state === 'paused' || receipt.wakeup_state === 'waiting_for_capacity' ? 'waiting' : 'accepted',
            receipt.wakeup_state,
          );
        }
        clearPendingAttachments(identity);
        resetComposerIntent();
        clearSubmittedComposerDraft(actorUserId, projectId, optimisticSend.draft_write_id);
        addToast('Message was already accepted; recovered its canonical receipt.', 'success');
        return;
      }

      await Promise.allSettled((optimisticSend.payload?.attachments ?? []).flatMap(attachment => (
        attachment.attachment_id
          ? [deleteMessageAttachmentUpload(projectId, attachment.attachment_id)]
          : []
      )));
      try {
        await clearStoredPendingAttachments(actorUserId, projectId, identity);
      } catch {
        // Cleanup intent is committed before deletion; the next composer mount retries it.
      }
      setPendingAttachments(previous => {
        revokePendingAttachmentPreviews(previous);
        pendingAttachmentsRef.current = [];
        return [];
      });
      conversationSend.clear();
      resetComposerIntent();
      clearSubmittedComposerDraft(actorUserId, projectId, optimisticSend.draft_write_id);
      addToast('Pending message abandoned after confirming it was not accepted.', 'info');
    } catch {
      if (selectedProjectIdRef.current === projectId && currentActorUserIdRef.current === actorUserId) {
        addToast('Could not confirm whether the message was accepted; it was not abandoned.', 'error');
      }
    } finally {
      if (submittingGuardRef.current.finish(submitOwner)) {
        setSubmitting(false);
      }
    }
  }

  const handlePickStarterPrompt = useCallback((value: string) => {
    setNewContent(value);
    const captured = captureCurrentDraftRefs();
    setDraftSourceRefs(captured.sourceRefs);
    setDraftArtifactRefs(captured.artifactRefs);
    persistComposerEnvelope({ content: value, sourceRefs: captured.sourceRefs, artifactRefs: captured.artifactRefs });
    focusComposerTextarea();
  }, [captureCurrentDraftRefs, focusComposerTextarea, persistComposerEnvelope]);

  return (
    <div className="flex-1 overflow-hidden flex flex-col min-h-0">
      {/* Header */}
      <MessagesPanelHeader
        dividerTime={dividerTime}
        totalNewCount={totalNewCount}
        newHighSignalCount={newHighSignalCount}
        highSignalOpenCount={highSignalOpen.length}
        openCount={openMessages.length}
        mutedCount={mutedCount}
        webhookStats={webhookStats}
        sortMode={sortMode}
        criticalOnly={criticalOnly}
        showOnlyNew={showOnlyNew}
        selectMode={selectMode}
        openFeedbackCount={openFeedbackMessages.length}
        muteActive={muteActive}
        muteWindow={muteWindow}
        onSetMuteWindow={setMuteWindowPersisted}
        showPrefsPanel={showPrefsPanel}
        onShowPrefsChange={setShowPrefsPanel}
        messages={messages}
        eventPrefs={eventPrefs}
        onUpdatePref={updatePref}
        onResetAllPrefs={handleResetAllPrefs}
        searchQuery={searchQuery}
        onSearchChange={setSearchQuery}
        onOpenSaveDialog={openSaveDialog}
        savedViews={savedViews}
        activeViewId={activeViewId}
        defaultViewId={DEFAULT_VIEW_ID}
        activeTabModified={activeTabModified}
        openKebabId={openKebabId}
        setOpenKebabId={setOpenKebabId}
        kebabRef={kebabRef}
        onApplyView={applyView}
        onRenameView={openRenameDialog}
        onDeleteView={deleteView}
        onToggleSort={handleToggleSort}
        onToggleCriticalOnly={handleToggleCriticalOnly}
        onToggleShowOnlyNew={handleToggleShowOnlyNew}
        onToggleSelectMode={handleToggleSelectMode}
        onOpenMutedPrefs={handleOpenMutedPrefs}
        onClose={onClose}
        showCloseButton={resolvedShowCloseButton}
        filterChrome={resolvedFilterChrome}
      />

      {conversationMode && (
        <ConversationUtilityBar
          expanded={chatChromeExpanded} onExpand={() => persistChatChrome(true)} onCollapse={() => persistChatChrome(false)}
          searchQuery={searchQuery} onSearchChange={setSearchQuery}
          needsActionCount={usesStrategicReadModel ? visibleAttentionNeedsActionItems.length : 0} alertCount={alerts?.length ?? 0}
        />
      )}

      {/* Inline alerts — collapsed into the utility bar above by default in conversation mode. */}
      {(!conversationMode || chatChromeExpanded) && alerts && alerts.length > 0 && (
        <div className="shrink-0 border-b border-workbench-border px-3 py-2 md:px-4" data-testid="alerts-inline-banner">
          <AlertsBanner
            alerts={alerts}
            onDismiss={onDismissAlert ?? (() => {})}
            onDismissAll={onDismissAllAlerts}
            onViewTask={onViewAlertTask}
            projectId={selectedProjectId ?? undefined}
          />
        </div>
      )}

      {showStrategicStatusStrip && (
        <div className="shrink-0 border-b border-workbench-border bg-workbench-base px-3 py-1.5 md:px-4" data-testid="strategic-context-package-strip">
          <div className="mx-auto flex w-full max-w-[940px] min-w-0 flex-wrap items-center gap-x-2 gap-y-1 text-xs text-neutral-500" aria-label="Strategic chat status">
            <span className="shrink-0 font-semibold text-neutral-200">Strategic</span>
            <span className="shrink-0 text-neutral-400">{workbenchModeLabel(workbenchState?.interaction_mode)}</span>
            {strategistActive && (
              <span className="text-cyan-300">{cycleProgress?.phase_label ?? 'Working'}</span>
            )}
            {!strategistActive && strategicRefresh?.requested_at && (
              <span className="text-amber-300">Steer queued</span>
            )}
            {!strategistActive && !strategicRefresh?.requested_at && strategicRefresh?.last_fired_at && (
              <span className="text-emerald-300">Steer picked up · {timeAgo(strategicRefresh.last_fired_at)}</span>
            )}
            {effectiveContextPackage?.status && effectiveContextPackage.status !== 'ready' && (
              <span className={contextPackageTextTone(effectiveContextPackage.status)}>
                Context {effectiveContextPackage.status}
              </span>
            )}
            {effectiveContextPackage?.status === 'stale' && currentStrategicState?.sha_freshness?.verdict === 'stale' && (() => {
              const cSha = currentStrategicState.sha_freshness?.active_candidate_sha?.slice(0, 7);
              const iSha = currentStrategicState.sha_freshness?.integration_head_sha?.slice(0, 7);
              if (cSha && iSha) return <span className="text-amber-500/70">sha diverged: @{cSha} vs @{iSha}</span>;
              if (cSha || iSha) return <span className="text-amber-500/70">sha diverged</span>;
              return null;
            })()}
            {strategicAttention?.level === 'needs_operator' && attentionNeeds > 0 && (
              <span className={attentionTextTone(strategicAttention.level)}>
                {attentionNeeds} needs attention
              </span>
            )}
          </div>
        </div>
      )}

      {/* Unified strategist status chip: covers active cycle (indigo), pending steer (indigo),
          and steer-received receipt (emerald). The cycle-completion banner below is kept
          separate because it carries rich detail (direction counts, etc.). */}
      {showStrategistChip && (
        <div className="px-4 md:px-6 py-2 border-b border-neutral-800 flex items-center gap-2 shrink-0">
          <StrategistStatusChip status={strategistStatus} />
          {strategicRefresh?.requested_at && (
            <button
              type="button"
              onClick={() => { void handleCancelStrategicRefresh(); }}
              disabled={cancellingRefresh}
              className="ml-auto text-xs font-medium text-amber-300 hover:text-amber-100 underline underline-offset-2 disabled:opacity-50 disabled:no-underline"
              title="Retract this armed steer so the strategist will not run it"
            >
              {cancellingRefresh ? 'Cancelling…' : 'Cancel'}
            </button>
          )}
        </div>
      )}

      {/* Cycle completion receipt — shown after a cycle finishes until the next one begins.
          Summarises what changed: direction counts, running-update, problem-model. */}
      {lastCycleCompletion && !strategistActive && !conversationMode && (
        <div className={`px-4 md:px-6 py-2 border-b border-neutral-800 flex items-start gap-2 shrink-0 ${completionReceiptTone?.banner ?? 'bg-emerald-950/30'}`} role="status" aria-live="polite">
          <span className={`w-2 h-2 rounded-full mt-0.5 shrink-0 ${completionReceiptTone?.dot ?? 'bg-emerald-400'}`} aria-hidden="true" />
          <span className={`text-xs min-w-0 ${completionReceiptTone?.text ?? 'text-emerald-300'}`}>
            <span className="font-medium">
              {completionReceiptTone?.label ?? 'Cycle complete'}
            </span>
            {conversationMode && lastCycleCompletion.failure_reason && (
              <span className={`ml-1.5 ${completionReceiptTone?.message ?? 'text-emerald-400/80'}`}>{lastCycleCompletion.failure_reason.slice(0, 80)}</span>
            )}
            {!conversationMode && lastCycleCompletion.failure_reason && (
              <span className={`ml-1.5 ${completionReceiptTone?.message ?? 'text-emerald-400/80'}`}>{lastCycleCompletion.failure_reason.slice(0, 80)}</span>
            )}
            {!conversationMode && (lastCycleCompletion.created_count ?? 0) > 0 && (
              <span className="ml-1.5">· +{lastCycleCompletion.created_count} direction{lastCycleCompletion.created_count !== 1 ? 's' : ''}</span>
            )}
            {!conversationMode && (lastCycleCompletion.retired_count ?? 0) > 0 && (
              <span className="ml-1.5">· −{lastCycleCompletion.retired_count} retired</span>
            )}
            {!conversationMode && (lastCycleCompletion.staged_count ?? 0) > 0 && (
              <span className="ml-1.5">· {lastCycleCompletion.staged_count} direction{lastCycleCompletion.staged_count !== 1 ? 's' : ''} staged</span>
            )}
            {!conversationMode && (lastCycleCompletion.updated_count ?? 0) > 0 && (
              <span className="ml-1.5">· {lastCycleCompletion.updated_count} updated</span>
            )}
            {!conversationMode && (lastCycleCompletion.tasks_spawned ?? 0) > 0 && (
              <span className="ml-1.5">· {lastCycleCompletion.tasks_spawned} task{lastCycleCompletion.tasks_spawned !== 1 ? 's' : ''} spawned</span>
            )}
            {!conversationMode && lastCycleCompletion.running_update_changed && (
              <span className="ml-1.5">· running update written</span>
            )}
            {!conversationMode && lastCycleCompletion.problem_model_changed && (
              <span className="ml-1.5">· problem model updated</span>
            )}
            {!conversationMode && lastCycleCompletion.status_message && (
              <span className={`ml-1.5 ${completionReceiptTone?.message ?? 'text-emerald-400/80'} italic`}>{lastCycleCompletion.status_message.slice(0, 80)}</span>
            )}
            {!conversationMode && lastCycleCompletion.strategic_judgment_challenge && (
              <span className="ml-1.5 align-middle">
                <StrategicChallengeChip summary={lastCycleCompletion.strategic_judgment_challenge} />
              </span>
            )}
          </span>
        </div>
      )}

      {/* Needs-action strip — derived from the canonical attention model when the
          strategic read model is available. Shows needs_operator items at the top
          with lifecycle.clears_when so the operator sees they will self-clear. */}
      {(!conversationMode || chatChromeExpanded) && usesStrategicReadModel && visibleAttentionNeedsActionItems.length > 0 && (
        <div className="shrink-0 border-b border-workbench-border bg-amber-950/20 px-3 py-2 md:px-4" data-testid="needs-action-strip">
          <div className="mx-auto flex w-full max-w-[940px] min-w-0 flex-col gap-1.5">
            <div className="flex items-center gap-2">
              <span className="text-xs font-semibold text-amber-200">Needs your action</span>
              <span className="shrink-0 rounded-full bg-amber-800/60 px-1.5 text-ui-sm font-semibold text-amber-200">{visibleAttentionNeedsActionItems.length}</span>
            </div>
            <div className="flex flex-wrap gap-1.5">
              {visibleAttentionNeedsActionItems.slice(0, 6).map(item => {
                const isMessage = item.kind === 'message';
                const isConfirmation = item.kind === 'confirmation';
                const messageId = isMessage ? item.ref?.id : null;
                const isExpanded = isConfirmation && expandedConfirmationItemId === item.id;
                const handleClick = () => {
                  if (isMessage && messageId) {
                    void scrollConversationMessageIntoView(messageId).then(revealed => {
                      if (revealed) setHighlightedMessageId(messageId);
                    });
                  } else if (isConfirmation) {
                    setExpandedConfirmationItemId(prev => prev === item.id ? null : item.id);
                  }
                };
                const handleAccept = async () => {
                  const projectId = item.ref?.project_id ?? selectedProjectId;
                  const pendingId = item.ref?.id ?? item.id;
                  if (!projectId) return;
                  try {
                    await acceptPendingConfirmation(projectId, pendingId);
                    setResolvedConfirmationIds(prev => new Set(prev).add(item.id));
                    setExpandedConfirmationItemId(null);
                    refreshStrategicState?.();
                  } catch {
                    addToast?.('Failed to accept — try again', 'error');
                  }
                };
                const handleDismiss = async () => {
                  const projectId = item.ref?.project_id ?? selectedProjectId;
                  const pendingId = item.ref?.id ?? item.id;
                  if (!projectId) return;
                  try {
                    await dismissPendingConfirmation(projectId, pendingId);
                    setResolvedConfirmationIds(prev => new Set(prev).add(item.id));
                    setExpandedConfirmationItemId(null);
                    refreshStrategicState?.();
                  } catch {
                    addToast?.('Failed to dismiss — try again', 'error');
                  }
                };
                const handleAck = () => {
                  // Client-side dismiss: removes the item from the strip for this session.
                  // No server-side effect — the item reappears if still needs_operator after
                  // the next attention poll. Driven by lifecycle.acknowledgeable=true.
                  setResolvedConfirmationIds(prev => new Set(prev).add(item.id));
                };
                const isAcknowledgeable = isMessage && (item.lifecycle?.acknowledgeable ?? false);
                const operatorActionHint = item.lifecycle?.operator_action
                  ? lifecycleOperatorActionLabel(item.lifecycle.operator_action)
                  : null;
                return (
                  <div key={item.id} className="flex flex-col gap-1">
                    <div className="flex items-center gap-1.5">
                      <button
                        type="button"
                        onClick={handleClick}
                        disabled={!isMessage && !isConfirmation}
                        title={item.lifecycle?.clears_when ? lifecycleClearsLabel(item.lifecycle.clears_when) : undefined}
                        className={`inline-flex min-h-[44px] items-center gap-1.5 rounded-md border px-2 py-1 text-left text-xs transition-colors ${
                          isMessage
                            ? 'border-amber-700/50 bg-amber-950/50 hover:border-amber-600 hover:bg-amber-900/50 cursor-pointer'
                            : isConfirmation
                            ? 'border-amber-700/50 bg-amber-950/50 hover:border-amber-600 hover:bg-amber-900/50 cursor-pointer'
                            : 'border-neutral-800 bg-neutral-950/50 cursor-default'
                        }`}
                      >
                        <span className="shrink-0 text-sm leading-none" aria-hidden="true">{attentionItemIcon(item.kind)}</span>
                        <span className="truncate max-w-[220px] font-medium text-amber-100">{item.label}</span>
                        {operatorActionHint && (
                          <span className="shrink-0 hidden sm:inline text-neutral-400 text-ui-sm" data-testid="operator-action-hint">
                            {operatorActionHint}
                          </span>
                        )}
                        {item.lifecycle?.clears_when && (
                          <span className="shrink-0 hidden sm:inline text-neutral-500" title={lifecycleClearsLabel(item.lifecycle.clears_when)}>
                            auto-clears
                          </span>
                        )}
                      </button>
                      {isAcknowledgeable && (
                        /* eslint-disable-next-line viewport/min-touch-target */
                        <button
                          type="button"
                          onClick={handleAck}
                          title={lifecycleAcknowledgeEffectLabel(item.lifecycle?.acknowledge_effect)}
                          className="shrink-0 rounded border border-neutral-700/60 bg-neutral-900/40 px-1.5 py-0.5 text-ui-sm font-medium text-neutral-400 hover:bg-neutral-800/50 hover:text-neutral-200 transition-colors"
                          data-testid="attention-ack-btn"
                        >
                          Ack
                        </button>
                      )}
                    </div>
                    {isExpanded && (
                      <div className="flex gap-1.5 pl-1" data-testid="confirmation-chip-actions">
                        {/* eslint-disable-next-line viewport/min-touch-target */}
                        <button
                          type="button"
                          onClick={() => { void handleAccept(); }}
                          className="rounded border border-green-700/60 bg-green-900/40 px-2 py-0.5 text-xs font-medium text-green-200 hover:bg-green-800/50 transition-colors"
                          data-testid="confirmation-accept-btn"
                        >
                          Accept
                        </button>
                        {/* eslint-disable-next-line viewport/min-touch-target */}
                        <button
                          type="button"
                          onClick={() => { void handleDismiss(); }}
                          className="rounded border border-neutral-700/60 bg-neutral-900/40 px-2 py-0.5 text-xs font-medium text-neutral-300 hover:bg-neutral-800/50 transition-colors"
                          data-testid="confirmation-dismiss-btn"
                        >
                          Dismiss
                        </button>
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {/* Needs-action filter toggle — visible in triage mode when the canonical
          attention read model is available so the inbox defaults to action-first. */}
      {!conversationMode && usesStrategicReadModel && (
        <div className="shrink-0 flex items-center gap-1 border-b border-workbench-border bg-workbench-base px-3 py-1.5 md:px-4" data-testid="needs-action-filter-bar">
          {/* eslint-disable-next-line viewport/min-touch-target */}
          <button
            type="button"
            data-testid="needs-action-filter-needs-action"
            onClick={() => setNeedsActionOnly(true)}
            aria-pressed={needsActionOnly}
            className={`inline-flex items-center rounded px-2 py-0.5 text-ui-sm font-medium transition-colors ${needsActionOnly ? 'bg-amber-800/60 text-amber-200' : 'text-neutral-500 hover:text-neutral-300'}`}
          >
            Needs action
          </button>
          {/* eslint-disable-next-line viewport/min-touch-target */}
          <button
            type="button"
            data-testid="needs-action-filter-show-all"
            onClick={() => setNeedsActionOnly(false)}
            aria-pressed={!needsActionOnly}
            className={`inline-flex items-center rounded px-2 py-0.5 text-ui-sm font-medium transition-colors ${!needsActionOnly ? 'bg-neutral-700/60 text-neutral-200' : 'text-neutral-500 hover:text-neutral-300'}`}
          >
            Show all
          </button>
        </div>
      )}

      {/* Save view dialog */}
      {showSaveDialog && (
        <SaveViewDialog
          open={showSaveDialog}
          mode={saveDialogMode === 'rename' ? 'rename' : 'create'}
          name={saveDialogName}
          description={saveDialogDescription}
          error={saveDialogError}
          summaryText={viewSummaryLine(searchQuery, eventPrefs, sortMode, showResolved, showOnlyNew)}
          onNameChange={value => { setSaveDialogName(value); setSaveDialogError(null); }}
          onDescriptionChange={setSaveDialogDescription}
          onCommit={commitSaveDialog}
          onClose={closeSaveDialog}
        />
      )}

      {/* Messages list — chat-style scroll area */}
      <MessagesListView
        presentation={conversationMode ? 'conversation' : 'triage'}
        hasOlderConversationMessages={conversationMode && conversationHasMore}
        loadingOlderConversationMessages={loadingOlderConversation}
        onLoadOlderConversationMessages={() => loadOlderConversation()}
        conversationListRef={conversationListRef}
        streamingBubble={streamingTurn ? <StrategicStreamingBubble key={streamingTurn.key} segments={streamingTurn.segments} /> : null}
        scrollRef={scrollRef}
        dividerDismissedRef={dividerDismissedRef}
        messages={conversationMode ? renderedConversationMessages : messages}
        loading={loading}
        dividerTime={dividerTime}
        totalNewCount={totalNewCount}
        newMessageCounts={newMessageCounts}
        routineOpen={routineOpen}
        linkedMessages={linkedMessages}
        pinnedBlockers={pinnedBlockers}
        pinnedQuestions={pinnedQuestions}
        otherHighSignal={otherHighSignal}
        promotedGroups={promotedGroups}
        remainingRoutineRows={remainingRoutineRows}
        autoExpandedKeys={autoExpandedKeys}
        visibleRoutineCount={visibleRoutineCount}
        mutedRoutineCount={mutedRoutineCount}
        visibleResolved={visibleResolved}
        hiddenResolvedCount={hiddenResolvedCount}
        resolvedMessages={resolvedMessages}
        searchQuery={searchQuery}
        showOnlyNew={showOnlyNew}
        showResolved={showResolved}
        routineExpanded={routineExpanded}
        showScrollDown={showScrollDown}
        muteActive={muteActive}
        showTempMuteMenu={showTempMuteMenu}
        expandedGroupKeys={expandedGroupKeys}
        tempMutes={tempMutes}
        bubbleProps={bubbleProps}
        highlightedMessageId={highlightedMessageId}
        onDismissDivider={dismissDivider}
        onShowOnlyNew={handleShowOnlyNew}
        setExpandedGroupKeys={setExpandedGroupKeys}
        setRoutineExpanded={setRoutineExpanded}
        setShowResolved={setShowResolved}
        setShowTempMuteMenu={setShowTempMuteMenu}
        addTemporaryMute={addTemporaryMute}
        getTop3NoisyEventTypes={getTop3NoisyEventTypes}
        handleResolveGroup={handleResolveGroup}
        handleBulkDeleteResolved={handleBulkDeleteResolved}
        scrollToBottom={scrollToBottom}
      />

      {/* Batch action bar */}
      {!conversationMode && selectMode && selectedIds.size > 0 && (
        <MessagesBatchActionBar
          selectedCount={selectedIds.size}
          batchCreating={batchCreating}
          onCreateDirections={handleBatchCreateDirections}
        />
      )}

      {/* Convert-to-direction modal */}
      {!conversationMode && pendingConvertMsg && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60" role="dialog" aria-modal="true" aria-label="Create direction from message">
          <div className="bg-neutral-950 border border-neutral-800 rounded-lg shadow-xl w-full max-w-lg mx-4 flex flex-col max-h-[90vh]">
            <div className="px-5 py-3 border-b border-neutral-800 flex items-center justify-between shrink-0">
              <span className="text-sm font-medium text-neutral-200">Create direction from message</span>
              <button
                type="button"
                onClick={() => { setPendingConvertMsg(null); convertForm.resetAddForm(); }}
                className="text-neutral-500 hover:text-neutral-300 transition-colors p-1 min-h-[44px] min-w-[44px] flex items-center justify-center md:min-h-auto md:min-w-auto"
                aria-label="Close"
              >
                <svg className="w-4 h-4" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M6 18L18 6M6 6l12 12" /></svg>
              </button>
            </div>
            <div className="overflow-y-auto flex-1 min-h-0">
              {/* eslint-disable react-hooks/immutability */}
              <DirectionsAddForm
                form={convertForm}
                addDirection={handleSubmitConvertForm}
                stageTargetVersion={null}
              />
              {/* eslint-enable react-hooks/immutability */}
            </div>
            <div className="px-5 py-3 border-t border-neutral-800 flex justify-end gap-2 shrink-0">
              <button
                type="button"
                onClick={() => { setPendingConvertMsg(null); convertForm.resetAddForm(); }}
                className="text-xs text-neutral-400 hover:text-neutral-200 px-3 py-1.5 rounded border border-neutral-700 bg-neutral-900 hover:bg-neutral-800 transition-colors"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={handleSubmitConvertForm}
                disabled={convertSubmitting || !convertForm.newText.trim()}
                className="text-xs text-neutral-200 px-3 py-1.5 rounded border border-neutral-600 bg-neutral-800 hover:bg-neutral-700 disabled:opacity-40 transition-colors"
              >
                {convertSubmitting ? 'Creating…' : 'Create direction'}
              </button>
            </div>
          </div>
        </div>
      )}

      {/* Compose area — bottom, chat-style */}
      <MessagesComposer
        textareaRef={textareaRef}
        newType={effectiveNewType}
        newContent={newContent}
        submitting={submitting}
        sendLocked={attachmentMutating || restoringPendingAttachments}
        attachmentLocked={attachmentMutating || restoringPendingAttachments}
        sendState={sendState}
        wakeupState={sendWakeupState}
        editingLocked={sendState === 'sending' || sendState === 'needs_retry'}
        replyTo={replyTo}
        lastArming={lastArming}
        strategistBusy={strategistActive}
        disabledReason={selectedProjectId ? null : 'Select a project to message Strategic'}
        pendingAttachments={pendingAttachments}
        retainedAttachmentCount={sendState === 'needs_retry'
          ? Math.max(
              optimisticSend?.payload?.attachments?.length ?? 0,
              Array.isArray(optimisticSend?.context?.attachments) ? optimisticSend.context.attachments.length : 0,
            )
          : 0}
        sourceRefs={draftSourceRefs}
        artifactRefs={draftArtifactRefs}
        uploading={uploading}
        onAddAttachment={handleAddAttachment}
        onRemoveAttachment={handleRemoveAttachment}
        onRemoveArtifactRef={handleRemoveArtifactRef}
        onReplaceAttachmentFile={handleReplaceAttachmentFile}
        onChangeType={handleChangeType}
        onChangeContent={handleChangeContent}
        onCancelReply={handleCancelReply}
        onAbandonPending={handleAbandonPendingSend}
        onSubmit={handleSubmit}
        starterPrompts={starterPrompts ?? undefined}
        capabilityHint={starterPrompts ? WORKDAY_CAPABILITY_HINT : undefined}
        onPickStarterPrompt={handlePickStarterPrompt}
        chrome={conversationMode ? 'minimal' : 'full'}
        strategicContext={strategicComposerContext}
        onOpenStrategicContext={conversationMode ? (onOpenStrategicContextWorkspace ?? onOpenStrategicContextCommands) : undefined}
        onOpenCommandPalette={conversationMode ? openMessageCommandSurface : undefined}
      />
    </div>
  );
});
