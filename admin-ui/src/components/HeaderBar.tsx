import type { RefObject } from 'react';
import { useMemo, useState, useRef, useEffect } from 'react';
import { Activity, Command, Compass, Microscope, BookOpen, MoreHorizontal, Settings, Bell, Plus } from 'lucide-react';
import { AutonomousStatus } from './AutonomousStatus';
import { SettingsDropdown, type MaxPlanAccount, type SectionId } from './SettingsDropdown';
import { OrchestratorHealthBar } from './OrchestratorHealthBar';
import type { DetailedHealth, PipelineThrottle } from './OrchestratorHealthBar';
import { ProjectSwitcherDropdown } from './ProjectSwitcherDropdown';
import type { AccountMutationReturn, AccountUpdatePayload, AuthMode, CategoryRoutingEntry, DeployState, FailedRollbackInfo, ModelPreference, OrchestratorHealth, ProviderCatalogEntry, ProviderName, ProviderSettingsByEffort, ProviderSettingsDefaults, TaskTypeProviderOverrides, TaskTypeRoutingEntry } from '../types/settings';
import type { Task } from '../types/task';
import type { BuildConfig, AutoRemediationConfig, AutoRemediationStats, IssueImplementationConfig, IssueImplementationConfigUpdate, AutoReviewConfig, ProjectResourceEnvelopeResponse, ResourceEnvelopeConfig, Project } from '../types/project';
import type { UserLaunchableTaskType } from '../taskCapabilities';
import type { ViewMode } from '../hooks/useViewPreferences';
import type { PrimaryTab } from '../hooks/useUIState';

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
  onOpenActivityFeed?: () => void;
  openMessageCount: number;
  newSinceLastVisitCount?: number;
  needsAttentionCount?: number;
  alertCount?: number;
  alertMaxSeverity?: 'ERROR' | 'WARNING' | 'INFO' | null;
  showAlertsPopover?: boolean;
  onToggleAlertsPopover?: () => void;
  onOpenNewTaskModal: () => void;
  onSignOut: () => void;
  onNewProject?: () => void;
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
  // Unified top bar — ProjectSwitcher
  projects: Project[];
  onSelectProject: (id: string | null) => void;
  // Unified top bar — tab nav
  activeTab: PrimaryTab;
  onTabChange: (tab: PrimaryTab) => void;
  onMessageStrategic?: () => void;
  onOpenCommandPalette?: () => void;
  // Unified top bar — HealthPill
  health: OrchestratorHealth | null;
  detailedHealth: DetailedHealth | null;
  pipelineThrottle?: PipelineThrottle | null;
  /** Opens the ProjectManagementModal (archive/delete/unarchive + new-project). */
  onManageProjects?: () => void;
}


