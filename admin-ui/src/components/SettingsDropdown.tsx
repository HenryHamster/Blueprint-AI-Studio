import { useEffect, useState } from 'react';
import { SettingsAdvancedSection } from './SettingsAdvancedSection';
import { SettingsGeneralSection } from './SettingsGeneralSection';
import { SettingsOrchestratorSection } from './SettingsOrchestratorSection';
import { SettingsNotificationsSection } from './SettingsNotificationsSection';
import type { NotificationConfig } from './SettingsNotificationsSection';
import { ProviderSettingsPanel } from './ProviderSettingsPanel';
import { ProviderCategoriesPanel } from './ProviderCategoriesPanel';
import { ProjectBuildConfigSection } from './ProjectBuildConfigSection';
import { ProjectAutoRemediationSection } from './ProjectAutoRemediationSection';
import { ProjectIssueImplementationSection } from './ProjectIssueImplementationSection';
import { ProjectAutoReviewSection } from './ProjectAutoReviewSection';
import { ProjectIntegrationBranchSection } from './ProjectIntegrationBranchSection';
import { ProjectPromotionTargetSection } from './ProjectPromotionTargetSection';
import { SettingsAccountsSection } from './SettingsAccountsSection';
import type { BuildConfig, AutoRemediationConfig, AutoRemediationStats, IssueImplementationConfig, IssueImplementationConfigUpdate, AutoReviewConfig } from '../types/project';
import { Settings, GitBranch, Shield, X, Sliders, ListChecks, Server, User, LogOut, Bell, Wrench, Tag, HelpCircle, FileDown } from 'lucide-react';
import type { UserLaunchableTaskType } from '../taskCapabilities';
import type { ViewMode } from '../hooks/useViewPreferences';
export type { MaxPlanAccount } from '../types/settings';
import type { AuthMode, CategoryRoutingEntry, DeployState, MaxPlanAccount, ModelPreference, ProviderCatalogEntry, ProviderName, ProviderSettingsByEffort, ProviderSettingsDefaults, TaskTypeProviderOverrides, TaskTypeRoutingEntry } from '../types/settings';
import { DeploySection } from './SettingsDeploySection';


interface SettingsDropdownProps {
  showSettings: boolean;
  onToggleSettings: () => void;
  settingsRef: React.RefObject<HTMLDivElement | null>;
  // When the panel is opened via a deep-link CTA, jump straight to this section
  // instead of the default General tab.
  initialSection?: SectionId | null;
  // Defaults
  defaultTaskType: UserLaunchableTaskType;
  onUpdateDefaultTaskType: (value: UserLaunchableTaskType) => void;
  viewMode: ViewMode;
  onSetViewMode: (mode: ViewMode) => void;
  // Notifications
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
  // Existing
  maxConcurrentVMs: number;
  onUpdateMaxVMs: (value: number) => void;
  maxRetries: number;
  onUpdateMaxRetries: (value: number) => void;
  requireApprovalGates: boolean;
  onUpdateRequireApprovalGates: (value: boolean) => void;
  autoArchiveEnabled: boolean;
  onUpdateAutoArchiveEnabled: (value: boolean) => void;
  autoArchiveTerminalAfterDays: number;
  onUpdateAutoArchiveTerminalAfterDays: (value: number) => void;
  autoArchiveMaxTerminalTasksPerProject: number;
  onUpdateAutoArchiveMaxTerminalTasksPerProject: (value: number) => void;
  autoArchiveIntervalMinutes: number;
  onUpdateAutoArchiveIntervalMinutes: (value: number) => void;
  maxAutonomousTasksPerHour: number;
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
  onRemoveAccount: (id: string) => void;
  onResetRateLimits: (id: string) => void;
  onRefreshAccountModels: (id: string) => void;
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
  // Provider routing (mode + categories + per-task overrides + effort matrix)
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
  // Zone health
  zoneHealth: Record<string, { status: string; last_success: string | null; last_failure: string | null; last_failure_reason: string | null; consecutive_failures: number; cooldown_active: boolean }> | null;
  // Notification config
  notificationConfig: NotificationConfig;
  onUpdateNotificationConfig: (config: Partial<NotificationConfig>) => void;
  dailyCostCeilingUsd: number;
  onUpdateDailyCostCeiling: (value: number) => void;
  // Optional class name for the trigger button (e.g. to hide it on mobile)
  triggerClassName?: string;
  userRole?: 'admin' | 'user' | null;
  // User / session
  userEmail: string;
  onSignOut: () => void;
  showUserManagement: boolean;
  onToggleUserManagement: () => void;
  showProjectMembers: boolean;
  onToggleProjectMembers: () => void;
  selectedProjectMyRole: string | null;
  // Build config (project-scoped)
  selectedProjectId: string | null;
  selectedProjectOwner: string | null;
  selectedProjectRepo: string | null;
  selectedProjectIsSelfRepo?: boolean;
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
  // Auto-review config (project-scoped)
  selectedProjectRole?: 'owner' | 'member' | 'viewer' | null;
  autoReviewConfig: AutoReviewConfig | null;
  onUpdateAutoReviewConfig: (patch: Partial<AutoReviewConfig>) => Promise<void>;
  // Integration branch config (project-scoped)
  integrationBranchData: { integration_branch: string; is_default: boolean } | null;
  onUpdateIntegrationBranch: (branch: string | null) => Promise<void>;
  // Promotion target config (project-scoped)
  promotionTargetData: { promotion_target: string; is_default: boolean } | null;
  onUpdatePromotionTarget: (branch: string | null) => Promise<void>;
}



