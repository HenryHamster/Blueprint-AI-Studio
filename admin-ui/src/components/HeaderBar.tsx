import type { RefObject } from 'react';
import { useMemo, useState, useRef, useEffect } from 'react';
import { Activity, Network, Compass, Microscope, Archive, Clock, Plus, LogOut, MoreHorizontal, Settings, Users } from 'lucide-react';
import { MetricsBar } from './MetricsBar';
import { SettingsDropdown, type MaxPlanAccount } from './SettingsDropdown';
import type { DeployState } from '../types/settings';
import type { Task } from '../types/task';

export interface HeaderBarProps {
  tasks: Task[];
  runningTasks: Task[];
  queuedTasks: Task[];
  globalActiveCount: number;
  globalEvaluatingCount: number;
  globalQueuedCount: number;
  maxConcurrentVMs: number;
  lastRefresh: Date | null;
  onRefresh: () => void;
  viewMode: 'kanban' | 'pipeline';
  onToggleViewMode: () => void;
  showResearchBrowser: boolean;
  onToggleResearchBrowser: () => void;
  showArchive: boolean;
  onToggleArchive: () => void;
  showActivityFeed: boolean;
  onToggleActivityFeed: () => void;
  newActivityCount: number;
  needsAttentionCount: number;
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
  defaultTaskType: 'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor';
  onUpdateDefaultTaskType: (value: 'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor') => void;
  notifyOnComplete: boolean;
  onUpdateNotifyOnComplete: (value: boolean) => void;
  notifyOnFail: boolean;
  onUpdateNotifyOnFail: (value: boolean) => void;
  notifyOnApprovalNeeded: boolean;
  onUpdateNotifyOnApprovalNeeded: (value: boolean) => void;
  soundOnComplete: boolean;
  onUpdateSoundOnComplete: (value: boolean) => void;
  maxRetries: number;
  maxAutonomousTasksPerHour: number;
  onUpdateMaxVMs: (value: number) => void;
  onUpdateMaxRetries: (value: number) => void;
  onUpdateMaxAutonomousRate: (value: number) => void;
  autonomousResearchIntervalMinutes: number;
  onUpdateAutonomousResearchInterval: (value: number) => void;
  directionCooldownMinutes: number;
  onUpdateDirectionCooldownMinutes: (value: number) => void;
  onSyncTaskTypeDefaults: () => void;
  healthCheckUrls: string[];
  onUpdateHealthCheckUrls: (urls: string[]) => void;
  agentPromptSuffixes: Record<string, string>;
  onUpdateAgentPromptSuffixes: (suffixes: Record<string, string>) => void;
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
  metricsHistory: { timestamp: number; completedCount: number }[];
  refreshInterval: number;
  onUpdateRefreshInterval: (value: number) => void;
  pageSize: number;
  onUpdatePageSize: (value: number) => void;
  apiHealthy: boolean;
  wsConnected: boolean;
  lastFetchTime: number;
  onRestartOrchestrator: () => Promise<void>;
  anyAutonomousEnabled: boolean;
  onPauseAllAutonomous: () => Promise<void>;
  // Deploy state
  deployState: DeployState | null;
  onPausePipeline: () => Promise<void>;
  onResumePipeline: () => Promise<void>;
  onResetHealth: () => Promise<void>;
  onRollback: () => Promise<void>;
  onMarkStable: () => Promise<void>;
  onRepairState: () => Promise<void>;
  zoneHealth: Record<string, { status: string; last_success: string | null; last_failure: string | null; last_failure_reason: string | null; consecutive_failures: number; cooldown_active: boolean }> | null;
  showUserManagement: boolean;
  onToggleUserManagement: () => void;
}


