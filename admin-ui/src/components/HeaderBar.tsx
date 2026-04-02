import type { RefObject } from 'react';
import { Activity, Network, Compass, Microscope, Archive, Plus, LogOut } from 'lucide-react';
import { MetricsBar } from './MetricsBar';
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
  apiHealthy: boolean;
  wsConnected: boolean;
  lastFetchTime: number;
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
    apiHealthy,
    wsConnected,
    lastFetchTime,
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

      <MetricsBar
        globalActiveCount={globalActiveCount}
        maxConcurrentVMs={maxConcurrentVMs}
        usingMaxPlan={usingMaxPlan}
        globalEvaluatingCount={globalEvaluatingCount}
        queuedTasksLength={queuedTasks.length}
        lastRefresh={lastRefresh}
        onRefresh={onRefresh}
        showMetrics={showMetrics}
        onToggleMetrics={onToggleMetrics}
        wsRef={wsRef}
        onRefreshTasks={onRefresh}
      />

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
          apiHealthy={apiHealthy}
          wsConnected={wsConnected}
          lastFetchTime={lastFetchTime}
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
