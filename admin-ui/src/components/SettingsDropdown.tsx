import { useEffect, useState } from 'react';
import type { NotificationConfig } from './SettingsNotificationsSection';
import { SettingsSectionRouter } from './SettingsSectionRouter';
import type { BuildConfig, AutoRemediationConfig, AutoRemediationStats, IssueImplementationConfig, IssueImplementationConfigUpdate, AutoReviewConfig, ProjectResourceEnvelopeResponse, ResourceEnvelopeConfig } from '../types/project';
import { Settings, GitBranch, Shield, X, Sliders, ListChecks, Server, User, LogOut, Bell, Wrench, Tag, HelpCircle, FileDown, Users } from 'lucide-react';
import type { UserLaunchableTaskType } from '../taskCapabilities';
import type { ViewMode } from '../hooks/useViewPreferences';
export type { MaxPlanAccount } from '../types/settings';
import type { AccountMutationReturn, AccountUpdatePayload, AuthMode, CategoryRoutingEntry, DeployState, FailedRollbackInfo, MaxPlanAccount, ModelPreference, ProviderCatalogEntry, ProviderName, ProviderSettingsByEffort, ProviderSettingsDefaults, TaskTypeProviderOverrides, TaskTypeRoutingEntry } from '../types/settings';


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
  // Standing failed-rollback signal (client-side), threaded to the deploy panel.
  failedRollback: FailedRollbackInfo | null;
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
  resourceEnvelopeData: ProjectResourceEnvelopeResponse | null;
  resourceEnvelopeLoading: boolean;
  resourceEnvelopeError: string | null;
  onUpdateResourceEnvelope: (patch: Partial<ResourceEnvelopeConfig>) => Promise<void>;
  // Integration branch config (project-scoped)
  integrationBranchData: { integration_branch: string; is_default: boolean } | null;
  onUpdateIntegrationBranch: (branch: string | null) => Promise<void>;
  // Promotion target config (project-scoped)
  promotionTargetData: { promotion_target: string; is_default: boolean } | null;
  onUpdatePromotionTarget: (branch: string | null) => Promise<void>;
}



export type SectionId = 'general' | 'provider-routing' | 'provider-settings' | 'orchestrator' | 'deploy' | 'accounts' | 'notifications' | 'advanced' | 'build-config' | 'auto-remediation' | 'issue-implementation' | 'auto-review' | 'resource-envelope' | 'branches' | 'access';

type SectionEntry = { id: SectionId; label: string; icon: typeof Settings; adminOnly?: boolean; projectOnly?: boolean };

