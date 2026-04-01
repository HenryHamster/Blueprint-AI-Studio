import type { RefObject } from 'react';
import { Activity, RefreshCw, Network, Compass, Microscope, Archive, Plus, LogOut } from 'lucide-react';
import { AutonomousStatus } from './AutonomousStatus';
import { SettingsDropdown, type MaxPlanAccount } from './SettingsDropdown';
import type { Task } from './TaskCard';

export interface HeaderBarProps {
  runningTasks: Task[];
  queuedTasks: Task[];
  globalActiveCount: number;
  globalEvaluatingCount: number;
  globalQueuedCount: number;
  maxConcurrentVMs: number;
  usingMaxPlan: boolean;
  lastRefresh: Date | null;
  onRefresh: () => void;
  viewMode: 'kanban' | 'pipeline';
  onToggleViewMode: () => void;
  showResearchBrowser: boolean;
  onToggleResearchBrowser: () => void;
  showArchive: boolean;
  onToggleArchive: () => void;
  onOpenNewTaskModal: () => void;
  onShowDirections: () => void;
  onSignOut: () => void;
  userEmail: string;
  wsRef: RefObject<WebSocket | null>;
  showShortcutHelp: boolean;
  onToggleShortcutHelp: () => void;
  shortcutHelpRef: RefObject<HTMLDivElement | null>;
  // SettingsDropdown props
  showSettings: boolean;
  onToggleSettings: () => void;
  settingsRef: RefObject<HTMLDivElement | null>;
  defaultTaskType: 'create' | 'plan' | 'research' | 'review' | 'fix';
  onUpdateDefaultTaskType: (value: 'create' | 'plan' | 'research' | 'review' | 'fix') => void;
  notifyOnComplete: boolean;
  onUpdateNotifyOnComplete: (value: boolean) => void;
  maxRetries: number;
  onUpdateMaxVMs: (value: number) => void;
  onUpdateMaxRetries: (value: number) => void;
  githubAccount: string | null;
  onConnectGitHub: () => void;
  maxPlanAccounts: MaxPlanAccount[];
  onToggleAccount: (id: string, enabled: boolean) => void;
  onRemoveAccount: (id: string) => void;
  newAccountName: string;
  setNewAccountName: (value: string) => void;
  newAccountToken: string;
  setNewAccountToken: (value: string) => void;
  onAddAccount: () => void;
  showMetrics: boolean;
  onToggleMetrics: () => void;
  refreshInterval: number;
  onUpdateRefreshInterval: (value: number) => void;
}

function formatTimeAgo(date: Date): string {
  const seconds = Math.floor((Date.now() - date.getTime()) / 1000);
  if (seconds < 10) return 'just now';
  if (seconds < 60) return `${Math.floor(seconds / 10) * 10}s ago`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  return `${Math.floor(minutes / 60)}h ago`;
}

