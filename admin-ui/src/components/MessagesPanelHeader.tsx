/* eslint-disable react-hooks/set-state-in-effect */
import { memo, type Dispatch, type SetStateAction, type RefObject, useState, useRef, useEffect } from 'react';
import { X, MessageSquare, AlertTriangle, ChevronUp, Save, SlidersHorizontal } from 'lucide-react';
import { type EventPref } from './eventPrefs';
import { type MuteWindow } from './muteWindow';
import { type SavedView } from './savedViews';
import type { ProjectMessage } from '../types/messages';
import { formatTimestamp } from './messageGrouping';
import { MuteWindowMenu } from './MuteWindowMenu';
import { EventVisibilityMenu } from './EventVisibilityMenu';
import { SavedViewsTabStrip } from './SavedViewsTabStrip';

export interface WebhookStats {
  total_dispatched: number;
  total_delivered: number;
  total_failed: number;
  last_failure_reason: string;
  last_failure_at: string | null;
  retry_queue_depth: number;
}

export type MessagesPanelFilterChrome = 'full' | 'compact' | 'minimal';

export interface MessagesPanelHeaderProps {
  // Last-visit anchor
  dividerTime: number | null;
  totalNewCount: number;
  // Title-row badges
  newHighSignalCount: number;
  highSignalOpenCount: number;
  openCount: number;
  mutedCount: number;
  webhookStats: WebhookStats | null;
  // Toolbar state
  sortMode: 'importance' | 'recent';
  criticalOnly: boolean;
  showOnlyNew: boolean;
  selectMode: boolean;
  openFeedbackCount: number;
  // Mute window + visibility menus
  muteActive: boolean;
  muteWindow: MuteWindow | null;
  onSetMuteWindow: (win: MuteWindow | null) => void;
  showPrefsPanel: boolean;
  onShowPrefsChange: Dispatch<SetStateAction<boolean>>;
  messages: ProjectMessage[];
  eventPrefs: Record<string, EventPref>;
  onUpdatePref: (et: string, pref: EventPref) => void;
  onResetAllPrefs: () => void;
  // Search + save view
  searchQuery: string;
  onSearchChange: (value: string) => void;
  onOpenSaveDialog: () => void;
  // Saved-views tab strip
  savedViews: SavedView[];
  activeViewId: string;
  defaultViewId: string;
  activeTabModified: boolean;
  openKebabId: string | null;
  setOpenKebabId: Dispatch<SetStateAction<string | null>>;
  kebabRef: RefObject<HTMLDivElement | null>;
  onApplyView: (viewId: string) => void;
  onRenameView: (view: SavedView) => void;
  onDeleteView: (viewId: string) => void;
  // Toolbar actions
  onToggleSort: () => void;
  onToggleCriticalOnly: () => void;
  onToggleShowOnlyNew: () => void;
  onToggleSelectMode: () => void;
  onOpenMutedPrefs: () => void;
  onClose?: () => void;
  showCloseButton?: boolean;
  filterChrome?: MessagesPanelFilterChrome;
}

/**
 * Header for MessagesPanel: the persistent last-visit anchor, the title row
 * with its open/high/new/muted badges and webhook-health popover, the toolbar
 * (sort / critical-only / since-last-visit / select / mute-window / event
 * visibility / close), the search input + save-view button, and the saved-views
 * tab strip. Purely presentational — every piece of state and persistence stays
 * owned by MessagesPanel and is wired through props. Locally-owned state is only
 * for ephemeral header chrome: popovers, debounced search input, and compact
 * filter expansion.
 */
