import { useEffect, useState } from 'react';
import { SettingsAdvancedSection } from './SettingsAdvancedSection';
import { SettingsNotificationsSection } from './SettingsNotificationsSection';
import type { NotificationConfig } from './SettingsNotificationsSection';
import { ProviderSettingsPanel } from './ProviderSettingsPanel';
import { ProviderCategoriesPanel } from './ProviderCategoriesPanel';
import { ProjectBuildConfigSection } from './ProjectBuildConfigSection';
import { ProjectAutoRemediationSection } from './ProjectAutoRemediationSection';
import { ProjectIssueImplementationSection } from './ProjectIssueImplementationSection';
import { ProjectAutoReviewSection } from './ProjectAutoReviewSection';
import type { BuildConfig, AutoRemediationConfig, AutoRemediationStats, IssueImplementationConfig, AutoReviewConfig } from '../types/project';
import { Settings, GitBranch, RotateCcw, Loader2, Shield, X, Sliders, ListChecks, Server, User, FileDown, LogOut, Users, Bell, Wrench, Tag, HelpCircle, Plus, ChevronDown, ChevronUp, RefreshCw } from 'lucide-react';
import { timeAgo, parseTimestamp } from '../utils/time';
import { USER_LAUNCHABLE_TASK_TYPE_OPTIONS } from '../taskCapabilities';
import type { UserLaunchableTaskType } from '../taskCapabilities';
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
  viewMode: 'kanban' | 'pipeline' | 'home';
  onSetViewMode: (mode: 'kanban' | 'pipeline' | 'home') => void;
  // Notifications
  notifyOnComplete: boolean;
  onUpdateNotifyOnComplete: (value: boolean) => void;
  notifyOnFail: boolean;
  onUpdateNotifyOnFail: (value: boolean) => void;
  notifyOnApprovalNeeded: boolean;
  onUpdateNotifyOnApprovalNeeded: (value: boolean) => void;
  soundOnComplete: boolean;
  onUpdateSoundOnComplete: (value: boolean) => void;
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
  onUpdateIssueImplementationConfig: (config: IssueImplementationConfig) => Promise<void>;
  // Auto-review config (project-scoped)
  selectedProjectRole?: 'owner' | 'member' | 'viewer' | null;
  autoReviewConfig: AutoReviewConfig | null;
  onUpdateAutoReviewConfig: (patch: Partial<AutoReviewConfig>) => Promise<void>;
}


const REFRESH_PRESETS = [
  { label: '10s', value: 10000 },
  { label: '30s', value: 30000 },
  { label: '60s', value: 60000 },
  { label: 'Off', value: 0 },
];

const PAGE_PRESETS = [25, 50, 100, 200];
const ALL_PAGE_SIZE = 500;

const PROVIDER_ORDER: ProviderName[] = ['anthropic', 'openai', 'gemini'];

const FALLBACK_AUTH_MODES: Record<ProviderName, AuthMode[]> = {
  anthropic: ['oauth', 'api_key'],
  openai: ['api_key', 'oauth'],
  gemini: ['api_key', 'service_account'],
};

const METADATA_HINTS: Record<string, { label: string; placeholder: string }> = {
  project: { label: 'GCP project ID', placeholder: 'my-gcp-project' },
  location: { label: 'Vertex region (e.g. us-central1)', placeholder: 'us-central1' },
  organization: { label: 'OpenAI organization ID (optional)', placeholder: 'org-xxxxxxxx' },
  region: { label: 'Region', placeholder: 'us-east-1' },
};

type CredentialGuide = { summary: string; steps: string[] };
const CREDENTIAL_GUIDES: Record<string, CredentialGuide> = {
  'anthropic:oauth': {
    summary: 'Claude Max / Pro subscription token.',
    steps: [
      'Install the Claude Code CLI if you haven\u2019t already.',
      'Run `claude setup-token` in your terminal and follow the browser prompt.',
      'Copy the printed token (starts with `sk-ant-oat01-`) and paste it here.',
    ],
  },
  'anthropic:api_key': {
    summary: 'Pay-as-you-go API billing.',
    steps: [
      'Go to https://console.anthropic.com/settings/keys',
      'Click \u201CCreate Key\u201D and copy the secret (starts with `sk-ant-`).',
    ],
  },
  'openai:oauth': {
    summary: 'ChatGPT Plus/Pro subscription via Codex CLI.',
    steps: [
      'Install the codex CLI and run `codex login` (or use ChatGPT Desktop \u2192 Sign in).',
      'It writes `~/.codex/auth.json` (or `~/.config/openai/auth.json`).',
      'Upload that JSON file below, or paste its contents.',
    ],
  },
  'openai:api_key': {
    summary: 'Pay-as-you-go API billing.',
    steps: [
      'Go to https://platform.openai.com/api-keys',
      'Click \u201CCreate new secret key\u201D and copy it (starts with `sk-`).',
    ],
  },
  'gemini:api_key': {
    summary: 'Google AI Studio key \u2014 quick to set up.',
    steps: [
      'Go to https://aistudio.google.com/apikey',
      'Click \u201CCreate API key\u201D and copy the value (starts with `AIza`).',
    ],
  },
  'gemini:service_account': {
    summary: 'GCP service account for Vertex AI (production-grade).',
    steps: [
      'In GCP Console \u2192 IAM & Admin \u2192 Service Accounts, create a service account.',
      'Grant it the `Vertex AI User` role (and `Service Account Token Creator` on itself if you use impersonation).',
      'Add a JSON key and download it.',
      'Upload the JSON below, then fill in the `project` (GCP project ID) and `location` (Vertex region) metadata fields.',
    ],
  },
};

export type SectionId = 'general' | 'provider-routing' | 'provider-settings' | 'orchestrator' | 'deploy' | 'accounts' | 'notifications' | 'advanced' | 'build-config' | 'auto-remediation' | 'issue-implementation' | 'auto-review';

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
  { id: 'advanced', label: 'Advanced', icon: FileDown },
];