export function HeaderBar(props: HeaderBarProps) {
  const {
    queuedTasks,
    globalActiveCount,
    globalEvaluatingCount,
    maxConcurrentVMs,
    usingMaxPlan,
    lastRefresh,
    onRefresh,
    viewMode,
    onToggleViewMode,
    showResearchBrowser,
    onToggleResearchBrowser,
    showArchive,
    onToggleArchive,
    onOpenNewTaskModal,
    onShowDirections,
    onSignOut,
    userEmail,
    wsRef,
    showShortcutHelp,
    onToggleShortcutHelp,
    shortcutHelpRef,
    showSettings,
    onToggleSettings,
    settingsRef,
    defaultTaskType,
    onUpdateDefaultTaskType,
    notifyOnComplete,
    onUpdateNotifyOnComplete,
    maxRetries,
    onUpdateMaxVMs,
    onUpdateMaxRetries,
    githubAccount,
    onConnectGitHub,
    maxPlanAccounts,
    onToggleAccount,
    onRemoveAccount,
    newAccountName,
    setNewAccountName,
    newAccountToken,
    setNewAccountToken,
    onAddAccount,
    showMetrics,
    onToggleMetrics,
    refreshInterval,
    onUpdateRefreshInterval,
  } = props;

  return (
    <header className="mb-6 flex justify-between items-center shrink-0">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-neutral-100 flex items-center gap-2">
          <Activity className="w-5 h-5 text-neutral-400" />
          Blueprint Launcher
        </h1>
        <p className="text-xs text-neutral-500 mt-1 font-mono uppercase tracking-wider">Claude Code PVM Orchestrator</p>
      </div>

      <div
        role="button"
        tabIndex={0}
        onClick={onToggleMetrics}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onToggleMetrics(); } }}
        aria-expanded={showMetrics}
        className="flex items-center gap-8 px-6 py-2 bg-neutral-900/30 rounded-lg border border-neutral-800/50 cursor-pointer hover:bg-neutral-900/50 transition-colors"
        title="Toggle metrics dashboard"
      >
        <div className="flex flex-col items-center">
          <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Active VMs</span>
          <span className="text-xl font-bold text-emerald-400 font-mono leading-none flex items-center gap-1.5">
            {globalActiveCount > 0 && <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>}
            {globalActiveCount}<span className="text-xs text-neutral-600 font-sans">/{maxConcurrentVMs}</span>
          </span>
        </div>
        <div className="w-px h-8 bg-neutral-800"></div>
        <div className="flex flex-col items-center">
          <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Est. Cost</span>
          <span className="text-xl font-bold text-neutral-200 font-mono leading-none">
            ${(globalActiveCount * (usingMaxPlan ? 0.07 : 1.07)).toFixed(2)}<span className="text-xs text-neutral-500 font-sans">/hr</span>
          </span>
          <span className="text-[9px] text-neutral-600 mt-0.5">{usingMaxPlan ? 'VM only (Max plan)' : 'VM + API tokens'}</span>
        </div>
        <div className="w-px h-8 bg-neutral-800"></div>
        <div className="flex flex-col items-center">
          <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Evaluating</span>
          <span className="text-xl font-bold text-amber-400 font-mono leading-none">{globalEvaluatingCount}</span>
        </div>
        <div className="w-px h-8 bg-neutral-800"></div>
        <div className="flex flex-col items-center">
          <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Queue</span>
          <span className="text-xl font-bold text-neutral-200 font-mono leading-none">{queuedTasks.length}</span>
        </div>
        <div className="w-px h-8 bg-neutral-800"></div>
        <button className="flex flex-col items-center group" onClick={(e) => { e.stopPropagation(); onRefresh(); }} title="Click to refresh">
          <span className="text-[10px] text-neutral-500 font-semibold uppercase tracking-wider mb-1">Refreshed</span>
          <span className="text-xs font-medium text-neutral-400 font-mono group-hover:text-neutral-200 transition-colors flex items-center gap-1.5">
            <RefreshCw className="w-3 h-3" />
            {lastRefresh ? formatTimeAgo(lastRefresh) : '...'}
          </span>
        </button>
        <div className="w-px h-8 bg-neutral-800"></div>
        <span onClick={(e) => e.stopPropagation()}>
          <AutonomousStatus onRefreshTasks={onRefresh} wsRef={wsRef} />
        </span>
        <div className="w-px h-8 bg-neutral-800"></div>
        <span className="text-neutral-500 text-xs transition-transform" style={{ transform: showMetrics ? 'rotate(180deg)' : 'rotate(0deg)' }}>▾</span>
      </div>

      <div className="flex items-center gap-2">
        <div className="relative" ref={shortcutHelpRef}>
          <button
            onClick={onToggleShortcutHelp}
            className="bg-neutral-800 hover:bg-neutral-700 text-neutral-500 hover:text-neutral-300 p-2 rounded text-xs transition-colors border border-neutral-700 font-mono"
            title="Keyboard shortcuts"
          >
            ?
          </button>
          {showShortcutHelp && (
            <div className="absolute right-0 top-full mt-2 w-56 bg-neutral-900 border border-neutral-700 rounded-lg shadow-2xl p-3 z-50">
              <h3 className="text-[11px] font-semibold text-neutral-400 uppercase tracking-wider mb-2">Keyboard Shortcuts</h3>
              <div className="space-y-1.5 text-xs">
                {[
                  ['N', 'New task'],
                  ['R', 'Refresh tasks'],
                  ['1', 'Kanban view'],
                  ['2', 'Pipeline view'],
                  ['D', 'Toggle directions'],
                  ['S', 'Toggle settings'],
                  ['M', 'Toggle metrics'],
                  ['A', 'Toggle archive'],
                  ['?', 'This help'],
                  ['Esc', 'Close panel/modal'],
                ].map(([key, desc]) => (
                  <div key={key} className="flex items-center justify-between">
                    <span className="text-neutral-400">{desc}</span>
                    <kbd className="bg-neutral-800 border border-neutral-700 text-neutral-300 px-1.5 py-0.5 rounded text-[10px] font-mono min-w-[24px] text-center">{key}</kbd>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
        <button
          onClick={onToggleViewMode}
          className={`p-2 rounded text-xs transition-colors border ${viewMode === 'pipeline' ? 'bg-neutral-700 text-neutral-100 border-neutral-600' : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'}`}
          title={viewMode === 'kanban' ? 'Switch to Pipeline view' : 'Switch to Kanban view'}
        >
          <Network className="w-4 h-4" />
        </button>
        <button
          onClick={onShowDirections}
          className="bg-neutral-800 hover:bg-neutral-700 text-neutral-300 p-2 rounded text-xs transition-colors border border-neutral-700"
          title="Directions"
        >
          <Compass className="w-4 h-4" />
        </button>
        <SettingsDropdown
          showSettings={showSettings}
          onToggleSettings={onToggleSettings}
          settingsRef={settingsRef}
          defaultTaskType={defaultTaskType}
          onUpdateDefaultTaskType={onUpdateDefaultTaskType}
          viewMode={viewMode}
          onToggleViewMode={onToggleViewMode}
          notifyOnComplete={notifyOnComplete}
          onUpdateNotifyOnComplete={onUpdateNotifyOnComplete}
          maxConcurrentVMs={maxConcurrentVMs}
          onUpdateMaxVMs={onUpdateMaxVMs}
          maxRetries={maxRetries}
          onUpdateMaxRetries={onUpdateMaxRetries}
          githubAccount={githubAccount}
          onConnectGitHub={onConnectGitHub}
          maxPlanAccounts={maxPlanAccounts}
          onToggleAccount={onToggleAccount}
          onRemoveAccount={onRemoveAccount}
          newAccountName={newAccountName}
          setNewAccountName={setNewAccountName}
          newAccountToken={newAccountToken}
          setNewAccountToken={setNewAccountToken}
          onAddAccount={onAddAccount}
          refreshInterval={refreshInterval}
          onUpdateRefreshInterval={onUpdateRefreshInterval}
        />
        <button
          onClick={onToggleArchive}
          className={`p-2 rounded text-xs transition-colors border ${
            showArchive
              ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-400'
              : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'
          }`}
          title="Archive"
        >
          <Archive className="w-4 h-4" />
        </button>
        <button
          onClick={onToggleResearchBrowser}
          className={`p-2 rounded text-xs transition-colors border ${
            showResearchBrowser
              ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-400'
              : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'
          }`}
          title="Research Findings"
        >
          <Microscope className="w-4 h-4" />
        </button>
        <button
          onClick={onOpenNewTaskModal}
          className="bg-white hover:bg-neutral-200 text-neutral-950 px-4 py-2 rounded text-xs font-semibold transition-colors shadow-sm flex items-center gap-2 h-fit"
        >
          <Plus className="w-3.5 h-3.5" />
          Launch Task
        </button>
        <button
          onClick={onSignOut}
          className="bg-neutral-800 hover:bg-neutral-700 text-neutral-400 p-2 rounded text-xs transition-colors border border-neutral-700"
          title={`Sign out (${userEmail})`}
        >
          <LogOut className="w-4 h-4" />
        </button>
      </div>
    </header>
  );
}