export function HeaderBar(props: HeaderBarProps) {
  const {
    tasks,
    queuedTasks,
    globalActiveCount,
    globalEvaluatingCount,
    maxConcurrentVMs,
    lastRefresh,
    onRefresh,
    viewMode,
    onToggleViewMode,
    showResearchBrowser,
    onToggleResearchBrowser,
    showArchive,
    onToggleArchive,
    showActivityFeed,
    onToggleActivityFeed,
    newActivityCount,
    needsAttentionCount,
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
    notifyOnFail,
    onUpdateNotifyOnFail,
    notifyOnApprovalNeeded,
    onUpdateNotifyOnApprovalNeeded,
    soundOnComplete,
    onUpdateSoundOnComplete,
    maxRetries,
    maxAutonomousTasksPerHour,
    onUpdateMaxVMs,
    onUpdateMaxRetries,
    onUpdateMaxAutonomousRate,
    autonomousResearchIntervalMinutes,
    onUpdateAutonomousResearchInterval,
    directionCooldownMinutes,
    onUpdateDirectionCooldownMinutes,
    onSyncTaskTypeDefaults,
    agentPromptSuffixes,
    onUpdateAgentPromptSuffixes,
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
    metricsHistory,
    refreshInterval,
    onUpdateRefreshInterval,
    pageSize,
    onUpdatePageSize,
    apiHealthy,
    wsConnected,
    lastFetchTime,
    onRestartOrchestrator,
    anyAutonomousEnabled,
    onPauseAllAutonomous,
  } = props;

  const [showMobileMenu, setShowMobileMenu] = useState(false);
  const [showMobileShortcuts, setShowMobileShortcuts] = useState(false);
  const mobileMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!showMobileMenu) return;
    function handleClickOutside(e: MouseEvent) {
      if (mobileMenuRef.current && !mobileMenuRef.current.contains(e.target as Node)) {
        setShowMobileMenu(false);
        setShowMobileShortcuts(false);
      }
    }
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [showMobileMenu]);

  const pendingFindingsCount = useMemo(() => {
    // Build set of implemented finding keys
    const implemented = new Set<string>();
    for (const t of tasks) {
      const ctx = t.context as Record<string, unknown> | undefined;
      if (ctx?.research_task_id && ctx?.finding_index !== undefined) {
        implemented.add(`${ctx.research_task_id}-${ctx.finding_index}`);
      }
    }
    // Count unimplemented findings from completed research tasks
    let count = 0;
    for (const t of tasks) {
      if (t.task_type !== 'research' || t.status !== 'COMPLETED' || !t.research_findings) continue;
      const arr = Array.isArray(t.research_findings) ? t.research_findings : [];
      arr.forEach((_: unknown, i: number) => {
        if (!implemented.has(`${t.id}-${i}`)) count++;
      });
    }
    return count;
  }, [tasks]);

  return (
    <header className="mb-6 flex justify-between items-center shrink-0">
      <div>
        <h1 className="text-xl font-semibold tracking-tight text-neutral-100 flex items-center gap-2">
          <Activity className="w-5 h-5 text-neutral-400" />
          Blueprint Launcher
        </h1>
        <p className="text-xs text-neutral-500 mt-1 font-mono uppercase tracking-wider">Claude Code PVM Orchestrator</p>
      </div>

      {/* MetricsBar — hidden on mobile to save space */}
      <div className="hidden md:flex">
        <MetricsBar
          globalActiveCount={globalActiveCount}
          maxConcurrentVMs={maxConcurrentVMs}
          globalEvaluatingCount={globalEvaluatingCount}
          queuedTasksLength={queuedTasks.length}
          lastRefresh={lastRefresh}
          onRefresh={onRefresh}
          showMetrics={showMetrics}
          onToggleMetrics={onToggleMetrics}
          wsRef={wsRef}
          onRefreshTasks={onRefresh}
          metricsHistory={metricsHistory}
          pauseReason={props.deployState?.reason ?? null}
        />
      </div>

      <div className="flex items-center gap-2">
        {/* Secondary buttons — desktop only, in original order: ? | Network | Compass */}
        <div className="hidden md:flex items-center gap-2">
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
                    ['A', 'Toggle activity feed'],
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
        </div>

        {/* SettingsDropdown — always in DOM so its panel renders when triggered from the mobile menu.
            The trigger button is hidden on mobile via triggerClassName; the absolute dropdown panel
            remains accessible because the wrapper div stays in the layout. */}
        <SettingsDropdown
          triggerClassName="hidden md:inline-block"
          showSettings={showSettings}
          onToggleSettings={onToggleSettings}
          settingsRef={settingsRef}
          defaultTaskType={defaultTaskType}
          onUpdateDefaultTaskType={onUpdateDefaultTaskType}
          viewMode={viewMode}
          onToggleViewMode={onToggleViewMode}
          notifyOnComplete={notifyOnComplete}
          onUpdateNotifyOnComplete={onUpdateNotifyOnComplete}
          notifyOnFail={notifyOnFail}
          onUpdateNotifyOnFail={onUpdateNotifyOnFail}
          notifyOnApprovalNeeded={notifyOnApprovalNeeded}
          onUpdateNotifyOnApprovalNeeded={onUpdateNotifyOnApprovalNeeded}
          soundOnComplete={soundOnComplete}
          onUpdateSoundOnComplete={onUpdateSoundOnComplete}
          maxConcurrentVMs={maxConcurrentVMs}
          onUpdateMaxVMs={onUpdateMaxVMs}
          maxRetries={maxRetries}
          onUpdateMaxRetries={onUpdateMaxRetries}
          maxAutonomousTasksPerHour={maxAutonomousTasksPerHour}
          onUpdateMaxAutonomousRate={onUpdateMaxAutonomousRate}
          autonomousResearchIntervalMinutes={autonomousResearchIntervalMinutes}
          onUpdateAutonomousResearchInterval={onUpdateAutonomousResearchInterval}
          directionCooldownMinutes={directionCooldownMinutes}
          onUpdateDirectionCooldownMinutes={onUpdateDirectionCooldownMinutes}
          onSyncTaskTypeDefaults={onSyncTaskTypeDefaults}
          healthCheckUrls={props.healthCheckUrls}
          onUpdateHealthCheckUrls={props.onUpdateHealthCheckUrls}
          agentPromptSuffixes={agentPromptSuffixes}
          onUpdateAgentPromptSuffixes={onUpdateAgentPromptSuffixes}
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
          pageSize={pageSize}
          onUpdatePageSize={onUpdatePageSize}
          apiHealthy={apiHealthy}
          wsConnected={wsConnected}
          lastFetchTime={lastFetchTime}
          onRestartOrchestrator={onRestartOrchestrator}
          anyAutonomousEnabled={anyAutonomousEnabled}
          onPauseAllAutonomous={onPauseAllAutonomous}
          deployState={props.deployState}
          onPausePipeline={props.onPausePipeline}
          onResumePipeline={props.onResumePipeline}
          onResetHealth={props.onResetHealth}
          onRollback={props.onRollback}
          onMarkStable={props.onMarkStable}
          onRepairState={props.onRepairState}
          zoneHealth={props.zoneHealth}
        />

        {/* More secondary buttons — desktop only */}
        <div className="hidden md:flex items-center gap-2">
          <button
            onClick={props.onToggleUserManagement}
            className={`p-2 rounded text-xs transition-colors border ${
              props.showUserManagement
                ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-400'
                : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'
            }`}
            title="User Management"
          >
            <Users className="w-4 h-4" />
          </button>
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
            onClick={onToggleActivityFeed}
            className={`p-2 rounded text-xs transition-colors border relative ${
              showActivityFeed
                ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-400'
                : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'
            }`}
            title="Toggle activity feed (A)"
          >
            <Clock className="w-4 h-4" />
            {newActivityCount > 0 && !showActivityFeed && (
              <span className="absolute top-0.5 right-0.5 w-2 h-2 bg-red-500 rounded-full" />
            )}
          </button>
          <button
            onClick={onToggleResearchBrowser}
            className={`p-2 rounded text-xs transition-colors border relative flex items-center ${
              showResearchBrowser
                ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-400'
                : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'
            }`}
            title="Research Findings"
          >
            <Microscope className="w-4 h-4" />
            {pendingFindingsCount > 0 && (
              <span className="ml-1 bg-cyan-500 text-black text-[9px] font-bold px-1.5 py-0.5 rounded-full leading-none">
                {pendingFindingsCount}
              </span>
            )}
          </button>
        </div>

        {/* Mobile overflow menu — hidden on desktop */}
        <div className="relative md:hidden" ref={mobileMenuRef}>
          <button
            onClick={() => setShowMobileMenu(prev => !prev)}
            className="bg-neutral-800 hover:bg-neutral-700 text-neutral-300 p-3 min-w-[44px] min-h-[44px] rounded text-xs transition-colors border border-neutral-700 flex items-center justify-center"
            title="More options"
          >
            <MoreHorizontal className="w-4 h-4" />
          </button>
          {showMobileMenu && (
            <div className="absolute right-0 top-full mt-2 w-56 bg-neutral-900 border border-neutral-700 rounded-lg shadow-2xl p-2 z-50">
              <button
                onClick={() => { onToggleViewMode(); setShowMobileMenu(false); }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded text-sm transition-colors text-left ${viewMode === 'pipeline' ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
              >
                <Network className="w-4 h-4 shrink-0" />
                <span>{viewMode === 'kanban' ? 'Switch to Pipeline' : 'Switch to Kanban'}</span>
              </button>
              <button
                onClick={() => { onToggleResearchBrowser(); setShowMobileMenu(false); }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded text-sm transition-colors text-left ${showResearchBrowser ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
              >
                <Microscope className="w-4 h-4 shrink-0" />
                <span>Research Browser</span>
                {pendingFindingsCount > 0 && (
                  <span className="ml-auto bg-cyan-500 text-black text-[9px] font-bold px-1.5 py-0.5 rounded-full leading-none">
                    {pendingFindingsCount}
                  </span>
                )}
              </button>
              <button
                onClick={() => { onToggleArchive(); setShowMobileMenu(false); }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded text-sm transition-colors text-left ${showArchive ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
              >
                <Archive className="w-4 h-4 shrink-0" />
                <span>Archive</span>
              </button>
              <button
                onClick={() => { onToggleActivityFeed(); setShowMobileMenu(false); }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 rounded text-sm transition-colors text-left ${showActivityFeed ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
              >
                <Clock className="w-4 h-4 shrink-0" />
                <span>Activity Feed</span>
                {newActivityCount > 0 && !showActivityFeed && (
                  <span className="ml-auto bg-red-500 text-white text-[9px] font-bold px-1.5 py-0.5 rounded-full leading-none">
                    {newActivityCount}
                  </span>
                )}
              </button>
              <button
                onClick={() => { onShowDirections(); setShowMobileMenu(false); }}
                className="w-full flex items-center gap-3 px-3 py-2.5 rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
              >
                <Compass className="w-4 h-4 shrink-0" />
                <span>Directions</span>
              </button>
              <button
                onClick={() => setShowMobileShortcuts(prev => !prev)}
                className="w-full flex items-center gap-3 px-3 py-2.5 rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
              >
                <span className="w-4 h-4 flex items-center justify-center font-mono shrink-0">?</span>
                <span>Keyboard Shortcuts</span>
                <span className="ml-auto text-neutral-500 text-xs">{showMobileShortcuts ? '▴' : '▾'}</span>
              </button>
              {showMobileShortcuts && (
                <div className="mx-2 mb-1 bg-neutral-800/50 rounded p-2">
                  <div className="space-y-1.5 text-xs">
                    {[
                      ['N', 'New task'],
                      ['R', 'Refresh tasks'],
                      ['1', 'Kanban view'],
                      ['2', 'Pipeline view'],
                      ['D', 'Toggle directions'],
                      ['S', 'Toggle settings'],
                      ['M', 'Toggle metrics'],
                      ['A', 'Toggle activity feed'],
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
              <button
                onClick={() => { onToggleSettings(); setShowMobileMenu(false); }}
                className="w-full flex items-center gap-3 px-3 py-2.5 rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
              >
                <Settings className="w-4 h-4 shrink-0" />
                <span>Settings</span>
              </button>
            </div>
          )}
        </div>

        {/* Needs attention badge — always visible when count > 0 */}
        {needsAttentionCount > 0 && (
          <span className="bg-red-500/20 border border-red-500/40 text-red-400 text-[10px] font-bold px-2 py-1 rounded-full leading-none whitespace-nowrap" title={`${needsAttentionCount} task${needsAttentionCount !== 1 ? 's' : ''} need${needsAttentionCount === 1 ? 's' : ''} attention`}>
            {needsAttentionCount} need{needsAttentionCount !== 1 ? '' : 's'} attention
          </span>
        )}

        {/* New Task — always visible (primary action) */}
        <button
          onClick={onOpenNewTaskModal}
          className="bg-white hover:bg-neutral-200 text-neutral-950 px-4 py-2 min-h-[44px] md:min-h-0 rounded text-xs font-semibold transition-colors shadow-sm flex items-center gap-2 h-fit"
        >
          <Plus className="w-3.5 h-3.5" />
          Launch Task
        </button>

        {/* Sign out — always visible */}
        <button
          onClick={onSignOut}
          className="bg-neutral-800 hover:bg-neutral-700 text-neutral-400 p-3 md:p-2 min-w-[44px] min-h-[44px] md:min-w-0 md:min-h-0 rounded text-xs transition-colors border border-neutral-700 flex items-center justify-center"
          title={`Sign out (${userEmail})`}
        >
          <LogOut className="w-4 h-4" />
        </button>
      </div>
    </header>
  );
}
