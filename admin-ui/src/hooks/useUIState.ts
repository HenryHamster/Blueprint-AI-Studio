import { useState, useRef, useReducer, useCallback } from 'react';
import type { ViewMode } from './useViewPreferences';

export type PrimaryTab =
  | 'chat'
  | 'pipeline'
  | 'project'
  | 'runtime';

export type LegacyPrimaryTab =
  | 'cockpit'
  | 'home'
  | 'board'
  | 'directions'
  | 'compute';

export type PrimaryTabInput = PrimaryTab | LegacyPrimaryTab;

export interface PrimaryTabTarget {
  tab: PrimaryTab;
  pipelineSection?: 'flow' | 'board' | 'archive';
  projectSection?: 'running-update' | 'directions' | 'problem-model' | 'design-doc' | 'manage';
  runtimeSection?: 'health' | 'devices' | 'compute' | 'accounts' | 'settings' | 'analytics';
  viewMode?: ViewMode;
  showArchive?: boolean;
}

export function resolvePrimaryTabTarget(value?: string | null): PrimaryTabTarget {
  if (value === 'pipeline') return { tab: 'pipeline' };
  if (value === 'project') return { tab: 'project' };
  if (value === 'runtime') return { tab: 'runtime' };
  if (value === 'cockpit') return { tab: 'project', projectSection: 'running-update' };
  if (value === 'directions') return { tab: 'project', projectSection: 'directions' };
  if (value === 'home') return { tab: 'runtime', runtimeSection: 'health' };
  if (value === 'compute') return { tab: 'runtime', runtimeSection: 'compute' };
  if (value === 'board') {
    return {
      tab: 'pipeline',
      pipelineSection: 'board',
      viewMode: 'kanban',
      showArchive: false,
    };
  }
  return { tab: 'chat' };
}

export function normalizePrimaryTab(value?: string | null): PrimaryTab {
  return resolvePrimaryTabTarget(value).tab;
}

export function useUIState() {
  const [activeTab, setActiveTab] = useState<PrimaryTabInput>('chat');
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

  const [showFleetDetail, setShowFleetDetail] = useState(false);
  const [showDirections, setShowDirections] = useState(false);
  const [showMetrics, setShowMetrics] = useState(false);
  const [showUserManagement, setShowUserManagement] = useState(false);

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
    activeTab, setActiveTab,
    showResearchBrowser, setShowResearchBrowser,
    showKnowledgeBrowser, setShowKnowledgeBrowser,
    showActivityFeed, setShowActivityFeed, lastActivityFeedOpen, prevLastActivityFeedOpen,
    showFleetDetail, setShowFleetDetail,
    showDirections, setShowDirections,
    showMetrics, setShowMetrics,
    showUserManagement, setShowUserManagement,
    selectionMode, setSelectionMode, selectedTaskIds, setSelectedTaskIds,
    toggleSelectionMode, toggleTaskSelection, bulkColumnToggle, shiftSelectRange,
    confirmDialog, setConfirmDialog,
  };
}
