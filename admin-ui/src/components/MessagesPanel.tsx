import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { auth } from '../firebase';
import { apiFetch, API_BASE_URL, authHeaders, type StrategicRefreshMarker } from '../api';
import type { StrategicCycleProgress, StrategicCycleCompletion } from '../hooks/useLogViewer';
import { parseTimestamp, timeAgo } from '../utils/time';
import { type EventPref, loadEventPrefs, saveEventPrefs, effectiveSignal, getToastSeverity, countHighSignalMessages } from './eventPrefs';
import { type MuteWindow, loadMuteWindow, saveMuteWindow, clearMuteWindow } from './muteWindow';
import { type SavedView, loadSavedViews, saveSavedViews, SAVED_VIEWS_CAP } from './savedViews';
import { type TemporaryMutes, loadTemporaryMutes, saveTemporaryMutes } from './temporaryMutes';
import { groupRoutineMessages, outcomeBreakdown, isFailingGroup } from './messageGrouping';
import { loadLastSeen, saveLastSeen } from './lastSeen';
import { fetchServerNoisePrefs, saveServerNoisePrefs, fetchServerSavedViews, saveServerSavedViews, fetchServerTempMutes, saveServerTempMutes, fetchServerMuteWindow, saveServerMuteWindow } from './serverSync';
import { type ProjectMessage } from './MessageBubble';
import { SaveViewDialog } from './SaveViewDialog';
import { useMessageList } from './useMessageList';
import { MessagesListView } from './MessagesListView';
import { MessagesPanelHeader, type WebhookStats } from './MessagesPanelHeader';
import { MessagesComposer } from './MessagesComposer';
import { armingToastCopy, parseArmingOutcome, type ArmingOutcome } from './messageArming';
import { MessagesBatchActionBar } from './MessagesBatchActionBar';
import { usePoller, DEFAULT_POLL_MS } from '../hooks/usePoller';
import { generateIdempotencyKey } from '../utils/idempotencyKey';
import { StrategicChallengeChip } from './StrategicChallengeChip';
import { useDirectionForm } from '../hooks/useDirectionForm';
import { DirectionsAddForm } from './DirectionsAddForm';

const DEFAULT_VIEW_ID = '__default__';

// Receipt shown for at most 2 hours after a steer fires, preventing stale "picked up"
// messages that grow without bound, and blocking stale receipts from resurfacing after
// an operator cancels a fresh steer.
const STEER_RECEIPT_MAX_AGE_MS = 7_200_000;

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

