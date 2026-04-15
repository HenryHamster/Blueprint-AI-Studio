import { useEffect, useState } from 'react';
import { SettingsAdvancedSection } from './SettingsAdvancedSection';
import { SettingsNotificationsSection } from './SettingsNotificationsSection';
import type { NotificationConfig } from './SettingsNotificationsSection';
import { ProjectBuildConfigSection } from './ProjectBuildConfigSection';
import { ProjectAutoRemediationSection } from './ProjectAutoRemediationSection';
import { ProjectIssueImplementationSection } from './ProjectIssueImplementationSection';
import type { BuildConfig, AutoRemediationConfig, IssueImplementationConfig } from '../types/project';
import { Settings, GitBranch, RotateCcw, Loader2, Shield, X, Sliders, ListChecks, Server, User, FileDown, LogOut, Users, Bell, Wrench, Tag } from 'lucide-react';
import { timeAgo, parseTimestamp } from '../utils/time';
export type { MaxPlanAccount } from '../types/settings';
import type { DeployState, MaxPlanAccount } from '../types/settings';
import { DeploySection } from './SettingsDeploySection';


interface SettingsDropdownProps {
  showSettings: boolean;
  onToggleSettings: () => void;
  settingsRef: React.RefObject<HTMLDivElement | null>;
  // Defaults
  defaultTaskType: 'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor';
  onUpdateDefaultTaskType: (value: 'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor') => void;
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
  maxAutonomousTasksPerHour: number;
  onUpdateMaxAutonomousRate: (value: number) => void;
  autonomousResearchIntervalMinutes: number;
  onUpdateAutonomousResearchInterval: (value: number) => void;
  directionCooldownMinutes: number;
  onUpdateDirectionCooldownMinutes: (value: number) => void;
  strategicResearchFrequency: number;
  onUpdateStrategicResearchFrequency: (value: number) => void;
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
  onResetRateLimits: (id: string) => void;
  newAccountName: string;
  setNewAccountName: (value: string) => void;
  newAccountToken: string;
  setNewAccountToken: (value: string) => void;
  onAddAccount: () => void;
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
  // Optional class name for the trigger button (e.g. to hide it on mobile)
  triggerClassName?: string;
  userRole?: 'admin' | 'user' | null;
  // User / session
  userEmail: string;
  onSignOut: () => void;
  showUserManagement: boolean;
  onToggleUserManagement: () => void;
  // Build config (project-scoped)
  selectedProjectId: string | null;
  selectedProjectOwner: string | null;
  selectedProjectRepo: string | null;
  buildConfig: BuildConfig | null;
  onUpdateBuildConfig: (config: BuildConfig) => Promise<void>;
  onDetectBuildConfig: () => Promise<void>;
  // Auto-remediation config (project-scoped)
  autoRemediationConfig: AutoRemediationConfig | null;
  onUpdateAutoRemediationConfig: (config: AutoRemediationConfig) => Promise<void>;
  // Issue implementation config (project-scoped)
  issueImplementationConfig: IssueImplementationConfig | null;
  onUpdateIssueImplementationConfig: (config: IssueImplementationConfig) => Promise<void>;
}


const REFRESH_PRESETS = [
  { label: '10s', value: 10000 },
  { label: '30s', value: 30000 },
  { label: '60s', value: 60000 },
  { label: 'Off', value: 0 },
];

const PAGE_PRESETS = [25, 50, 100, 200];
const ALL_PAGE_SIZE = 10000;

const TASK_TYPES = ['create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor', 'security_audit', 'qa', 'deps', 'docs', 'smoketest', 'debug', 'perf', 'investigate', 'rollup', 'strategic_review', 'design_doc_update'] as const;
type TaskType = typeof TASK_TYPES[number];

const ALL_PROMPT_SUFFIX_TYPES = [
  'create', 'review', 'fix', 'plan', 'research', 'security_audit', 'qa',
  'docs', 'deps', 'smoketest', 'debug', 'perf', 'refactor', 'migrate',
  'investigate', 'rollup', 'strategic_review', 'design_doc_update',
] as const;

type SectionId = 'general' | 'task-types' | 'orchestrator' | 'deploy' | 'accounts' | 'notifications' | 'advanced' | 'build-config' | 'auto-remediation' | 'issue-implementation';