export function HeaderBar(props: HeaderBarProps) {
  const {
    tasks,
    maxConcurrentVMs,
    onRefresh,
    viewMode,
    onSetViewMode,
    showResearchBrowser,
    onToggleResearchBrowser,
    showKnowledgeBrowser,
    onToggleKnowledgeBrowser,
    onOpenActivityFeed,
    onOpenNewTaskModal,
    openMessageCount,
    newSinceLastVisitCount,
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
  const [healthExpanded, setHealthExpanded] = useState(false);
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

  const healthDotColor = useMemo(() => {
    if (!apiHealthy) return 'bg-red-500 animate-pulse';
    if (!props.health) return 'bg-neutral-500';
    if (props.health.orchestrator_hung) return 'bg-red-500 animate-pulse';
    const cycleAge = props.health.last_cycle_seconds_ago;
    if (cycleAge !== null && cycleAge >= 120) return 'bg-red-500 animate-pulse';
    if (cycleAge !== null && cycleAge >= 60) return 'bg-amber-400';
    return 'bg-emerald-500';
  }, [apiHealthy, props.health]);

  return (
    <div className="shrink-0">
      {/* Health expanded panel — shows full OrchestratorHealthBar below the top bar on click */}
      {healthExpanded && (
        <div className="mb-1">
          <OrchestratorHealthBar
            health={props.health}
            detailedHealth={props.detailedHealth}
            apiHealthy={apiHealthy}
            wsConnected={wsConnected}
            lastFetchTime={lastFetchTime}
            tasks={tasks}
            pipelineThrottle={props.pipelineThrottle}
          />
        </div>
      )}

      {/* Unified top bar: compact global shell with fixed utility clusters and a scrolling tab strip. */}
      <header className="flex min-h-10 flex-wrap items-center gap-2 border-b border-neutral-800 bg-[#05070a] px-2 py-1.5 md:flex-nowrap">
        {/* LEFT: ProjectSwitcher + Manage projects slot */}
        <div className="flex min-w-0 shrink-0 items-center gap-1.5">
          <div className="hidden items-center gap-2 pr-1 lg:flex">
            <span className="flex h-7 w-7 items-center justify-center rounded-md border border-neutral-800 bg-[#0b0f14] text-neutral-300 shadow-inner shadow-black/20">
              <Compass className="h-3.5 w-3.5" />
            </span>
            <span className="text-sm font-semibold tracking-normal text-neutral-100">BlueprintAIStudio</span>
          </div>
          <ProjectSwitcherDropdown
            projects={props.projects}
            selectedProjectId={props.selectedProjectId}
            onSelectProject={props.onSelectProject}
          />
          {props.onManageProjects && (
            /* eslint-disable-next-line viewport/min-touch-target */
            <button
              onClick={props.onManageProjects}
              className="hidden rounded px-2 py-1 text-xs text-neutral-600 transition-colors hover:bg-neutral-900 hover:text-neutral-300 sm:inline-flex"
              title="Manage projects"
            >
              Manage
            </button>
          )}
        </div>

        <button
          type="button"
          onClick={() => {
            if (props.onMessageStrategic) props.onMessageStrategic();
            else props.onTabChange('chat');
          }}
          className="hidden min-w-0 flex-1 items-center rounded-md border border-neutral-800 bg-[#0b0f14] px-3 py-1.5 text-left font-mono text-xs text-neutral-500 transition-colors hover:border-neutral-700 hover:bg-neutral-900 hover:text-neutral-300 lg:flex"
        >
          <span className="truncate">Message Strategic...</span>
        </button>

        {/* CENTER: Primary tab group — intentional horizontal-scroll strip
            (overflow-x-auto + min-w-0) so the tabs scroll rather than wrap or
            push the bar wide on narrow viewports; opt the scroll container out
            of the overflow/truncation invariants (its descendants legitimately
            extend past the fold inside the scroll region). */}
        <nav
          data-viewport-allow-overflow
          data-viewport-allow-truncation
          className="order-3 flex min-w-0 flex-1 basis-full items-center gap-0.5 overflow-x-auto rounded-md border border-neutral-800 bg-[#0b0f14] p-0.5 sm:order-none sm:basis-auto md:flex-none"
        >
          {(['chat', 'pipeline', 'project', 'runtime'] as const).map(tab => {
            const isActive = props.activeTab === tab;
            const chatBadgeCount = tab === 'chat'
              ? Math.max(openMessageCount ?? 0, newSinceLastVisitCount ?? 0)
              : 0;
            const pipelineBadgeCount = tab === 'pipeline' ? props.needsAttentionCount ?? 0 : 0;
            const badgeCount = Math.max(chatBadgeCount, pipelineBadgeCount);
            const label = tab === 'chat' ? 'Strategic' : tab.charAt(0).toUpperCase() + tab.slice(1);
            return (
              <button
                key={tab}
                onClick={() => props.onTabChange(tab)}
                className={`inline-flex min-h-[44px] shrink-0 items-center rounded px-2.5 py-1 text-sm font-semibold transition-colors whitespace-nowrap md:min-h-0 ${
                  isActive
                    ? 'bg-neutral-800 text-neutral-100 shadow-[0_1px_0_rgba(255,255,255,0.06)]'
                    : 'text-neutral-300 hover:bg-neutral-900 hover:text-neutral-100'
                }`}
              >
                {label}
                {badgeCount > 0 && (
                  <span className="ml-1.5 inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-amber-500 px-1 text-ui-sm font-bold leading-none text-black">
                    {badgeCount > 99 ? '99+' : badgeCount}
                  </span>
                )}
              </button>
            );
          })}
        </nav>

        {/* RIGHT: Icon cluster + SettingsDropdown + mobile controls + Launch Task */}
        <div className="ml-auto flex shrink-0 items-center gap-1">

          {/* Desktop icon cluster */}
          <div className="hidden items-center gap-0.5 rounded-md border border-neutral-800 bg-[#0b0f14] p-0.5 md:flex">
            {props.onOpenCommandPalette && (
              /* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */
              <button
                type="button"
                onClick={props.onOpenCommandPalette}
                title="Open command palette (Ctrl+K)"
                aria-label="Open command palette"
                className="flex h-8 items-center gap-1 rounded px-2 text-xs font-semibold text-neutral-400 transition-colors hover:bg-neutral-900 hover:text-neutral-100"
              >
                <Command className="w-3.5 h-3.5" />
                <kbd className="hidden rounded border border-neutral-700 bg-neutral-950 px-1 py-0.5 font-mono text-ui-sm text-neutral-500 xl:inline-block">
                  Ctrl+K
                </kbd>
              </button>
            )}
            {/* Alerts — only when active alerts exist */}
            {(props.alertCount ?? 0) > 0 && (
              /* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */
              <button
                onClick={props.onToggleAlertsPopover}
                title={`${props.alertCount} alert${(props.alertCount ?? 0) > 1 ? 's' : ''}`}
                className={`flex h-8 items-center gap-1 rounded px-2 text-xs transition-colors ${
                  props.showAlertsPopover
                    ? (props.alertMaxSeverity === 'ERROR' ? 'text-red-400 bg-red-500/10'
                      : props.alertMaxSeverity === 'WARNING' ? 'text-amber-400 bg-amber-500/10'
                      : 'text-blue-400 bg-blue-500/10')
                    : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'
                }`}
              >
                <span className={`w-2 h-2 rounded-full ${
                  props.alertMaxSeverity === 'ERROR' ? 'bg-red-500' :
                  props.alertMaxSeverity === 'WARNING' ? 'bg-amber-500' : 'bg-blue-500'
                }`} />
                <Bell className="w-3.5 h-3.5" />
                <span>{props.alertCount}</span>
              </button>
            )}
            <button
              onClick={onToggleResearchBrowser}
              title="Research findings"
              aria-pressed={showResearchBrowser}
              className={`relative flex h-8 w-8 items-center justify-center rounded text-xs transition-colors ${
                showResearchBrowser ? 'bg-cyan-500/10 text-cyan-300' : 'text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200'
              }`}
            >
              <Microscope className="w-4 h-4" />
              {pendingFindingsCount > 0 && (
                <span className="absolute -right-0.5 -top-0.5 flex h-3.5 min-w-3.5 items-center justify-center rounded-full bg-cyan-400 px-0.5 text-ui-2xs font-bold leading-none text-neutral-950">
                  {pendingFindingsCount > 9 ? '9+' : pendingFindingsCount}
                </span>
              )}
            </button>
            <button
              onClick={onToggleKnowledgeBrowser}
              title="Knowledge library"
              aria-pressed={showKnowledgeBrowser}
              className={`flex h-8 w-8 items-center justify-center rounded text-xs transition-colors ${
                showKnowledgeBrowser ? 'bg-violet-500/10 text-violet-300' : 'text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200'
              }`}
            >
              <BookOpen className="w-4 h-4" />
            </button>
            {props.activeTab !== 'pipeline' && (
              /* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */
              <button
                onClick={onOpenNewTaskModal}
                title="New task"
                className="flex h-8 items-center gap-1 rounded px-2 text-xs font-semibold text-neutral-300 transition-colors hover:bg-neutral-900 hover:text-neutral-100"
              >
                <Plus className="w-3.5 h-3.5" />
                <span>New</span>
              </button>
            )}
            {/* Activity */}
            {onOpenActivityFeed && (
              <button
                onClick={onOpenActivityFeed}
                title="Chronological activity stream"
                className="flex h-8 w-8 items-center justify-center rounded text-xs text-neutral-400 transition-colors hover:bg-neutral-900 hover:text-neutral-200"
              >
                <Activity className="w-4 h-4" />
              </button>
            )}
            {/* Health pill — expands/collapses the full OrchestratorHealthBar */}
            {/* eslint-disable-next-line viewport/min-touch-target -- desktop-only (inside hidden md:flex); never visible at 375px mobile width */}
            <button
              onClick={() => setHealthExpanded(prev => !prev)}
              title={healthExpanded ? 'Hide health panel' : 'Show orchestrator health'}
              className={`flex h-8 items-center gap-1 rounded px-2 text-xs transition-colors ${healthExpanded ? 'text-neutral-200 bg-neutral-800' : 'text-neutral-400 hover:bg-neutral-900 hover:text-neutral-200'}`}
            >
              <span className={`w-2 h-2 rounded-full shrink-0 ${healthDotColor}`} />
              <span className="hidden font-mono xl:inline">health</span>
            </button>
          </div>

          {/* SettingsDropdown — always in DOM so its fixed overlay renders when triggered from mobile menu.
              The trigger button is hidden on mobile via triggerClassName; the fixed panel bypasses parent display:none. */}
          <SettingsDropdown
            triggerClassName="hidden md:inline-flex h-9 w-9 items-center justify-center rounded-md border border-neutral-800 bg-[#0b0f14] hover:bg-neutral-900"
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

          {/* Mobile-only autonomy control */}
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
              className="flex min-h-[44px] min-w-[44px] items-center justify-center rounded-md border border-neutral-800 bg-[#0b0f14] p-3 text-xs text-neutral-300 transition-colors hover:bg-neutral-900"
              title="More options"
            >
              <MoreHorizontal className="w-4 h-4" />
            </button>
            {showMobileMenu && (
              <div className="absolute right-0 top-full z-50 mt-2 max-h-[calc(100vh-96px)] w-60 overflow-auto rounded-md border border-neutral-800 bg-[#0b0f14] p-2 shadow-2xl">
                {props.onOpenCommandPalette && (
                  <button
                    onClick={() => { props.onOpenCommandPalette?.(); setShowMobileMenu(false); }}
                    className="w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
                  >
                    <Command className="w-4 h-4 shrink-0" />
                    <span>Command palette</span>
                    <kbd className="ml-auto rounded border border-neutral-700 bg-neutral-900 px-1.5 py-0.5 font-mono text-ui-sm text-neutral-500">
                      Ctrl+K
                    </kbd>
                  </button>
                )}
                <button
                  onClick={() => { onToggleResearchBrowser(); setShowMobileMenu(false); }}
                  className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${showResearchBrowser ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-300 hover:bg-neutral-800'}`}
                >
                  <Microscope className="w-4 h-4 shrink-0" />
                  <span>Research Browser</span>
                  {pendingFindingsCount > 0 && (
                    <span className="ml-auto bg-cyan-500 text-black text-ui-xs font-bold px-1.5 py-0.5 rounded-full leading-none">
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
                  onClick={() => { onOpenNewTaskModal(); setShowMobileMenu(false); }}
                  className="w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left text-neutral-300 hover:bg-neutral-800"
                >
                  <span className="w-4 h-4 flex items-center justify-center font-mono shrink-0">N</span>
                  <span>New task</span>
                </button>
                {(['chat', 'pipeline', 'project', 'runtime'] as const).map(tab => (
                  <button
                    key={tab}
                    onClick={() => { props.onTabChange(tab); setShowMobileMenu(false); }}
                    className={`w-full flex items-center gap-3 px-3 py-2.5 min-h-[44px] rounded text-sm transition-colors text-left ${
                      props.activeTab === tab ? 'bg-neutral-800 text-neutral-100' : 'text-neutral-300 hover:bg-neutral-800'
                    }`}
                  >
                    <Compass className="w-4 h-4 shrink-0" />
                    <span>{tab === 'chat' ? 'Strategic' : tab.charAt(0).toUpperCase() + tab.slice(1)}</span>
                    {tab === 'chat' && (openMessageCount > 0 || (newSinceLastVisitCount ?? 0) > 0) && (
                      <span className="ml-auto bg-amber-500 text-black text-ui-xs font-bold px-1.5 py-0.5 rounded-full leading-none">
                        {Math.max(openMessageCount, newSinceLastVisitCount ?? 0)}
                      </span>
                    )}
                  </button>
                ))}
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
                        ['Ctrl+K', 'Command palette'],
                        ['N', 'New task'],
                        ['R', 'Refresh'],
                        ['1', 'Strategic chat'],
                        ['2', 'Pipeline'],
                        ['3', 'Project update'],
                        ['4', 'Runtime'],
                        ['S', 'Settings'],
                        ['A', 'Archive (pipeline)'],
                        ['Esc', 'Close panel/modal'],
                        ['Del', 'Cancel selected (selection mode)'],
                        ['R*', 'Retry selected (selection mode)'],
                        ['Enter', 'Open selected task (selection mode)'],
                        ['Esc*', 'Clear selection / exit mode'],
                      ].map(([key, desc]) => (
                        <div key={key} className="flex items-center justify-between">
                          <span className="text-neutral-400">{desc}</span>
                          <kbd className="bg-neutral-800 border border-neutral-700 text-neutral-300 px-1.5 py-0.5 rounded text-ui-sm font-mono min-w-[24px] text-center">{key}</kbd>
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

        </div>
      </header>
    </div>
  );
}