export type SectionId = 'general' | 'provider-routing' | 'provider-settings' | 'orchestrator' | 'deploy' | 'accounts' | 'notifications' | 'advanced' | 'build-config' | 'auto-remediation' | 'issue-implementation' | 'auto-review' | 'integration-branch' | 'promotion-target';

const SECTIONS: { id: SectionId; label: string; icon: typeof Settings; adminOnly?: boolean; projectOnly?: boolean }[] = [
  { id: 'general', label: 'General', icon: Sliders },
  { id: 'provider-routing', label: 'Provider Routing', icon: GitBranch, adminOnly: true },
  { id: 'provider-settings', label: 'Provider Settings', icon: ListChecks, adminOnly: true },
  { id: 'orchestrator', label: 'Orchestrator', icon: Server, adminOnly: true },
  { id: 'deploy', label: 'Deploy & Health', icon: Shield, adminOnly: true },
  { id: 'accounts', label: 'Accounts', icon: User },
  { id: 'notifications', label: 'Notifications', icon: Bell },
  { id: 'build-config', label: 'Build Config', icon: Wrench, projectOnly: true },
  { id: 'auto-review', label: 'Auto-Review', icon: HelpCircle, projectOnly: true },
  { id: 'auto-remediation', label: 'Auto-Remediation', icon: Settings, projectOnly: true },
  { id: 'issue-implementation', label: 'Issue Implementation', icon: Tag, projectOnly: true },
  { id: 'integration-branch', label: 'Integration Branch', icon: GitBranch, projectOnly: true },
  { id: 'promotion-target', label: 'Promotion Target', icon: GitBranch, projectOnly: true },
  { id: 'advanced', label: 'Advanced', icon: FileDown },
];

