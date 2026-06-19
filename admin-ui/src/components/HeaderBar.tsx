import type { RefObject } from 'react';
import { useMemo, useState, useRef, useEffect } from 'react';
import { Activity, Network, Compass, Microscope, BookOpen, Archive, Plus, MoreHorizontal, Settings, LayoutGrid, LayoutDashboard, MessageSquare, CalendarClock } from 'lucide-react';
import { MetricsBar } from './MetricsBar';
import { AutonomousStatus } from './AutonomousStatus';
import { SettingsDropdown, type MaxPlanAccount, type SectionId } from './SettingsDropdown';
import type { AccountMutationReturn, AccountUpdatePayload, AuthMode, CategoryRoutingEntry, DeployState, FailedRollbackInfo, ModelPreference, ProviderCatalogEntry, ProviderName, ProviderSettingsByEffort, ProviderSettingsDefaults, TaskTypeProviderOverrides, TaskTypeRoutingEntry } from '../types/settings';
import type { Task } from '../types/task';
import type { BuildConfig, AutoRemediationConfig, AutoRemediationStats, IssueImplementationConfig, IssueImplementationConfigUpdate, AutoReviewConfig, ProjectResourceEnvelopeResponse, ResourceEnvelopeConfig } from '../types/project';
import type { UserLaunchableTaskType } from '../taskCapabilities';
import type { ViewMode } from '../hooks/useViewPreferences';

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
  viewMode: ViewMode;
  onSetViewMode: (mode: ViewMode) => void;
  showResearchBrowser: boolean;
  onToggleResearchBrowser: () => void;
  showKnowledgeBrowser: boolean;
  onToggleKnowledgeBrowser: () => void;
  showArchive: boolean;
  onToggleArchive: () => void;
  showMessages: boolean;
  onToggleMessages: () => void;
  onOpenActivityFeed?: () => void;
  openMessageCount: number;
  newSinceLastVisitCount?: number;
  needsAttentionCount?: number;
  onOpenNewTaskModal: () => void;
  onShowDirections: () => void;
  onSignOut: () => void;
  userEmail: string;
  wsRef: RefObject<WebSocket | null>;
  // SettingsDropdown props
  showSettings: boolean;
  onToggleSettings: () => void;
  settingsRef: RefObject<HTMLDivElement | null>;
  settingsInitialSection?: SectionId | null;
  defaultTaskType: UserLaunchableTaskType;
  onUpdateDefaultTaskType: (value: UserLaunchableTaskType) => void;
  notifyOnComplete: boolean;
  onUpdateNotifyOnComplete: (value: boolean) => void;
  notifyOnFail: boolean;
  onUpdateNotifyOnFail: (value: boolean) => void;
  notifyOnApprovalNeeded: boolean;
  onUpdateNotifyOnApprovalNeeded: (value: boolean) => void;
  soundOnComplete: boolean;
  onUpdateSoundOnComplete: (value: boolean) => void;
  notifyOnHighSignalMessage: boolean;
  onUpdateNotifyOnHighSignalMessage: (value: boolean) => void;
  maxRetries: number;
  maxAutonomousTasksPerHour: number;
  maxGlobalAutonomousTasksPerHour: number;
  onUpdateMaxGlobalAutonomousRate: (value: number) => void;
  maxPipelineSlots: number;
  onUpdateMaxPipelineSlots: (value: number) => void;
  reservedSlotsForTypes: Record<string, number>;
  onUpdateReservedSlotsForTypes: (value: Record<string, number>) => void;
  evaluatingPipelineTimeoutMinutes: number;
  onUpdateEvaluatingPipelineTimeout: (value: number) => void;
  evaluatingFailHours: number;
  onUpdateEvaluatingFailHours: (value: number) => void;
  ciBackstopTimeoutMinutes: number;
  onUpdateCiBackstopTimeout: (value: number) => void;
  requireApprovalGates: boolean;
  autoArchiveEnabled: boolean;
  autoArchiveTerminalAfterDays: number;
  autoArchiveMaxTerminalTasksPerProject: number;
  autoArchiveIntervalMinutes: number;
  onUpdateAutoArchiveEnabled: (value: boolean) => void;
  onUpdateAutoArchiveTerminalAfterDays: (value: number) => void;
  onUpdateAutoArchiveMaxTerminalTasksPerProject: (value: number) => void;
  onUpdateAutoArchiveIntervalMinutes: (value: number) => void;
  onUpdateMaxVMs: (value: number) => void;
  onUpdateRequireApprovalGates: (value: boolean) => void;
  onUpdateMaxRetries: (value: number) => void;
  onUpdateMaxAutonomousRate: (value: number) => void;
  autonomousResearchIntervalMinutes: number;
  onUpdateAutonomousResearchInterval: (value: number) => void;
  directionCooldownMinutes: number;
  onUpdateDirectionCooldownMinutes: (value: number) => void;
  strategicResearchFrequency: number;
  onUpdateStrategicResearchFrequency: (value: number) => void;
  healthCheckUrls: string[];
  onUpdateHealthCheckUrls: (urls: string[]) => void;
  githubAccount: string | null;
  onConnectGitHub: () => void;
  maxPlanAccounts: MaxPlanAccount[];
  onToggleAccount: (id: string, enabled: boolean) => void;
  onRemoveAccount: (id: string, force?: boolean) => AccountMutationReturn;
  onResetRateLimits: (id: string) => void;
  onRefreshAccountModels: (id: string) => void;
  onUpdateAccount: (id: string, payload: AccountUpdatePayload) => AccountMutationReturn;
  newAccountName: string;
  setNewAccountName: (value: string) => void;
  newAccountProvider: ProviderName;
  setNewAccountProvider: (value: ProviderName) => void;
  newAccountAuthMode: AuthMode;
  setNewAccountAuthMode: (value: AuthMode) => void;
  newAccountToken: string;
  setNewAccountToken: (value: string) => void;
  newAccountProviderMetadata: Record<string, string>;
  setNewAccountProviderMetadata: (value: Record<string, string>) => void;
  newAccountClaudeConfigJson: string;
  setNewAccountClaudeConfigJson: (value: string) => void;
  newAccountDailyTaskCap: number | null;
  setNewAccountDailyTaskCap: (value: number | null) => void;
  newAccountMaxConcurrentTasks: number | null;
  setNewAccountMaxConcurrentTasks: (value: number | null) => void;
  newAccountAutoDisableAfterRateLimits: number | null;
  setNewAccountAutoDisableAfterRateLimits: (value: number | null) => void;
  onAddAccount: () => void;
  onUpdateAccountCaps: (id: string, caps: { daily_task_cap?: number | null; max_concurrent_tasks?: number | null; auto_disable_after_rate_limits?: number | null }) => void;
  providerTierModelMap: Record<string, Record<string, string>>;
  taskTypeModelPreferences: Record<string, ModelPreference[]>;
  providers: Record<string, ProviderCatalogEntry>;
  providerSettingsDefaults: ProviderSettingsDefaults;
  taskTypeProviderOverrides: TaskTypeProviderOverrides;
  onUpdateProviderSettingsDefaults: (value: ProviderSettingsDefaults) => Promise<void>;
  onUpdateTaskTypeProviderOverrides: (value: TaskTypeProviderOverrides) => Promise<void>;
  providerSelectionMode: 'balanced' | 'preferred' | 'exclusive';
  onUpdateProviderSelectionMode: (mode: 'balanced' | 'preferred' | 'exclusive') => Promise<void>;
  taskTypeCategories: Record<string, string[]>;
  onUpdateTaskTypeCategories: (value: Record<string, string[]>) => Promise<void>;
  categoryRouting: Record<string, CategoryRoutingEntry>;
  onUpdateCategoryRouting: (value: Record<string, CategoryRoutingEntry>) => Promise<void>;
  taskTypeRouting: Record<string, TaskTypeRoutingEntry>;
  onUpdateTaskTypeRouting: (value: Record<string, TaskTypeRoutingEntry>) => Promise<void>;
  providerSettingsByEffort: ProviderSettingsByEffort;
  onUpdateProviderSettingsByEffort: (value: ProviderSettingsByEffort) => Promise<void>;
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
  // Standing client-side signal that the last manual rollback failed (the
  // backend returned status='rollback_failed' and left state untouched). Null
  // when there is no outstanding failed rollback. Threaded to the deploy panel
  // so the affordance outlives the transient error toast.
  failedRollback: FailedRollbackInfo | null;
  zoneHealth: Record<string, { status: string; last_success: string | null; last_failure: string | null; last_failure_reason: string | null; consecutive_failures: number; cooldown_active: boolean }> | null;
  showUserManagement: boolean;
  onToggleUserManagement: () => void;
  showProjectMembers: boolean;
  onToggleProjectMembers: () => void;
  selectedProjectMyRole: string | null;
  userRole: 'admin' | 'user' | null;
  notificationConfig: import('./SettingsNotificationsSection').NotificationConfig;
  onUpdateNotificationConfig: (config: Partial<import('./SettingsNotificationsSection').NotificationConfig>) => void;
  dailyCostCeilingUsd: number;
  onUpdateDailyCostCeiling: (value: number) => void;
  // Build config (project-scoped)
  selectedProjectId: string | null;
  selectedProjectOwner: string | null;
  selectedProjectRepo: string | null;
  selectedProjectIsSelfRepo?: boolean;
  selectedProjectRole?: 'owner' | 'member' | 'viewer' | null;
  buildConfig: BuildConfig | null;
  onUpdateBuildConfig: (config: BuildConfig) => Promise<void>;
  onDetectBuildConfig: () => Promise<void>;
  // Auto-remediation config (project-scoped)
  autoRemediationConfig: AutoRemediationConfig | null;
  autoRemediationStats: AutoRemediationStats | null;
  onUpdateAutoRemediationConfig: (config: AutoRemediationConfig) => Promise<void>;
  // Issue implementation config (project-scoped)
  issueImplementationConfig: IssueImplementationConfig | null;
  onUpdateIssueImplementationConfig: (config: IssueImplementationConfigUpdate) => Promise<void>;
  autoReviewConfig: AutoReviewConfig | null;
  onUpdateAutoReviewConfig: (patch: Partial<AutoReviewConfig>) => Promise<void>;
  resourceEnvelopeData: ProjectResourceEnvelopeResponse | null;
  resourceEnvelopeLoading: boolean;
  resourceEnvelopeError: string | null;
  onUpdateResourceEnvelope: (patch: Partial<ResourceEnvelopeConfig>) => Promise<void>;
  integrationBranchData: { integration_branch: string; is_default: boolean } | null;
  onUpdateIntegrationBranch: (branch: string | null) => Promise<void>;
  promotionTargetData: { promotion_target: string; is_default: boolean } | null;
  onUpdatePromotionTarget: (branch: string | null) => Promise<void>;
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
    onSetViewMode,
    showResearchBrowser,
    onToggleResearchBrowser,
    showKnowledgeBrowser,
    onToggleKnowledgeBrowser,
    showArchive,
    onToggleArchive,
    showMessages,
    onToggleMessages,
    onOpenActivityFeed,
    onOpenNewTaskModal,
    onShowDirections,
    wsRef,
    showSettings,
    onToggleSettings,
    settingsRef,
    settingsInitialSection,
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
    notifyOnHighSignalMessage,
    onUpdateNotifyOnHighSignalMessage,
    maxRetries,
    maxAutonomousTasksPerHour,
    maxGlobalAutonomousTasksPerHour,
    onUpdateMaxGlobalAutonomousRate,
    maxPipelineSlots,
    onUpdateMaxPipelineSlots,
    reservedSlotsForTypes,
    onUpdateReservedSlotsForTypes,
    evaluatingPipelineTimeoutMinutes,
    onUpdateEvaluatingPipelineTimeout,
    evaluatingFailHours,
    onUpdateEvaluatingFailHours,
    ciBackstopTimeoutMinutes,
    onUpdateCiBackstopTimeout,
    requireApprovalGates,
    autoArchiveEnabled,
    autoArchiveTerminalAfterDays,
    autoArchiveMaxTerminalTasksPerProject,
    autoArchiveIntervalMinutes,
    onUpdateAutoArchiveEnabled,
    onUpdateAutoArchiveTerminalAfterDays,
    onUpdateAutoArchiveMaxTerminalTasksPerProject,
    onUpdateAutoArchiveIntervalMinutes,
    onUpdateMaxVMs,
    onUpdateMaxRetries,
    onUpdateRequireApprovalGates,
    onUpdateMaxAutonomousRate,
    autonomousResearchIntervalMinutes,
    onUpdateAutonomousResearchInterval,
    directionCooldownMinutes,
    onUpdateDirectionCooldownMinutes,
    strategicResearchFrequency,
    onUpdateStrategicResearchFrequency,
    githubAccount,
    onConnectGitHub,
    maxPlanAccounts,
    onToggleAccount,
    onRemoveAccount,
    onResetRateLimits,
    onRefreshAccountModels,
    onUpdateAccount,
    newAccountName,
    setNewAccountName,
    newAccountProvider,
    setNewAccountProvider,
    newAccountAuthMode,
    setNewAccountAuthMode,
    newAccountToken,
    setNewAccountToken,
    newAccountProviderMetadata,
    setNewAccountProviderMetadata,
    newAccountClaudeConfigJson,
    setNewAccountClaudeConfigJson,
    newAccountDailyTaskCap,
    setNewAccountDailyTaskCap,
    newAccountMaxConcurrentTasks,
    setNewAccountMaxConcurrentTasks,
    newAccountAutoDisableAfterRateLimits,
    setNewAccountAutoDisableAfterRateLimits,
    onAddAccount,
    onUpdateAccountCaps,
    providerTierModelMap,
    taskTypeModelPreferences,
    providers,
    providerSettingsDefaults,
    providerSelectionMode,
    onUpdateProviderSelectionMode,
    taskTypeCategories,
    onUpdateTaskTypeCategories,
    categoryRouting,
    onUpdateCategoryRouting,
    taskTypeRouting,
    onUpdateTaskTypeRouting,
    providerSettingsByEffort,
    onUpdateProviderSettingsByEffort,
    taskTypeProviderOverrides,
    onUpdateProviderSettingsDefaults,
    onUpdateTaskTypeProviderOverrides,
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
  const currentViewMode = viewMode;
  const setViewMode = onSetViewMode;

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
    <header className="mb-6 relative flex items-center shrink-0">
      {/* Left: title */}
      <div className="shrink-0">
        <h1 className="text-base font-semibold tracking-tight text-neutral-100 flex items-center gap-2">
          <Activity className="w-4 h-4 text-neutral-400" />
          Blueprint
        </h1>
      </div>

      {/* Center: MetricsBar — absolutely positioned so it's truly centered */}
      <div className="hidden md:flex items-center gap-3 absolute left-1/2 -translate-x-1/2 z-30">
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
        {/* API connection problem — only visible when API is unreachable */}
        {!apiHealthy && (
          <div className="flex items-center gap-2 text-[10px] px-2 py-1 rounded bg-red-950/30 border border-red-900/30">
            <div className="flex items-center gap-1">
              <span className="w-1.5 h-1.5 rounded-full bg-red-500 animate-pulse" />
              <span className="text-red-400">API down</span>
            </div>
          </div>
        )}
      </div>

      {/* Right: buttons */}
      <div className="flex items-center gap-1.5 ml-auto">
        <div className="hidden md:flex items-center gap-1">
          <div className="flex items-center rounded-md overflow-hidden">
            {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */}
            <button
              onClick={() => onSetViewMode('home')}
              className={`p-2 text-xs transition-colors ${viewMode === 'home' ? 'bg-neutral-700/80 text-neutral-100' : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'}`}
              title="Dashboard home"
            >
              <LayoutDashboard className="w-4 h-4" />
            </button>
            {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */}
            <button
              onClick={() => onSetViewMode('kanban')}
              className={`p-2 text-xs transition-colors ${viewMode === 'kanban' ? 'bg-neutral-700/80 text-neutral-100' : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'}`}
              title="Kanban view"
            >
              <LayoutGrid className="w-4 h-4" />
            </button>
            {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */}
            <button
              onClick={() => onSetViewMode('pipeline')}
              className={`p-2 text-xs transition-colors ${viewMode === 'pipeline' ? 'bg-neutral-700/80 text-neutral-100' : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'}`}
              title="Pipeline view"
            >
              <Network className="w-4 h-4" />
            </button>
            {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */}
            <button
              onClick={() => setViewMode('timeline')}
              className={`p-2 text-xs transition-colors ${currentViewMode === 'timeline' ? 'bg-neutral-700/80 text-neutral-100' : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'}`}
              title="Timeline view"
            >
              <CalendarClock className="w-4 h-4" />
            </button>
          </div>
          {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */}
          <button
            onClick={onShowDirections}
            className="text-neutral-400 hover:text-neutral-200 p-2 rounded-md text-xs transition-colors hover:bg-neutral-800"
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
          initialSection={settingsInitialSection ?? null}
          defaultTaskType={defaultTaskType}
          onUpdateDefaultTaskType={onUpdateDefaultTaskType}
          viewMode={viewMode}
          onSetViewMode={onSetViewMode}
          notifyOnComplete={notifyOnComplete}
          onUpdateNotifyOnComplete={onUpdateNotifyOnComplete}
          notifyOnFail={notifyOnFail}
          onUpdateNotifyOnFail={onUpdateNotifyOnFail}
          notifyOnApprovalNeeded={notifyOnApprovalNeeded}
          onUpdateNotifyOnApprovalNeeded={onUpdateNotifyOnApprovalNeeded}
          soundOnComplete={soundOnComplete}
          onUpdateSoundOnComplete={onUpdateSoundOnComplete}
          notifyOnHighSignalMessage={notifyOnHighSignalMessage}
          onUpdateNotifyOnHighSignalMessage={onUpdateNotifyOnHighSignalMessage}
          maxConcurrentVMs={maxConcurrentVMs}
          onUpdateMaxVMs={onUpdateMaxVMs}
          maxRetries={maxRetries}
          onUpdateMaxRetries={onUpdateMaxRetries}
          requireApprovalGates={requireApprovalGates}
          onUpdateRequireApprovalGates={onUpdateRequireApprovalGates}
          autoArchiveEnabled={autoArchiveEnabled}
          onUpdateAutoArchiveEnabled={onUpdateAutoArchiveEnabled}
          autoArchiveTerminalAfterDays={autoArchiveTerminalAfterDays}
          onUpdateAutoArchiveTerminalAfterDays={onUpdateAutoArchiveTerminalAfterDays}
          autoArchiveMaxTerminalTasksPerProject={autoArchiveMaxTerminalTasksPerProject}
          onUpdateAutoArchiveMaxTerminalTasksPerProject={onUpdateAutoArchiveMaxTerminalTasksPerProject}
          autoArchiveIntervalMinutes={autoArchiveIntervalMinutes}
          onUpdateAutoArchiveIntervalMinutes={onUpdateAutoArchiveIntervalMinutes}
          maxAutonomousTasksPerHour={maxAutonomousTasksPerHour}
          onUpdateMaxAutonomousRate={onUpdateMaxAutonomousRate}
          maxGlobalAutonomousTasksPerHour={maxGlobalAutonomousTasksPerHour}
          onUpdateMaxGlobalAutonomousRate={onUpdateMaxGlobalAutonomousRate}
          maxPipelineSlots={maxPipelineSlots}
          onUpdateMaxPipelineSlots={onUpdateMaxPipelineSlots}
          reservedSlotsForTypes={reservedSlotsForTypes}
          onUpdateReservedSlotsForTypes={onUpdateReservedSlotsForTypes}
          evaluatingPipelineTimeoutMinutes={evaluatingPipelineTimeoutMinutes}
          onUpdateEvaluatingPipelineTimeout={onUpdateEvaluatingPipelineTimeout}
          evaluatingFailHours={evaluatingFailHours}
          onUpdateEvaluatingFailHours={onUpdateEvaluatingFailHours}
          ciBackstopTimeoutMinutes={ciBackstopTimeoutMinutes}
          onUpdateCiBackstopTimeout={onUpdateCiBackstopTimeout}
          autonomousResearchIntervalMinutes={autonomousResearchIntervalMinutes}
          onUpdateAutonomousResearchInterval={onUpdateAutonomousResearchInterval}
          directionCooldownMinutes={directionCooldownMinutes}
          onUpdateDirectionCooldownMinutes={onUpdateDirectionCooldownMinutes}
          strategicResearchFrequency={strategicResearchFrequency}
          onUpdateStrategicResearchFrequency={onUpdateStrategicResearchFrequency}
          healthCheckUrls={props.healthCheckUrls}
          onUpdateHealthCheckUrls={props.onUpdateHealthCheckUrls}
          githubAccount={githubAccount}
          onConnectGitHub={onConnectGitHub}
          maxPlanAccounts={maxPlanAccounts}
          onToggleAccount={onToggleAccount}
          onRemoveAccount={onRemoveAccount}
          onResetRateLimits={onResetRateLimits}
          onRefreshAccountModels={onRefreshAccountModels}
          onUpdateAccount={onUpdateAccount}
          newAccountName={newAccountName}
          setNewAccountName={setNewAccountName}
          newAccountProvider={newAccountProvider}
          setNewAccountProvider={setNewAccountProvider}
          newAccountAuthMode={newAccountAuthMode}
          setNewAccountAuthMode={setNewAccountAuthMode}
          newAccountToken={newAccountToken}
          setNewAccountToken={setNewAccountToken}
          newAccountProviderMetadata={newAccountProviderMetadata}
          setNewAccountProviderMetadata={setNewAccountProviderMetadata}
          newAccountClaudeConfigJson={newAccountClaudeConfigJson}
          setNewAccountClaudeConfigJson={setNewAccountClaudeConfigJson}
          newAccountDailyTaskCap={newAccountDailyTaskCap}
          setNewAccountDailyTaskCap={setNewAccountDailyTaskCap}
          newAccountMaxConcurrentTasks={newAccountMaxConcurrentTasks}
          setNewAccountMaxConcurrentTasks={setNewAccountMaxConcurrentTasks}
          newAccountAutoDisableAfterRateLimits={newAccountAutoDisableAfterRateLimits}
          setNewAccountAutoDisableAfterRateLimits={setNewAccountAutoDisableAfterRateLimits}
          onAddAccount={onAddAccount}
          onUpdateAccountCaps={onUpdateAccountCaps}
          providerTierModelMap={providerTierModelMap}
          taskTypeModelPreferences={taskTypeModelPreferences}
          providers={providers}
          providerSettingsDefaults={providerSettingsDefaults}
          providerSelectionMode={providerSelectionMode}
          onUpdateProviderSelectionMode={onUpdateProviderSelectionMode}
          taskTypeCategories={taskTypeCategories}
          onUpdateTaskTypeCategories={onUpdateTaskTypeCategories}
          categoryRouting={categoryRouting}
          onUpdateCategoryRouting={onUpdateCategoryRouting}
          taskTypeRouting={taskTypeRouting}
          onUpdateTaskTypeRouting={onUpdateTaskTypeRouting}
          providerSettingsByEffort={providerSettingsByEffort}
          onUpdateProviderSettingsByEffort={onUpdateProviderSettingsByEffort}
          taskTypeProviderOverrides={taskTypeProviderOverrides}
          onUpdateProviderSettingsDefaults={onUpdateProviderSettingsDefaults}
          onUpdateTaskTypeProviderOverrides={onUpdateTaskTypeProviderOverrides}
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
          failedRollback={props.failedRollback}
          zoneHealth={props.zoneHealth}
          userRole={props.userRole}
          userEmail={props.userEmail}
          onSignOut={props.onSignOut}
          showUserManagement={props.showUserManagement}
          onToggleUserManagement={props.onToggleUserManagement}
          showProjectMembers={props.showProjectMembers}
          onToggleProjectMembers={props.onToggleProjectMembers}
          selectedProjectMyRole={props.selectedProjectMyRole}
          notificationConfig={props.notificationConfig}
          onUpdateNotificationConfig={props.onUpdateNotificationConfig}
          dailyCostCeilingUsd={props.dailyCostCeilingUsd}
          onUpdateDailyCostCeiling={props.onUpdateDailyCostCeiling}
          selectedProjectId={props.selectedProjectId}
          selectedProjectOwner={props.selectedProjectOwner}
          selectedProjectRepo={props.selectedProjectRepo}
          selectedProjectIsSelfRepo={props.selectedProjectIsSelfRepo}
          selectedProjectRole={props.selectedProjectRole}
          buildConfig={props.buildConfig}
          onUpdateBuildConfig={props.onUpdateBuildConfig}
          onDetectBuildConfig={props.onDetectBuildConfig}
          autoRemediationConfig={props.autoRemediationConfig}
          autoRemediationStats={props.autoRemediationStats}
          onUpdateAutoRemediationConfig={props.onUpdateAutoRemediationConfig}
          issueImplementationConfig={props.issueImplementationConfig}
          onUpdateIssueImplementationConfig={props.onUpdateIssueImplementationConfig}
          autoReviewConfig={props.autoReviewConfig}
          onUpdateAutoReviewConfig={props.onUpdateAutoReviewConfig}
          resourceEnvelopeData={props.resourceEnvelopeData}
          resourceEnvelopeLoading={props.resourceEnvelopeLoading}
          resourceEnvelopeError={props.resourceEnvelopeError}
          onUpdateResourceEnvelope={props.onUpdateResourceEnvelope}
          integrationBranchData={props.integrationBranchData}
          onUpdateIntegrationBranch={props.onUpdateIntegrationBranch}
          promotionTargetData={props.promotionTargetData}
          onUpdatePromotionTarget={props.onUpdatePromotionTarget}
        />

        {/* More secondary buttons — desktop only */}
        <div className="hidden md:flex items-center gap-1">
          {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */}
          <button
            onClick={onToggleArchive}
            className={`p-2 rounded-md text-xs transition-colors ${
              showArchive ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'
            }`}
            title="Archive"
          >
            <Archive className="w-4 h-4" />
          </button>
          {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */}
          <button
            onClick={onToggleMessages}
            className={`p-2 rounded-md text-xs transition-colors relative flex items-center ${
              showMessages ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'
            }`}
            title="Messages"
          >
            <MessageSquare className="w-4 h-4" />
            {props.openMessageCount > 0 && (
              <span className="ml-0.5 bg-blue-500 text-white text-[8px] font-bold w-4 h-4 rounded-full leading-none flex items-center justify-center">
                {props.openMessageCount > 9 ? '9+' : props.openMessageCount}
              </span>
            )}
            {!showMessages && (props.newSinceLastVisitCount ?? 0) > 0 && (
              <span className="ml-0.5 bg-amber-500 text-black text-[8px] font-bold w-4 h-4 rounded-full leading-none flex items-center justify-center" title={`${props.newSinceLastVisitCount} new since last visit`}>
                {(props.newSinceLastVisitCount ?? 0) > 9 ? '9+' : props.newSinceLastVisitCount}
              </span>
            )}
          </button>
          {onOpenActivityFeed && (
            <>
              {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */}
              <button
                onClick={onOpenActivityFeed}
                className="p-2 rounded-md text-xs transition-colors flex items-center gap-1.5 text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800"
                title="Chronological activity stream"
              >
                <Activity className="w-4 h-4" />
                <span className="hidden xl:inline">Activity</span>
              </button>
            </>
          )}
          {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */}
          <button
            onClick={onToggleResearchBrowser}
            className={`p-2 rounded-md text-xs transition-colors relative flex items-center ${
              showResearchBrowser ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'
            }`}
            title="Research Findings"
          >
            <Microscope className="w-4 h-4" />
            {pendingFindingsCount > 0 && (
              <span className="ml-0.5 bg-cyan-500 text-black text-[8px] font-bold w-4 h-4 rounded-full leading-none flex items-center justify-center">
                {pendingFindingsCount}
              </span>
            )}
          </button>
          {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */}
          <button
            onClick={onToggleKnowledgeBrowser}
            className={`p-2 rounded-md text-xs transition-colors flex items-center ${
              showKnowledgeBrowser ? 'text-violet-400 bg-violet-500/10' : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'
            }`}
            title="Knowledge Library"
          >
            <BookOpen className="w-4 h-4" />
          </button>
        </div>

        {/* Mobile-only autonomy control — full MetricsBar is desktop-only, but autonomy on/off must be reachable on mobile */}
        <div className="md:hidden flex items-center mr-1">
          <AutonomousStatus
            onRefreshTasks={onRefresh}
            wsRef={wsRef}
            pauseReason={props.deployState?.reason ?? null}
          />
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
                onClick={() => { onSetViewMode('home'); setShowMobileMenu(false); }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${viewMode === 'home' ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
              >
                <LayoutDashboard className="w-4 h-4 shrink-0" />
                <span>Dashboard</span>
              </button>
              <button
                onClick={() => { onSetViewMode('kanban'); setShowMobileMenu(false); }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${viewMode === 'kanban' ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
              >
                <LayoutGrid className="w-4 h-4 shrink-0" />
                <span>Kanban view</span>
              </button>
              <button
                onClick={() => { onSetViewMode('pipeline'); setShowMobileMenu(false); }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${viewMode === 'pipeline' ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
              >
                <Network className="w-4 h-4 shrink-0" />
                <span>Pipeline view</span>
              </button>
              <button
                onClick={() => { setViewMode('timeline'); setShowMobileMenu(false); }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${currentViewMode === 'timeline' ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
              >
                <CalendarClock className="w-4 h-4 shrink-0" />
                <span>Timeline view</span>
              </button>
              <button
                onClick={() => { onToggleResearchBrowser(); setShowMobileMenu(false); }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${showResearchBrowser ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
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
                onClick={() => { onToggleKnowledgeBrowser(); setShowMobileMenu(false); }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${showKnowledgeBrowser ? 'text-violet-400 bg-violet-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
              >
                <BookOpen className="w-4 h-4 shrink-0" />
                <span>Knowledge Library</span>
              </button>
              <button
                onClick={() => { onToggleArchive(); setShowMobileMenu(false); }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${showArchive ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
              >
                <Archive className="w-4 h-4 shrink-0" />
                <span>Archive</span>
              </button>
              {/* Pipeline Diagnostics & Activity Feed removed */}
              <button
                onClick={() => { onToggleMessages(); setShowMobileMenu(false); }}
                className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${showMessages ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
              >
                <MessageSquare className="w-4 h-4 shrink-0" />
                <span>Messages</span>
                {props.openMessageCount > 0 && (
                  <span className="ml-auto bg-blue-500 text-white text-[9px] font-bold px-1.5 py-0.5 rounded-full leading-none">
                    {props.openMessageCount > 9 ? '9+' : props.openMessageCount}
                  </span>
                )}
                {!showMessages && (props.newSinceLastVisitCount ?? 0) > 0 && (
                  <span className="ml-auto bg-amber-500 text-black text-[9px] font-bold px-1.5 py-0.5 rounded-full leading-none">
                    {(props.newSinceLastVisitCount ?? 0) > 9 ? '9+' : props.newSinceLastVisitCount} new
                  </span>
                )}
              </button>
              {onOpenActivityFeed && (
                <button
                  onClick={() => { onOpenActivityFeed(); setShowMobileMenu(false); }}
                  className="w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
                >
                  <Activity className="w-4 h-4 shrink-0" />
                  <span>Activity stream</span>
                </button>
              )}
              <button
                onClick={() => { onShowDirections(); setShowMobileMenu(false); }}
                className="w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
              >
                <Compass className="w-4 h-4 shrink-0" />
                <span>Directions</span>
              </button>
              <button
                onClick={() => setShowMobileShortcuts(prev => !prev)}
                className="w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
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
                      ['3', 'Timeline view'],
                      ['D', 'Toggle directions'],
                      ['S', 'Toggle settings'],
                      ['M', 'Toggle metrics'],
                      ['A', 'Toggle activity feed'],
                      ['?', 'This help'],
                      ['Esc', 'Close panel/modal'],
                      ['Del', 'Cancel selected (selection mode)'],
                      ['R*', 'Retry selected (selection mode)'],
                      ['Enter', 'Open selected task (selection mode)'],
                      ['Esc*', 'Clear selection / exit mode'],
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
                className="w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
              >
                <Settings className="w-4 h-4 shrink-0" />
                <span>Settings</span>
              </button>
            </div>
          )}
        </div>

        {/* New Task — always visible (primary action) */}
        <button
          onClick={onOpenNewTaskModal}
          className="bg-white hover:bg-neutral-200 text-neutral-950 px-4 py-2 min-h-[44px] md:min-h-0 rounded text-xs font-semibold transition-colors shadow-sm flex items-center gap-2 h-fit"
        >
          <Plus className="w-3.5 h-3.5" />
          Launch Task
        </button>
      </div>
    </header>
  );
}
