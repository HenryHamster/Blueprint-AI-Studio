import { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { X, MessageSquare, Send, Check, AlertTriangle, ChevronDown, Settings, ChevronUp, BellOff, Save, MoreVertical, Pencil, Trash2, Reply } from 'lucide-react';
import { auth } from '../firebase';
import { apiFetch, API_BASE_URL, authHeaders } from '../api';
import { parseTimestamp } from '../utils/time';
import { type EventPref, loadEventPrefs, saveEventPrefs, effectiveSignal, getToastSeverity, countHighSignalMessages } from './eventPrefs';
import { type MuteWindow, loadMuteWindow, saveMuteWindow, clearMuteWindow, formatMuteCountdown } from './muteWindow';
import { type SavedView, loadSavedViews, saveSavedViews, SAVED_VIEWS_CAP } from './savedViews';
import { type TemporaryMutes, loadTemporaryMutes, saveTemporaryMutes } from './temporaryMutes';
import { groupRoutineMessages, outcomeBreakdown, isFailingGroup, formatTimestamp } from './messageGrouping';
import { loadLastSeen, saveLastSeen } from './lastSeen';
import { fetchServerNoisePrefs, saveServerNoisePrefs, fetchServerSavedViews, saveServerSavedViews, fetchServerTempMutes, saveServerTempMutes, fetchServerMuteWindow, saveServerMuteWindow } from './serverSync';
import { type ProjectMessage, TYPE_CONFIG } from './MessageBubble';
import { SaveViewDialog } from './SaveViewDialog';
import { useMessageList } from './useMessageList';
import { MessagesListView } from './MessagesListView';

const DEFAULT_VIEW_ID = '__default__';

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

interface WebhookStats {
  total_dispatched: number;
  total_delivered: number;
  total_failed: number;
  last_failure_reason: string;
  last_failure_at: string | null;
  retry_queue_depth: number;
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

const MSG_TYPE_OPTIONS = [
  { label: 'Feedback', value: 'FEEDBACK' },
  { label: 'Question', value: 'QUESTION' },
  { label: 'Bug Report', value: 'BUG_REPORT' },
  { label: 'Blocker', value: 'BLOCKER' },
  { label: 'Note', value: 'NOTE' },
];

function MessageTypeDropdown({ value, onChange }: { value: string; onChange: (v: string) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    if (open) document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [open]);

  const current = MSG_TYPE_OPTIONS.find(o => o.value === value);
  const config = TYPE_CONFIG[value] || TYPE_CONFIG.NOTE;

  return (
    <div ref={ref} className="relative shrink-0">
      <button
        onClick={() => setOpen(v => !v)}
        className={`flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs font-medium transition-all border min-h-[44px] ${config.color} bg-neutral-900/50 border-neutral-800/50 hover:border-neutral-700`}
      >
        <span>{current?.label ?? value}</span>
        <ChevronDown className={`w-3 h-3 transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>
      {open && (
        <div className="absolute bottom-full mb-1 left-0 z-50 min-w-[140px] bg-neutral-900 border border-neutral-700 rounded-lg shadow-xl overflow-hidden">
          {MSG_TYPE_OPTIONS.map(opt => {
            const optConfig = TYPE_CONFIG[opt.value] || TYPE_CONFIG.NOTE;
            return (
              <button
                key={opt.value}
                onClick={() => { onChange(opt.value); setOpen(false); }}
                className={`w-full flex items-center justify-between px-3 py-2.5 text-xs hover:bg-neutral-800 transition-colors min-h-[44px] ${
                  opt.value === value ? 'text-neutral-100' : optConfig.color
                }`}
              >
                <span>{opt.label}</span>
                {opt.value === value && <Check className="w-3 h-3 text-neutral-400" />}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
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

export function MessagesPanel({ selectedProjectId, githubRepo, defaultBranch, onClose, addToast, onMessageCountChange, onOpenTask, notifyOnHighSignalMessage }: MessagesPanelProps) {
  const [messages, setMessages] = useState<ProjectMessage[]>([]);
  const [loading, setLoading] = useState(true);
  // Compose
  const [newType, setNewType] = useState('FEEDBACK');
  const [newContent, setNewContent] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [replyTo, setReplyTo] = useState<{ id: string; content: string; from: string } | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

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
  const [showWebhookDetail, setShowWebhookDetail] = useState(false);
  const [muteWindow, setMuteWindow] = useState<MuteWindow | null>(() => loadMuteWindow(auth.currentUser?.email, selectedProjectId));
  const [showMuteMenu, setShowMuteMenu] = useState(false);
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

  // Calculate 24-hour volume for each event type and memoize event types
  const { eventType24hVolume, sortedEventTypes } = useMemo(() => {
    const now = Date.now();
    const twentyFourHoursMs = 24 * 60 * 60 * 1000;
    const counts = new Map<string, number>();
    for (const msg of messages) {
      const et = msg.context.event_type as string | undefined;
      if (!et) continue;
      if (msg.created_at) {
        const msgTime = parseTimestamp(msg.created_at).getTime();
        if (now - msgTime <= twentyFourHoursMs) {
          counts.set(et, (counts.get(et) ?? 0) + 1);
        }
      }
    }

    // Source from open messages without coupling to criticalOnly — the popover is for
    // managing prefs across all event types, so toggling criticalOnly should not hide
    // event types whose only currently-open messages are non-critical.
    const allEts = [...new Set([
      ...Object.keys(eventPrefs),
      ...messages.filter(m => m.status === 'open')
        .flatMap(m => { const et = m.context.event_type as string | undefined; return et ? [et] : []; }),
    ])];

    const sorted = [...allEts].sort((a, b) => {
      const countA = counts.get(a) ?? 0;
      const countB = counts.get(b) ?? 0;
      return countB - countA;
    });

    return { eventType24hVolume: counts, sortedEventTypes: sorted };
  }, [messages, eventPrefs]);

  const mutedCount = Object.values(eventPrefs).filter(v => v === 'mute').length;

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
      const params = new URLSearchParams({ project_id: selectedProjectId, status: 'all', limit: '100' });
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
    } catch {
      addToastRef.current('Failed to load messages', 'error');
    } finally {
      setLoading(false);
    }
  }, [selectedProjectId, onMessageCountChange, eventPrefs]);

  useEffect(() => {
    fetchMessages();
    const interval = setInterval(fetchMessages, 30_000);
    return () => clearInterval(interval);
  }, [fetchMessages]);

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

  // Auto-resize textarea
  function handleTextareaInput() {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = 'auto';
    el.style.height = `${Math.min(el.scrollHeight, 96)}px`; // max 3 rows ~96px
  }

  async function handleSubmit() {
    if (!selectedProjectId || !newContent.trim()) return;
    setSubmitting(true);
    try {
      const context: Record<string, unknown> = {};
      if (replyTo) {
        context.reply_to = replyTo.id;
        context.reply_preview = replyTo.content.slice(0, 120);
      }
      const res = await apiFetch(`${API_BASE_URL}/api/admin/messages`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project_id: selectedProjectId,
          message_type: newType,
          content: newContent.trim(),
          ...(Object.keys(context).length > 0 ? { context } : {}),
        }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      setNewContent('');
      setReplyTo(null);
      if (textareaRef.current) textareaRef.current.style.height = 'auto';
      addToast('Message sent', 'success');
      await fetchMessages();
    } catch {
      addToast('Failed to send message', 'error');
    } finally {
      setSubmitting(false);
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
      const res = await apiFetch(`${API_BASE_URL}/api/admin/projects/${encodeURIComponent(selectedProjectId)}/directions`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ text, priority: 'medium' }),
      });
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const data = await res.json();
      const directionId: string = data.direction?.id ?? '';
      await apiFetch(`${API_BASE_URL}/api/admin/messages/${msg.id}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ resolution: `Direction created: ${directionId}` }),
      });
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
    onCreateDirection: (msg: ProjectMessage) => { void handleCreateDirection(msg); },
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
      <div className="px-4 md:px-6 py-3 border-b border-neutral-800 flex flex-col gap-2 shrink-0">
        {/* Persistent last-visit anchor */}
        {dividerTime && (
          <div className="flex items-center gap-2 text-xs text-neutral-500 px-1">
            <span>Last visit: {formatTimestamp(dividerTime)}</span>
            {totalNewCount > 0 && (
              <span className="text-blue-400 font-medium">
                ({totalNewCount} new)
              </span>
            )}
          </div>
        )}
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <MessageSquare className="w-4 h-4 text-neutral-400" />
            <h2 className="text-sm font-semibold text-neutral-100">Messages</h2>
            {newHighSignalCount > 0 && (
              <span className="bg-blue-600/30 text-blue-300 border border-blue-500/50 text-xs font-semibold px-2 py-0.5 rounded-full animate-pulse">
                {newHighSignalCount} new
              </span>
            )}
            {highSignalOpen.length > 0 && (
              <span className="bg-amber-500/20 text-amber-400 border border-amber-500/40 text-xs font-semibold px-2 py-0.5 rounded-full">
                {highSignalOpen.length} high
              </span>
            )}
            {openMessages.length > 0 && (
              <span className="bg-blue-500/20 text-blue-400 border border-blue-500/40 text-xs font-semibold px-2 py-0.5 rounded-full">
                {openMessages.length}
              </span>
            )}
            {mutedCount > 0 && (
              <button onClick={() => setShowPrefsPanel(true)} className="text-[11px] text-neutral-600 hover:text-neutral-400 transition-colors">
                {mutedCount} muted
              </button>
            )}
            {webhookStats && webhookStats.total_dispatched > 0 && (
              <div className="relative">
                <button
                  onClick={() => setShowWebhookDetail(v => !v)}
                  title="Webhook delivery health"
                  className={`text-[11px] px-2 py-0.5 rounded-full border font-semibold transition-colors min-h-[44px] md:min-h-[28px] flex items-center ${
                    webhookStats.total_failed > 0
                      ? 'bg-amber-500/20 text-amber-400 border-amber-500/40'
                      : 'bg-emerald-500/20 text-emerald-400 border-emerald-500/40'
                  }`}
                >
                  {webhookStats.total_failed > 0 ? `${webhookStats.total_failed} webhook err` : 'webhooks ok'}
                </button>
                {showWebhookDetail && (
                  <>
                    <div className="fixed inset-0 z-40" onClick={() => setShowWebhookDetail(false)} />
                    <div className="absolute left-0 top-full mt-1 z-50 w-52 bg-neutral-900 border border-neutral-700 rounded-xl shadow-xl p-3 space-y-1 text-[11px]">
                      <p className="text-neutral-300 font-medium mb-1.5">Webhook delivery</p>
                      <div className="flex justify-between text-neutral-400"><span>Delivered</span><span className="text-emerald-400">{webhookStats.total_delivered}</span></div>
                      <div className="flex justify-between text-neutral-400"><span>Failed</span><span className={webhookStats.total_failed > 0 ? 'text-amber-400' : 'text-neutral-500'}>{webhookStats.total_failed}</span></div>
                      <div className="flex justify-between text-neutral-400"><span>Retry queue</span><span>{webhookStats.retry_queue_depth}</span></div>
                      {webhookStats.last_failure_reason && (
                        <p className="mt-1 text-neutral-600 truncate" title={webhookStats.last_failure_reason}>Last err: {webhookStats.last_failure_reason.slice(0, 40)}</p>
                      )}
                    </div>
                  </>
                )}
              </div>
            )}
          </div>
          <div className="flex items-center gap-1 flex-wrap justify-end">
          <button
            onClick={() => setSortMode(m => m === 'importance' ? 'recent' : 'importance')}
            title="Toggle sort order"
            className="text-xs px-2 py-1 rounded-lg transition-colors border border-neutral-800/60 text-neutral-500 hover:text-neutral-300 min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center"
          >
            {sortMode === 'importance' ? 'Importance' : 'Recent'}
          </button>
          <button
            onClick={() => setCriticalOnly(v => !v)}
            title={criticalOnly ? 'Show all severities' : 'Critical only (BLOCKER, BUG, critical NOTEs)'}
            className={`text-xs px-2 py-1 rounded-lg transition-colors border min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center ${
              criticalOnly
                ? 'text-amber-400 bg-amber-500/10 border-amber-500/30'
                : 'text-neutral-500 hover:text-neutral-300 border-neutral-800/60'
            }`}
          >
            <AlertTriangle className="w-3.5 h-3.5" />
          </button>
          <button
            onClick={() => setShowOnlyNew(v => !v)}
            disabled={!dividerTime}
            title={!dividerTime ? 'No messages to filter (visit first)' : showOnlyNew ? 'Show all messages' : 'Only new since last visit'}
            className={`text-xs px-2 py-1 rounded-lg transition-colors border min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center disabled:opacity-40 disabled:cursor-not-allowed ${
              showOnlyNew && dividerTime
                ? 'text-blue-400 bg-blue-500/10 border-blue-500/30'
                : 'text-neutral-500 hover:text-neutral-300 border-neutral-800/60'
            }`}
          >
            <ChevronUp className="w-3.5 h-3.5" />
          </button>
          {openFeedbackMessages.length > 1 && (
            <button
              onClick={() => {
                if (selectMode) { setSelectMode(false); setSelectedIds(new Set()); }
                else { setSelectMode(true); setSelectedIds(new Set(openFeedbackMessages.map(m => m.id))); }
              }}
              className={`text-xs px-2.5 py-1.5 rounded-lg transition-colors min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center ${
                selectMode ? 'text-purple-400 bg-purple-500/10 border border-purple-500/30' : 'text-neutral-500 hover:text-neutral-300 border border-transparent'
              }`}
            >
              {selectMode ? 'Cancel' : 'Select'}
            </button>
          )}
          <div className="relative">
            <button
              onClick={() => setShowMuteMenu(v => !v)}
              title={muteActive && muteWindow ? `Muted · ${formatMuteCountdown(muteWindow.expiresAt)}` : 'Mute routine ops temporarily'}
              className={`px-2 py-1 rounded-lg transition-colors border min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center text-xs font-semibold ${muteActive ? 'bg-amber-500/20 text-amber-400 border-amber-500/40' : 'text-neutral-500 hover:text-neutral-300 border-transparent'}`}
            >
              {muteActive && muteWindow ? `🔕 ${formatMuteCountdown(muteWindow.expiresAt)}` : <BellOff className="w-4 h-4" />}
            </button>
            {showMuteMenu && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setShowMuteMenu(false)} />
                <div className="absolute right-0 top-full mt-1 z-50 w-44 bg-neutral-900 border border-neutral-700 rounded-xl shadow-xl p-2 space-y-0.5">
                  {muteActive ? (
                    <>
                      <p className="text-[10px] text-neutral-500 px-2 py-1">Mute active</p>
                      {([['Extend +1h', 3600000], ['Extend +4h', 14400000], ['Extend +8h', 28800000]] as [string, number][]).map(([label, ms]) => (
                        <button key={label} onClick={() => {
                          const updated = { expiresAt: (muteWindow!.expiresAt) + ms };
                          saveMuteWindow(auth.currentUser?.email, selectedProjectId, updated);
                          if (selectedProjectId) void saveServerMuteWindow(selectedProjectId, updated);
                          setMuteWindow(updated);
                          setShowMuteMenu(false);
                        }} className="w-full text-left text-xs px-2 py-2 rounded text-neutral-300 hover:bg-neutral-800 transition-colors min-h-[44px] md:min-h-auto flex items-center">{label}</button>
                      ))}
                      <div className="border-t border-neutral-800 my-1" />
                      <button onClick={() => {
                        clearMuteWindow(auth.currentUser?.email, selectedProjectId);
                        if (selectedProjectId) void saveServerMuteWindow(selectedProjectId, null);
                        setMuteWindow(null);
                        setShowMuteMenu(false);
                      }} className="w-full text-left text-xs px-2 py-2 rounded text-amber-400 hover:bg-amber-500/10 transition-colors min-h-[44px] md:min-h-auto flex items-center">End mute early</button>
                    </>
                  ) : (
                    <>
                      <p className="text-[10px] text-neutral-500 px-2 py-1">Mute routine ops for</p>
                      {([['1 hour', 3600000], ['4 hours', 14400000], ['8 hours', 28800000]] as [string, number][]).map(([label, ms]) => (
                        <button key={label} onClick={() => {
                          const newWin = { expiresAt: Date.now() + ms };
                          saveMuteWindow(auth.currentUser?.email, selectedProjectId, newWin);
                          if (selectedProjectId) void saveServerMuteWindow(selectedProjectId, newWin);
                          setMuteWindow(newWin);
                          setShowMuteMenu(false);
                        }} className="w-full text-left text-xs px-2 py-2 rounded text-neutral-300 hover:bg-neutral-800 transition-colors min-h-[44px] md:min-h-auto flex items-center">{label}</button>
                      ))}
                    </>
                  )}
                </div>
              </>
            )}
          </div>
          <div className="relative">
            <button
              onClick={() => setShowPrefsPanel(v => !v)}
              title="Event type visibility"
              className={`p-2 rounded-lg transition-colors border min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-[36px] flex items-center justify-center ${showPrefsPanel ? 'text-neutral-300 bg-neutral-800 border-neutral-700' : 'text-neutral-500 hover:text-neutral-300 border-transparent'}`}
            >
              <Settings className="w-4 h-4" />
            </button>
            {showPrefsPanel && (
              <>
                <div className="fixed inset-0 z-40" onClick={() => setShowPrefsPanel(false)} />
                <div className="absolute right-0 top-full mt-1 z-50 w-60 bg-neutral-900 border border-neutral-700 rounded-xl shadow-xl p-3 space-y-1.5">
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium text-neutral-300">Event visibility</span>
                    {Object.keys(eventPrefs).length > 0 && (
                      <button onClick={() => { setEventPrefs({}); saveEventPrefs(auth.currentUser?.email, selectedProjectId, {}); if (selectedProjectId) void saveServerNoisePrefs(selectedProjectId, {}); }} className="text-[10px] text-neutral-500 hover:text-red-400 transition-colors">Reset all</button>
                    )}
                  </div>
                  {sortedEventTypes.length === 0 ? (
                    <p className="text-[11px] text-neutral-600 py-1">No events loaded</p>
                  ) : sortedEventTypes.map(et => {
                    const pref = eventPrefs[et] ?? 'routine';
                    const count = eventType24hVolume.get(et) ?? 0;
                    const isHighVolume = count >= 10;
                    return (
                      <div key={et} className="flex items-center gap-1.5">
                        <div className="flex items-center gap-1 flex-1 min-w-0">
                          <span className="text-[11px] text-neutral-400 truncate">{et.replace(/_/g, ' ')}</span>
                          {count > 0 && (
                            <span className={`text-[10px] shrink-0 px-1.5 py-0.5 rounded-full font-medium ${
                              isHighVolume
                                ? 'bg-orange-500/20 text-orange-400 border border-orange-500/30'
                                : 'bg-neutral-800 text-neutral-500'
                            }`}>
                              {count}
                            </span>
                          )}
                        </div>
                        {(['surfaced', 'routine', 'mute'] as EventPref[]).map(p => (
                          <button key={p} onClick={() => updatePref(et, p)}
                            title={p}
                            className={`text-[10px] px-1.5 py-0.5 rounded border transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center ${pref === p ? (p === 'mute' ? 'border-red-500/50 text-red-400 bg-red-500/10' : p === 'surfaced' ? 'border-amber-500/50 text-amber-400 bg-amber-500/10' : 'border-neutral-600 text-neutral-300 bg-neutral-800') : 'border-transparent text-neutral-600 hover:text-neutral-400'}`}>
                            {p === 'surfaced' ? '↑' : p === 'mute' ? '✕' : '·'}
                          </button>
                        ))}
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </div>
          <button onClick={onClose} className="bg-neutral-800 hover:bg-neutral-700 text-neutral-400 p-2 rounded-lg text-xs transition-colors border border-neutral-700 min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-[36px] flex items-center justify-center">
            <X className="w-4 h-4" />
          </button>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <input
            type="text"
            value={searchQuery}
            onChange={e => setSearchQuery(e.target.value)}
            placeholder="Search messages..."
            className="flex-1 bg-neutral-900/50 border border-neutral-800/50 text-neutral-100 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-700 placeholder-neutral-600"
          />
          <button
            onClick={openSaveDialog}
            title="Save current filters as a view"
            className="shrink-0 flex items-center justify-center gap-1 px-2.5 py-2 rounded-lg text-xs text-neutral-400 hover:text-neutral-200 border border-neutral-800/60 hover:border-neutral-700 transition-colors min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto"
          >
            <Save className="w-3.5 h-3.5" />
            <span className="hidden md:inline">Save view</span>
          </button>
        </div>
        {/* Saved views tab strip with compact tabs: allows small touch targets for view-switcher buttons */}
        <div
          className="flex items-center gap-1 overflow-x-auto -mx-1 px-1"
          data-testid="saved-views-tab-strip"
          data-viewport-allow-small-target
        >
          {(() => {
            const tabs: { id: string; name: string; isDefault: boolean; view: SavedView | null }[] = [
              { id: DEFAULT_VIEW_ID, name: 'Default', isDefault: true, view: null },
              ...savedViews.map(v => ({ id: v.id, name: v.name, isDefault: false, view: v })),
            ];
            return tabs.map(tab => {
              const isActive = activeViewId === tab.id;
              const showModified = isActive && activeTabModified;
              return (
                <div key={tab.id} className="relative shrink-0 flex items-center">
                  <button
                    onClick={() => applyView(tab.id)}
                    title={tab.view?.description || tab.name}
                    className={`flex items-center gap-1 px-2.5 py-1 rounded-md text-xs transition-colors border min-h-[44px] md:min-h-[36px] ${
                      isActive
                        ? 'bg-blue-500/15 text-blue-300 border-blue-500/40'
                        : 'text-neutral-500 hover:text-neutral-300 border-transparent hover:border-neutral-800/60'
                    }`}
                  >
                    <span>{tab.name}</span>
                    {showModified && (
                      <span title="Filters changed" className="w-1.5 h-1.5 rounded-full bg-amber-400" data-testid="view-modified-dot" />
                    )}
                  </button>
                  {!tab.isDefault && tab.view && (
                    <div ref={openKebabId === tab.id ? kebabRef : undefined} className="relative">
                      <button
                        onClick={() => setOpenKebabId(prev => (prev === tab.id ? null : tab.id))}
                        title="Manage view"
                        aria-label={`Manage view ${tab.name}`}
                        className="text-neutral-600 hover:text-neutral-300 p-1 rounded transition-colors min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center"
                      >
                        <MoreVertical className="w-3.5 h-3.5" />
                      </button>
                      {openKebabId === tab.id && (
                        <div className="absolute right-0 top-full mt-1 z-50 min-w-[120px] bg-neutral-900 border border-neutral-700 rounded-lg shadow-xl overflow-hidden">
                          <button
                            onClick={() => openRenameDialog(tab.view!)}
                            className="w-full flex items-center gap-2 px-3 py-2 text-xs text-neutral-300 hover:bg-neutral-800 transition-colors min-h-[44px]"
                          >
                            <Pencil className="w-3 h-3" />
                            Rename
                          </button>
                          <button
                            onClick={() => deleteView(tab.id)}
                            className="w-full flex items-center gap-2 px-3 py-2 text-xs text-red-400 hover:bg-red-500/10 transition-colors min-h-[44px]"
                          >
                            <Trash2 className="w-3 h-3" />
                            Delete
                          </button>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            });
          })()}
        </div>
      </div>

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
        <div className="px-4 md:px-6 py-2 border-t border-neutral-800 bg-neutral-900 flex items-center gap-3 shrink-0">
          <span className="text-xs text-neutral-400">{selectedIds.size} selected</span>
          <button
            onClick={handleBatchCreateDirections}
            disabled={batchCreating}
            className="text-xs bg-purple-600 hover:bg-purple-500 text-white px-3 py-1.5 rounded-lg disabled:opacity-40 transition-colors min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center"
          >
            {batchCreating ? 'Creating...' : 'Create Directions'}
          </button>
        </div>
      )}

      {/* Compose area — bottom, chat-style */}
      <div className="px-4 md:px-6 py-3 border-t border-neutral-800 shrink-0 bg-neutral-950/50">
        {/* Reply quote bar */}
        {replyTo && (
          <div className="flex items-center gap-2 mb-2 px-3 py-1.5 bg-neutral-800/60 border-l-2 border-blue-500/50 rounded-r-lg">
            <Reply className="w-3 h-3 text-blue-400 shrink-0" />
            <span className="text-xs text-neutral-400 truncate flex-1">
              Replying to <span className="text-neutral-300">{replyTo.from}</span>: {replyTo.content.slice(0, 80)}{replyTo.content.length > 80 ? '...' : ''}
            </span>
            <button onClick={() => setReplyTo(null)} className="text-neutral-500 hover:text-neutral-300 p-0.5 shrink-0 min-h-[44px] min-w-[44px] md:min-h-auto md:min-w-auto flex items-center justify-center">
              <X className="w-3 h-3" />
            </button>
          </div>
        )}
        <div className="flex gap-2 items-end">
          <MessageTypeDropdown value={newType} onChange={setNewType} />
          <div className="flex-1 relative">
            <textarea
              ref={textareaRef}
              value={newContent}
              onChange={e => { setNewContent(e.target.value); handleTextareaInput(); }}
              onKeyDown={e => {
                if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleSubmit(); }
              }}
              placeholder="Type a message..."
              rows={1}
              className="w-full bg-neutral-900/50 border border-neutral-800/50 text-neutral-100 rounded-xl px-4 py-2.5 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-700 placeholder-neutral-600 resize-none min-h-[44px] leading-relaxed"
            />
          </div>
          <button
            onClick={handleSubmit}
            disabled={submitting || !newContent.trim()}
            className="bg-blue-600 hover:bg-blue-500 text-white p-2.5 rounded-xl text-xs font-medium transition-colors disabled:opacity-40 shrink-0 min-h-[44px] min-w-[44px] flex items-center justify-center"
          >
            <Send className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
}