export function SettingsDropdown({
  showSettings,
  onToggleSettings,
  settingsRef,
  initialSection,
  defaultTaskType,
  onUpdateDefaultTaskType,
  viewMode,
  onSetViewMode,
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
  maxConcurrentVMs,
  onUpdateMaxVMs,
  maxRetries,
  onUpdateMaxRetries,
  requireApprovalGates,
  onUpdateRequireApprovalGates,
  autoArchiveEnabled,
  onUpdateAutoArchiveEnabled,
  autoArchiveTerminalAfterDays,
  onUpdateAutoArchiveTerminalAfterDays,
  autoArchiveMaxTerminalTasksPerProject,
  onUpdateAutoArchiveMaxTerminalTasksPerProject,
  autoArchiveIntervalMinutes,
  onUpdateAutoArchiveIntervalMinutes,
  maxAutonomousTasksPerHour,
  onUpdateMaxAutonomousRate,
  autonomousResearchIntervalMinutes,
  onUpdateAutonomousResearchInterval,
  directionCooldownMinutes,
  onUpdateDirectionCooldownMinutes,
  strategicResearchFrequency,
  onUpdateStrategicResearchFrequency,
  healthCheckUrls,
  onUpdateHealthCheckUrls,
  githubAccount,
  onConnectGitHub,
  maxPlanAccounts,
  onToggleAccount,
  onRemoveAccount,
  onResetRateLimits,
  onRefreshAccountModels,
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
  taskTypeProviderOverrides,
  onUpdateProviderSettingsDefaults,
  onUpdateTaskTypeProviderOverrides,
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
  deployState,
  onPausePipeline,
  onResumePipeline,
  onResetHealth,
  onRollback,
  onMarkStable,
  onRepairState,
  zoneHealth,
  notificationConfig,
  onUpdateNotificationConfig,
  dailyCostCeilingUsd,
  onUpdateDailyCostCeiling,
  triggerClassName,
  userRole,
  userEmail,
  onSignOut,
  showUserManagement,
  onToggleUserManagement,
  showProjectMembers,
  onToggleProjectMembers,
  selectedProjectMyRole,
  selectedProjectId,
  selectedProjectOwner,
  selectedProjectRepo,
  selectedProjectIsSelfRepo,
  buildConfig,
  onUpdateBuildConfig,
  onDetectBuildConfig,
  autoRemediationConfig,
  autoRemediationStats,
  onUpdateAutoRemediationConfig,
  issueImplementationConfig,
  onUpdateIssueImplementationConfig,
  selectedProjectRole,
  autoReviewConfig,
  onUpdateAutoReviewConfig,
  integrationBranchData,
  onUpdateIntegrationBranch,
  promotionTargetData,
  onUpdatePromotionTarget,
}: SettingsDropdownProps) {
  const [, setTick] = useState(0);
  const [activeSection, setActiveSection] = useState<SectionId>('general');

  const [healthCheckUrlsInput, setHealthCheckUrlsInput] = useState(() => healthCheckUrls.join('\n'));

  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => { setHealthCheckUrlsInput(healthCheckUrls.join('\n')); }, [healthCheckUrls]);

  // Jump to a requested section when the panel is opened via a deep-link CTA.
  useEffect(() => {
    if (showSettings && initialSection) setActiveSection(initialSection);
  }, [showSettings, initialSection]);

  // Close on Escape
  useEffect(() => {
    if (!showSettings) return;
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onToggleSettings();
    };
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [showSettings, onToggleSettings]);

  const isAdmin = userRole === 'admin';
  const visibleSections = SECTIONS.filter(s =>
    (!s.adminOnly || isAdmin) && (!s.projectOnly || selectedProjectId !== null)
  );
  const allModelOptions = Array.from(new Set([
    ...Object.values(providerTierModelMap).flatMap(providerMap => Object.values(providerMap || {})),
    ...Object.values(taskTypeModelPreferences).flatMap(prefs => prefs.map(pref => pref.model)),
  ])).filter(Boolean).sort();

  // --- Section renderers ---

  function renderGeneral() {
    return (
      <SettingsGeneralSection
        apiHealthy={apiHealthy}
        wsConnected={wsConnected}
        lastFetchTime={lastFetchTime}
        defaultTaskType={defaultTaskType}
        onUpdateDefaultTaskType={onUpdateDefaultTaskType}
        viewMode={viewMode}
        onSetViewMode={onSetViewMode}
        allModelOptions={allModelOptions}
        refreshInterval={refreshInterval}
        onUpdateRefreshInterval={onUpdateRefreshInterval}
        pageSize={pageSize}
        onUpdatePageSize={onUpdatePageSize}
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
        onNavigateToNotifications={() => setActiveSection('notifications')}
      />
    );
  }
  function renderOrchestrator() {
    return (
      <SettingsOrchestratorSection
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
        autonomousResearchIntervalMinutes={autonomousResearchIntervalMinutes}
        onUpdateAutonomousResearchInterval={onUpdateAutonomousResearchInterval}
        directionCooldownMinutes={directionCooldownMinutes}
        onUpdateDirectionCooldownMinutes={onUpdateDirectionCooldownMinutes}
        strategicResearchFrequency={strategicResearchFrequency}
        onUpdateStrategicResearchFrequency={onUpdateStrategicResearchFrequency}
        anyAutonomousEnabled={anyAutonomousEnabled}
        onPauseAllAutonomous={onPauseAllAutonomous}
        onRestartOrchestrator={onRestartOrchestrator}
      />
    );
  }

  function renderDeploy() {
    return (
      <DeploySection
        deployState={deployState}
        onPausePipeline={onPausePipeline}
        onResumePipeline={onResumePipeline}
        onResetHealth={onResetHealth}
        onRollback={onRollback}
        onMarkStable={onMarkStable}
        onRepairState={onRepairState}
        zoneHealth={zoneHealth}
      />
    );
  }

  function renderAccounts() {
    return (
      <SettingsAccountsSection
        githubAccount={githubAccount}
        onConnectGitHub={onConnectGitHub}
        isAdmin={isAdmin}
        maxPlanAccounts={maxPlanAccounts}
        onToggleAccount={onToggleAccount}
        onRemoveAccount={onRemoveAccount}
        onResetRateLimits={onResetRateLimits}
        onRefreshAccountModels={onRefreshAccountModels}
        onUpdateAccountCaps={onUpdateAccountCaps}
        showUserManagement={showUserManagement}
        onToggleUserManagement={onToggleUserManagement}
        showProjectMembers={showProjectMembers}
        onToggleProjectMembers={onToggleProjectMembers}
        selectedProjectId={selectedProjectId}
        selectedProjectMyRole={selectedProjectMyRole}
        onToggleSettings={onToggleSettings}
        providers={providers}
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
      />
    );
  }

  function renderNotifications() {
    return (
      <SettingsNotificationsSection
        webhookUrl={notificationConfig.webhook_url}
        onUpdateWebhookUrl={url => onUpdateNotificationConfig({ webhook_url: url })}
        enabledEvents={notificationConfig.enabled_events}
        onUpdateEnabledEvents={events => onUpdateNotificationConfig({ enabled_events: events })}
        throttleSeconds={notificationConfig.throttle_seconds}
        onUpdateThrottleSeconds={secs => onUpdateNotificationConfig({ throttle_seconds: secs })}
        dailyCostCeilingUsd={dailyCostCeilingUsd}
        onUpdateDailyCostCeiling={onUpdateDailyCostCeiling}
        isAdmin={isAdmin}
      />
    );
  }

  function renderAdvanced() {
    return (
      <SettingsAdvancedSection
        healthCheckUrlsInput={healthCheckUrlsInput}
        onHealthCheckUrlsChange={setHealthCheckUrlsInput}
        onUpdateHealthCheckUrls={onUpdateHealthCheckUrls}
      />
    );
  }

  function renderBuildConfig() {
    return (
      <ProjectBuildConfigSection
        githubOwner={selectedProjectOwner}
        githubRepo={selectedProjectRepo}
        isSelfRepo={selectedProjectIsSelfRepo}
        buildConfig={buildConfig}
        onSave={onUpdateBuildConfig}
        onDetect={onDetectBuildConfig}
        detectAvailable={Boolean(selectedProjectOwner && selectedProjectRepo)}
        projectId={selectedProjectId}
      />
    );
  }

  function renderAutoRemediation() {
    return (
      <ProjectAutoRemediationSection
        githubOwner={selectedProjectOwner}
        githubRepo={selectedProjectRepo}
        config={autoRemediationConfig}
        stats={autoRemediationStats}
        onSave={onUpdateAutoRemediationConfig}
        integrationBranch={integrationBranchData?.integration_branch}
        promotionTarget={promotionTargetData?.promotion_target}
      />
    );
  }

  function renderAutoReview() {
    return (
      <ProjectAutoReviewSection
        config={autoReviewConfig}
        canSave={selectedProjectRole === 'owner'}
        onSave={onUpdateAutoReviewConfig}
      />
    );
  }

  function renderIntegrationBranch() {
    return (
      <ProjectIntegrationBranchSection
        data={integrationBranchData}
        canSave={selectedProjectRole === 'owner'}
        onSave={onUpdateIntegrationBranch}
        relatedPromotionTarget={promotionTargetData?.promotion_target}
      />
    );
  }

  function renderPromotionTarget() {
    return (
      <ProjectPromotionTargetSection
        data={promotionTargetData}
        canSave={selectedProjectRole === 'owner'}
        onSave={onUpdatePromotionTarget}
        relatedIntegrationBranch={integrationBranchData?.integration_branch}
      />
    );
  }

  function renderIssueImplementation() {
    return (
      <ProjectIssueImplementationSection
        githubOwner={selectedProjectOwner}
        githubRepo={selectedProjectRepo}
        config={issueImplementationConfig}
        onSave={onUpdateIssueImplementationConfig}
      />
    );
  }

  function renderProviderSettings() {
    return (
      <ProviderSettingsPanel
        providers={providers}
        providerSettingsDefaults={providerSettingsDefaults}
        taskTypeProviderOverrides={taskTypeProviderOverrides}
        providerSettingsByEffort={providerSettingsByEffort}
        maxPlanAccounts={maxPlanAccounts}
        onUpdateDefaults={onUpdateProviderSettingsDefaults}
        onUpdateOverrides={onUpdateTaskTypeProviderOverrides}
        onUpdateProviderSettingsByEffort={onUpdateProviderSettingsByEffort}
      />
    );
  }

  function renderProviderRouting() {
    return (
      <ProviderCategoriesPanel
        providers={providers}
        selectionMode={providerSelectionMode}
        onUpdateSelectionMode={onUpdateProviderSelectionMode}
        taskTypeCategories={taskTypeCategories}
        onUpdateTaskTypeCategories={onUpdateTaskTypeCategories}
        categoryRouting={categoryRouting}
        onUpdateCategoryRouting={onUpdateCategoryRouting}
        taskTypeRouting={taskTypeRouting}
        onUpdateTaskTypeRouting={onUpdateTaskTypeRouting}
      />
    );
  }

  function renderActiveSection() {
    switch (activeSection) {
      case 'general': return renderGeneral();
      case 'provider-routing': return renderProviderRouting();
      case 'provider-settings': return renderProviderSettings();
      case 'orchestrator': return renderOrchestrator();
      case 'deploy': return renderDeploy();
      case 'accounts': return renderAccounts();
      case 'notifications': return renderNotifications();
      case 'build-config': return renderBuildConfig();
      case 'auto-review': return renderAutoReview();
      case 'auto-remediation': return renderAutoRemediation();
      case 'issue-implementation': return renderIssueImplementation();
      case 'integration-branch': return renderIntegrationBranch();
      case 'promotion-target': return renderPromotionTarget();
      case 'advanced': return renderAdvanced();
    }
  }

  return (
    <div className="relative" ref={settingsRef}>
      {/* reason: dense-UI icon trigger (p-2 around 16px icon) — operator-density convention for settings panels */}
      <button
        data-viewport-allow-small-target
        onClick={onToggleSettings}
        className={`bg-neutral-800 hover:bg-neutral-700 text-neutral-300 p-2 rounded text-xs transition-colors border border-neutral-700${triggerClassName ? ` ${triggerClassName}` : ''}`}
        title="Settings"
      >
        <Settings className="w-4 h-4" />
      </button>
      {showSettings && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onToggleSettings}>
          {/* desktop-class settings modal: sidebar + content layout, 900px desktop width constrained to 95vw on mobile */}
          <div
            // eslint-disable-next-line viewport/no-fixed-width -- intentional modal, constrained by max-w-[95vw] on mobile
            className="bg-neutral-950 border border-neutral-700 rounded-xl shadow-2xl flex flex-col md:flex-row w-[900px] max-w-[95vw] h-[80vh] max-h-[700px] overflow-hidden"
            onClick={e => e.stopPropagation()}
          >
            {/* Desktop Sidebar */}
            <nav className="hidden md:flex w-48 shrink-0 bg-neutral-900/50 border-r border-neutral-800 flex-col">
              <div className="px-4 py-4 border-b border-neutral-800">
                <h2 className="text-sm font-semibold text-neutral-100 flex items-center gap-2">
                  <Settings className="w-4 h-4 text-neutral-400" />
                  Settings
                </h2>
              </div>
              <div className="flex-1 py-2 space-y-0.5 px-2">
                {visibleSections.map(section => {
                  const Icon = section.icon;
                  const isActive = activeSection === section.id;
                  return (
                    <button
                      key={section.id}
                      onClick={() => setActiveSection(section.id)}
                      className={`w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-left text-xs transition-colors ${
                        isActive
                          ? 'bg-neutral-800 text-neutral-100'
                          : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800/50'
                      }`}
                    >
                      <Icon className="w-3.5 h-3.5 shrink-0" />
                      {section.label}
                    </button>
                  );
                })}
              </div>
              <div className="px-2 py-3 border-t border-neutral-800">
                <div className="px-3 mb-2">
                  <span className="text-[11px] text-neutral-500 truncate block" title={userEmail}>{userEmail}</span>
                </div>
                <button
                  onClick={onSignOut}
                  className="w-full flex items-center gap-2.5 px-3 py-2 rounded-lg text-left text-xs text-neutral-400 hover:text-red-400 hover:bg-red-950/20 transition-colors"
                >
                  <LogOut className="w-3.5 h-3.5 shrink-0" />
                  Sign Out
                </button>
              </div>
            </nav>

            {/* Content */}
            <div className="flex-1 flex flex-col min-w-0">
              <div className="flex items-center justify-between px-4 md:px-6 py-4 border-b border-neutral-800 gap-2">
                <div className="flex-1 min-w-0">
                  {/* Mobile dropdown for section selection */}
                  <select
                    value={activeSection}
                    onChange={e => setActiveSection(e.target.value as SectionId)}
                    className="md:hidden w-full bg-neutral-900 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                  >
                    {visibleSections.map(section => (
                      <option key={section.id} value={section.id}>{section.label}</option>
                    ))}
                  </select>
                  <h3 className="hidden md:block text-sm font-semibold text-neutral-100">
                    {visibleSections.find(s => s.id === activeSection)?.label}
                  </h3>
                </div>
                <button
                  data-viewport-allow-small-target
                  onClick={onToggleSettings}
                  className="text-neutral-500 hover:text-neutral-300 transition-colors p-1 rounded hover:bg-neutral-800 shrink-0"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
              <div className="flex-1 overflow-y-auto px-4 md:px-6 py-5 custom-scrollbar">
                {renderActiveSection()}
              </div>
              {/* Mobile footer with user info and Sign Out */}
              <div className="md:hidden px-4 py-3 border-t border-neutral-800 bg-neutral-900/50 flex items-center justify-between gap-3">
                <span className="text-[11px] text-neutral-500 truncate flex-1" title={userEmail}>{userEmail}</span>
                <button
                  onClick={onSignOut}
                  className="flex items-center gap-1.5 px-3 py-3.5 rounded-lg text-xs text-neutral-400 hover:text-red-400 hover:bg-red-950/20 transition-colors whitespace-nowrap"
                >
                  <LogOut className="w-3.5 h-3.5 shrink-0" />
                  Sign Out
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
