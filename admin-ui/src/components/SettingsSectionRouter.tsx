import { SettingsAdvancedSection } from './SettingsAdvancedSection';
import { SettingsGeneralSection } from './SettingsGeneralSection';
import { SettingsOrchestratorSection } from './SettingsOrchestratorSection';
import { SettingsNotificationsSection } from './SettingsNotificationsSection';
import type { NotificationConfig } from './SettingsNotificationsSection';
import { Users } from 'lucide-react';
import { ProviderSettingsPanel } from './ProviderSettingsPanel';
import { ProviderCategoriesPanel } from './ProviderCategoriesPanel';
import { ProjectBuildConfigSection } from './ProjectBuildConfigSection';
import { ProjectResourceEnvelopeSection } from './ProjectResourceEnvelopeSection';
import { ProjectAutoRemediationSection } from './ProjectAutoRemediationSection';
import { ProjectIssueImplementationSection } from './ProjectIssueImplementationSection';
import { ProjectAutoReviewSection } from './ProjectAutoReviewSection';
import { ProjectIntegrationBranchSection } from './ProjectIntegrationBranchSection';
import { ProjectPromotionTargetSection } from './ProjectPromotionTargetSection';
import { SettingsAccountsSection } from './SettingsAccountsSection';
import { DeploySection } from './SettingsDeploySection';
import type {
  BuildConfig,
  AutoRemediationConfig,
  AutoRemediationStats,
  IssueImplementationConfig,
  IssueImplementationConfigUpdate,
  AutoReviewConfig,
  ProjectResourceEnvelopeResponse,
  ResourceEnvelopeConfig,
} from '../types/project';
import type { UserLaunchableTaskType } from '../taskCapabilities';
import type { ViewMode } from '../hooks/useViewPreferences';
import type {
  AccountMutationReturn,
  AccountUpdatePayload,
  AuthMode,
  CategoryRoutingEntry,
  DeployState,
  FailedRollbackInfo,
  MaxPlanAccount,
  ProviderCatalogEntry,
  ProviderName,
  ProviderSettingsByEffort,
  ProviderSettingsDefaults,
  TaskTypeProviderOverrides,
  TaskTypeRoutingEntry,
} from '../types/settings';
import type { SectionId } from './SettingsDropdown';

/**
 * Props consumed by {@link SettingsSectionRouter}. These are exactly the
 * section-specific props that were previously threaded through every
 * `renderXxx()` helper inside `SettingsDropdown`. Extracting them here keeps
 * the dropdown's own surface limited to the modal shell + navigation state.
 *
 * The shape is intentionally identical (same names, same types) to the
 * corresponding slice of `SettingsDropdownProps` so the behavior is preserved
 * exactly — the router just dispatches on `activeSection`.
 */
export interface SettingsSectionRouterProps {
  activeSection: SectionId;
  isAdmin: boolean;
  /** Navigate to another section (used by the General section's CTA). */
  onNavigateToSection: (section: SectionId) => void;
  /** Close the whole settings panel (used by the Accounts section). */
  onToggleSettings: () => void;
  /** Computed union of every model name referenced by routing/preferences. */
  allModelOptions: string[];
  /** Health-check URLs textarea buffer + setter (owned by the dropdown). */
  healthCheckUrlsInput: string;
  onHealthCheckUrlsInputChange: (value: string) => void;
  /** Commit the parsed health-check URLs upstream. */
  onUpdateHealthCheckUrls: (urls: string[]) => void;

  // General
  defaultTaskType: UserLaunchableTaskType;
  onUpdateDefaultTaskType: (value: UserLaunchableTaskType) => void;
  viewMode: ViewMode;
  onSetViewMode: (mode: ViewMode) => void;
  refreshInterval: number;
  onUpdateRefreshInterval: (value: number) => void;
  pageSize: number;
  onUpdatePageSize: (value: number) => void;
  apiHealthy: boolean;
  wsConnected: boolean;
  lastFetchTime: number;

  // Notifications (General quick-toggles)
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

  // Orchestrator
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
  autonomousResearchIntervalMinutes: number;
  onUpdateAutonomousResearchInterval: (value: number) => void;
  directionCooldownMinutes: number;
  onUpdateDirectionCooldownMinutes: (value: number) => void;
  strategicResearchFrequency: number;
  onUpdateStrategicResearchFrequency: (value: number) => void;
  anyAutonomousEnabled: boolean;
  onPauseAllAutonomous: () => Promise<void>;
  onRestartOrchestrator: () => Promise<void>;