const SECTION_GROUPS: { group: string; sections: SectionEntry[] }[] = [
  {
    group: 'Personal',
    sections: [
      { id: 'general', label: 'General', icon: Sliders },
      { id: 'notifications', label: 'Notifications', icon: Bell },
    ],
  },
  {
    group: 'Project',
    sections: [
      { id: 'build-config', label: 'Build Config', icon: Wrench, projectOnly: true },
      { id: 'auto-review', label: 'Auto-Review', icon: HelpCircle, projectOnly: true },
      { id: 'resource-envelope', label: 'Resource Envelope', icon: Server, projectOnly: true },
      { id: 'auto-remediation', label: 'Auto-Remediation', icon: Settings, projectOnly: true },
      { id: 'issue-implementation', label: 'Issue Implementation', icon: Tag, projectOnly: true },
      { id: 'branches', label: 'Branches', icon: GitBranch, projectOnly: true },
    ],
  },
  {
    group: 'Infrastructure',
    sections: [
      { id: 'orchestrator', label: 'Orchestrator', icon: Server, adminOnly: true },
      { id: 'deploy', label: 'Deploy & Health', icon: Shield, adminOnly: true },
      { id: 'provider-routing', label: 'Provider Routing', icon: GitBranch, adminOnly: true },
      { id: 'provider-settings', label: 'Provider Settings', icon: ListChecks, adminOnly: true },
      { id: 'accounts', label: 'Accounts', icon: User },
      { id: 'access', label: 'Access', icon: Users },
    ],
  },
  {
    group: 'Advanced',
    sections: [
      { id: 'advanced', label: 'Advanced', icon: FileDown },
    ],
  },
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
  failedRollback,
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
  resourceEnvelopeData,
  resourceEnvelopeLoading,
  resourceEnvelopeError,
  onUpdateResourceEnvelope,
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
  const allModelOptions = Array.from(new Set([
    ...Object.values(providerTierModelMap).flatMap(providerMap => Object.values(providerMap || {})),
    ...Object.values(taskTypeModelPreferences).flatMap(prefs => prefs.map(pref => pref.model)),
  ])).filter(Boolean).sort();

  return (
    <div className="relative" ref={settingsRef}>
      {/* reason: dense-UI icon trigger (p-2 around 16px icon) — operator-density convention for settings panels */}
      <button
        data-viewport-allow-small-target
        onClick={onToggleSettings}
        className={`p-2 rounded-md text-xs transition-colors flex items-center ${showSettings ? 'text-cyan-400 bg-cyan-500/10' : 'text-neutral-400 hover:text-neutral-200 hover:bg-neutral-800'}${triggerClassName ? ` ${triggerClassName}` : ''}`}
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
              <div className="flex-1 py-2 overflow-y-auto px-2 custom-scrollbar">
                {SECTION_GROUPS.map(group => {
                  const visibleGroupSections = group.sections.filter(s =>
                    (!s.adminOnly || isAdmin) && (!s.projectOnly || selectedProjectId !== null)
                  );
                  if (visibleGroupSections.length === 0) return null;
                  return (
                    <div key={group.group} className="mb-2">
                      <div className="px-3 py-1.5">
                        <span className="text-ui-sm font-semibold text-neutral-500 uppercase tracking-wider">{group.group}</span>
                      </div>
                      <div className="space-y-0.5">
                        {visibleGroupSections.map(section => {
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
                    </div>
                  );
                })}
              </div>
              <div className="px-2 py-3 border-t border-neutral-800">
                <div className="px-3 mb-2">
                  <span className="text-ui-md text-neutral-500 truncate block" title={userEmail}>{userEmail}</span>
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
                    {SECTION_GROUPS.map(group => {
                      const visibleGroupSections = group.sections.filter(s =>
                        (!s.adminOnly || isAdmin) && (!s.projectOnly || selectedProjectId !== null)
                      );
                      if (visibleGroupSections.length === 0) return null;
                      return (
                        <optgroup key={group.group} label={group.group}>
                          {visibleGroupSections.map(section => (
                            <option key={section.id} value={section.id}>{section.label}</option>
                          ))}
                        </optgroup>
                      );
                    })}
                  </select>
                  <h3 className="hidden md:block text-sm font-semibold text-neutral-100">
                    {SECTION_GROUPS.flatMap(g => g.sections).find(s => s.id === activeSection)?.label}
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
                <SettingsSectionRouter
                  activeSection={activeSection}
                  isAdmin={isAdmin}
                  onNavigateToSection={setActiveSection}
                  onToggleSettings={onToggleSettings}
                  allModelOptions={allModelOptions}
                  healthCheckUrlsInput={healthCheckUrlsInput}
                  onHealthCheckUrlsInputChange={setHealthCheckUrlsInput}
                  onUpdateHealthCheckUrls={onUpdateHealthCheckUrls}
                  defaultTaskType={defaultTaskType}
                  onUpdateDefaultTaskType={onUpdateDefaultTaskType}
                  viewMode={viewMode}
                  onSetViewMode={onSetViewMode}
                  refreshInterval={refreshInterval}
                  onUpdateRefreshInterval={onUpdateRefreshInterval}
                  pageSize={pageSize}
                  onUpdatePageSize={onUpdatePageSize}
                  apiHealthy={apiHealthy}
                  wsConnected={wsConnected}
                  lastFetchTime={lastFetchTime}
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
                  anyAutonomousEnabled={anyAutonomousEnabled}
                  onPauseAllAutonomous={onPauseAllAutonomous}
                  onRestartOrchestrator={onRestartOrchestrator}
                  githubAccount={githubAccount}
                  onConnectGitHub={onConnectGitHub}
                  maxPlanAccounts={maxPlanAccounts}
                  onToggleAccount={onToggleAccount}
                  onRemoveAccount={onRemoveAccount}
                  onResetRateLimits={onResetRateLimits}
                  onRefreshAccountModels={onRefreshAccountModels}
                  onUpdateAccount={onUpdateAccount}
                  onUpdateAccountCaps={onUpdateAccountCaps}
                  showUserManagement={showUserManagement}
                  onToggleUserManagement={onToggleUserManagement}
                  showProjectMembers={showProjectMembers}
                  onToggleProjectMembers={onToggleProjectMembers}
                  selectedProjectMyRole={selectedProjectMyRole}
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
                  providers={providers}
                  providerSettingsDefaults={providerSettingsDefaults}
                  taskTypeProviderOverrides={taskTypeProviderOverrides}
                  providerSettingsByEffort={providerSettingsByEffort}
                  onUpdateProviderSettingsDefaults={onUpdateProviderSettingsDefaults}
                  onUpdateTaskTypeProviderOverrides={onUpdateTaskTypeProviderOverrides}
                  onUpdateProviderSettingsByEffort={onUpdateProviderSettingsByEffort}
                  providerSelectionMode={providerSelectionMode}
                  onUpdateProviderSelectionMode={onUpdateProviderSelectionMode}
                  taskTypeCategories={taskTypeCategories}
                  onUpdateTaskTypeCategories={onUpdateTaskTypeCategories}
                  categoryRouting={categoryRouting}
                  onUpdateCategoryRouting={onUpdateCategoryRouting}
                  taskTypeRouting={taskTypeRouting}
                  onUpdateTaskTypeRouting={onUpdateTaskTypeRouting}
                  deployState={deployState}
                  onPausePipeline={onPausePipeline}
                  onResumePipeline={onResumePipeline}
                  onResetHealth={onResetHealth}
                  onRollback={onRollback}
                  onMarkStable={onMarkStable}
                  onRepairState={onRepairState}
                  failedRollback={failedRollback}
                  zoneHealth={zoneHealth}
                  notificationConfig={notificationConfig}
                  onUpdateNotificationConfig={onUpdateNotificationConfig}
                  dailyCostCeilingUsd={dailyCostCeilingUsd}
                  onUpdateDailyCostCeiling={onUpdateDailyCostCeiling}
                  selectedProjectId={selectedProjectId}
                  selectedProjectOwner={selectedProjectOwner}
                  selectedProjectRepo={selectedProjectRepo}
                  selectedProjectIsSelfRepo={selectedProjectIsSelfRepo}
                  selectedProjectRole={selectedProjectRole}
                  buildConfig={buildConfig}
                  onUpdateBuildConfig={onUpdateBuildConfig}
                  onDetectBuildConfig={onDetectBuildConfig}
                  autoRemediationConfig={autoRemediationConfig}
                  autoRemediationStats={autoRemediationStats}
                  onUpdateAutoRemediationConfig={onUpdateAutoRemediationConfig}
                  issueImplementationConfig={issueImplementationConfig}
                  onUpdateIssueImplementationConfig={onUpdateIssueImplementationConfig}
                  autoReviewConfig={autoReviewConfig}
                  onUpdateAutoReviewConfig={onUpdateAutoReviewConfig}
                  resourceEnvelopeData={resourceEnvelopeData}
                  resourceEnvelopeLoading={resourceEnvelopeLoading}
                  resourceEnvelopeError={resourceEnvelopeError}
                  onUpdateResourceEnvelope={onUpdateResourceEnvelope}
                  integrationBranchData={integrationBranchData}
                  onUpdateIntegrationBranch={onUpdateIntegrationBranch}
                  promotionTargetData={promotionTargetData}
                  onUpdatePromotionTarget={onUpdatePromotionTarget}
                />
              </div>
              {/* Mobile footer with user info and Sign Out */}
              <div className="md:hidden px-4 py-3 border-t border-neutral-800 bg-neutral-900/50 flex items-center justify-between gap-3">
                <span className="text-ui-md text-neutral-500 truncate flex-1" title={userEmail}>{userEmail}</span>
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
