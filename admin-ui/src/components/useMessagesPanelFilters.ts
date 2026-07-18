/* eslint-disable react-hooks/set-state-in-effect */
import { useState, useRef, useEffect, useCallback, useMemo } from 'react';
import { auth } from '../firebase';
import { type EventPref, loadEventPrefs, saveEventPrefs } from './eventPrefs';
import { type MuteWindow, loadMuteWindow, saveMuteWindow, clearMuteWindow } from './muteWindow';
import { type SavedView, loadSavedViews, saveSavedViews, SAVED_VIEWS_CAP } from './savedViews';
import { type TemporaryMutes, loadTemporaryMutes, saveTemporaryMutes } from './temporaryMutes';
import {
  fetchServerNoisePrefs, saveServerNoisePrefs,
  fetchServerSavedViews, saveServerSavedViews,
  fetchServerTempMutes, saveServerTempMutes,
  fetchServerMuteWindow, saveServerMuteWindow,
} from './serverSync';

const DEFAULT_VIEW_ID = '__default__';

type MessagesPanelPrefs = { version: 1; routineExpanded: boolean; showResolved: boolean; criticalOnly: boolean };

function messagesPanelPrefsKey(userId?: string | null): string {
  return `messages_panel_prefs:${userId || 'anonymous'}`;
}