  // Accounts
  githubAccount: string | null;
  onConnectGitHub: () => void;
  maxPlanAccounts: MaxPlanAccount[];
  onToggleAccount: (id: string, enabled: boolean) => void;
  onRemoveAccount: (id: string, force?: boolean) => AccountMutationReturn;
  onResetRateLimits: (id: string) => void;
  onRefreshAccountModels: (id: string) => void;
  onUpdateAccount: (id: string, payload: AccountUpdatePayload) => AccountMutationReturn;
  onUpdateAccountCaps: (
    id: string,
    caps: { daily_task_cap?: number | null; max_concurrent_tasks?: number | null; auto_disable_after_rate_limits?: number | null },
  ) => void;
  showUserManagement: boolean;
  onToggleUserManagement: () => void;
  showProjectMembers: boolean;
  onToggleProjectMembers: () => void;
  selectedProjectMyRole: string | null;
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

  // Provider settings
  providers: Record<string, ProviderCatalogEntry>;
  providerSettingsDefaults: ProviderSettingsDefaults;
  taskTypeProviderOverrides: TaskTypeProviderOverrides;
  providerSettingsByEffort: ProviderSettingsByEffort;
  onUpdateProviderSettingsDefaults: (value: ProviderSettingsDefaults) => Promise<void>;
  onUpdateTaskTypeProviderOverrides: (value: TaskTypeProviderOverrides) => Promise<void>;
  onUpdateProviderSettingsByEffort: (value: ProviderSettingsByEffort) => Promise<void>;

  // Provider routing
  providerSelectionMode: 'balanced' | 'preferred' | 'exclusive';
  onUpdateProviderSelectionMode: (mode: 'balanced' | 'preferred' | 'exclusive') => Promise<void>;
  taskTypeCategories: Record<string, string[]>;
  onUpdateTaskTypeCategories: (value: Record<string, string[]>) => Promise<void>;
  categoryRouting: Record<string, CategoryRoutingEntry>;
  onUpdateCategoryRouting: (value: Record<string, CategoryRoutingEntry>) => Promise<void>;
  taskTypeRouting: Record<string, TaskTypeRoutingEntry>;
  onUpdateTaskTypeRouting: (value: Record<string, TaskTypeRoutingEntry>) => Promise<void>;

  // Deploy & health
  deployState: DeployState | null;
  onPausePipeline: () => Promise<void>;
  onResumePipeline: () => Promise<void>;
  onResetHealth: () => Promise<void>;
  onRollback: () => Promise<void>;
  onMarkStable: () => Promise<void>;
  onRepairState: () => Promise<void>;
  // Standing failed-rollback signal (client-side), rendered as a durable banner
  // in the deploy section so it outlives the transient rollback error toast.
  failedRollback: FailedRollbackInfo | null;
  zoneHealth:
    | Record<string, { status: string; last_success: string | null; last_failure: string | null; last_failure_reason: string | null; consecutive_failures: number; cooldown_active: boolean }>
    | null;

  // Notifications (full panel)
  notificationConfig: NotificationConfig;
  onUpdateNotificationConfig: (config: Partial<NotificationConfig>) => void;
  dailyCostCeilingUsd: number;
  onUpdateDailyCostCeiling: (value: number) => void;