export const MessagesPanelHeader = memo(function MessagesPanelHeader({
  dividerTime,
  totalNewCount,
  newHighSignalCount,
  highSignalOpenCount,
  openCount,
  mutedCount,
  webhookStats,
  sortMode,
  criticalOnly,
  showOnlyNew,
  selectMode,
  openFeedbackCount,
  muteActive,
  muteWindow,
  onSetMuteWindow,
  showPrefsPanel,
  onShowPrefsChange,
  messages,
  eventPrefs,
  onUpdatePref,
  onResetAllPrefs,
  searchQuery,
  onSearchChange,
  onOpenSaveDialog,
  savedViews,
  activeViewId,
  defaultViewId,
  activeTabModified,
  openKebabId,
  setOpenKebabId,
  kebabRef,
  onApplyView,
  onRenameView,
  onDeleteView,
  onToggleSort,
  onToggleCriticalOnly,
  onToggleShowOnlyNew,
  onToggleSelectMode,
  onOpenMutedPrefs,
  onClose,
  showCloseButton,
  filterChrome,
}: MessagesPanelHeaderProps) {
  const [showWebhookDetail, setShowWebhookDetail] = useState(false);
  const [localSearchQuery, setLocalSearchQuery] = useState(searchQuery);
  const [compactControlsOpen, setCompactControlsOpen] = useState(false);
  const debounceTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const resolvedFilterChrome = filterChrome ?? 'full';
  const resolvedShowCloseButton = showCloseButton ?? Boolean(onClose);
  const controlsExpanded = resolvedFilterChrome === 'full' || compactControlsOpen;
  const hasActiveFilterChrome = Boolean(
    searchQuery.trim() ||
    localSearchQuery.trim() ||
    sortMode !== 'importance' ||
    criticalOnly ||
    (showOnlyNew && dividerTime) ||
    selectMode ||
    muteActive ||
    Object.keys(eventPrefs).length > 0 ||
    activeViewId !== defaultViewId ||
    activeTabModified,
  );

  const openMutedPrefs = () => {
    if (resolvedFilterChrome === 'compact') setCompactControlsOpen(true);
    onOpenMutedPrefs();
  };

  useEffect(() => {
    setLocalSearchQuery(searchQuery);
  }, [searchQuery]);

  useEffect(() => {
    if (showPrefsPanel && resolvedFilterChrome === 'compact') {
      setCompactControlsOpen(true);
    }
  }, [showPrefsPanel, resolvedFilterChrome]);

  useEffect(() => {
    debounceTimerRef.current = setTimeout(() => {
      onSearchChange(localSearchQuery);
    }, 300);

    return () => {
      if (debounceTimerRef.current) {
        clearTimeout(debounceTimerRef.current);
      }
    };
  }, [localSearchQuery, onSearchChange]);

  if (resolvedFilterChrome === 'minimal') return null;

  return (
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
          {highSignalOpenCount > 0 && (
            <span className="bg-amber-500/20 text-amber-400 border border-amber-500/40 text-xs font-semibold px-2 py-0.5 rounded-full">
              {highSignalOpenCount} high
            </span>
          )}
          {openCount > 0 && (
            <span className="bg-blue-500/20 text-blue-400 border border-blue-500/40 text-xs font-semibold px-2 py-0.5 rounded-full">
              {openCount}
            </span>
          )}
          {mutedCount > 0 && (
            <button onClick={openMutedPrefs} className="text-ui-md text-neutral-600 hover:text-neutral-400 transition-colors">
              {mutedCount} muted
            </button>
          )}
          {webhookStats && webhookStats.total_dispatched > 0 && (
            <div className="relative">
              <button
                onClick={() => setShowWebhookDetail(v => !v)}
                title="Webhook delivery health"
                className={`text-ui-md px-2 py-0.5 rounded-full border font-semibold transition-colors min-h-[44px] md:min-h-[28px] flex items-center ${
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
                  <div className="absolute left-0 top-full mt-1 z-50 w-52 bg-neutral-900 border border-neutral-700 rounded-xl shadow-xl p-3 space-y-1 text-ui-md">
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
        {resolvedFilterChrome === 'compact' && (
          <button
            type="button"
            onClick={() => setCompactControlsOpen(v => !v)}
            title={controlsExpanded ? 'Hide filters and saved views' : 'Show filters and saved views'}
            aria-expanded={controlsExpanded}
            className={`text-xs px-2 py-1 rounded-lg transition-colors border min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center gap-1.5 ${
              controlsExpanded || hasActiveFilterChrome
                ? 'text-blue-300 bg-blue-500/10 border-blue-500/30'
                : 'text-neutral-500 hover:text-neutral-300 border-neutral-800/60'
            }`}
          >
            <SlidersHorizontal className="w-3.5 h-3.5" />
            <span className="hidden md:inline">Filters</span>
            {hasActiveFilterChrome && !controlsExpanded && (
              <span className="w-1.5 h-1.5 rounded-full bg-blue-400" aria-hidden="true" />
            )}
          </button>
        )}
        {controlsExpanded && (
          <>
            <button
              onClick={onToggleSort}
              title="Toggle sort order"
              className="text-xs px-2 py-1 rounded-lg transition-colors border border-neutral-800/60 text-neutral-500 hover:text-neutral-300 min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center"
            >
              {sortMode === 'importance' ? 'Importance' : 'Recent'}
            </button>
            <button
              onClick={onToggleCriticalOnly}
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
              onClick={onToggleShowOnlyNew}
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
            {openFeedbackCount > 1 && (
              <button
                onClick={onToggleSelectMode}
                className={`text-xs px-2.5 py-1.5 rounded-lg transition-colors min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto flex items-center justify-center ${
                  selectMode ? 'text-purple-400 bg-purple-500/10 border border-purple-500/30' : 'text-neutral-500 hover:text-neutral-300 border border-transparent'
                }`}
              >
                {selectMode ? 'Cancel' : 'Select'}
              </button>
            )}
            <MuteWindowMenu
              muteActive={muteActive}
              muteWindow={muteWindow}
              onSetMuteWindow={onSetMuteWindow}
            />
            <EventVisibilityMenu
              open={showPrefsPanel}
              onOpenChange={onShowPrefsChange}
              messages={messages}
              eventPrefs={eventPrefs}
              onUpdatePref={onUpdatePref}
              onResetAll={onResetAllPrefs}
            />
          </>
        )}
        {resolvedShowCloseButton && onClose && (
          <button
            onClick={onClose}
            aria-label="Close messages panel"
            className="bg-neutral-800 hover:bg-neutral-700 text-neutral-400 p-2 rounded-lg text-xs transition-colors border border-neutral-700 min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-[36px] flex items-center justify-center"
          >
            <X className="w-4 h-4" />
          </button>
        )}
        </div>
      </div>
      {controlsExpanded && (
        <>
          <div className="flex items-center gap-2">
            <input
              type="text"
              value={localSearchQuery}
              onChange={e => setLocalSearchQuery(e.target.value)}
              placeholder="Search messages..."
              className="flex-1 bg-neutral-900/50 border border-neutral-800/50 text-neutral-100 rounded-lg px-3 py-2 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-700 placeholder-neutral-600"
            />
            <button
              onClick={onOpenSaveDialog}
              title="Save current filters as a view"
              className="shrink-0 flex items-center justify-center gap-1 px-2.5 py-2 rounded-lg text-xs text-neutral-400 hover:text-neutral-200 border border-neutral-800/60 hover:border-neutral-700 transition-colors min-h-[44px] min-w-[44px] md:min-h-[36px] md:min-w-auto"
            >
              <Save className="w-3.5 h-3.5" />
              <span className="hidden md:inline">Save view</span>
            </button>
          </div>
          {/* Saved views tab strip */}
          <SavedViewsTabStrip
            savedViews={savedViews}
            activeViewId={activeViewId}
            defaultViewId={defaultViewId}
            activeTabModified={activeTabModified}
            openKebabId={openKebabId}
            setOpenKebabId={setOpenKebabId}
            kebabRef={kebabRef}
            onApplyView={onApplyView}
            onRenameView={onRenameView}
            onDeleteView={onDeleteView}
          />
        </>
      )}
    </div>
  );
});