function loadMessagesPanelPrefs(userId?: string | null): MessagesPanelPrefs {
  try {
    const raw = localStorage.getItem(messagesPanelPrefsKey(userId));
    if (!raw) return { version: 1, routineExpanded: false, showResolved: false, criticalOnly: false };
    const parsed = JSON.parse(raw) as Partial<MessagesPanelPrefs>;
    if (
      parsed.version !== 1 ||
      typeof parsed.routineExpanded !== 'boolean' ||
      typeof parsed.showResolved !== 'boolean' ||
      typeof parsed.criticalOnly !== 'boolean'
    ) {
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

export function useMessagesPanelFilters(selectedProjectId: string | null) {
  const userEmail = auth.currentUser?.email;

  // Cross-project display prefs (loaded once at mount, persisted on change)
  const [showResolved, setShowResolved] = useState(() => loadMessagesPanelPrefs(userEmail).showResolved);
  const [routineExpanded, setRoutineExpanded] = useState(() => loadMessagesPanelPrefs(userEmail).routineExpanded);
  const [criticalOnly, setCriticalOnly] = useState(() => loadMessagesPanelPrefs(userEmail).criticalOnly);
  const [expandedGroupKeys, setExpandedGroupKeys] = useState<Set<string>>(new Set());

  // Per-project filter state
  const [eventPrefs, setEventPrefs] = useState<Record<string, EventPref>>(() =>
    loadEventPrefs(userEmail, selectedProjectId),
  );
  const [showPrefsPanel, setShowPrefsPanel] = useState(false);
  const [sortMode, setSortMode] = useState<'importance' | 'recent'>('importance');
  const [searchQuery, setSearchQuery] = useState('');
  const [muteWindow, setMuteWindow] = useState<MuteWindow | null>(() => loadMuteWindow(userEmail, selectedProjectId));
  const [tempMutes, setTempMutes] = useState<TemporaryMutes>(() => loadTemporaryMutes(userEmail, selectedProjectId));
  const [showTempMuteMenu, setShowTempMuteMenu] = useState(false);

  // Saved views
  const [savedViews, setSavedViews] = useState<SavedView[]>(() => loadSavedViews(userEmail, selectedProjectId));
  const [activeViewId, setActiveViewId] = useState<string>(DEFAULT_VIEW_ID);
  const [showSaveDialog, setShowSaveDialog] = useState(false);
  const [saveDialogMode, setSaveDialogMode] = useState<'create' | 'rename'>('create');
  const [saveDialogTargetId, setSaveDialogTargetId] = useState<string | null>(null);
  const [saveDialogName, setSaveDialogName] = useState('');
  const [saveDialogDescription, setSaveDialogDescription] = useState('');
  const [saveDialogError, setSaveDialogError] = useState<string | null>(null);
  const [openKebabId, setOpenKebabId] = useState<string | null>(null);
  const kebabRef = useRef<HTMLDivElement>(null);
  const [showOnlyNew, setShowOnlyNew] = useState(false);
  // Needs-action-first mode: when true, the message list shows only server-flagged
  // needs_operator items. Defaults true so the inbox leads with what needs action.
  const [needsActionOnly, setNeedsActionOnly] = useState(true);

  // Reset per-project filter state and sync from server on project switch
  useEffect(() => {
    let active = true;
    const email = auth.currentUser?.email;
    const localPrefs = loadEventPrefs(email, selectedProjectId);
    setEventPrefs(localPrefs);
    setTempMutes(loadTemporaryMutes(email, selectedProjectId));
    setMuteWindow(loadMuteWindow(email, selectedProjectId));
    setSavedViews(loadSavedViews(email, selectedProjectId));
    setActiveViewId(DEFAULT_VIEW_ID);
    setNeedsActionOnly(true);
    setOpenKebabId(null);
    if (selectedProjectId) {
      // Prefer server noise prefs over localStorage — picks up prefs set on another device.
      fetchServerNoisePrefs(selectedProjectId).then(serverPrefs => {
        if (!active) return;
        if (serverPrefs !== null) {
          setEventPrefs(serverPrefs);
          saveEventPrefs(email, selectedProjectId, serverPrefs);
        }
      });
      // Prefer server temp-mutes over localStorage — expiry semantics preserved (expired entries dropped).
      fetchServerTempMutes(selectedProjectId).then(serverMutes => {
        if (!active) return;
        if (serverMutes !== null) {
          setTempMutes(serverMutes);
          saveTemporaryMutes(email, selectedProjectId, serverMutes);
        }
      });
      // Prefer server mute-window over localStorage — undefined = fetch failed, keep local fallback.
      fetchServerMuteWindow(selectedProjectId).then(serverWin => {
        if (!active) return;
        if (serverWin !== undefined) {
          setMuteWindow(serverWin);
          if (serverWin !== null) {
            saveMuteWindow(email, selectedProjectId, serverWin);
          } else {
            clearMuteWindow(email, selectedProjectId);
          }
        }
      });
      // Prefer server saved views over localStorage — picks up views created on another device.
      fetchServerSavedViews(selectedProjectId).then(serverViews => {
        if (!active) return;
        if (serverViews !== null) {
          setSavedViews(serverViews);
          saveSavedViews(email, selectedProjectId, serverViews);
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

  // Persist cross-project display prefs when they change
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
      Object.entries(tempMutes).filter(([, expiresAt]) => expiresAt > now),
    );
    if (Object.keys(cleaned).length !== Object.keys(tempMutes).length) {
      setTempMutes(cleaned);
      saveTemporaryMutes(auth.currentUser?.email, selectedProjectId, cleaned);
    }
  }, [selectedProjectId, tempMutes]);

  const activeView = useMemo(
    () => savedViews.find(v => v.id === activeViewId) ?? null,
    [savedViews, activeViewId],
  );

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
      setNeedsActionOnly(true);
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

  return {
    showResolved, setShowResolved,
    routineExpanded, setRoutineExpanded,
    criticalOnly, setCriticalOnly,
    expandedGroupKeys, setExpandedGroupKeys,
    eventPrefs, setEventPrefs,
    showPrefsPanel, setShowPrefsPanel,
    sortMode, setSortMode,
    searchQuery, setSearchQuery,
    muteWindow,
    tempMutes,
    showTempMuteMenu, setShowTempMuteMenu,
    savedViews,
    activeViewId,
    showSaveDialog,
    saveDialogMode,
    saveDialogTargetId,
    saveDialogName, setSaveDialogName,
    saveDialogDescription, setSaveDialogDescription,
    saveDialogError, setSaveDialogError,
    openKebabId, setOpenKebabId,
    kebabRef,
    showOnlyNew, setShowOnlyNew,
    needsActionOnly, setNeedsActionOnly,
    activeView,
    activeTabModified,
    setMuteWindowPersisted,
    updatePref,
    addTemporaryMute,
    applyView,
    openSaveDialog,
    openRenameDialog,
    closeSaveDialog,
    commitSaveDialog,
    deleteView,
  };
}
