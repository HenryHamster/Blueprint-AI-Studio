/* eslint-disable react-hooks/immutability, react-hooks/refs, react-hooks/set-state-in-effect */
import { memo, useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { auth, getIdToken } from '../firebase';
import { apiFetch, API_BASE_URL, authHeaders, type StrategicRefreshMarker } from '../api';
import type { StrategicCycleProgress, StrategicCycleCompletion, StrategicSessionDelta } from '../hooks/useLogViewer';
import { parseTimestamp, timeAgo } from '../utils/time';
import { type EventPref, loadEventPrefs, saveEventPrefs, effectiveSignal, getToastSeverity, countHighSignalMessages } from './eventPrefs';
import { type MuteWindow, loadMuteWindow, saveMuteWindow, clearMuteWindow } from './muteWindow';
import { type SavedView, loadSavedViews, saveSavedViews, SAVED_VIEWS_CAP } from './savedViews';
import { type TemporaryMutes, loadTemporaryMutes, saveTemporaryMutes } from './temporaryMutes';
import { groupRoutineMessages, outcomeBreakdown, isFailingGroup } from './messageGrouping';
import { loadLastSeen, saveLastSeen } from './lastSeen';
import { fetchServerNoisePrefs, saveServerNoisePrefs, fetchServerSavedViews, saveServerSavedViews, fetchServerTempMutes, saveServerTempMutes, fetchServerMuteWindow, saveServerMuteWindow } from './serverSync';
import type { MessageAttachment, ProjectMessage } from '../types/messages';
import {
  computeStrategicStreamingTurn,
  computeStrategicSessionActivity,
  countStrategicReplies,
  type StrategicLiveActivity,
} from '../utils/strategicSessionLive';
import { StrategicStreamingBubble, StrategicActivityStrip } from './StrategicLiveTurn';
import { SaveViewDialog } from './SaveViewDialog';
import { useMessageList } from './useMessageList';
import { MessagesListView } from './MessagesListView';
import { MessagesPanelHeader, type MessagesPanelFilterChrome, type WebhookStats } from './MessagesPanelHeader';
import { MessagesComposer, type PendingAttachment } from './MessagesComposer';
import { armingToastCopy, parseArmingOutcome, type ArmingOutcome, WORKDAY_CAPABILITY_HINT, WORKDAY_STARTER_PROMPTS } from './messageArming';
import { MessagesBatchActionBar } from './MessagesBatchActionBar';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { generateIdempotencyKey } from '../utils/idempotencyKey';
import { StrategicChallengeChip } from './StrategicChallengeChip';
import { useDirectionForm } from '../hooks/useDirectionForm';
import { DirectionsAddForm } from './DirectionsAddForm';
import {
  createPendingAttachment,
  removePendingAttachment,
  revokePendingAttachmentPreviews,
  uploadPendingMessageAttachments,
} from './messageAttachmentUpload';
import {
  createStrategicSessionMessage,
  fetchStrategicConversation,
  type StrategicContextPackageDescriptor,
  type StrategicMessageTarget,
  type StrategicStateResponse,
  type StrategicWorkbenchState,
} from '../strategicStateApi';
import { buildStrategicContextDescriptor, SESSION_IDLE_TTL_MS } from '../utils/strategicContextDescriptor';

const DEFAULT_VIEW_ID = '__default__';
const MESSAGE_CONTEXT_SELECTED_REF_DESCRIPTOR_LIMIT = 8;

// Receipt shown for at most 2 hours after a steer fires, preventing stale "picked up"
// messages that grow without bound, and blocking stale receipts from resurfacing after
// an operator cancels a fresh steer.
const STEER_RECEIPT_MAX_AGE_MS = 7_200_000;
const OPTIMISTIC_STRATEGIC_REFRESH_MAX_AGE_MS = 45_000;

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

function viewMatches(
  view: { query: string; prefs: Record<string, EventPref>; sort: 'importance' | 'recent'; showResolved: boolean; routineExpanded: boolean; showOnlyNew: boolean },
  state: { query: string; prefs: Record<string, EventPref>; sort: 'importance' | 'recent'; showResolved: boolean; routineExpanded: boolean; showOnlyNew: boolean },
): boolean {
  if (view.query !== state.query) return false;
  if (view.sort !== state.sort) return false;
  if (view.showResolved !== state.showResolved) return false;
  if (view.routineExpanded !== state.routineExpanded) return false;
  if (view.showOnlyNew !== state.showOnlyNew) return false;
  const aKeys = Object.keys(view.prefs);
  const bKeys = Object.keys(state.prefs);
  if (aKeys.length !== bKeys.length) return false;
  for (const k of aKeys) {
    if (view.prefs[k] !== state.prefs[k]) return false;
  }
  return true;
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
  /** Backwards-compatible alias for older call sites. */
  onOpenStrategicContextCommands?: () => void;
  /** Legacy refresh callback kept for callers that still wire the old prop name. */
  onStrategicRefresh?: () => void;
  /**
   * Live resident-session transcript deltas (from `useLogViewer`, one per
   * `strategic_session` WS frame). Rendered as a streaming/"typing" bubble in the
   * conversation transcript while a turn streams; reconciled into the durable
   * `from_type=strategic` ProjectMessage once it lands via the conversation refresh.
   */
  strategicSessionTranscript?: StrategicSessionDelta[];
}

type MessagesPanelPrefs = { version: 1; routineExpanded: boolean; showResolved: boolean; criticalOnly: boolean };

function messagesPanelPrefsKey(userId?: string | null): string {
  return `messages_panel_prefs:${userId || 'anonymous'}`;
}

function loadMessagesPanelPrefs(userId?: string | null): MessagesPanelPrefs {
  try {
    const raw = localStorage.getItem(messagesPanelPrefsKey(userId));
    if (!raw) return { version: 1, routineExpanded: false, showResolved: false, criticalOnly: false };
    const parsed = JSON.parse(raw) as Partial<MessagesPanelPrefs>;
    if (parsed.version !== 1 || typeof parsed.routineExpanded !== 'boolean' || typeof parsed.showResolved !== 'boolean' || typeof parsed.criticalOnly !== 'boolean') {
      return { version: 1, routineExpanded: false, showResolved: false, criticalOnly: false };
    }
    return parsed as MessagesPanelPrefs;
  } catch {
    return { version: 1, routineExpanded: false, showResolved: false, criticalOnly: false };
  }
}

function saveMessagesPanelPrefs(userId: string | null | undefined, prefs: MessagesPanelPrefs): void {
  try { localStorage.setItem(messagesPanelPrefsKey(userId), JSON.stringify(prefs)); } catch { /* ignore */ }
}

function composerDraftKey(userId?: string | null, projectId?: string | null): string {
  return `messages_composer_draft:${userId || 'anonymous'}:${projectId || 'unknown'}`;
}

function loadComposerDraft(userId?: string | null, projectId?: string | null): { type: string; content: string } | null {
  try {
    const raw = localStorage.getItem(composerDraftKey(userId, projectId));
    if (!raw) return null;
    const parsed = JSON.parse(raw) as { type?: unknown; content?: unknown };
    if (typeof parsed.type !== 'string' || typeof parsed.content !== 'string') return null;
    return { type: parsed.type, content: parsed.content };
  } catch {
    return null;
  }
}

function saveComposerDraft(userId: string | null | undefined, projectId: string | null | undefined, draft: { type: string; content: string }): void {
  if (!draft.content) return;
  try { localStorage.setItem(composerDraftKey(userId, projectId), JSON.stringify(draft)); } catch { /* ignore */ }
}

function clearComposerDraft(userId: string | null | undefined, projectId: string | null | undefined): void {
  try { localStorage.removeItem(composerDraftKey(userId, projectId)); } catch { /* ignore */ }
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
    && JSON.stringify(a.context ?? {}) === JSON.stringify(b.context ?? {});
}

function messageListsEqual(a: ProjectMessage[], b: ProjectMessage[]): boolean {
  return a.length === b.length && a.every((message, i) => messagesEqual(message, b[i]));
}

export const MessagesPanel = memo(function MessagesPanel({ selectedProjectId, githubRepo, defaultBranch, onClose, addToast, onMessageCountChange, onOpenTask, notifyOnHighSignalMessage, variant, filterChrome, showCloseButton, strategistActive, cycleProgress, lastCycleCompletion, focusComposerSignal, realtimeRefreshSignal, contextPackage, strategicState, strategicWorkbenchState, targetMessage, onTargetMessageHandled, onStrategicStateRefresh, onStrategicInteraction, onOpenStrategicContextWorkspace, onOpenCommandPalette, onOpenStrategicContextCommands, onStrategicRefresh, strategicSessionTranscript }: MessagesPanelProps) {
  const resolvedVariant = variant ?? (onClose ? 'modal' : 'page');
  const resolvedFilterChrome = filterChrome ?? (resolvedVariant === 'page' ? 'compact' : 'full');
  const resolvedShowCloseButton = showCloseButton ?? (resolvedVariant !== 'page' && Boolean(onClose));
  const conversationMode = resolvedFilterChrome === 'minimal';
  const currentStrategicState = strategicState?.project_id === selectedProjectId ? strategicState : null;
  const usesStrategicReadModel = currentStrategicState != null;
  const effectiveContextPackage = contextPackage ?? currentStrategicState?.context_package ?? null;
  const workbenchState = strategicWorkbenchState ?? currentStrategicState?.workbench ?? null;
  const strategicAttention = currentStrategicState?.attention ?? null;
  const refreshStrategicState = onStrategicStateRefresh ?? onStrategicRefresh;
  const recordStrategicInteraction = useCallback(() => {
    if (!onStrategicInteraction) return;
    Promise.resolve(onStrategicInteraction()).catch(() => {});
  }, [onStrategicInteraction]);
  const selectedProjectIdRef = useRef(selectedProjectId);
  // eslint-disable-next-line react-hooks/refs
  selectedProjectIdRef.current = selectedProjectId;
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
  const [messages, setMessages] = useState<ProjectMessage[]>([]);
  const [loading, setLoading] = useState(true);
  // Compose — initialized from the per-project draft (if any) so a reload restores it.
  const [newType, setNewType] = useState(() => loadComposerDraft(auth.currentUser?.email, selectedProjectId)?.type ?? 'FEEDBACK');
  const [newContent, setNewContent] = useState(() => loadComposerDraft(auth.currentUser?.email, selectedProjectId)?.content ?? '');
  const [submitting, setSubmitting] = useState(false);
  const [pendingAttachments, setPendingAttachments] = useState<PendingAttachment[]>([]);
  const [uploading, setUploading] = useState(false);
  const pendingAttachmentsRef = useRef<PendingAttachment[]>([]);
  // eslint-disable-next-line react-hooks/refs
  pendingAttachmentsRef.current = pendingAttachments;
  const clearPendingAttachments = useCallback(() => {
    setPendingAttachments(prev => {
      revokePendingAttachmentPreviews(prev);
      pendingAttachmentsRef.current = [];
      return [];
    });
  }, []);
  // Synchronous in-flight guard. `submitting` state updates asynchronously, so
  // two rapid keypresses (double-Enter) can both read the pre-update value and
  // each fire a request before React re-renders. This ref is set synchronously
  // at the very top of handleSubmit and cleared in its finally, closing that
  // race window so a burst collapses to a single POST on the client side (the
  // server idempotency key is the backstop for retries that do reach it).
  const submittingRef = useRef(false);
  const [replyTo, setReplyTo] = useState<{ id: string; content: string; from: string; message_type: string; status: string } | null>(null);
  const [highlightedMessageId, setHighlightedMessageId] = useState<string | null>(null);
  // INSTANT ACK — the operator's just-sent message, rendered optimistically at
  // the tail of the chat the moment they hit Send (before the POST round-trips),
  // then dropped once the durable message lands via fetchMessages. Reply-only /
  // resolve paths do not use it (those messages are the operator's answer to an
  // existing thread, not a new chat turn).
  // On mount, restore any in-flight optimistic bubble that was serialised to
  // sessionStorage before a tab switch unmounted this component. The bubble
  // survives the round-trip and is cleared once the durable message lands (see
  // the useEffect below). Entries older than 60 s are treated as stale and
  // discarded so they never ghost into a fresh session.
  const [optimisticSend, setOptimisticSend] = useState<{
    key: string; content: string; created_at: string; message_type: string;
  } | null>(() => {
    if (!selectedProjectId) return null;
    try {
      const raw = sessionStorage.getItem(`optimisticSend:${selectedProjectId}`);
      if (!raw) return null;
      const parsed = JSON.parse(raw) as { key: string; content: string; created_at: string; message_type: string };
      const age = Date.now() - new Date(parsed.created_at).getTime();
      return age < 60_000 ? parsed : null;
    } catch {
      return null;
    }
  });
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
  const [showScrollDown, setShowScrollDown] = useState(false);
  const prevMessageCountRef = useRef(0);
  const fetchMessagesRequestSeqRef = useRef(0);
  const lastStrategicSessionTargetFetchRef = useRef<string | null>(null);
  const lastRealtimeRefreshSignalRef = useRef<number | null>(null);
  const getStrategicSessionIncludeMessageId = useCallback((projectId: string | null, currentTargetMessageId: string | null): string | null => {
    if (!projectId) return null;
    if (currentTargetMessageId) return currentTargetMessageId;
    const pinnedTarget = strategicSessionPinnedTargetRef.current;
    return pinnedTarget?.projectId === projectId ? pinnedTarget.messageId : null;
  }, []);

  // Collapsed resolved
  const [showResolved, setShowResolved] = useState(() => loadMessagesPanelPrefs(auth.currentUser?.email).showResolved);
  // Routine ops group collapsed by default
  const [routineExpanded, setRoutineExpanded] = useState(() => loadMessagesPanelPrefs(auth.currentUser?.email).routineExpanded);
  // Critical-only severity filter for incident triage
  const [criticalOnly, setCriticalOnly] = useState(() => loadMessagesPanelPrefs(auth.currentUser?.email).criticalOnly);
  // Track which grouped-chip event_type keys are expanded
  const [expandedGroupKeys, setExpandedGroupKeys] = useState<Set<string>>(new Set());
  // Per-event-type mute/surface preferences (scoped to project)
  const [eventPrefs, setEventPrefs] = useState<Record<string, EventPref>>(() =>
    loadEventPrefs(auth.currentUser?.email, selectedProjectId),
  );
  const [showPrefsPanel, setShowPrefsPanel] = useState(false);
  const [sortMode, setSortMode] = useState<'importance' | 'recent'>('importance');
  const [searchQuery, setSearchQuery] = useState('');
  const [webhookStats, setWebhookStats] = useState<WebhookStats | null>(null);
  // Pending strategic-refresh marker (project.vision._strategic_refresh): set when
  // operator steering arms a cycle, cleared backend-side once the autonomous loop
  // consumes it. Drives the "strategic cycle pending" badge until then.
  const [strategicRefresh, setStrategicRefresh] = useState<StrategicRefreshMarker | null>(null);
  const optimisticStrategicRefreshRef = useRef<StrategicRefreshMarker | null>(null);
  const completionReceiptTone = lastCycleCompletion ? getCompletionReceiptTone(lastCycleCompletion.status) : null;
  // In-flight guard for the "cancel armed steer" affordance on the pending badge.
  const [cancellingRefresh, setCancellingRefresh] = useState(false);
  const [muteWindow, setMuteWindow] = useState<MuteWindow | null>(() => loadMuteWindow(auth.currentUser?.email, selectedProjectId));
  const [tempMutes, setTempMutes] = useState<TemporaryMutes>(() =>
    loadTemporaryMutes(auth.currentUser?.email, selectedProjectId),
  );
  const [showTempMuteMenu, setShowTempMuteMenu] = useState(false);

  // Saved views — named filter combos persisted per-user-per-project. activeViewId
  // tracks which tab is currently applied; when the operator mutates any underlying
  // filter the active tab is rendered as "modified" but state is not auto-cleared.
  const [savedViews, setSavedViews] = useState<SavedView[]>(() =>
    loadSavedViews(auth.currentUser?.email, selectedProjectId),
  );
  const [activeViewId, setActiveViewId] = useState<string>(DEFAULT_VIEW_ID);
  const [showSaveDialog, setShowSaveDialog] = useState(false);
  const [saveDialogMode, setSaveDialogMode] = useState<'create' | 'rename'>('create');
  const [saveDialogTargetId, setSaveDialogTargetId] = useState<string | null>(null);
  const [saveDialogName, setSaveDialogName] = useState('');
  const [saveDialogDescription, setSaveDialogDescription] = useState('');
  const [saveDialogError, setSaveDialogError] = useState<string | null>(null);
  const [openKebabId, setOpenKebabId] = useState<string | null>(null);
  const kebabRef = useRef<HTMLDivElement>(null);

  const openMessageCommandSurface = useCallback(() => {
    focusComposerTextarea();
    if (conversationMode) onOpenCommandPalette?.();
  }, [conversationMode, focusComposerTextarea, onOpenCommandPalette]);

  // Last-seen tracking: dividerTime is the threshold; when operator scrolls past or timeout fires, we persist it
  const [dividerTime, setDividerTime] = useState<number | null>(null);
  const dividerTimeRef = useRef<number | null>(null);
  const dividerDismissedRef = useRef(false);
  // Toggle to show only messages newer than the divider time
  const [showOnlyNew, setShowOnlyNew] = useState(false);

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
  }, []);

  // Stable ref for addToast
  const addToastRef = useRef(addToast);
  // eslint-disable-next-line react-hooks/refs
  addToastRef.current = addToast;
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
  const seenPromotedGroupKeysRef = useRef<Set<string>>(new Set());
  const eventPrefsRef = useRef(eventPrefs);
  // eslint-disable-next-line react-hooks/refs
  eventPrefsRef.current = eventPrefs;
  const tempMutesRef = useRef(tempMutes);
  // eslint-disable-next-line react-hooks/refs
  tempMutesRef.current = tempMutes;

  // Reset toast suppression state when switching projects so that the next
  // fetch is treated as an initial load for the new project (avoids a flood
  // of toasts for project B's pre-existing open BLOCKER/BUG/FEEDBACK items).
  // Also reload event prefs and initialize divider time for new project.
  useEffect(() => {
    let active = true;
    seenMessageIdsRef.current = new Set();
    seenPromotedGroupKeysRef.current = new Set();
    hasInitiallyLoadedRef.current = false;
    const localPrefs = loadEventPrefs(auth.currentUser?.email, selectedProjectId);
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setEventPrefs(localPrefs);
    setTempMutes(loadTemporaryMutes(auth.currentUser?.email, selectedProjectId));
    setWebhookStats(null);
    optimisticStrategicRefreshRef.current = null;
    if (!usesStrategicReadModelRef.current) setStrategicRefresh(null);
    setMuteWindow(loadMuteWindow(auth.currentUser?.email, selectedProjectId));
    const lastSeen = loadLastSeen(auth.currentUser?.email, selectedProjectId);
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
          saveLastSeen(auth.currentUser?.email, selectedProjectId, now);
          void postBackendLastSeen(selectedProjectId, now);
        }
      });
      // Prefer server noise prefs over localStorage — picks up prefs set on another device.
      fetchServerNoisePrefs(selectedProjectId).then(serverPrefs => {
        if (!active) return;
        if (serverPrefs !== null) {
          setEventPrefs(serverPrefs);
          saveEventPrefs(auth.currentUser?.email, selectedProjectId, serverPrefs);
        }
      });
      // Prefer server temp-mutes over localStorage — expiry semantics preserved (expired entries dropped).
      fetchServerTempMutes(selectedProjectId).then(serverMutes => {
        if (!active) return;
        if (serverMutes !== null) {
          setTempMutes(serverMutes);
          saveTemporaryMutes(auth.currentUser?.email, selectedProjectId, serverMutes);
        }
      });
      // Prefer server mute-window over localStorage — undefined = fetch failed, keep local fallback.
      fetchServerMuteWindow(selectedProjectId).then(serverWin => {
        if (!active) return;
        if (serverWin !== undefined) {
          setMuteWindow(serverWin);
          if (serverWin !== null) {
            saveMuteWindow(auth.currentUser?.email, selectedProjectId, serverWin);
          } else {
            clearMuteWindow(auth.currentUser?.email, selectedProjectId);
          }
        }
      });
    }
    // Reload saved views for the new project — views never bleed across projects.
    setSavedViews(loadSavedViews(auth.currentUser?.email, selectedProjectId));
    setActiveViewId(DEFAULT_VIEW_ID);
    setOpenKebabId(null);
    // Reload composer draft for the new project — drafts are scoped per-project
    // so switching projects shows that project's in-progress draft, not another's.
    const draft = loadComposerDraft(auth.currentUser?.email, selectedProjectId);
    setNewContent(draft?.content ?? '');
    setNewType(draft?.type ?? 'FEEDBACK');
    clearPendingAttachments();
    setUploading(false);
    if (selectedProjectId) {
      // Prefer server saved views over localStorage — picks up views created on another device.
      fetchServerSavedViews(selectedProjectId).then(serverViews => {
        if (!active) return;
        if (serverViews !== null) {
          setSavedViews(serverViews);
          saveSavedViews(auth.currentUser?.email, selectedProjectId, serverViews);
        }
      });
    }
    return () => { active = false; };
  }, [selectedProjectId, clearPendingAttachments]);

  useEffect(() => {
    return () => revokePendingAttachmentPreviews(pendingAttachmentsRef.current);
  }, []);

  // Close kebab menu on outside click — same pattern as MessageTypeDropdown.
  useEffect(() => {
    if (!openKebabId) return;
    function handleClickOutside(e: MouseEvent) {
      if (kebabRef.current && !kebabRef.current.contains(e.target as Node)) setOpenKebabId(null);
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [openKebabId]);

  useEffect(() => {
    if (!focusComposerSignal) return;
    focusComposerTextarea();
  }, [focusComposerSignal, focusComposerTextarea]);

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
  });

  // Badge/tab-title count. Passes eventPrefs/tempMutes so muted types are excluded and
  // surfaced types are included — matching App.tsx's closed-panel count exactly so the
  // badge does not jump when the operator opens the panel.
  const highSignalCount = useMemo(
    () => countHighSignalMessages(messages, eventPrefs, tempMutes),
    [messages, eventPrefs, tempMutes],
  );
  const conversationMessages = useMemo(() => {
    return messages
      .filter(msg => isConversationMessage(msg, eventPrefs, tempMutes))
      .slice()
      .sort((a, b) => {
        const aTime = a.created_at ? parseTimestamp(a.created_at).getTime() : 0;
        const bTime = b.created_at ? parseTimestamp(b.created_at).getTime() : 0;
        return aTime - bTime;
      });
  }, [messages, eventPrefs, tempMutes]);
  // Live streaming turn: the resident strategist's in-flight reply, previewed as a
  // "typing" bubble at the tail of the chat and reconciled into the durable
  // ProjectMessage once it lands (see computeStrategicStreamingTurn). Only relevant
  // in conversation mode; the persisted-message path renders regardless.
  const streamingTurn = useMemo(
    () => (conversationMode ? computeStrategicStreamingTurn(strategicSessionTranscript, conversationMessages) : null),
    [conversationMode, strategicSessionTranscript, conversationMessages],
  );
  // Live, ephemeral activity feed (commands / tools) for the in-flight turn.
  const liveActivity = useMemo(
    () => (conversationMode
      ? computeStrategicSessionActivity(strategicSessionTranscript, conversationMessages)
      : { items: [], latestLifecycle: null } as StrategicLiveActivity),
    [conversationMode, strategicSessionTranscript, conversationMessages],
  );
  // The activity strip is visible while any resident turn is in flight: operator
  // turns (responding) or autonomous turns (working), but not after done.
  // `workingSince` covers the optimistic-send period before the first lifecycle
  // frame lands. `responding` drives the operator-waiting indicator; `working`
  // drives the autonomous-advancing indicator — both show the strip.
  const strategicTurnWorking = conversationMode && (
    workingSince != null
    || liveActivity.latestLifecycle === 'responding'
    || liveActivity.latestLifecycle === 'working'
  ) && liveActivity.latestLifecycle !== 'done';
  // Render the operator's optimistic bubble until the durable copy lands — dedup
  // against any already-present user message with the same content so there is
  // never a duplicate flash between fetchMessages resolving and the clear.
  const renderedConversationMessages = useMemo(() => {
    if (!conversationMode || !optimisticSend) return conversationMessages;
    const alreadyDurable = conversationMessages.some(m => {
      const ft = (m.from_type || '').toLowerCase();
      return (ft === 'user' || ft === 'operator') && m.content === optimisticSend.content;
    });
    if (alreadyDurable) return conversationMessages;
    const optimisticMessage = {
      id: `optimistic:${optimisticSend.key}`,
      project_id: selectedProjectId ?? '',
      message_type: optimisticSend.message_type as ProjectMessage['message_type'],
      from_type: 'user' as ProjectMessage['from_type'],
      from_task_id: null,
      content: optimisticSend.content,
      context: {},
      status: 'open' as ProjectMessage['status'],
      resolution: null,
      resolved_at: null,
      created_at: optimisticSend.created_at,
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
    }
  }, [workingSince, conversationMessages]);
  // A `done` lifecycle frame ends the turn — clear the working state / strip even
  // before the durable reply is refetched (the streaming bubble keeps the final
  // reply text until it reconciles).
  useEffect(() => {
    if (workingSince == null) return;
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (liveActivity.latestLifecycle === 'done') setWorkingSince(null);
  }, [workingSince, liveActivity.latestLifecycle]);
  // Safety net: never leave the working state stuck if a turn silently stalls.
  useEffect(() => {
    if (workingSince == null) return;
    const timer = window.setTimeout(() => setWorkingSince(null), 120_000);
    return () => window.clearTimeout(timer);
  }, [workingSince]);
  // Reset instant-ack state on project switch so a prior project's pending turn
  // never bleeds onto the newly-selected project. Skip the first run so a
  // sessionStorage-restored optimistic bubble (from a same-project tab-switch)
  // is not immediately cleared on mount.
  const projectSwitchMountedRef = useRef(false);
  useEffect(() => {
    if (!projectSwitchMountedRef.current) {
      projectSwitchMountedRef.current = true;
      return;
    }
    setOptimisticSend(null);
    setWorkingSince(null);
  }, [selectedProjectId]);
  // Mirror optimisticSend to sessionStorage so it survives a tab-switch unmount.
  // Any setOptimisticSend call (set or clear) automatically keeps storage in sync.
  useEffect(() => {
    if (!selectedProjectId) return;
    const key = `optimisticSend:${selectedProjectId}`;
    if (optimisticSend) {
      try { sessionStorage.setItem(key, JSON.stringify(optimisticSend)); } catch { /* quota */ }
    } else {
      sessionStorage.removeItem(key);
    }
  }, [optimisticSend, selectedProjectId]);
  // After a remount (e.g. the user switched tabs and came back), handleSubmit
  // from the previous mount may have finished its POST without clearing the
  // optimistic bubble. Clear it here once the durable message lands so there
  // is no permanent ghost bubble.
  useEffect(() => {
    if (!optimisticSend) return;
    const landed = conversationMessages.some(m => {
      const ft = (m.from_type || '').toLowerCase();
      return (ft === 'user' || ft === 'operator') && m.content === optimisticSend.content;
    });
    // eslint-disable-next-line react-hooks/set-state-in-effect
    if (landed) setOptimisticSend(null);
  }, [conversationMessages, optimisticSend]);
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
    if (!targetMessageId) return;
    if (!conversationMode) {
      onTargetMessageHandled?.();
      return;
    }
    const visibleTarget = conversationMessages.find(msg => msg.id === targetMessageId) ?? null;
    const target = visibleTarget ?? messages.find(msg => msg.id === targetMessageId) ?? null;
    if (!target) {
      if (!loading && lastStrategicSessionTargetFetchRef.current === targetMessageId) {
        onTargetMessageHandled?.();
      }
      return;
    }
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setHighlightedMessageId(target.id);
    const node = scrollRef.current?.querySelector(`[data-message-id="${target.id}"]`);
    node?.scrollIntoView({ block: 'center', behavior: 'smooth' });
    if (targetMessage?.action === 'reply' && visibleTarget && canPrimeStrategicChatReply(visibleTarget)) {
      setReplyTo({
        id: visibleTarget.id,
        content: visibleTarget.content,
        from: messageReplyFromLabel(visibleTarget),
        message_type: visibleTarget.message_type,
        status: visibleTarget.status,
      });
      textareaRef.current?.focus();
    } else if (targetMessage?.action === 'reply') {
      addToastRef.current('Reply not primed: target must be an open Agent or Strategic message.', 'info');
    }
    onTargetMessageHandled?.();
  }, [
    conversationMessages,
    conversationMode,
    loading,
    messages,
    onTargetMessageHandled,
    targetMessage?.action,
    targetMessageId,
  ]);

  useEffect(() => {
    if (!highlightedMessageId) return;
    const timer = window.setTimeout(() => setHighlightedMessageId(null), 4000);
    return () => window.clearTimeout(timer);
  }, [highlightedMessageId]);

  // Persist + apply a mute-window change (arm/extend pass a window, clear passes null).
  // MuteWindowMenu owns its own open/close UI state and pushes changes back through here.
  const setMuteWindowPersisted = useCallback((win: MuteWindow | null) => {
    if (win) {
      saveMuteWindow(auth.currentUser?.email, selectedProjectId, win);
      if (selectedProjectId) void saveServerMuteWindow(selectedProjectId, win);
    } else {
      clearMuteWindow(auth.currentUser?.email, selectedProjectId);
      if (selectedProjectId) void saveServerMuteWindow(selectedProjectId, null);
    }
    setMuteWindow(win);
  }, [selectedProjectId]);

  function updatePref(et: string, pref: EventPref) {
    setEventPrefs(prev => {
      const next = pref === 'routine'
        ? Object.fromEntries(Object.entries(prev).filter(([k]) => k !== et)) as Record<string, EventPref>
        : { ...prev, [et]: pref };
      saveEventPrefs(auth.currentUser?.email, selectedProjectId, next);
      if (selectedProjectId) void saveServerNoisePrefs(selectedProjectId, next);
      return next;
    });
  }

  function addTemporaryMute(eventType: string, durationMs: number) {
    setTempMutes(prev => {
      const next = { ...prev, [eventType]: Date.now() + durationMs };
      saveTemporaryMutes(auth.currentUser?.email, selectedProjectId, next);
      if (selectedProjectId) void saveServerTempMutes(selectedProjectId, next);
      return next;
    });
    setShowTempMuteMenu(false);
  }

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

  const activeView = useMemo(
    () => savedViews.find(v => v.id === activeViewId) ?? null,
    [savedViews, activeViewId],
  );

  // Active tab is "modified" when current panel state diverges from its saved
  // snapshot. For Default the baseline is the empty/initial state.
  const activeTabModified = useMemo(() => {
    const current = { query: searchQuery, prefs: eventPrefs, sort: sortMode, showResolved, routineExpanded, showOnlyNew };
    if (activeViewId === DEFAULT_VIEW_ID) {
      return !viewMatches(
        { query: '', prefs: {}, sort: 'importance', showResolved: false, routineExpanded: false, showOnlyNew: false },
        current,
      );
    }
    if (!activeView) return false;
    return !viewMatches(activeView, current);
  }, [activeView, activeViewId, searchQuery, eventPrefs, sortMode, showResolved, routineExpanded, showOnlyNew]);

  function applyView(viewId: string) {
    setOpenKebabId(null);
    if (viewId === DEFAULT_VIEW_ID) {
      setActiveViewId(DEFAULT_VIEW_ID);
      setSearchQuery('');
      setEventPrefs({});
      saveEventPrefs(auth.currentUser?.email, selectedProjectId, {});
      if (selectedProjectId) void saveServerNoisePrefs(selectedProjectId, {});
      setSortMode('importance');
      setShowResolved(false);
      setRoutineExpanded(false);
      setShowOnlyNew(false);
      return;
    }
    const view = savedViews.find(v => v.id === viewId);
    if (!view) return;
    setActiveViewId(view.id);
    setSearchQuery(view.query);
    setEventPrefs(view.prefs);
    saveEventPrefs(auth.currentUser?.email, selectedProjectId, view.prefs);
    if (selectedProjectId) void saveServerNoisePrefs(selectedProjectId, view.prefs);
    setSortMode(view.sort);
    setShowResolved(view.showResolved);
    setRoutineExpanded(view.routineExpanded);
    setShowOnlyNew(view.showOnlyNew);
  }

  function openSaveDialog() {
    setSaveDialogMode('create');
    setSaveDialogTargetId(null);
    setSaveDialogName('');
    setSaveDialogDescription('');
    setSaveDialogError(null);
    setShowSaveDialog(true);
  }

  function openRenameDialog(view: SavedView) {
    setSaveDialogMode('rename');
    setSaveDialogTargetId(view.id);
    setSaveDialogName(view.name);
    setSaveDialogDescription(view.description ?? '');
    setSaveDialogError(null);
    setShowSaveDialog(true);
    setOpenKebabId(null);
  }

  function closeSaveDialog() {
    setShowSaveDialog(false);
    setSaveDialogError(null);
  }

  function commitSaveDialog() {
    const name = saveDialogName.trim();
    if (!name) {
      setSaveDialogError('Name is required');
      return;
    }
    if (saveDialogMode === 'create') {
      if (savedViews.length >= SAVED_VIEWS_CAP) {
        setSaveDialogError(`Cap reached (${SAVED_VIEWS_CAP}). Delete a view to add another.`);
        return;
      }
      const view: SavedView = {
        id: `view_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`,
        name,
        description: saveDialogDescription.trim() || undefined,
        query: searchQuery,
        prefs: eventPrefs,
        sort: sortMode,
        showResolved,
        routineExpanded,
        showOnlyNew,
        createdAt: Date.now(),
      };
      const next = [...savedViews, view];
      setSavedViews(next);
      saveSavedViews(auth.currentUser?.email, selectedProjectId, next);
      if (selectedProjectId) void saveServerSavedViews(selectedProjectId, next);
      setActiveViewId(view.id);
    } else if (saveDialogTargetId) {
      const next = savedViews.map(v =>
        v.id === saveDialogTargetId
          ? { ...v, name, description: saveDialogDescription.trim() || undefined }
          : v,
      );
      setSavedViews(next);
      saveSavedViews(auth.currentUser?.email, selectedProjectId, next);
      if (selectedProjectId) void saveServerSavedViews(selectedProjectId, next);
    }
    closeSaveDialog();
  }

  function deleteView(viewId: string) {
    const next = savedViews.filter(v => v.id !== viewId);
    setSavedViews(next);
    saveSavedViews(auth.currentUser?.email, selectedProjectId, next);
    if (selectedProjectId) void saveServerSavedViews(selectedProjectId, next);
    setOpenKebabId(null);
    if (activeViewId === viewId) setActiveViewId(DEFAULT_VIEW_ID);
  }

  useEffect(() => {
    saveMessagesPanelPrefs(auth.currentUser?.email, { version: 1, routineExpanded, showResolved, criticalOnly });
  }, [routineExpanded, showResolved, criticalOnly]);

  // Auto-clear mute window when it expires
  const muteExpiresAt = muteWindow?.expiresAt;
  useEffect(() => {
    if (!muteExpiresAt) return;
    const interval = setInterval(() => {
      if (Date.now() >= muteExpiresAt) {
        clearMuteWindow(auth.currentUser?.email, selectedProjectId);
        setMuteWindow(null);
      }
    }, 30_000);
    return () => clearInterval(interval);
  }, [muteExpiresAt, selectedProjectId]);

  // Auto-clean expired temporary mutes
  useEffect(() => {
    const now = Date.now();
    const cleaned = Object.fromEntries(
      Object.entries(tempMutes).filter(([, expiresAt]) => expiresAt > now)
    );
    if (Object.keys(cleaned).length !== Object.keys(tempMutes).length) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setTempMutes(cleaned);
      saveTemporaryMutes(auth.currentUser?.email, selectedProjectId, cleaned);
    }
  }, [selectedProjectId, tempMutes]);

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
        const includeMessageId = getStrategicSessionIncludeMessageId(selectedProjectId, targetMessageId);
        const result = await fetchStrategicConversation(selectedProjectId, includeMessageId);
        const currentIncludeMessageId = getStrategicSessionIncludeMessageId(
          selectedProjectIdRef.current,
          targetMessageIdRef.current,
        );
        if (
          selectedProjectIdRef.current !== selectedProjectId
          || !isCurrentRequest()
          || currentIncludeMessageId !== includeMessageId
        ) {
          return;
        }
        lastStrategicSessionTargetFetchRef.current = includeMessageId;
        const sessionMessages = result.conversation?.items ?? [];
        sessionMessages.forEach(m => seenMessageIdsRef.current.add(m.id));
        hasInitiallyLoadedRef.current = true;
        setMessages(prev => {
          if (messageListsEqual(prev, sessionMessages)) return prev;
          return sessionMessages;
        });
        if (!usesStrategicReadModelRef.current) {
          onMessageCountChange?.(countHighSignalMessages(sessionMessages, eventPrefsRef.current, tempMutesRef.current));
        }
        return;
      }
      const params = new URLSearchParams({ project_id: selectedProjectId, status: 'all', limit: searchQuery.trim() ? '200' : '100' });
      if (searchQuery.trim()) params.set('q', searchQuery.trim());
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      if (!isCurrentRequest()) return;
      const msgs: ProjectMessage[] = data.messages ?? [];

      // Detect new high-signal messages for toast alerts
      if (hasInitiallyLoadedRef.current) {
        const newHighSignal = msgs.filter(m =>
          m.status === 'open' &&
          effectiveSignal(m, eventPrefsRef.current, tempMutesRef.current) === 'high' &&
          !seenMessageIdsRef.current.has(m.id)
        );

        if (newHighSignal.length > 0) {
          const summary = newHighSignal.length === 1
            ? `New ${newHighSignal[0].message_type === 'BLOCKER' ? 'BLOCKER' : newHighSignal[0].message_type === 'BUG_REPORT' ? 'BUG' : newHighSignal[0].message_type === 'FEEDBACK' ? 'FEEDBACK' : 'high-signal message'}: ${newHighSignal[0].content.slice(0, 80)}`
            : `${newHighSignal.length} new high-signal messages`;

          // Use the highest severity among the new messages
          const toastSeverity = newHighSignal.some(m => getToastSeverity(m) === 'error') ? 'error' : 'info';
          addToastRef.current(summary, toastSeverity);

          // Fire desktop notifications for each high-signal message when opted in
          if (notifyOnHighSignalMessageRef.current && 'Notification' in window && Notification.permission === 'granted') {
            for (const msg of newHighSignal) {
              new Notification(msg.message_type, { body: msg.content.slice(0, 100) });
            }
          }
        }
      }

      // Detect + toast for newly promoted failing routine groups
      const wasLoaded = hasInitiallyLoadedRef.current;
      const routineMsgs = msgs.filter(m => m.status === 'open' && effectiveSignal(m, eventPrefsRef.current, tempMutesRef.current) === 'routine');
      for (const row of groupRoutineMessages(routineMsgs)) {
        if (row.kind === 'group' && isFailingGroup(row.msgs) && eventPrefsRef.current[row.event_type] !== 'mute') {
          if (wasLoaded && !seenPromotedGroupKeysRef.current.has(row.key)) {
            const bd = outcomeBreakdown(row.msgs);
            addToastRef.current(`Failing group: ${row.msgs.length} ${row.label} ${bd}`, 'error');
          }
          seenPromotedGroupKeysRef.current.add(row.key);
        }
      }

      // Update tracking of seen message IDs
      msgs.forEach(m => seenMessageIdsRef.current.add(m.id));
      hasInitiallyLoadedRef.current = true;

      // Only update state if messages actually changed — prevents scroll jumps from polling
      setMessages(prev => {
        if (messageListsEqual(prev, msgs)) return prev;
        return msgs;
      });
      onMessageCountChange?.(countHighSignalMessages(msgs, eventPrefsRef.current, tempMutesRef.current));
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
  }, [conversationMode, getStrategicSessionIncludeMessageId, selectedProjectId, onMessageCountChange, searchQuery, targetMessageId, usesStrategicReadModel]);

  usePoller(fetchMessages, { intervalMs: DEFAULT_POLL_MS, deps: [fetchMessages] });

  // Record chat data-ready timing when conversation messages first render (once per page load)
  const initialLoadDoneRef = useRef(false);
  useEffect(() => {
    if (loading || initialLoadDoneRef.current || !conversationMode) return;
    initialLoadDoneRef.current = true;
    const ms = Math.round(performance.now());
    const surface = (window.location.hash.replace(/^#\/?/, '').split('/')[0] || 'chat').slice(0, 64);
    getIdToken().then(token => {
      if (!token) return;
      fetch('/api/admin/ui-load-timing', {
        method: 'POST',
        headers: { 'Authorization': `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ surface, page_ready_ms: ms, sample_type: 'chat_data_ready' }),
      }).catch(() => {});
    }).catch(() => {});
  }, [loading, conversationMode]);

  useEffect(() => {
    if (!realtimeRefreshSignal || realtimeRefreshSignal === lastRealtimeRefreshSignalRef.current) return;
    lastRealtimeRefreshSignalRef.current = realtimeRefreshSignal;
    void fetchMessages();
  }, [fetchMessages, realtimeRefreshSignal]);

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
      // First load — jump to bottom instantly
      requestAnimationFrame(() => el.scrollTo({ top: el.scrollHeight, behavior: 'instant' }));
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

  // Keep the chat pinned to the live streaming preview as it grows — but only
  // when the operator is already near the bottom, so a deliberate scroll-up to
  // read history is never yanked back down mid-turn.
  const streamingText = streamingTurn?.text ?? '';
  useEffect(() => {
    if (!conversationMode || !streamingText) return;
    const el = scrollRef.current;
    if (!el) return;
    const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 160;
    if (nearBottom) {
      requestAnimationFrame(() => el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' }));
    }
  }, [conversationMode, streamingText]);

  // Update parent with the high-signal count for the tab-title badge. highSignalCount is
  // countHighSignalMessages(messages), identical to the fetch callback and the closed-panel
  // App.tsx path, so the badge stays stable across open/closed transitions.
  useEffect(() => {
    if (conversationMode && usesStrategicReadModel) return;
    onMessageCountChange?.(highSignalCount);
  }, [conversationMode, highSignalCount, onMessageCountChange, usesStrategicReadModel]);

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

  function handleAddAttachment(file: File) {
    if (submittingRef.current) return;
    setPendingAttachments(prev => {
      const next = [...prev, createPendingAttachment(file)];
      pendingAttachmentsRef.current = next;
      return next;
    });
  }

  function handleRemoveAttachment(id: string) {
    if (submittingRef.current) return;
    setPendingAttachments(prev => {
      const next = removePendingAttachment(prev, id);
      pendingAttachmentsRef.current = next;
      return next;
    });
  }

  async function handleSubmit() {
    const submittedContent = newContent.trim();
    const attachmentsForSubmit = pendingAttachmentsRef.current;
    if (!selectedProjectId || (!submittedContent && attachmentsForSubmit.length === 0)) return;
    if (replyTo && replyTo.message_type === 'QUESTION' && replyTo.status === 'open' && !submittedContent) return;
    // Synchronous guard first — reject re-entrant calls before the async
    // `setSubmitting` has a chance to re-render, so rapid double-Enter cannot
    // launch two requests.
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    try {
      // When the operator types an answer to an open QUESTION, resolve the question
      // via PATCH — the backend posts a question_answer NOTE (linked back to the
      // question) and marks the question resolved so the strategist sees it answered.
      if (replyTo && replyTo.message_type === 'QUESTION' && replyTo.status === 'open') {
        const workbenchContext = buildMessageWorkbenchContext(currentStrategicState, strategicWorkbenchState);
        const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/${replyTo.id}`, {
          method: 'PATCH',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            resolution: submittedContent,
            ...(workbenchContext ? { workbench_context: workbenchContext } : {}),
          }),
        });
        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        idempotencyKeyRef.current = null;
        setNewContent('');
        clearComposerDraft(auth.currentUser?.email, selectedProjectId);
        setReplyTo(null);
        setLastArming(null);
        if (textareaRef.current) textareaRef.current.style.height = 'auto';
        addToast('Question answered', 'success');
        void refreshStrategicState?.();
        recordStrategicInteraction();
        await fetchMessages();
        return;
      }
      let uploadedAttachments: MessageAttachment[] = [];
      if (attachmentsForSubmit.length > 0) {
        setUploading(true);
        try {
          uploadedAttachments = await uploadPendingMessageAttachments(selectedProjectId, attachmentsForSubmit);
        } catch (err) {
          addToast(err instanceof Error ? err.message : 'Failed to upload attachment', 'error');
          return;
        } finally {
          setUploading(false);
        }
      }

      const context: Record<string, unknown> = {};
      if (uploadedAttachments.length > 0) {
        context.attachments = uploadedAttachments;
      }
      if (replyTo) {
        context.reply_to = replyTo.id;
        context.reply_preview = replyTo.content.slice(0, 120);
      }
      const workbenchContext = buildMessageWorkbenchContext(currentStrategicState, strategicWorkbenchState);
      // Reuse the draft's key across a double-submit / retry so the backend
      // dedups; only mint a new one when the previous send fully succeeded.
      if (!idempotencyKeyRef.current) {
        idempotencyKeyRef.current = generateIdempotencyKey();
      }
      if (conversationMode) {
        // INSTANT ACK: optimistically show the operator's message + working state
        // the moment they Send, before the POST round-trips (zero dead time). The
        // durable copy lands via fetchMessages below and the optimistic one is
        // dropped; the working state persists until the strategist replies.
        setOptimisticSend({
          key: idempotencyKeyRef.current,
          content: submittedContent,
          created_at: new Date().toISOString(),
          message_type: effectiveNewType,
        });
        strategicReplyBaselineRef.current = countStrategicReplies(conversationMessages);
        setWorkingSince(Date.now());
        // Clear the composer immediately so the optimistic bubble reads as sent.
        setNewContent('');
        clearComposerDraft(auth.currentUser?.email, selectedProjectId);
        if (textareaRef.current) textareaRef.current.style.height = 'auto';
        const data = await createStrategicSessionMessage(selectedProjectId, {
          message_type: effectiveNewType,
          content: submittedContent,
          idempotency_key: idempotencyKeyRef.current,
          ...(Object.keys(context).length > 0 ? { context } : {}),
          ...(workbenchContext ? { workbench_context: workbenchContext } : {}),
        });
        const arming = parseArmingOutcome(data?.arming);
        idempotencyKeyRef.current = null;
        setReplyTo(null);
        setLastArming(arming);
        clearPendingAttachments();
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
        // Durable operator message is now present — drop the optimistic bubble
        // (renderedConversationMessages also dedups, so there is no flash).
        setOptimisticSend(null);
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
      // Read the server's real arming decision (additive, backward-compatible
      // field) so the toast and composer hint reflect what actually happened
      // rather than a type-only guess. Tolerate older responses without it.
      let arming: ArmingOutcome | null = null;
      try {
        const data = await res.json();
        arming = parseArmingOutcome(data?.arming);
      } catch {
        // No/invalid JSON body — fall back to generic confirmation copy.
      }
      // Sent cleanly — the next distinct message gets a fresh key.
      idempotencyKeyRef.current = null;
      setNewContent('');
      clearComposerDraft(auth.currentUser?.email, selectedProjectId);
      setReplyTo(null);
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
      // The send failed — drop the optimistic bubble + working state so the UI
      // reflects reality (the server idempotency key covers a manual resend).
      setOptimisticSend(null);
      setWorkingSince(null);
      addToast('Failed to send message', 'error');
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
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
    textareaRef.current?.focus();
  }, []);

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
        onResetAllPrefs={() => { setEventPrefs({}); saveEventPrefs(auth.currentUser?.email, selectedProjectId, {}); if (selectedProjectId) void saveServerNoisePrefs(selectedProjectId, {}); }}
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
        onToggleSort={() => setSortMode(m => m === 'importance' ? 'recent' : 'importance')}
        onToggleCriticalOnly={() => setCriticalOnly(v => !v)}
        onToggleShowOnlyNew={() => setShowOnlyNew(v => !v)}
        onToggleSelectMode={() => {
          if (selectMode) { setSelectMode(false); setSelectedIds(new Set()); }
          else { setSelectMode(true); setSelectedIds(new Set(openFeedbackMessages.map(m => m.id))); }
        }}
        onOpenMutedPrefs={() => setShowPrefsPanel(true)}
        onClose={onClose}
        showCloseButton={resolvedShowCloseButton}
        filterChrome={resolvedFilterChrome}
      />

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

      {/* Strategic-refresh indicator: either pending (armed but not yet fired) or
          a receipt showing when a prior steer was picked up and acted upon. Hidden
          once the cycle starts (strategistActive), at which point "Strategist working…"
          takes over. Displays the queued directive excerpt and coalesce count. */}
      {strategicRefresh && !strategistActive && !conversationMode && (
        <div className={`px-4 md:px-6 py-2 border-b ${strategicRefresh.requested_at ? 'border-neutral-800 bg-amber-950/40' : 'border-green-800 bg-emerald-950/40'} flex items-center gap-2 shrink-0`} role="status" aria-live="polite">
          <span className={`w-2 h-2 rounded-full ${strategicRefresh.requested_at ? 'bg-amber-400 animate-pulse' : 'bg-emerald-400'}`} aria-hidden="true" />
          <span className={`text-xs font-medium ${strategicRefresh.requested_at ? 'text-amber-300' : 'text-emerald-300'}`}>
            {strategicRefresh.requested_at ? (
              <>
                Strategic cycle pending{strategicRefresh.origin ? ` · ${strategicRefresh.origin}` : ''}
                {strategicRefresh.excerpt && (
                  <>
                    {' · '}
                    <span className="italic">{strategicRefresh.excerpt.slice(0, 120)}{strategicRefresh.excerpt.length > 120 ? '…' : ''}</span>
                  </>
                )}
                {strategicRefresh.operator_count && strategicRefresh.operator_count > 1 && (
                  <> · +{strategicRefresh.operator_count - 1} {strategicRefresh.operator_count - 1 === 1 ? 'steer' : 'steers'} coalesced</>
                )}
                {` · armed ${timeAgo(strategicRefresh.requested_at)}`}
              </>
            ) : (
              <>
                Steer picked up{strategicRefresh.last_fired_at ? ` · strategic cycle ran ${timeAgo(strategicRefresh.last_fired_at)}` : ''}
              </>
            )}
          </span>
          {strategicRefresh.requested_at && (
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

      {/* Live strategist activity indicator — driven by strategic_cycle_started/completed
          WS events. Shows phase label and steering excerpt when the enriched event payload
          is available, otherwise falls back to the minimal "working" text. */}
      {strategistActive && !conversationMode && (
        <div className="px-4 md:px-6 py-2 border-b border-neutral-800 bg-indigo-950/40 flex items-start gap-2 shrink-0" role="status" aria-live="polite">
          <span className="w-2 h-2 rounded-full bg-indigo-400 animate-pulse mt-0.5 shrink-0" aria-hidden="true" />
          <span className="text-xs text-indigo-300 font-medium min-w-0">
            {cycleProgress?.phase_label ? `Strategist: ${cycleProgress.phase_label}…` : 'Strategist working…'}
            {cycleProgress?.steering_excerpt && (
              <span className="ml-1.5 text-indigo-400/80 italic font-normal truncate block">
                "{cycleProgress.steering_excerpt.slice(0, 100)}{cycleProgress.steering_excerpt.length > 100 ? '…' : ''}"
              </span>
            )}
          </span>
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
        streamingBubble={streamingTurn ? <StrategicStreamingBubble key={streamingTurn.key} text={streamingTurn.text} /> : null}
        activityStrip={strategicTurnWorking ? <StrategicActivityStrip items={liveActivity.items} /> : null}
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
        replyTo={replyTo}
        lastArming={lastArming}
        strategistBusy={strategistActive}
        disabledReason={selectedProjectId ? null : 'Select a project to message Strategic'}
        pendingAttachments={pendingAttachments}
        uploading={uploading}
        onAddAttachment={handleAddAttachment}
        onRemoveAttachment={handleRemoveAttachment}
        onChangeType={(value) => { setNewType(value); setLastArming(null); if (newContent) saveComposerDraft(auth.currentUser?.email, selectedProjectId, { type: value, content: newContent }); }}
        onChangeContent={(value) => { setNewContent(value); if (value) { saveComposerDraft(auth.currentUser?.email, selectedProjectId, { type: effectiveNewType, content: value }); } else { clearComposerDraft(auth.currentUser?.email, selectedProjectId); } }}
        onCancelReply={() => setReplyTo(null)}
        onSubmit={handleSubmit}
        starterPrompts={starterPrompts ?? undefined}
        capabilityHint={starterPrompts ? WORKDAY_CAPABILITY_HINT : undefined}
        onPickStarterPrompt={(value) => { setNewContent(value); saveComposerDraft(auth.currentUser?.email, selectedProjectId, { type: effectiveNewType, content: value }); focusComposerTextarea(); }}
        chrome={conversationMode ? 'minimal' : 'full'}
        strategicContext={strategicComposerContext}
        onOpenStrategicContext={conversationMode ? (onOpenStrategicContextWorkspace ?? onOpenStrategicContextCommands) : undefined}
        onOpenCommandPalette={conversationMode ? openMessageCommandSurface : undefined}
      />
    </div>
  );
});