interface MessagesPanelProps {
  selectedProjectId: string | null;
  githubRepo?: string | null;
  defaultBranch?: string;
  onClose: () => void;
  addToast: (message: string, type: 'success' | 'error' | 'info') => void;
  onMessageCountChange?: (count: number) => void;
  onOpenTask?: (taskId: string) => void;
  notifyOnHighSignalMessage?: boolean;
  /** Live signal: whether a strategic cycle is currently running for this project. */
  strategistActive?: boolean;
  /** Phase-level detail about the currently running cycle, if any. */
  cycleProgress?: StrategicCycleProgress | null;
  /** Outcome receipt from the most recently completed cycle, shown until the next cycle begins. */
  lastCycleCompletion?: StrategicCycleCompletion | null;
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

export function MessagesPanel({ selectedProjectId, githubRepo, defaultBranch, onClose, addToast, onMessageCountChange, onOpenTask, notifyOnHighSignalMessage, strategistActive, cycleProgress, lastCycleCompletion }: MessagesPanelProps) {
  const [messages, setMessages] = useState<ProjectMessage[]>([]);
  const [loading, setLoading] = useState(true);
  // Compose — initialized from the per-project draft (if any) so a reload restores it.
  const [newType, setNewType] = useState(() => loadComposerDraft(auth.currentUser?.email, selectedProjectId)?.type ?? 'FEEDBACK');
  const [newContent, setNewContent] = useState(() => loadComposerDraft(auth.currentUser?.email, selectedProjectId)?.content ?? '');
  const [submitting, setSubmitting] = useState(false);
  // Synchronous in-flight guard. `submitting` state updates asynchronously, so
  // two rapid keypresses (double-Enter) can both read the pre-update value and
  // each fire a request before React re-renders. This ref is set synchronously
  // at the very top of handleSubmit and cleared in its finally, closing that
  // race window so a burst collapses to a single POST on the client side (the
  // server idempotency key is the backstop for retries that do reach it).
  const submittingRef = useRef(false);
  const [replyTo, setReplyTo] = useState<{ id: string; content: string; from: string } | null>(null);
  // Real arming outcome the server reported for the last send (null before any
  // send, or after the operator changes the message type so the composer reverts
  // to a fresh type-based prediction).
  const [lastArming, setLastArming] = useState<ArmingOutcome | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
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

  // Refs for scrolling to sections from summary card
  const blockersRefEl = useRef<HTMLDivElement>(null);
  const questionsRefEl = useRef<HTMLDivElement>(null);
  const routineRefEl = useRef<HTMLDivElement>(null);

  // Stable ref for addToast
  const addToastRef = useRef(addToast);
  addToastRef.current = addToast;
  const notifyOnHighSignalMessageRef = useRef(notifyOnHighSignalMessage);
  notifyOnHighSignalMessageRef.current = notifyOnHighSignalMessage;

  // Track seen messages to detect new arrivals and avoid initial-load toast spam
  const seenMessageIdsRef = useRef<Set<string>>(new Set());
  const hasInitiallyLoadedRef = useRef(false);
  const seenPromotedGroupKeysRef = useRef<Set<string>>(new Set());
  const eventPrefsRef = useRef(eventPrefs);
  eventPrefsRef.current = eventPrefs;
  const tempMutesRef = useRef(tempMutes);
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
    setEventPrefs(localPrefs);
    setTempMutes(loadTemporaryMutes(auth.currentUser?.email, selectedProjectId));
    setWebhookStats(null);
    setStrategicRefresh(null);
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
  }, [selectedProjectId]);

  // Close kebab menu on outside click — same pattern as MessageTypeDropdown.
  useEffect(() => {
    if (!openKebabId) return;
    function handleClickOutside(e: MouseEvent) {
      if (kebabRef.current && !kebabRef.current.contains(e.target as Node)) setOpenKebabId(null);
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [openKebabId]);

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
  const highSignalCount = countHighSignalMessages(messages, eventPrefs, tempMutes);

  const mutedCount = Object.values(eventPrefs).filter(v => v === 'mute').length;

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

  function getTop3NoisyEventTypes(): Array<[string, number]> {
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
  }

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
      setTempMutes(cleaned);
      saveTemporaryMutes(auth.currentUser?.email, selectedProjectId, cleaned);
    }
  }, [selectedProjectId]);

  const fetchMessages = useCallback(async () => {
    if (!selectedProjectId) { setLoading(false); return; }
    try {
      const params = new URLSearchParams({ project_id: selectedProjectId, status: 'all', limit: searchQuery.trim() ? '200' : '100' });
      if (searchQuery.trim()) params.set('q', searchQuery.trim());
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages?${params}`);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
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
        if (prev.length === msgs.length && prev.every((m, i) => m.id === msgs[i].id && m.status === msgs[i].status)) return prev;
        return msgs;
      });
      onMessageCountChange?.(countHighSignalMessages(msgs, eventPrefsRef.current, tempMutesRef.current));
      // Fetch webhook delivery stats (non-critical — errors are silently ignored)
      try {
        const statsRes = await apiFetch(`${API_BASE_URL}/api/admin/notification-dispatch/stats?project_id=${selectedProjectId}`);
        if (statsRes.ok) setWebhookStats(await statsRes.json());
      } catch { /* ignore */ }
      // Pending strategic-refresh marker — surfaced as a "strategic cycle pending"
      // badge until the autonomous loop consumes it (when it clears backend-side).
      // After consumption, shows a receipt via last_fired_at only if it was operator-origin
      // AND fired within STEER_RECEIPT_MAX_AGE_MS (prevents stale receipts that grow without
      // bound, and prevents a just-cancelled steer from showing an old "picked up" receipt).
      // Non-critical: errors leave the prior marker state untouched.
      try {
        const visRes = await apiFetch(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(selectedProjectId)}`);
        if (visRes.ok) {
          const proj = await visRes.json() as { vision?: { _strategic_refresh?: StrategicRefreshMarker | null; _strategic_refresh_last_fired_at?: string; _strategic_refresh_last_fired_origin?: string } };
          const marker = proj.vision?._strategic_refresh;
          const lastFiredAt = proj.vision?._strategic_refresh_last_fired_at;
          const lastFiredOrigin = proj.vision?._strategic_refresh_last_fired_origin;
          // Build marker state: include pending marker if present, or add receipt if it was
          // operator-origin AND fired recently (within STEER_RECEIPT_MAX_AGE_MS).
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
        }
      } catch { /* ignore */ }
    } catch {
      addToastRef.current('Failed to load messages', 'error');
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId, onMessageCountChange, eventPrefs, searchQuery]);

  usePoller(fetchMessages, { intervalMs: DEFAULT_POLL_MS, deps: [fetchMessages] });

  // Auto-scroll to bottom only on initial load or when user sends a new message
  // (new message = count increased AND user is near the bottom)
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const currentCount = highSignalOpen.length + routineOpen.length + visibleResolved.length;
    const isInitialLoad = prevMessageCountRef.current === 0 && currentCount > 0;
    if (isInitialLoad) {
      // First load — jump to bottom instantly
      requestAnimationFrame(() => el.scrollTo({ top: el.scrollHeight, behavior: 'instant' }));
    } else if (currentCount > prevMessageCountRef.current) {
      // New message arrived — only scroll if user is already near bottom
      const nearBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 120;
      if (nearBottom) {
        requestAnimationFrame(() => el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' }));
      }
    }
    // Don't scroll at all on polling refresh with same count — preserves position
    prevMessageCountRef.current = currentCount;
  }, [highSignalOpen, routineOpen, visibleResolved]);

  // Update parent with the high-signal count for the tab-title badge. highSignalCount is
  // countHighSignalMessages(messages), identical to the fetch callback and the closed-panel
  // App.tsx path, so the badge stays stable across open/closed transitions.
  useEffect(() => {
    onMessageCountChange?.(highSignalCount);
  }, [highSignalCount, onMessageCountChange]);

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

  async function handleSubmit() {
    if (!selectedProjectId || !newContent.trim()) return;
    // Synchronous guard first — reject re-entrant calls before the async
    // `setSubmitting` has a chance to re-render, so rapid double-Enter cannot
    // launch two requests.
    if (submittingRef.current) return;
    submittingRef.current = true;
    setSubmitting(true);
    try {
      const context: Record<string, unknown> = {};
      if (replyTo) {
        context.reply_to = replyTo.id;
        context.reply_preview = replyTo.content.slice(0, 120);
      }
      // Reuse the draft's key across a double-submit / retry so the backend
      // dedups; only mint a new one when the previous send fully succeeded.
      if (!idempotencyKeyRef.current) {
        idempotencyKeyRef.current = generateIdempotencyKey();
      }
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_id: selectedProjectId,
          message_type: newType,
          content: newContent.trim(),
          idempotency_key: idempotencyKeyRef.current,
          ...(Object.keys(context).length > 0 ? { context } : {}),
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
      if (textareaRef.current) textareaRef.current.style.height = 'auto';
      // A genuine steering message the server could not arm (`not_armed`) is a
      // partial outcome — the message saved, but the strategist was not woken.
      // Surface it as info, not a green success, so the receipt is truthful.
      addToast(armingToastCopy(arming), arming === 'not_armed' ? 'info' : 'success');
      await fetchMessages();
    } catch {
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
      setStrategicRefresh(null);
      addToast('Strategic steer cancelled', 'success');
      await fetchMessages();
    } catch {
      addToast('Failed to cancel strategic steer', 'error');
    } finally {
      setCancellingRefresh(false);
    }
  }

  async function handleResolve(msgId: string, resolution: string) {
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
      addToast('Failed to resolve message', 'error');
    }
  }

  async function handleDelete(msgId: string) {
    setDeletingIds(prev => new Set(prev).add(msgId));
    try {
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages/${msgId}`, { method: 'DELETE' });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      await fetchMessages();
    } catch {
      addToast('Failed to delete message', 'error');
    } finally {
      setDeletingIds(prev => { const next = new Set(prev); next.delete(msgId); return next; });
    }
  }

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
      return true;
    } catch {
      addToast('Failed to create direction', 'error');
      return false;
    } finally {
      setCreatingDirectionIds(prev => { const next = new Set(prev); next.delete(msg.id); return next; });
    }
  }

  function handleOpenConvertModal(msg: ProjectMessage) {
    convertForm.seedFromMessage(msg.content);
    setPendingConvertMsg(msg);
  }

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
    } catch {
      convertForm.markAddAttemptFailed();
      addToast('Failed to create direction', 'error');
    } finally {
      setConvertSubmitting(false);
    }
  }

  async function handleCreateInvestigation(msg: ProjectMessage): Promise<boolean> {
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
      addToast('Investigation task created', 'success');
      return true;
    } catch (err) {
      addToast(`Failed to create investigation: ${err instanceof Error ? err.message : 'Unknown error'}`, 'error');
      return false;
    } finally {
      setInvestigatingIds(prev => { const next = new Set(prev); next.delete(msg.id); return next; });
    }
  }

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

  function toggleSelect(msgId: string) {
    setSelectedIds(prev => {
      const next = new Set(prev);
      if (next.has(msgId)) next.delete(msgId); else next.add(msgId);
      return next;
    });
  }

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

  const bubbleProps = {
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
    onCreateDirection: (msg: ProjectMessage) => { handleOpenConvertModal(msg); },
    onCreateInvestigation: (msg: ProjectMessage) => { void handleCreateInvestigation(msg); },
    onResolve: handleResolve,
    onDelete: (msgId: string) => { void handleDelete(msgId); },
    onStartResolve: (msgId: string) => { setResolvingId(msgId); setResolveInput(''); },
    onCancelResolve: () => setResolvingId(null),
    onSetResolveInput: setResolveInput,
    onStartInvestigate: (msgId: string) => setInvestigatingId(msgId),
    onCancelInvestigate: () => setInvestigatingId(null),
    onReply: (replyTo: { id: string; content: string; from: string }) => {
      setReplyTo(replyTo);
      textareaRef.current?.focus();
    },
  };

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
      />

      {/* Strategic-refresh indicator: either pending (armed but not yet fired) or
          a receipt showing when a prior steer was picked up and acted upon. Hidden
          once the cycle starts (strategistActive), at which point "Strategist working…"
          takes over. Displays the queued directive excerpt and coalesce count. */}
      {strategicRefresh && !strategistActive && (
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
      {strategistActive && (
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
      {lastCycleCompletion && !strategistActive && (
        <div className={`px-4 md:px-6 py-2 border-b border-neutral-800 flex items-start gap-2 shrink-0 ${completionReceiptTone?.banner ?? 'bg-emerald-950/30'}`} role="status" aria-live="polite">
          <span className={`w-2 h-2 rounded-full mt-0.5 shrink-0 ${completionReceiptTone?.dot ?? 'bg-emerald-400'}`} aria-hidden="true" />
          <span className={`text-xs min-w-0 ${completionReceiptTone?.text ?? 'text-emerald-300'}`}>
            <span className="font-medium">
              {completionReceiptTone?.label ?? 'Cycle complete'}
            </span>
            {lastCycleCompletion.failure_reason && (
              <span className={`ml-1.5 ${completionReceiptTone?.message ?? 'text-emerald-400/80'}`}>{lastCycleCompletion.failure_reason.slice(0, 80)}</span>
            )}
            {(lastCycleCompletion.created_count ?? 0) > 0 && (
              <span className="ml-1.5">· +{lastCycleCompletion.created_count} direction{lastCycleCompletion.created_count !== 1 ? 's' : ''}</span>
            )}
            {(lastCycleCompletion.retired_count ?? 0) > 0 && (
              <span className="ml-1.5">· −{lastCycleCompletion.retired_count} retired</span>
            )}
            {(lastCycleCompletion.staged_count ?? 0) > 0 && (
              <span className="ml-1.5">· {lastCycleCompletion.staged_count} direction{lastCycleCompletion.staged_count !== 1 ? 's' : ''} staged</span>
            )}
            {(lastCycleCompletion.updated_count ?? 0) > 0 && (
              <span className="ml-1.5">· {lastCycleCompletion.updated_count} updated</span>
            )}
            {(lastCycleCompletion.tasks_spawned ?? 0) > 0 && (
              <span className="ml-1.5">· {lastCycleCompletion.tasks_spawned} task{lastCycleCompletion.tasks_spawned !== 1 ? 's' : ''} spawned</span>
            )}
            {lastCycleCompletion.running_update_changed && (
              <span className="ml-1.5">· running update written</span>
            )}
            {lastCycleCompletion.problem_model_changed && (
              <span className="ml-1.5">· problem model updated</span>
            )}
            {lastCycleCompletion.status_message && (
              <span className={`ml-1.5 ${completionReceiptTone?.message ?? 'text-emerald-400/80'} italic`}>{lastCycleCompletion.status_message.slice(0, 80)}</span>
            )}
            {lastCycleCompletion.strategic_judgment_challenge && (
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
        scrollRef={scrollRef}
        blockersRefEl={blockersRefEl}
        questionsRefEl={questionsRefEl}
        routineRefEl={routineRefEl}
        dividerDismissedRef={dividerDismissedRef}
        messages={messages}
        loading={loading}
        dividerTime={dividerTime}
        totalNewCount={totalNewCount}
        newMessageCounts={newMessageCounts}
        highSignalOpen={highSignalOpen}
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
      {selectMode && selectedIds.size > 0 && (
        <MessagesBatchActionBar
          selectedCount={selectedIds.size}
          batchCreating={batchCreating}
          onCreateDirections={handleBatchCreateDirections}
        />
      )}

      {/* Convert-to-direction modal */}
      {pendingConvertMsg && (
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
              <DirectionsAddForm
                form={convertForm}
                addDirection={handleSubmitConvertForm}
                stageTargetVersion={null}
              />
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
        newType={newType}
        newContent={newContent}
        submitting={submitting}
        replyTo={replyTo}
        lastArming={lastArming}
        strategistBusy={strategistActive}
        onChangeType={(value) => { setNewType(value); setLastArming(null); if (newContent) saveComposerDraft(auth.currentUser?.email, selectedProjectId, { type: value, content: newContent }); }}
        onChangeContent={(value) => { setNewContent(value); if (value) { saveComposerDraft(auth.currentUser?.email, selectedProjectId, { type: newType, content: value }); } else { clearComposerDraft(auth.currentUser?.email, selectedProjectId); } }}
        onCancelReply={() => setReplyTo(null)}
        onSubmit={handleSubmit}
      />
    </div>
  );
}