const SECTIONS: { id: SectionId; label: string; icon: typeof Settings; adminOnly?: boolean; projectOnly?: boolean }[] = [
  { id: 'general', label: 'General', icon: Sliders },
  { id: 'task-types', label: 'Task Types', icon: ListChecks },
  { id: 'orchestrator', label: 'Orchestrator', icon: Server, adminOnly: true },
  { id: 'deploy', label: 'Deploy & Health', icon: Shield, adminOnly: true },
  { id: 'accounts', label: 'Accounts', icon: User },
  { id: 'notifications', label: 'Notifications', icon: Bell },
  { id: 'build-config', label: 'Build Config', icon: Wrench, projectOnly: true },
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
  maxAutonomousTasksPerHour,
  onUpdateMaxAutonomousRate,
  autonomousResearchIntervalMinutes,
  onUpdateAutonomousResearchInterval,
  directionCooldownMinutes,
  onUpdateDirectionCooldownMinutes,
  strategicResearchFrequency,
  onUpdateStrategicResearchFrequency,
  onSyncTaskTypeDefaults,
  healthCheckUrls,
  onUpdateHealthCheckUrls,
  agentPromptSuffixes,
  onUpdateAgentPromptSuffixes,
  githubAccount,
  onConnectGitHub,
  maxPlanAccounts,
  onToggleAccount,
  onRemoveAccount,
  onResetRateLimits,
  newAccountName,
  setNewAccountName,
  newAccountToken,
  setNewAccountToken,
  onAddAccount,
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
  triggerClassName,
  userRole,
  userEmail,
  onSignOut,
  showUserManagement,
  onToggleUserManagement,
  selectedProjectId,
  selectedProjectOwner,
  selectedProjectRepo,
  buildConfig,
  onUpdateBuildConfig,
  onDetectBuildConfig,
  autoRemediationConfig,
  onUpdateAutoRemediationConfig,
  issueImplementationConfig,
  onUpdateIssueImplementationConfig,
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
  const [expandedSuffixType, setExpandedSuffixType] = useState<string | null>(null);
  const [suffixDraft, setSuffixDraft] = useState('');

  // Global model/effort defaults
  const [defaultModel, setDefaultModel] = useState<string>(
    () => JSON.parse(localStorage.getItem('blueprint-admin:defaultModel') ?? 'null') ?? ''
  );
  const [defaultEffort, setDefaultEffort] = useState<string>(
    () => JSON.parse(localStorage.getItem('blueprint-admin:defaultEffort') ?? 'null') ?? ''
  );


  // Per-task-type overrides
  const [taskTypeModels, setTaskTypeModels] = useState<Record<TaskType, string>>(() => {
    const result = {} as Record<TaskType, string>;
    for (const t of ['create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor'] as TaskType[]) {
      result[t] = JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeModel:${t}`) ?? 'null') ?? '';
    }
    return result;
  });
  const [taskTypeEfforts, setTaskTypeEfforts] = useState<Record<TaskType, string>>(() => {
    const result = {} as Record<TaskType, string>;
    for (const t of ['create', 'plan', 'research', 'review', 'fix', 'migrate', 'refactor'] as TaskType[]) {
      result[t] = JSON.parse(localStorage.getItem(`blueprint-admin:taskTypeEffort:${t}`) ?? 'null') ?? '';
    }
    return result;
  });
  const [healthCheckUrlsInput, setHealthCheckUrlsInput] = useState(() => healthCheckUrls.join('\n'));

  useEffect(() => {
    const id = setInterval(() => setTick(t => t + 1), 1000);
    return () => clearInterval(id);
  }, []);
  useEffect(() => { setVmInputVal(String(maxConcurrentVMs)); }, [maxConcurrentVMs]);
  useEffect(() => { setRetriesInputVal(String(maxRetries)); }, [maxRetries]);
  useEffect(() => { setAutonomousRateInputVal(String(maxAutonomousTasksPerHour)); }, [maxAutonomousTasksPerHour]);
  useEffect(() => { setHealthCheckUrlsInput(healthCheckUrls.join('\n')); }, [healthCheckUrls]);

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
                onChange={e => onUpdateDefaultTaskType(e.target.value as 'create' | 'plan' | 'research' | 'review' | 'fix' | 'migrate' | 'refactor')}
                className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600"
              >
                <option value="create">Create</option>
                <option value="refactor">Refactor</option>
                <option value="plan">Plan</option>
                <option value="research">Research</option>
                <option value="review">Review</option>
                <option value="fix">Fix</option>
                <option value="migrate">Migrate</option>
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
                <option value="claude-opus-4-6">claude-opus-4-6</option>
                <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
                <option value="claude-haiku-4-5-20251001">claude-haiku-4-5-20251001</option>
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
                  <button
                    key={p}
                    onClick={() => { onUpdatePageSize(p); setPageSizeInputVal(''); }}
                    className={`flex-1 text-xs py-1.5 rounded-lg border transition-colors ${pageSize === p && !pageSizeInputVal ? 'bg-neutral-600 border-neutral-500 text-neutral-100' : 'bg-neutral-950 border-neutral-700 text-neutral-400 hover:text-neutral-200'}`}
                  >
                    {p}
                  </button>
                ))}
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
                  placeholder="Custom"
                  value={pageSizeInputVal}
                  onChange={e => setPageSizeInputVal(e.target.value)}
                  onBlur={() => {
                    const n = parseInt(pageSizeInputVal);
                    if (n > 0) onUpdatePageSize(n);
                    setPageSizeInputVal('');
                  }}
                  onKeyDown={e => {
                    if (e.key === 'Enter') {
                      const n = parseInt(pageSizeInputVal);
                      if (n > 0) onUpdatePageSize(n);
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

  function renderTaskTypes() {
    return (
      <div className="space-y-6">
        {/* Per-task-type overrides */}
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Model & Effort Overrides</h3>
          <div className="grid grid-cols-2 gap-3">
            {TASK_TYPES.map(type => (
              <div key={type} className="bg-neutral-900 border border-neutral-800 rounded-lg px-3 py-2.5">
                <div className="text-xs font-medium text-neutral-300 capitalize mb-2">{type.replace('_', ' ')}</div>
                <div className="grid grid-cols-2 gap-2">
                  <div>
                    <div className="text-[10px] text-neutral-500 mb-1">Model</div>
                    <select
                      value={taskTypeModels[type]}
                      onChange={e => {
                        const val = e.target.value;
                        setTaskTypeModels(prev => ({ ...prev, [type]: val }));
                        localStorage.setItem(`blueprint-admin:taskTypeModel:${type}`, JSON.stringify(val));
                        setTimeout(onSyncTaskTypeDefaults, 0);
                      }}
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    >
                      <option value="">default</option>
                      <option value="claude-opus-4-6">opus-4-6</option>
                      <option value="claude-sonnet-4-6">sonnet-4-6</option>
                      <option value="claude-haiku-4-5-20251001">haiku-4-5</option>
                    </select>
                  </div>
                  <div>
                    <div className="text-[10px] text-neutral-500 mb-1">Effort</div>
                    <select
                      value={taskTypeEfforts[type]}
                      onChange={e => {
                        const val = e.target.value;
                        setTaskTypeEfforts(prev => ({ ...prev, [type]: val }));
                        localStorage.setItem(`blueprint-admin:taskTypeEffort:${type}`, JSON.stringify(val));
                        setTimeout(onSyncTaskTypeDefaults, 0);
                      }}
                      className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded px-2 py-1 text-xs focus:outline-none focus:ring-1 focus:ring-neutral-600"
                    >
                      <option value="">default</option>
                      <option value="low">low</option>
                      <option value="medium">medium</option>
                      <option value="high">high</option>
                      <option value="max">max</option>
                    </select>
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        {/* Prompt suffixes */}
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-1">Prompt Suffixes</h3>
          <p className="text-[11px] text-neutral-600 mb-3">Appended to the agent prompt for each task type. Keep under 500 chars.</p>
          <div className="grid grid-cols-2 gap-2">
            {ALL_PROMPT_SUFFIX_TYPES.map(type => {
              const isOpen = expandedSuffixType === type;
              const savedValue = agentPromptSuffixes[type] ?? '';
              const draft = isOpen ? suffixDraft : savedValue;
              const charCount = draft.length;
              return (
                <div key={type} className={`border border-neutral-800 rounded-lg overflow-hidden ${isOpen ? 'col-span-2' : ''}`}>
                  <button
                    className="w-full flex items-center justify-between px-3 py-2 bg-neutral-900 hover:bg-neutral-800 transition-colors text-left"
                    onClick={() => {
                      if (isOpen) {
                        if (suffixDraft !== savedValue) {
                          onUpdateAgentPromptSuffixes({ ...agentPromptSuffixes, [type]: suffixDraft });
                        }
                        setExpandedSuffixType(null);
                      } else {
                        if (expandedSuffixType && suffixDraft !== (agentPromptSuffixes[expandedSuffixType] ?? '')) {
                          onUpdateAgentPromptSuffixes({ ...agentPromptSuffixes, [expandedSuffixType]: suffixDraft });
                        }
                        setExpandedSuffixType(type);
                        setSuffixDraft(savedValue);
                      }
                    }}
                  >
                    <span className="text-xs text-neutral-300 capitalize">{type.replace('_', ' ')}</span>
                    <span className="flex items-center gap-1.5">
                      {savedValue && <span className="w-1.5 h-1.5 rounded-full bg-blue-500" />}
                      <span className="text-[10px] text-neutral-600">{isOpen ? '\u25B2' : '\u25BC'}</span>
                    </span>
                  </button>
                  {isOpen && (
                    <div className="px-3 pb-3 bg-neutral-900">
                      <textarea
                        value={suffixDraft}
                        onChange={e => setSuffixDraft(e.target.value)}
                        onBlur={() => {
                          if (suffixDraft !== savedValue) {
                            onUpdateAgentPromptSuffixes({ ...agentPromptSuffixes, [type]: suffixDraft });
                          }
                        }}
                        rows={3}
                        className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-neutral-600 resize-none mt-2"
                        placeholder={`Additional instructions for ${type} tasks\u2026`}
                      />
                      <div className={`text-[10px] text-right mt-1 ${charCount > 500 ? 'text-red-400' : 'text-neutral-600'}`}>
                        {charCount}/500
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
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

        {/* Max Plan Accounts */}
        <div>
          <h3 className="text-xs font-semibold text-neutral-300 uppercase tracking-wider mb-3">Max Plan Accounts</h3>
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
              return (
                <div key={a.id} className={`bg-neutral-900 rounded-lg p-3 border ${isRateLimited ? 'border-amber-900/60' : 'border-neutral-800'}`}>
                  <div className="flex items-center gap-3">
                    <button onClick={() => onToggleAccount(a.id, !a.enabled)}
                      className={`w-2.5 h-2.5 rounded-full shrink-0 ${isRateLimited ? 'bg-amber-500' : a.enabled ? 'bg-emerald-500' : 'bg-neutral-600'}`}
                      title={isRateLimited ? 'Rate-limited' : a.enabled ? 'Enabled (click to disable)' : 'Disabled (click to enable)'} />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm text-neutral-200 truncate">{a.name}</div>
                      <div className="text-[11px] text-neutral-500 font-mono">
                        {a.active_tasks} active
                        {tokenAge && <span className={`ml-2 ${isExpiring ? 'text-amber-400' : ''}`}>&middot; token {tokenAge}</span>}
                      </div>
                    </div>
                    <button onClick={() => onRemoveAccount(a.id)}
                      className="text-neutral-600 hover:text-red-400 text-sm transition-colors shrink-0">&times;</button>
                  </div>
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
                      <button onClick={() => onResetRateLimits(a.id)}
                        className="mt-1.5 w-full text-[11px] text-amber-400/70 hover:text-amber-300 bg-amber-950/30 hover:bg-amber-950/50 rounded-lg px-2 py-1 transition-colors">
                        Clear rate limits
                      </button>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
          <div className="mt-4 space-y-2">
            <input value={newAccountName} onChange={e => setNewAccountName(e.target.value)}
              placeholder="Account name" className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600" />
            <input value={newAccountToken} onChange={e => setNewAccountToken(e.target.value)}
              placeholder="sk-ant-oat01-... (from claude setup-token)" type="password"
              className="w-full bg-neutral-950 border border-neutral-700 text-neutral-100 rounded-lg px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-neutral-600 font-mono" />
            <button onClick={onAddAccount} disabled={!newAccountName || !newAccountToken}
              className="bg-neutral-800 hover:bg-neutral-700 disabled:opacity-30 text-neutral-300 text-xs py-2 px-4 rounded-lg transition-colors border border-neutral-700">
              Add Account
            </button>
          </div>
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
        buildConfig={buildConfig}
        onSave={onUpdateBuildConfig}
        onDetect={onDetectBuildConfig}
        detectAvailable={false}
      />
    );
  }

  function renderAutoRemediation() {
    return (
      <ProjectAutoRemediationSection
        githubOwner={selectedProjectOwner}
        githubRepo={selectedProjectRepo}
        config={autoRemediationConfig}
        onSave={onUpdateAutoRemediationConfig}
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

  function renderActiveSection() {
    switch (activeSection) {
      case 'general': return renderGeneral();
      case 'task-types': return renderTaskTypes();
      case 'orchestrator': return renderOrchestrator();
      case 'deploy': return renderDeploy();
      case 'accounts': return renderAccounts();
      case 'notifications': return renderNotifications();
      case 'build-config': return renderBuildConfig();
      case 'auto-remediation': return renderAutoRemediation();
      case 'issue-implementation': return renderIssueImplementation();
      case 'advanced': return renderAdvanced();
    }
  }

  return (
    <div className="relative" ref={settingsRef}>
      <button
        onClick={onToggleSettings}
        className={`bg-neutral-800 hover:bg-neutral-700 text-neutral-300 p-2 rounded text-xs transition-colors border border-neutral-700${triggerClassName ? ` ${triggerClassName}` : ''}`}
        title="Settings"
      >
        <Settings className="w-4 h-4" />
      </button>
      {showSettings && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm" onClick={onToggleSettings}>
          <div
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