  // Project-scoped config
  selectedProjectId: string | null;
  selectedProjectOwner: string | null;
  selectedProjectRepo: string | null;
  selectedProjectIsSelfRepo?: boolean;
  selectedProjectRole?: 'owner' | 'member' | 'viewer' | null;
  buildConfig: BuildConfig | null;
  onUpdateBuildConfig: (config: BuildConfig) => Promise<void>;
  onDetectBuildConfig: () => Promise<void>;
  autoRemediationConfig: AutoRemediationConfig | null;
  autoRemediationStats: AutoRemediationStats | null;
  onUpdateAutoRemediationConfig: (config: AutoRemediationConfig) => Promise<void>;
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

/**
 * Renders the body of the currently-active settings section.
 *
 * This was extracted verbatim from the `renderXxx()` helpers that previously
 * lived inside `SettingsDropdown`; each section delegates to its own focused
 * sub-component. Behavior is unchanged — only the location of the dispatch
 * moved, isolating the section-routing concern from the modal chrome.
 */
export function SettingsSectionRouter(props: SettingsSectionRouterProps) {
  switch (props.activeSection) {
    case 'general':
      return (
        <SettingsGeneralSection
          apiHealthy={props.apiHealthy}
          wsConnected={props.wsConnected}
          lastFetchTime={props.lastFetchTime}
          defaultTaskType={props.defaultTaskType}
          onUpdateDefaultTaskType={props.onUpdateDefaultTaskType}
          viewMode={props.viewMode}
          onSetViewMode={props.onSetViewMode}
          allModelOptions={props.allModelOptions}
          refreshInterval={props.refreshInterval}
          onUpdateRefreshInterval={props.onUpdateRefreshInterval}
          pageSize={props.pageSize}
          onUpdatePageSize={props.onUpdatePageSize}
          notifyOnComplete={props.notifyOnComplete}
          onUpdateNotifyOnComplete={props.onUpdateNotifyOnComplete}
          notifyOnFail={props.notifyOnFail}
          onUpdateNotifyOnFail={props.onUpdateNotifyOnFail}
          notifyOnApprovalNeeded={props.notifyOnApprovalNeeded}
          onUpdateNotifyOnApprovalNeeded={props.onUpdateNotifyOnApprovalNeeded}
          soundOnComplete={props.soundOnComplete}
          onUpdateSoundOnComplete={props.onUpdateSoundOnComplete}
          notifyOnHighSignalMessage={props.notifyOnHighSignalMessage}
          onUpdateNotifyOnHighSignalMessage={props.onUpdateNotifyOnHighSignalMessage}
          onNavigateToNotifications={() => props.onNavigateToSection('notifications')}
        />
      );
    case 'provider-routing':
      return (
        <ProviderCategoriesPanel
          providers={props.providers}
          selectionMode={props.providerSelectionMode}
          onUpdateSelectionMode={props.onUpdateProviderSelectionMode}
          taskTypeCategories={props.taskTypeCategories}
          onUpdateTaskTypeCategories={props.onUpdateTaskTypeCategories}
          categoryRouting={props.categoryRouting}
          onUpdateCategoryRouting={props.onUpdateCategoryRouting}
          taskTypeRouting={props.taskTypeRouting}
          onUpdateTaskTypeRouting={props.onUpdateTaskTypeRouting}
        />
      );
    case 'provider-settings':
      return (
        <ProviderSettingsPanel
          providers={props.providers}
          providerSettingsDefaults={props.providerSettingsDefaults}
          taskTypeProviderOverrides={props.taskTypeProviderOverrides}
          providerSettingsByEffort={props.providerSettingsByEffort}
          maxPlanAccounts={props.maxPlanAccounts}
          onUpdateDefaults={props.onUpdateProviderSettingsDefaults}
          onUpdateOverrides={props.onUpdateTaskTypeProviderOverrides}
          onUpdateProviderSettingsByEffort={props.onUpdateProviderSettingsByEffort}
        />
      );
    case 'orchestrator':
      return (
        <SettingsOrchestratorSection
          maxConcurrentVMs={props.maxConcurrentVMs}
          onUpdateMaxVMs={props.onUpdateMaxVMs}
          maxRetries={props.maxRetries}
          onUpdateMaxRetries={props.onUpdateMaxRetries}
          requireApprovalGates={props.requireApprovalGates}
          onUpdateRequireApprovalGates={props.onUpdateRequireApprovalGates}
          autoArchiveEnabled={props.autoArchiveEnabled}
          onUpdateAutoArchiveEnabled={props.onUpdateAutoArchiveEnabled}
          autoArchiveTerminalAfterDays={props.autoArchiveTerminalAfterDays}
          onUpdateAutoArchiveTerminalAfterDays={props.onUpdateAutoArchiveTerminalAfterDays}
          autoArchiveMaxTerminalTasksPerProject={props.autoArchiveMaxTerminalTasksPerProject}
          onUpdateAutoArchiveMaxTerminalTasksPerProject={props.onUpdateAutoArchiveMaxTerminalTasksPerProject}
          autoArchiveIntervalMinutes={props.autoArchiveIntervalMinutes}
          onUpdateAutoArchiveIntervalMinutes={props.onUpdateAutoArchiveIntervalMinutes}
          maxAutonomousTasksPerHour={props.maxAutonomousTasksPerHour}
          onUpdateMaxAutonomousRate={props.onUpdateMaxAutonomousRate}
          maxGlobalAutonomousTasksPerHour={props.maxGlobalAutonomousTasksPerHour}
          onUpdateMaxGlobalAutonomousRate={props.onUpdateMaxGlobalAutonomousRate}
          maxPipelineSlots={props.maxPipelineSlots}
          onUpdateMaxPipelineSlots={props.onUpdateMaxPipelineSlots}
          reservedSlotsForTypes={props.reservedSlotsForTypes}
          onUpdateReservedSlotsForTypes={props.onUpdateReservedSlotsForTypes}
          evaluatingPipelineTimeoutMinutes={props.evaluatingPipelineTimeoutMinutes}
          onUpdateEvaluatingPipelineTimeout={props.onUpdateEvaluatingPipelineTimeout}
          evaluatingFailHours={props.evaluatingFailHours}
          onUpdateEvaluatingFailHours={props.onUpdateEvaluatingFailHours}
          ciBackstopTimeoutMinutes={props.ciBackstopTimeoutMinutes}
          onUpdateCiBackstopTimeout={props.onUpdateCiBackstopTimeout}
          autonomousResearchIntervalMinutes={props.autonomousResearchIntervalMinutes}
          onUpdateAutonomousResearchInterval={props.onUpdateAutonomousResearchInterval}
          directionCooldownMinutes={props.directionCooldownMinutes}
          onUpdateDirectionCooldownMinutes={props.onUpdateDirectionCooldownMinutes}
          strategicResearchFrequency={props.strategicResearchFrequency}
          onUpdateStrategicResearchFrequency={props.onUpdateStrategicResearchFrequency}
          anyAutonomousEnabled={props.anyAutonomousEnabled}
          onPauseAllAutonomous={props.onPauseAllAutonomous}
          onRestartOrchestrator={props.onRestartOrchestrator}
          selectedProjectId={props.selectedProjectId}
        />
      );
    case 'deploy':
      return (
        <DeploySection
          deployState={props.deployState}
          onPausePipeline={props.onPausePipeline}
          onResumePipeline={props.onResumePipeline}
          onResetHealth={props.onResetHealth}
          onRollback={props.onRollback}
          onMarkStable={props.onMarkStable}
          onRepairState={props.onRepairState}
          failedRollback={props.failedRollback}
          zoneHealth={props.zoneHealth}
        />
      );
    case 'accounts':
      return (
        <SettingsAccountsSection
          githubAccount={props.githubAccount}
          onConnectGitHub={props.onConnectGitHub}
          maxPlanAccounts={props.maxPlanAccounts}
          onToggleAccount={props.onToggleAccount}
          onRemoveAccount={props.onRemoveAccount}
          onResetRateLimits={props.onResetRateLimits}
          onRefreshAccountModels={props.onRefreshAccountModels}
          onUpdateAccount={props.onUpdateAccount}
          onUpdateAccountCaps={props.onUpdateAccountCaps}
          providers={props.providers}
          newAccountName={props.newAccountName}
          setNewAccountName={props.setNewAccountName}
          newAccountProvider={props.newAccountProvider}
          setNewAccountProvider={props.setNewAccountProvider}
          newAccountAuthMode={props.newAccountAuthMode}
          setNewAccountAuthMode={props.setNewAccountAuthMode}
          newAccountToken={props.newAccountToken}
          setNewAccountToken={props.setNewAccountToken}
          newAccountProviderMetadata={props.newAccountProviderMetadata}
          setNewAccountProviderMetadata={props.setNewAccountProviderMetadata}
          newAccountClaudeConfigJson={props.newAccountClaudeConfigJson}
          setNewAccountClaudeConfigJson={props.setNewAccountClaudeConfigJson}
          newAccountDailyTaskCap={props.newAccountDailyTaskCap}
          setNewAccountDailyTaskCap={props.setNewAccountDailyTaskCap}
          newAccountMaxConcurrentTasks={props.newAccountMaxConcurrentTasks}
          setNewAccountMaxConcurrentTasks={props.setNewAccountMaxConcurrentTasks}
          newAccountAutoDisableAfterRateLimits={props.newAccountAutoDisableAfterRateLimits}
          setNewAccountAutoDisableAfterRateLimits={props.setNewAccountAutoDisableAfterRateLimits}
          onAddAccount={props.onAddAccount}
        />
      );
    case 'access':
      return (
        <div className="space-y-6">
          {props.isAdmin && (
            <div>
              <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-1">User Management</h3>
              <p className="text-xs text-neutral-500 mb-3">Manage which users can access this Blueprint instance and their roles.</p>
              <button
                onClick={() => { props.onToggleUserManagement(); props.onToggleSettings(); }}
                className={`flex items-center gap-2 min-h-[44px] text-xs py-2 px-4 rounded-lg transition-colors border ${
                  props.showUserManagement
                    ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-400'
                    : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'
                }`}
              >
                <Users className="w-3.5 h-3.5" />
                {props.showUserManagement ? 'Close User Management' : 'Manage Users'}
              </button>
            </div>
          )}
          {props.selectedProjectId && (props.selectedProjectMyRole === 'owner' || props.isAdmin) && (
            <div>
              <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-1">Project Members</h3>
              <p className="text-xs text-neutral-500 mb-3">Control who has access to this project and their roles.</p>
              <button
                data-testid="toggle-project-members"
                onClick={() => { props.onToggleProjectMembers(); props.onToggleSettings(); }}
                className={`flex items-center gap-2 min-h-[44px] text-xs py-2 px-4 rounded-lg transition-colors border ${
                  props.showProjectMembers
                    ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-400'
                    : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'
                }`}
              >
                <Users className="w-3.5 h-3.5" />
                {props.showProjectMembers ? 'Close Project Members' : 'Manage Project Members'}
              </button>
            </div>
          )}
          {!props.isAdmin && !(props.selectedProjectId && props.selectedProjectMyRole === 'owner') && (
            <p className="text-xs text-neutral-500">No access management options are available for your current role.</p>
          )}
        </div>
      );
    case 'notifications':
      return (
        <SettingsNotificationsSection
          webhookUrl={props.notificationConfig.webhook_url}
          onUpdateWebhookUrl={url => props.onUpdateNotificationConfig({ webhook_url: url })}
          enabledEvents={props.notificationConfig.enabled_events}
          onUpdateEnabledEvents={events => props.onUpdateNotificationConfig({ enabled_events: events })}
          throttleSeconds={props.notificationConfig.throttle_seconds}
          onUpdateThrottleSeconds={secs => props.onUpdateNotificationConfig({ throttle_seconds: secs })}
          dailyCostCeilingUsd={props.dailyCostCeilingUsd}
          onUpdateDailyCostCeiling={props.onUpdateDailyCostCeiling}
          isAdmin={props.isAdmin}
        />
      );
    case 'build-config':
      return (
        <ProjectBuildConfigSection
          githubOwner={props.selectedProjectOwner}
          githubRepo={props.selectedProjectRepo}
          isSelfRepo={props.selectedProjectIsSelfRepo}
          buildConfig={props.buildConfig}
          onSave={props.onUpdateBuildConfig}
          onDetect={props.onDetectBuildConfig}
          detectAvailable={Boolean(props.selectedProjectOwner && props.selectedProjectRepo)}
          projectId={props.selectedProjectId}
        />
      );
    case 'auto-review':
      return (
        <ProjectAutoReviewSection
          config={props.autoReviewConfig}
          canSave={props.selectedProjectRole === 'owner'}
          onSave={props.onUpdateAutoReviewConfig}
        />
      );
    case 'resource-envelope':
      return (
        <ProjectResourceEnvelopeSection
          githubOwner={props.selectedProjectOwner}
          githubRepo={props.selectedProjectRepo}
          data={props.resourceEnvelopeData}
          loading={props.resourceEnvelopeLoading}
          error={props.resourceEnvelopeError}
          canSave={props.selectedProjectRole === 'owner'}
          onSave={props.onUpdateResourceEnvelope}
        />
      );
    case 'auto-remediation':
      return (
        <ProjectAutoRemediationSection
          githubOwner={props.selectedProjectOwner}
          githubRepo={props.selectedProjectRepo}
          config={props.autoRemediationConfig}
          stats={props.autoRemediationStats}
          onSave={props.onUpdateAutoRemediationConfig}
          integrationBranch={props.integrationBranchData?.integration_branch}
          promotionTarget={props.promotionTargetData?.promotion_target}
        />
      );
    case 'issue-implementation':
      return (
        <ProjectIssueImplementationSection
          githubOwner={props.selectedProjectOwner}
          githubRepo={props.selectedProjectRepo}
          config={props.issueImplementationConfig}
          onSave={props.onUpdateIssueImplementationConfig}
        />
      );
    case 'integration-branch':
      return (
        <ProjectIntegrationBranchSection
          data={props.integrationBranchData}
          canSave={props.selectedProjectRole === 'owner'}
          onSave={props.onUpdateIntegrationBranch}
          relatedPromotionTarget={props.promotionTargetData?.promotion_target}
        />
      );
    case 'promotion-target':
      return (
        <ProjectPromotionTargetSection
          data={props.promotionTargetData}
          canSave={props.selectedProjectRole === 'owner'}
          onSave={props.onUpdatePromotionTarget}
          relatedIntegrationBranch={props.integrationBranchData?.integration_branch}
        />
      );
    case 'advanced':
      return (
        <SettingsAdvancedSection
          healthCheckUrlsInput={props.healthCheckUrlsInput}
          onHealthCheckUrlsChange={props.onHealthCheckUrlsInputChange}
          onUpdateHealthCheckUrls={props.onUpdateHealthCheckUrls}
        />
      );
  }
}
