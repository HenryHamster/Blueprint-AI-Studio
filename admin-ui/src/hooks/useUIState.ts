import { useState, useEffect, useRef, useReducer, useCallback } from 'react';

export function useUIState() {
  // Panel visibility
  const [showResearchBrowser, setShowResearchBrowser] = useState(false);
  const [showKnowledgeBrowser, setShowKnowledgeBrowser] = useState(false);

  const [showActivityFeed, setShowActivityFeedState] = useState(false);
  const [activityFeedTimestamps, dispatchActivityFeedTimestamp] = useReducer(
    (state: { last: number | null; prev: number | null }, now: number) => ({
      prev: state.last,
      last: now,
    }),
    null,
    () => {
      const stored = localStorage.getItem('blueprint-admin:lastActivityFeedOpen');
      return { last: stored ? parseInt(stored, 10) : null, prev: null };
    },
  );
  const lastActivityFeedOpen = activityFeedTimestamps.last;
  const prevLastActivityFeedOpen = activityFeedTimestamps.prev;

  const setShowActivityFeed = useCallback((show: boolean | ((prev: boolean) => boolean)) => {
    setShowActivityFeedState(prev => {
      const next = typeof show === 'function' ? show(prev) : show;
      if (next && !prev) {
        const now = Date.now();
        dispatchActivityFeedTimestamp(now);
        localStorage.setItem('blueprint-admin:lastActivityFeedOpen', String(now));
      }
      return next;
    });
  }, []);

  const [showMessages, setShowMessages] = useState(false);
  const [showSettings, setShowSettings] = useState(false);
  const [showDirections, setShowDirections] = useState(false);
  const [showMetrics, setShowMetrics] = useState(false);
  const [showDiagnostics, setShowDiagnostics] = useState(false);
  const [showShortcutHelp, setShowShortcutHelp] = useState(false);
  const [showUserManagement, setShowUserManagement] = useState(false);

  const settingsRef = useRef<HTMLDivElement>(null);
  const shortcutHelpRef = useRef<HTMLDivElement>(null);

  // Close shortcut help on outside click
  useEffect(() => {
    if (!showShortcutHelp) return;
    const handleClick = (e: MouseEvent) => {
      if (shortcutHelpRef.current && !shortcutHelpRef.current.contains(e.target as Node)) {
        setShowShortcutHelp(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [showShortcutHelp]);

  // Selection mode
  const [selectionMode, setSelectionMode] = useState(false);
  const [selectedTaskIds, setSelectedTaskIds] = useState<Set<string>>(new Set());
  const lastSelectedTaskIdRef = useRef<string | null>(null);

  const toggleSelectionMode = () => {
    setSelectionMode(prev => !prev);
    setSelectedTaskIds(new Set());
    lastSelectedTaskIdRef.current = null;
  };

  const toggleTaskSelection = (taskId: string) => {
    lastSelectedTaskIdRef.current = taskId;
    setSelectedTaskIds(prev => {
      const next = new Set(prev);
      if (next.has(taskId)) {
        next.delete(taskId);
      } else {
        next.add(taskId);
      }
      return next;
    });
  };

  const shiftSelectRange = (taskId: string, orderedTaskIds: string[]) => {
    const lastId = lastSelectedTaskIdRef.current;
    if (!lastId || !orderedTaskIds.includes(lastId)) {
      toggleTaskSelection(taskId);
      return;
    }
    const lastIdx = orderedTaskIds.indexOf(lastId);
    const currIdx = orderedTaskIds.indexOf(taskId);
    if (currIdx === -1) {
      toggleTaskSelection(taskId);
      return;
    }
    const [from, to] = lastIdx <= currIdx ? [lastIdx, currIdx] : [currIdx, lastIdx];
    const rangeIds = orderedTaskIds.slice(from, to + 1);
    setSelectedTaskIds(prev => {
      const next = new Set(prev);
      for (const id of rangeIds) {
        next.add(id);
      }
      return next;
    });
    lastSelectedTaskIdRef.current = taskId;
  };

  const bulkColumnToggle = (taskIds: string[], selected: boolean) => {
    setSelectedTaskIds(prev => {
      const next = new Set(prev);
      for (const id of taskIds) {
        if (selected) {
          next.add(id);
        } else {
          next.delete(id);
        }
      }
      return next;
    });
  };

  // Confirm dialog
  const [confirmDialog, setConfirmDialog] = useState<{
    title: string;
    message: string;
    confirmLabel: string;
    onConfirm: () => void;
  } | null>(null);

  return {
    showResearchBrowser, setShowResearchBrowser,
    showKnowledgeBrowser, setShowKnowledgeBrowser,
    showActivityFeed, setShowActivityFeed, lastActivityFeedOpen, prevLastActivityFeedOpen,
    showMessages, setShowMessages,
    showSettings, setShowSettings,
    showDirections, setShowDirections,
    showMetrics, setShowMetrics,
    showDiagnostics, setShowDiagnostics,
    showShortcutHelp, setShowShortcutHelp,
    showUserManagement, setShowUserManagement,
    settingsRef, shortcutHelpRef,
    selectionMode, setSelectionMode, selectedTaskIds, setSelectedTaskIds,
    toggleSelectionMode, toggleTaskSelection, bulkColumnToggle, shiftSelectRange,
    confirmDialog, setConfirmDialog,
  };
}