function Toggle({ enabled, onChange }: { enabled: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      onClick={() => onChange(!enabled)}
      className={`relative inline-flex h-5 w-9 items-center rounded-full transition-colors shrink-0 ${enabled ? 'bg-emerald-600' : 'bg-neutral-700'}`}
    >
      <span className={`inline-block h-3.5 w-3.5 rounded-full bg-white transition-transform ${enabled ? 'translate-x-[18px]' : 'translate-x-[3px]'}`} />
    </button>
  );
}

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
}: SettingsDropdownProps) {
  const [, setTick] = useState(0);
  const [activeSection, setActiveSection] = useState<SectionId>('general');
  const [restarting, setRestarting] = useState(false);
  const [restartStatus, setRestartStatus] = useState<'idle' | 'success' | 'error'>('idle');
  const [refreshInputVal, setRefreshInputVal] = useState('');
  const [pageSizeInputVal, setPageSizeInputVal] = useState('');
  const [vmInputVal, setVmInputVal] = useState(String(maxConcurrentVMs));
  const [retriesInputVal, setRetriesInputVal] = useState(String(maxRetries));
  const [autonomousRateInputVal, setAutonomousRateInputVal] = useState(String(maxAutonomousTasksPerHour));

  // Global model/effort defaults
  const [defaultModel, setDefaultModel] = useState<string>(
    () => JSON.parse(localStorage.getItem('blueprint-admin:defaultModel') ?? 'null') ?? ''
  );
  const [defaultEffort, setDefaultEffort] = useState<string>(
    () => JSON.parse(localStorage.getItem('blueprint-admin:defaultEffort') ?? 'null') ?? ''
  );


  const [healthCheckUrlsInput, setHealthCheckUrlsInput] = useState(() => healthCheckUrls.join('\n'));
  const [showAddAccountForm, setShowAddAccountForm] = useState(false);
  const [showAddAccountCaps, setShowAddAccountCaps] = useState(false);
  const [expandedAccountCaps, setExpandedAccountCaps] = useState<string | null>(null);
  const [capDraft, setCapDraft] = useState<{ daily_task_cap: string; max_concurrent_tasks: string; auto_disable_after_rate_limits: string }>({ daily_task_cap: '', max_concurrent_tasks: '', auto_disable_after_rate_limits: '' });

  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => { setVmInputVal(String(maxConcurrentVMs)); }, [maxConcurrentVMs]);
  useEffect(() => { setRetriesInputVal(String(maxRetries)); }, [maxRetries]);
  useEffect(() => { setAutonomousRateInputVal(String(maxAutonomousTasksPerHour)); }, [maxAutonomousTasksPerHour]);
  useEffect(() => { setHealthCheckUrlsInput(healthCheckUrls.join('\n')); }, [healthCheckUrls]);
  useEffect(() => {
    const availableAuthModes = (Object.keys(providers[newAccountProvider]?.auth_modes ?? {}) as AuthMode[]).length > 0
      ? Object.keys(providers[newAccountProvider]?.auth_modes ?? {}) as AuthMode[]
      : FALLBACK_AUTH_MODES[newAccountProvider];
    if (!availableAuthModes.includes(newAccountAuthMode) && availableAuthModes.length > 0) {
      setNewAccountAuthMode(availableAuthModes[0]);
    }
  }, [newAccountAuthMode, newAccountProvider, providers, setNewAccountAuthMode]);

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
  const providerNames = Object.keys(providers).length > 0 ? (Object.keys(providers) as ProviderName[]) : PROVIDER_ORDER;
  const providerSelectOptions = providerNames.filter(Boolean);
  const allModelOptions = Array.from(new Set([
    ...Object.values(providerTierModelMap).flatMap(providerMap => Object.values(providerMap || {})),
    ...Object.values(taskTypeModelPreferences).flatMap(prefs => prefs.map(pref => pref.model)),
  ])).filter(Boolean).sort();
  const requiredMetadataFields = providers[newAccountProvider]?.auth_modes?.[newAccountAuthMode]?.required_metadata_fields ?? [];
  const newAccountAuthModes = (Object.keys(providers[newAccountProvider]?.auth_modes ?? {}) as AuthMode[]).length > 0
    ? Object.keys(providers[newAccountProvider]?.auth_modes ?? {}) as AuthMode[]
    : FALLBACK_AUTH_MODES[newAccountProvider];
  const newAccountSecretLabel =
    newAccountProvider === 'gemini' && newAccountAuthMode === 'service_account'
      ? 'Service account JSON'
      : newAccountProvider === 'openai' && newAccountAuthMode === 'oauth'
      ? 'auth.json'
      : newAccountProvider === 'gemini'
      ? 'Gemini API key'
      : newAccountProvider === 'openai'
      ? 'OpenAI API key'
      : newAccountAuthMode === 'api_key'
      ? 'Anthropic API key'
      : 'OAuth token';

  // --- Section renderers ---

  function renderGeneral() {
    return (
      <div className="space-y-6">
        {/* Connection status */}
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Connection Status</h3>
          <div className={`flex items-center gap-5 px-3 py-2.5 rounded-lg border ${!apiHealthy ? 'bg-red-950/20 border-red-900/40' : 'bg-neutral-900 border-neutral-800'}`}>
            <span className="flex items-center gap-2">
              <span className={`w-2 h-2 rounded-full ${apiHealthy ? 'bg-emerald-500' : 'bg-red-500 animate-pulse'}`} />
              <span className={`text-xs ${apiHealthy ? 'text-neutral-300' : 'text-red-400'}`}>{apiHealthy ? 'API Connected' : 'API Error'}</span>
            </span>
            <span className="flex items-center gap-2">
              <span className={`w-2 h-2 rounded-full ${wsConnected ? 'bg-blue-500' : 'bg-neutral-600'}`} />
              <span className={`text-xs ${wsConnected ? 'text-blue-400' : 'text-neutral-500'}`}>{wsConnected ? 'WebSocket Active' : 'WebSocket Idle'}</span>
            </span>
            <span className="text-xs text-neutral-500">Fetched {timeAgo(lastFetchTime)}</span>
          </div>
        </div>

        {/* Defaults */}
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Defaults</h3>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Default Task Type</label>
              <select
                value={defaultTaskType}
                onChange={e => onUpdateDefaultTaskType(e.target.value as UserLaunchableTaskType)}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
              >
                {USER_LAUNCHABLE_TASK_TYPE_OPTIONS.map(option => (
                  <option key={option.value} value={option.value}>{option.label}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Default View Mode</label>
              <select
                value={viewMode}
                onChange={e => onSetViewMode(e.target.value as 'kanban' | 'pipeline')}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
              >
                <option value="kanban">Kanban</option>
                <option value="pipeline">Pipeline</option>
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Default Model</label>
              <select
                value={defaultModel}
                onChange={e => {
                  setDefaultModel(e.target.value);
                  localStorage.setItem('blueprint-admin:defaultModel', JSON.stringify(e.target.value));
                }}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
              >
                <option value="">Orchestrator default</option>
                {allModelOptions.map(model => (
                  <option key={model} value={model}>{model}</option>
                ))}
              </select>
            </div>
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Default Effort</label>
              <select
                value={defaultEffort}
                onChange={e => {
                  setDefaultEffort(e.target.value);
                  localStorage.setItem('blueprint-admin:defaultEffort', JSON.stringify(e.target.value));
                }}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
              >
                <option value="">Orchestrator default</option>
                <option value="low">low</option>
                <option value="medium">medium</option>
                <option value="high">high</option>
                <option value="max">max</option>
              </select>
            </div>
          </div>
        </div>

        {/* UI Preferences */}
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">UI Preferences</h3>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Auto-Refresh Interval</label>
              <div className="flex gap-1 mb-1.5">
                {REFRESH_PRESETS.map(p => (
                  // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
                  <button
                    key={p.value}
                    onClick={() => { onUpdateRefreshInterval(p.value); setRefreshInputVal(''); }}
                    className={`flex-1 text-xs py-1.5 rounded-lg border transition-colors ${refreshInterval === p.value && !refreshInputVal ? 'bg-neutral-600 border-neutral-500 text-neutral-100' : 'bg-neutral-950 border-neutral-700 text-neutral-400 hover:text-neutral-200'}`}
                  >
                    {p.label}
                  </button>
                ))}
              </div>
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={1}
                  placeholder="Custom (s)"
                  value={refreshInputVal}
                  onChange={e => setRefreshInputVal(e.target.value)}
                  onBlur={() => {
                    const secs = parseInt(refreshInputVal);
                    if (secs > 0) onUpdateRefreshInterval(secs * 1000);
                    setRefreshInputVal('');
                  }}
                  onKeyDown={e => {
                    if (e.key === 'Enter') {
                      const secs = parseInt(refreshInputVal);
                      if (secs > 0) onUpdateRefreshInterval(secs * 1000);
                      setRefreshInputVal('');
                    }
                  }}
                  className="flex-1 bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-1.5 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600"
                />
                <span className="text-xs text-neutral-500 shrink-0">
                  {refreshInterval === 0 ? 'off' : `${refreshInterval / 1000}s`}
                </span>
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Tasks Per Page</label>
              <div className="flex gap-1 mb-1.5">
                {PAGE_PRESETS.map(p => (
                  // eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction
                  <button
                    key={p}
                    onClick={() => { onUpdatePageSize(p); setPageSizeInputVal(''); }}
                    className={`flex-1 text-xs py-1.5 rounded-lg border transition-colors ${pageSize === p && !pageSizeInputVal ? 'bg-neutral-600 border-neutral-500 text-neutral-100' : 'bg-neutral-950 border-neutral-700 text-neutral-400 hover:text-neutral-200'}`}
                  >
                    {p}
                  </button>
                ))}
                {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                <button
                  onClick={() => { onUpdatePageSize(ALL_PAGE_SIZE); setPageSizeInputVal(''); }}
                  className={`flex-1 text-xs py-1.5 rounded-lg border transition-colors ${pageSize === ALL_PAGE_SIZE && !pageSizeInputVal ? 'bg-neutral-600 border-neutral-500 text-neutral-100' : 'bg-neutral-950 border-neutral-700 text-neutral-400 hover:text-neutral-200'}`}
                >
                  All
                </button>
              </div>
              <div className="flex items-center gap-2">
                <input
                  type="number"
                  min={1}
                  max={ALL_PAGE_SIZE}
                  placeholder="Custom"
                  value={pageSizeInputVal}
                  onChange={e => setPageSizeInputVal(e.target.value)}
                  onBlur={() => {
                    const n = parseInt(pageSizeInputVal);
                    if (n > 0) onUpdatePageSize(Math.min(n, ALL_PAGE_SIZE));
                    setPageSizeInputVal('');
                  }}
                  onKeyDown={e => {
                    if (e.key === 'Enter') {
                      const n = parseInt(pageSizeInputVal);
                      if (n > 0) onUpdatePageSize(Math.min(n, ALL_PAGE_SIZE));
                      setPageSizeInputVal('');
                    }
                  }}
                  className="flex-1 bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-1.5 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600 placeholder-neutral-600"
                />
                <span className="text-xs text-neutral-500 shrink-0">
                  {pageSize >= ALL_PAGE_SIZE ? 'all' : pageSize}
                </span>
              </div>
            </div>
          </div>
        </div>

        {/* Notifications */}
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Notifications</h3>
          <div className="space-y-3">
            <div className="flex items-center justify-between">
              <span className="text-xs text-neutral-400">Browser notification on task completion</span>
              <Toggle enabled={notifyOnComplete} onChange={v => {
                if (v && 'Notification' in window && Notification.permission === 'default') Notification.requestPermission();
                onUpdateNotifyOnComplete(v);
              }} />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-neutral-400">Browser notification on failure</span>
              <Toggle enabled={notifyOnFail} onChange={v => {
                if (v && 'Notification' in window && Notification.permission === 'default') Notification.requestPermission();
                onUpdateNotifyOnFail(v);
              }} />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-neutral-400">Browser notification on approval needed</span>
              <Toggle enabled={notifyOnApprovalNeeded} onChange={v => {
                if (v && 'Notification' in window && Notification.permission === 'default') Notification.requestPermission();
                onUpdateNotifyOnApprovalNeeded(v);
              }} />
            </div>
            <div className="flex items-center justify-between">
              <span className="text-xs text-neutral-400">Play sound on task completion</span>
              <Toggle enabled={soundOnComplete} onChange={onUpdateSoundOnComplete} />
            </div>
            <p className="text-[11px] text-neutral-600 pt-1">
              These are browser-only notifications. For external webhooks (Slack, PagerDuty, etc.), see the{' '}
              <button
                onClick={() => setActiveSection('notifications')}
                className="text-neutral-400 underline hover:text-neutral-200 transition-colors"
              >
                Notifications tab
              </button>.
            </p>
          </div>
        </div>
      </div>
    );
  }
  function renderOrchestrator() {
    return (
      <div className="space-y-6">
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Pipeline Settings</h3>
          <div className="grid grid-cols-2 gap-4">
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Max Concurrent VMs (0-100)</label>
              <input
                type="number"
                min={0}
                max={100}
                value={vmInputVal}
                onChange={e => setVmInputVal(e.target.value)}
                onBlur={() => { const n = parseInt(vmInputVal); if (n >= 0 && n <= 100) onUpdateMaxVMs(n); else setVmInputVal(String(maxConcurrentVMs)); }}
                onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(vmInputVal); if (n >= 0 && n <= 100) onUpdateMaxVMs(n); else setVmInputVal(String(maxConcurrentVMs)); } }}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Max Retries (0-10)</label>
              <input
                type="number"
                min={0}
                max={10}
                value={retriesInputVal}
                onChange={e => setRetriesInputVal(e.target.value)}
                onBlur={() => { const n = parseInt(retriesInputVal); if (!isNaN(n) && n >= 0 && n <= 10) onUpdateMaxRetries(n); else setRetriesInputVal(String(maxRetries)); }}
                onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(retriesInputVal); if (!isNaN(n) && n >= 0 && n <= 10) onUpdateMaxRetries(n); else setRetriesInputVal(String(maxRetries)); } }}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Max Autonomous Tasks/Hour (1-50)</label>
              <input
                type="number"
                min={1}
                max={50}
                value={autonomousRateInputVal}
                onChange={e => setAutonomousRateInputVal(e.target.value)}
                onBlur={() => { const n = parseInt(autonomousRateInputVal); if (n >= 1 && n <= 50) onUpdateMaxAutonomousRate(n); else setAutonomousRateInputVal(String(maxAutonomousTasksPerHour)); }}
                onKeyDown={e => { if (e.key === 'Enter') { const n = parseInt(autonomousRateInputVal); if (n >= 1 && n <= 50) onUpdateMaxAutonomousRate(n); else setAutonomousRateInputVal(String(maxAutonomousTasksPerHour)); } }}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
            <div className="flex items-end">
              <div className="w-full">
                <label className="block text-xs font-medium text-neutral-400 mb-1.5">Require Approval Gates</label>
                <div className="flex items-center gap-3 bg-neutral-950 border border-neutral-700 rounded-lg px-3 py-2">
                  <Toggle enabled={requireApprovalGates} onChange={onUpdateRequireApprovalGates} />
                  <span className="text-xs text-neutral-400">{requireApprovalGates ? 'Enabled' : 'Disabled'}</span>
                </div>
              </div>
            </div>
          </div>
        </div>

        {/* Auto-Archive */}
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Auto-Archive Terminal Tasks</h3>
          <p className="text-xs text-neutral-500 mb-3">
            Archives tasks when EITHER cap hits: age (days since terminal) or per-project count (keep most-recent N).
          </p>
          <div className="grid grid-cols-4 gap-4">
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Enabled</label>
              <div className="flex items-center gap-3 bg-neutral-950 border border-neutral-700 rounded-lg px-3 py-2">
                <Toggle enabled={autoArchiveEnabled} onChange={onUpdateAutoArchiveEnabled} />
                <span className="text-xs text-neutral-400">{autoArchiveEnabled ? 'On' : 'Off'}</span>
              </div>
            </div>
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Age Cap (1-90 days)</label>
              <input
                type="number"
                min={1}
                max={90}
                value={autoArchiveTerminalAfterDays}
                onChange={e => onUpdateAutoArchiveTerminalAfterDays(parseInt(e.target.value) || 7)}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Per-Project Cap (0=off)</label>
              <input
                type="number"
                min={0}
                max={10000}
                value={autoArchiveMaxTerminalTasksPerProject}
                onChange={e => onUpdateAutoArchiveMaxTerminalTasksPerProject(parseInt(e.target.value) || 0)}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Interval (5-1440 min)</label>
              <input
                type="number"
                min={5}
                max={1440}
                value={autoArchiveIntervalMinutes}
                onChange={e => onUpdateAutoArchiveIntervalMinutes(parseInt(e.target.value) || 60)}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
          </div>
        </div>

        {/* Autonomous Research */}
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Autonomous Research</h3>
          <div className="grid grid-cols-3 gap-4">
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Research Interval (min)</label>
              <input
                type="number"
                min={1}
                max={1440}
                value={autonomousResearchIntervalMinutes}
                onChange={e => onUpdateAutonomousResearchInterval(parseInt(e.target.value) || 30)}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Direction Cooldown (min)</label>
              <input
                type="number"
                min={1}
                max={10080}
                value={directionCooldownMinutes}
                onChange={e => onUpdateDirectionCooldownMinutes(parseInt(e.target.value) || 240)}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
            </div>
            <div>
              <label className="block text-xs font-medium text-neutral-400 mb-1.5">Strategic Freq (0=off)</label>
              <input
                type="number"
                min={0}
                max={100}
                value={strategicResearchFrequency}
                onChange={e => onUpdateStrategicResearchFrequency(parseInt(e.target.value) || 0)}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
              />
              <p className="text-[10px] text-neutral-600 mt-1">Every Nth cycle reviews vision</p>
            </div>
          </div>
          <button
            onClick={onPauseAllAutonomous}
            disabled={!anyAutonomousEnabled}
            className="mt-3 bg-amber-950/30 hover:bg-amber-900/40 disabled:opacity-30 disabled:cursor-not-allowed text-amber-400 text-xs py-2 px-4 rounded-lg transition-colors border border-amber-900/50"
          >
            Pause All Autonomous
          </button>
        </div>

        {/* Restart */}
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Danger Zone</h3>
          <button
            disabled={restarting}
            onClick={async () => {
              if (!window.confirm('Restart the orchestrator? In-flight task state will be preserved.')) return;
              setRestarting(true);
              setRestartStatus('idle');
              try {
                await onRestartOrchestrator();
                setRestartStatus('success');
              } catch {
                setRestartStatus('error');
              } finally {
                setRestarting(false);
              }
            }}
            className="bg-neutral-800 hover:bg-amber-900/30 disabled:opacity-50 border border-neutral-700 text-amber-400 text-xs py-2 px-4 rounded-lg transition-colors flex items-center gap-2"
          >
            {restarting
              ? <><Loader2 className="w-3.5 h-3.5 animate-spin" /> Restarting&hellip;</>
              : <><RotateCcw className="w-3.5 h-3.5" /> Restart Orchestrator</>}
          </button>
          {restartStatus === 'success' && <p className="text-xs text-emerald-400 mt-2">Orchestrator restarted successfully.</p>}
          {restartStatus === 'error' && <p className="text-xs text-red-400 mt-2">Restart failed. Check console for details.</p>}
        </div>
      </div>
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
      <div className="space-y-6">
        {/* User Management (admin) */}
        {isAdmin && (
          <div>
            <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">User Management</h3>
            <button
              onClick={() => { onToggleUserManagement(); onToggleSettings(); }}
              className={`flex items-center gap-2 text-xs py-2 px-4 rounded-lg transition-colors border ${
                showUserManagement
                  ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-400'
                  : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'
              }`}
            >
              <Users className="w-3.5 h-3.5" />
              {showUserManagement ? 'Close User Management' : 'Open User Management'}
            </button>
          </div>
        )}

        {/* Project Members */}
        {selectedProjectId && (selectedProjectMyRole === 'owner' || isAdmin) && (
          <div>
            <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Project Members</h3>
            <button
              data-testid="toggle-project-members"
              onClick={() => { onToggleProjectMembers(); onToggleSettings(); }}
              className={`flex items-center gap-2 text-xs py-2 px-4 rounded-lg transition-colors border ${
                showProjectMembers
                  ? 'bg-cyan-500/10 border-cyan-500/30 text-cyan-400'
                  : 'bg-neutral-800 hover:bg-neutral-700 text-neutral-300 border-neutral-700'
              }`}
            >
              <Users className="w-3.5 h-3.5" />
              {showProjectMembers ? 'Close Project Members' : 'Manage Project Members'}
            </button>
          </div>
        )}

        {/* GitHub */}
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">GitHub</h3>
          {githubAccount ? (
            <div className="flex items-center gap-2 bg-neutral-900 rounded-lg p-3 border border-neutral-800">
              <span className="w-2 h-2 rounded-full bg-emerald-500 shrink-0" />
              <span className="text-sm text-neutral-200">Connected: <span className="font-mono">{githubAccount}</span></span>
            </div>
          ) : (
            <button onClick={onConnectGitHub}
              className="bg-neutral-800 hover:bg-neutral-700 text-neutral-300 text-xs py-2 px-4 rounded-lg transition-colors border border-neutral-700 flex items-center gap-2">
              <GitBranch className="w-3.5 h-3.5" /> Connect GitHub
            </button>
          )}
        </div>

        {/* LLM Accounts */}
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">LLM Accounts</h3>
          {maxPlanAccounts.length > 0 && (() => {
            const total = maxPlanAccounts.length;
            const available = maxPlanAccounts.filter(a => a.enabled && !Object.keys(a.disabled_models || {}).length).length;
            const warn = available < total;
            return (
              <div className={`flex items-center justify-between text-xs mb-3 px-3 py-2 rounded-lg border ${warn ? 'bg-amber-950/30 border-amber-900/50 text-amber-400' : 'bg-neutral-900 border-neutral-800 text-neutral-400'}`}>
                <span>Pool: <span className={warn ? 'text-amber-300 font-medium' : 'text-neutral-200 font-medium'}>{available}/{total}</span> available</span>
                {warn && <span className="text-amber-500 text-[11px]">degraded</span>}
              </div>
            );
          })()}
          {maxPlanAccounts.length === 0 && (
            <p className="text-xs text-neutral-600 mb-3">No accounts &mdash; using API key fallback</p>
          )}
          <div className="space-y-2">
            {maxPlanAccounts.map(a => {
              let tokenAge: string | null = null;
              let isExpiring = false;
              if (a.token_created_at) {
                const created = parseTimestamp(a.token_created_at);
                const diffMs = new Date().getTime() - created.getTime();
                const days = Math.floor(diffMs / 86400000);
                isExpiring = days > 300;
                if (days < 1) tokenAge = 'today';
                else if (days < 30) tokenAge = `${days}d old`;
                else tokenAge = `${Math.floor(days / 30)}mo old`;
              }
              const disabledModels = Object.entries(a.disabled_models || {});
              const isRateLimited = disabledModels.length > 0;
              const isCapExpanded = expandedAccountCaps === a.id;
              const dailyCount = a.daily_task_count ?? 0;
              const dailyCap = a.daily_task_cap ?? null;
              const concurrentCap = a.max_concurrent_tasks ?? null;
              const autoDisableAfter = a.auto_disable_after_rate_limits ?? null;
              return (
                <div key={a.id} className={`bg-neutral-900 rounded-lg p-3 border ${isRateLimited ? 'border-amber-900/60' : a.disabled_reason ? 'border-red-900/60' : 'border-neutral-800'}`}>
                  <div className="flex items-center gap-3">
                    <button onClick={() => onToggleAccount(a.id, !a.enabled)}
                      className={`w-2.5 h-2.5 rounded-full shrink-0 ${isRateLimited ? 'bg-amber-500' : a.enabled ? 'bg-emerald-500' : 'bg-neutral-600'}`}
                      title={isRateLimited ? 'Rate-limited' : a.enabled ? 'Enabled (click to disable)' : 'Disabled (click to enable)'} />
                    <div className="flex-1 min-w-0">
                      <div className="flex items-center gap-2 min-w-0 flex-wrap">
                        <div className="text-sm text-neutral-200 truncate">{a.name}</div>
                        <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-400 border border-neutral-700">{a.provider}</span>
                        <span className="text-[10px] uppercase tracking-wide px-1.5 py-0.5 rounded bg-neutral-800 text-neutral-500 border border-neutral-700">{a.auth_mode}</span>
                        {(dailyCap !== null || concurrentCap !== null || autoDisableAfter !== null) && (
                          <span className="text-[10px] px-1.5 py-0.5 rounded bg-cyan-950/40 text-cyan-400 border border-cyan-900/50" title="Usage caps configured">
                            {dailyCap !== null && <>{dailyCount}/{dailyCap} today</>}
                            {dailyCap !== null && concurrentCap !== null && <> &middot; </>}
                            {concurrentCap !== null && <>max {concurrentCap}</>}
                          </span>
                        )}
                      </div>
                      <div className="text-[11px] text-neutral-500 font-mono">
                        {a.active_tasks} active
                        {tokenAge && <span className={`ml-2 ${isExpiring ? 'text-amber-400' : ''}`}>&middot; token {tokenAge}</span>}
                        {a.provider_metadata?.project && typeof a.provider_metadata.project === 'string' && <span className="ml-2">&middot; {a.provider_metadata.project}</span>}
                        {a.provider_metadata?.location && typeof a.provider_metadata.location === 'string' && <span className="ml-2">@ {a.provider_metadata.location}</span>}
                      </div>
                      {accountSupportsAvailabilityCheck(a) && (
                        <AccountAvailabilityLine
                          account={a}
                          onRefresh={() => onRefreshAccountModels(a.id)}
                        />
                      )}
                    </div>
                    {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                    <button
                      onClick={() => {
                        if (isCapExpanded) { setExpandedAccountCaps(null); return; }
                        setExpandedAccountCaps(a.id);
                        setCapDraft({
                          daily_task_cap: dailyCap !== null ? String(dailyCap) : '',
                          max_concurrent_tasks: concurrentCap !== null ? String(concurrentCap) : '',
                          auto_disable_after_rate_limits: autoDisableAfter !== null ? String(autoDisableAfter) : '',
                        });
                      }}
                      title={isCapExpanded ? 'Close caps editor' : 'Edit usage caps'}
                      className="text-neutral-600 hover:text-neutral-300 transition-colors shrink-0 p-1"
                    >
                      <Sliders className="w-3.5 h-3.5" />
                    </button>
                    <button onClick={() => onRemoveAccount(a.id)}
                      className="text-neutral-600 hover:text-red-400 text-sm transition-colors shrink-0">&times;</button>
                  </div>
                  {a.disabled_reason && (
                    <div className="mt-2 pt-2 border-t border-red-900/40 text-[11px] text-red-400 flex items-center justify-between gap-2">
                      <span>&#9888; {a.disabled_reason}</span>
                      <button onClick={() => onToggleAccount(a.id, true)} className="text-red-300 hover:text-red-200 underline">Re-enable</button>
                    </div>
                  )}
                  {isRateLimited && (
                    <div className="mt-2 pt-2 border-t border-neutral-800/50">
                      {disabledModels.map(([model, info]) => {
                        const cooldownMin = Math.min(30 * Math.pow(2, (info.hits - 1)), 240);
                        const disabledAt = info.at ? parseTimestamp(info.at) : null;
                        const recoveryAt = disabledAt ? new Date(disabledAt.getTime() + cooldownMin * 60000) : null;
                        const remaining = recoveryAt ? Math.max(0, Math.round((recoveryAt.getTime() - Date.now()) / 60000)) : null;
                        const shortModel = model.replace('claude-', '').replace('-4-6', '');
                        return (
                          <div key={model} className="flex items-center justify-between text-[11px] text-amber-400/80">
                            <span className="font-mono">{shortModel} <span className="text-amber-500/60">hit #{info.hits}</span></span>
                            <span className="text-amber-500/60">{remaining !== null && remaining > 0 ? `${remaining}m left` : 'recovering'}</span>
                          </div>
                        );
                      })}
                      {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                      <button onClick={() => onResetRateLimits(a.id)}
                        className="mt-1.5 w-full text-[11px] text-amber-400/70 hover:text-amber-300 bg-amber-950/30 hover:bg-amber-950/50 rounded-lg px-2 py-1 transition-colors">
                        Clear rate limits
                      </button>
                    </div>
                  )}
                  {isCapExpanded && (
                    <div className="mt-2 pt-2 border-t border-neutral-800/50 space-y-2">
                      <div className="text-[10px] uppercase tracking-wider text-neutral-500">Usage caps</div>
                      <div className="grid grid-cols-3 gap-2">
                        <label className="block">
                          <div className="text-[10px] text-neutral-500 mb-1">Tasks/day</div>
                          <input
                            type="number" min={1}
                            value={capDraft.daily_task_cap}
                            placeholder="unlimited"
                            onChange={e => setCapDraft({ ...capDraft, daily_task_cap: e.target.value })}
                            className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                          />
                        </label>
                        <label className="block">
                          <div className="text-[10px] text-neutral-500 mb-1">Max concurrent</div>
                          <input
                            type="number" min={1}
                            value={capDraft.max_concurrent_tasks}
                            placeholder="unlimited"
                            onChange={e => setCapDraft({ ...capDraft, max_concurrent_tasks: e.target.value })}
                            className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                          />
                        </label>
                        <label className="block">
                          <div className="text-[10px] text-neutral-500 mb-1">Auto-disable after N RL</div>
                          <input
                            type="number" min={1}
                            value={capDraft.auto_disable_after_rate_limits}
                            placeholder="off"
                            onChange={e => setCapDraft({ ...capDraft, auto_disable_after_rate_limits: e.target.value })}
                            className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                          />
                        </label>
                      </div>
                      <div className="flex items-center gap-2">
                        <button
                          onClick={() => {
                            const parse = (v: string): number | null => {
                              const trimmed = v.trim();
                              if (!trimmed) return null;
                              const n = parseInt(trimmed, 10);
                              return Number.isFinite(n) && n > 0 ? n : null;
                            };
                            onUpdateAccountCaps(a.id, {
                              daily_task_cap: parse(capDraft.daily_task_cap),
                              max_concurrent_tasks: parse(capDraft.max_concurrent_tasks),
                              auto_disable_after_rate_limits: parse(capDraft.auto_disable_after_rate_limits),
                            });
                            setExpandedAccountCaps(null);
                          }}
                          className="bg-neutral-800 hover:bg-neutral-700 text-neutral-200 text-[11px] py-1 px-3 rounded border border-neutral-700"
                        >
                          Save caps
                        </button>
                        <button
                          onClick={() => setExpandedAccountCaps(null)}
                          className="text-[11px] text-neutral-500 hover:text-neutral-300"
                        >
                          Cancel
                        </button>
                        <span className="text-[10px] text-neutral-600 ml-auto">Leave a field empty for unlimited/off.</span>
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          {!showAddAccountForm ? (
            <button
              onClick={() => setShowAddAccountForm(true)}
              className="mt-4 w-full flex items-center justify-center gap-2 bg-neutral-900 hover:bg-neutral-800 border border-dashed border-neutral-700 text-neutral-400 text-xs py-2.5 px-4 rounded-lg transition-colors"
            >
              <Plus className="w-3.5 h-3.5" /> Add account
            </button>
          ) : (
            <div className="mt-4 bg-neutral-900 border border-neutral-800 rounded-lg p-4 space-y-3">
              <div className="flex items-center justify-between">
                <div className="text-xs font-semibold text-neutral-200 uppercase tracking-wider">New account</div>
                <button
                  onClick={() => setShowAddAccountForm(false)}
                  className="text-neutral-600 hover:text-neutral-300 transition-colors"
                  title="Cancel"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
              <div className="grid grid-cols-2 gap-2">
                <div>
                  <div className="text-[10px] text-neutral-500 mb-1 uppercase">Provider</div>
                  <select
                    value={newAccountProvider}
                    onChange={e => setNewAccountProvider(e.target.value as ProviderName)}
                    className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                  >
                    {providerSelectOptions.map(providerName => (
                      <option key={providerName} value={providerName}>{providerName}</option>
                    ))}
                  </select>
                </div>
                <div>
                  <div className="text-[10px] text-neutral-500 mb-1 uppercase">Auth mode</div>
                  <select
                    value={newAccountAuthMode}
                    onChange={e => setNewAccountAuthMode(e.target.value as AuthMode)}
                    className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                  >
                    {newAccountAuthModes.map(mode => (
                      <option key={mode} value={mode}>{mode}</option>
                    ))}
                  </select>
                </div>
              </div>
              {(() => {
                const guide = CREDENTIAL_GUIDES[`${newAccountProvider}:${newAccountAuthMode}`];
                if (!guide) return null;
                return (
                  <div className="bg-neutral-950 border border-neutral-800 rounded-lg p-3 flex gap-2">
                    <HelpCircle className="w-4 h-4 text-cyan-400/80 shrink-0 mt-0.5" />
                    <div className="min-w-0 flex-1">
                      <div className="text-xs text-neutral-300">{guide.summary}</div>
                      <ol className="list-decimal list-inside mt-1.5 space-y-0.5 text-[11px] text-neutral-400">
                        {guide.steps.map((step, i) => (
                          <li key={i}><span className="[&_code]:text-cyan-300 [&_code]:bg-neutral-900 [&_code]:px-1 [&_code]:rounded" dangerouslySetInnerHTML={{ __html: step.replace(/`([^`]+)`/g, '<code>$1</code>') }} /></li>
                        ))}
                      </ol>
                    </div>
                  </div>
                );
              })()}
              <div>
                <div className="text-[10px] text-neutral-500 mb-1 uppercase">Name</div>
                <input
                  value={newAccountName}
                  onChange={e => setNewAccountName(e.target.value)}
                  placeholder="e.g. my-claude-max"
                  className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                />
              </div>
              {requiredMetadataFields.length > 0 && (
                <div className="grid grid-cols-2 gap-2">
                  {requiredMetadataFields.map(field => {
                    const hint = METADATA_HINTS[field];
                    return (
                      <div key={field}>
                        <div className="text-[10px] text-neutral-500 mb-1 uppercase">{field}</div>
                        <input
                          value={newAccountProviderMetadata[field] ?? ''}
                          onChange={e => setNewAccountProviderMetadata({ ...newAccountProviderMetadata, [field]: e.target.value })}
                          placeholder={hint?.placeholder ?? field}
                          className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
                        />
                        {hint && <div className="text-[10px] text-neutral-600 mt-0.5">{hint.label}</div>}
                      </div>
                    );
                  })}
                </div>
              )}
              <div>
                <div className="text-[10px] text-neutral-500 mb-1 uppercase">{newAccountSecretLabel}</div>
                {((newAccountProvider === 'openai' && newAccountAuthMode === 'oauth') || (newAccountProvider === 'gemini' && newAccountAuthMode === 'service_account')) ? (
                  <div className="space-y-1.5">
                    <textarea
                      value={newAccountToken}
                      onChange={e => setNewAccountToken(e.target.value)}
                      rows={4}
                      placeholder={`Paste ${newAccountSecretLabel} or upload a .json file below`}
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600 resize-none"
                    />
                    <label className="flex items-center gap-2 text-[11px] text-neutral-500 hover:text-neutral-300 cursor-pointer transition-colors">
                      <span className="inline-flex items-center gap-1 bg-neutral-800 border border-neutral-700 rounded px-2 py-1 hover:bg-neutral-700">
                        <FileDown className="w-3 h-3" /> Upload .json file
                      </span>
                      <input
                        type="file"
                        accept=".json,application/json"
                        className="hidden"
                        onChange={e => {
                          const file = e.target.files?.[0];
                          if (!file) return;
                          const reader = new FileReader();
                          reader.onload = ev => {
                            const text = String(ev.target?.result ?? '');
                            setNewAccountToken(text);
                            if (!newAccountName) setNewAccountName(file.name.replace(/\.json$/i, ''));
                          };
                          reader.readAsText(file);
                          e.target.value = '';
                        }}
                      />
                    </label>
                  </div>
                ) : (
                  <input
                    value={newAccountToken}
                    onChange={e => setNewAccountToken(e.target.value)}
                    placeholder={newAccountSecretLabel}
                    type="password"
                    className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600 font-mono"
                  />
                )}
              </div>
              {newAccountProvider === 'anthropic' && newAccountAuthMode === 'oauth' && (
                <div>
                  <div className="text-[10px] text-neutral-500 mb-1 uppercase flex items-center gap-1">
                    claude_config_json
                    <span className="text-neutral-600" title="Optional. Contents of ~/.claude.json — lets the agent skip onboarding screens on first run. Not required for most setups.">
                      <HelpCircle className="w-3 h-3" />
                    </span>
                    <span className="text-[10px] text-neutral-600 normal-case">(optional)</span>
                  </div>
                  <textarea
                    value={newAccountClaudeConfigJson}
                    onChange={e => setNewAccountClaudeConfigJson(e.target.value)}
                    rows={2}
                    placeholder='{"onboardingAccepted": true}'
                    className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600 resize-none"
                  />
                </div>
              )}
              <button
                onClick={() => setShowAddAccountCaps(v => !v)}
                className="flex items-center gap-1.5 text-[11px] text-neutral-500 hover:text-neutral-300 transition-colors"
              >
                {showAddAccountCaps ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
                Usage limits (optional)
              </button>
              {showAddAccountCaps && (
                <div className="grid grid-cols-3 gap-2 bg-neutral-950 border border-neutral-800 rounded-lg p-3">
                  <label className="block">
                    <div className="text-[10px] text-neutral-500 mb-1 flex items-center gap-1">
                      Tasks/day
                      <span title="Hard cap on tasks dispatched to this account per rolling 24h window. Empty = unlimited.">
                        <HelpCircle className="w-3 h-3 text-neutral-600" />
                      </span>
                    </div>
                    <input
                      type="number" min={1}
                      value={newAccountDailyTaskCap ?? ''}
                      onChange={e => setNewAccountDailyTaskCap(e.target.value ? parseInt(e.target.value, 10) : null)}
                      placeholder="unlimited"
                      className="w-full bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                  </label>
                  <label className="block">
                    <div className="text-[10px] text-neutral-500 mb-1 flex items-center gap-1">
                      Max concurrent
                      <span title="Maximum number of tasks running on this account at once. Empty = unlimited.">
                        <HelpCircle className="w-3 h-3 text-neutral-600" />
                      </span>
                    </div>
                    <input
                      type="number" min={1}
                      value={newAccountMaxConcurrentTasks ?? ''}
                      onChange={e => setNewAccountMaxConcurrentTasks(e.target.value ? parseInt(e.target.value, 10) : null)}
                      placeholder="unlimited"
                      className="w-full bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                  </label>
                  <label className="block">
                    <div className="text-[10px] text-neutral-500 mb-1 flex items-center gap-1">
                      Auto-disable
                      <span title="Disable the account (needs manual re-enable) after this many consecutive rate-limit hits. Empty = off.">
                        <HelpCircle className="w-3 h-3 text-neutral-600" />
                      </span>
                    </div>
                    <input
                      type="number" min={1}
                      value={newAccountAutoDisableAfterRateLimits ?? ''}
                      onChange={e => setNewAccountAutoDisableAfterRateLimits(e.target.value ? parseInt(e.target.value, 10) : null)}
                      placeholder="off"
                      className="w-full bg-neutral-900 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    />
                  </label>
                </div>
              )}
              {(() => {
                const missingMeta = requiredMetadataFields.filter(f => !(newAccountProviderMetadata[f] ?? '').trim());
                const missing: string[] = [];
                if (!newAccountName) missing.push('name');
                if (!newAccountToken) missing.push(newAccountSecretLabel.toLowerCase());
                if (missingMeta.length) missing.push(...missingMeta);
                const disabled = missing.length > 0;
                return (
                  <div className="flex items-center gap-2 pt-1">
                    <button
                      onClick={onAddAccount}
                      disabled={disabled}
                      title={disabled ? `Missing: ${missing.join(', ')}` : undefined}
                      className="bg-emerald-900/40 hover:bg-emerald-800/50 disabled:opacity-30 disabled:cursor-not-allowed text-emerald-300 text-xs py-2 px-4 rounded-lg transition-colors border border-emerald-900/60"
                    >
                      Add Account
                    </button>
                    <button
                      onClick={() => setShowAddAccountForm(false)}
                      className="text-xs text-neutral-500 hover:text-neutral-300 transition-colors"
                    >
                      Cancel
                    </button>
                    {disabled && (
                      <span className="text-[11px] text-neutral-600 ml-auto">Missing: {missing.join(', ')}</span>
                    )}
                  </div>
                );
              })()}
            </div>
          )}
        </div>
      </div>
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
      case 'advanced': return renderAdvanced();
    }
  }

  return (
    <div className="relative" ref={settingsRef}>
      {/* reason: dense-UI icon trigger (p-2 around 16px icon) — operator-density convention for settings panels */}
      {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
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
          {/* reason: desktop-class settings modal (sidebar + content layout, dense controls) — operator-density convention. Opts the entire panel out of viewport assertions; the SettingsDropdown.viewport.test.tsx still validates the trigger and ensures the panel renders without crashing at 375px. */}
          <div
            data-viewport-allow-overflow
            data-viewport-allow-small-target
            // eslint-disable-next-line viewport/no-fixed-width -- intentional modal, constrained by max-w-[95vw] on mobile
            className="bg-neutral-950 border border-neutral-700 rounded-xl shadow-2xl flex w-[900px] max-w-[95vw] h-[80vh] max-h-[700px] overflow-hidden"
            onClick={e => e.stopPropagation()}
          >
            {/* Sidebar */}
            <nav className="w-48 shrink-0 bg-neutral-900/50 border-r border-neutral-800 flex flex-col">
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
              <div className="flex items-center justify-between px-6 py-4 border-b border-neutral-800">
                <h3 className="text-sm font-semibold text-neutral-100">
                  {visibleSections.find(s => s.id === activeSection)?.label}
                </h3>
                {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
                <button
                  onClick={onToggleSettings}
                  className="text-neutral-500 hover:text-neutral-300 transition-colors p-1 rounded hover:bg-neutral-800"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
              <div className="flex-1 overflow-y-auto px-6 py-5 custom-scrollbar">
                {renderActiveSection()}
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}


// Model-availability line for account list rows.
//
// Shows how many models this specific API key can see, split GA vs preview
// (Gemini only — OpenAI doesn't have a stable preview/GA distinction we can
// label without guessing). Surfaces the refresh timestamp + any cached
// error string from the last probe, plus a manual refresh icon.
//
// Only rendered for providers/modes where list_available_models is wired
// in the backend — gating via `accountSupportsAvailabilityCheck`.

// Keep in sync with backend/app/providers/gemini.py::_GEMINI_PREVIEW_MODELS.
// Frontend-duplicated because rendering the GA/preview split shouldn't
// require a network round trip to the backend on every settings-panel open.
const GEMINI_PREVIEW_MODEL_IDS = new Set<string>([
  'gemini-3.1-pro-preview',
  'gemini-3-flash-preview',
  'gemini-3.1-flash-lite-preview',
]);

function accountSupportsAvailabilityCheck(account: MaxPlanAccount): boolean {
  // Backend only implements list_available_models for API-key auth on
  // Gemini + OpenAI today (Anthropic doesn't gate models per key; Gemini
  // service-account needs OAuth2 token minting not yet wired).
  if (account.auth_mode !== 'api_key') return false;
  return account.provider === 'gemini' || account.provider === 'openai';
}

function AccountAvailabilityLine({
  account,
  onRefresh,
}: {
  account: MaxPlanAccount;
  onRefresh: () => void;
}) {
  const metadata = account.provider_metadata || {};
  const models = Array.isArray(metadata.available_models) ? metadata.available_models : null;
  const refreshedAtRaw = typeof metadata.available_models_refreshed_at === 'string'
    ? metadata.available_models_refreshed_at
    : null;
  const error = typeof metadata.available_models_error === 'string'
    ? metadata.available_models_error
    : null;

  const refreshedAt = refreshedAtRaw ? parseTimestamp(refreshedAtRaw) : null;
  const refreshedAgo = refreshedAt ? timeAgo(refreshedAt) : null;

  let body: React.ReactNode;
  if (models === null && !error) {
    body = <span className="text-neutral-600 italic">availability unchecked</span>;
  } else if (models === null && error) {
    body = <span className="text-amber-500/70" title={error}>check failed &mdash; {error.length > 60 ? error.slice(0, 60) + '...' : error}</span>;
  } else if (models !== null) {
    const isGemini = account.provider === 'gemini';
    const preview = isGemini ? models.filter(m => GEMINI_PREVIEW_MODEL_IDS.has(m)) : [];
    const modelTitle = models.length > 0 ? models.join(', ') : '(none)';
    body = (
      <span title={modelTitle}>
        <span className="text-neutral-400">{models.length} model{models.length === 1 ? '' : 's'} visible</span>
        {isGemini && preview.length > 0 && (
          <span className="ml-1.5 text-violet-400" title={`Preview-gated models visible on this key: ${preview.join(', ')}`}>
            &middot; {preview.length} preview model{preview.length === 1 ? '' : 's'}
          </span>
        )}
        {isGemini && models.length > 0 && preview.length === 0 && (
          <span className="ml-1.5 text-neutral-600" title="No preview models visible — preview requests will fall back to GA counterparts">
            &middot; no preview access
          </span>
        )}
        {refreshedAgo && <span className="ml-1.5 text-neutral-600">&middot; {refreshedAgo}</span>}
      </span>
    );
  }

  return (
    <div className="text-[11px] font-mono flex items-center gap-1.5 mt-0.5">
      {body}
      {/* eslint-disable-next-line viewport/min-touch-target -- pre-existing, dense UI; audit per rule introduction */}
      <button
        type="button"
        onClick={onRefresh}
        title="Re-check which models this key can see"
        className="text-neutral-600 hover:text-neutral-300 transition-colors p-0.5 rounded shrink-0"
      >
        <RefreshCw className="w-3 h-3" />
      </button>
    </div>
  );
}
