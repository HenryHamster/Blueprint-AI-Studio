import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SettingsDropdown } from './SettingsDropdown';

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
}));

function makeProps(overrides: Record<string, unknown> = {}) {
  return {
    showSettings: true,
    onToggleSettings: vi.fn(),
    settingsRef: { current: null },
    defaultTaskType: 'create' as const,
    onUpdateDefaultTaskType: vi.fn(),
    viewMode: 'kanban' as const,
    onSetViewMode: vi.fn(),
    notifyOnComplete: false,
    onUpdateNotifyOnComplete: vi.fn(),
    notifyOnFail: false,
    onUpdateNotifyOnFail: vi.fn(),
    notifyOnApprovalNeeded: false,
    onUpdateNotifyOnApprovalNeeded: vi.fn(),
    soundOnComplete: false,
    onUpdateSoundOnComplete: vi.fn(),
    maxConcurrentVMs: 4,
    onUpdateMaxVMs: vi.fn(),
    maxRetries: 3,
    onUpdateMaxRetries: vi.fn(),
    requireApprovalGates: false,
    onUpdateRequireApprovalGates: vi.fn(),
    autoArchiveEnabled: false,
    onUpdateAutoArchiveEnabled: vi.fn(),
    autoArchiveTerminalAfterDays: 7,
    onUpdateAutoArchiveTerminalAfterDays: vi.fn(),
    autoArchiveMaxTerminalTasksPerProject: 0,
    onUpdateAutoArchiveMaxTerminalTasksPerProject: vi.fn(),
    autoArchiveIntervalMinutes: 60,
    onUpdateAutoArchiveIntervalMinutes: vi.fn(),
    maxAutonomousTasksPerHour: 10,
    onUpdateMaxAutonomousRate: vi.fn(),
    autonomousResearchIntervalMinutes: 30,
    onUpdateAutonomousResearchInterval: vi.fn(),
    directionCooldownMinutes: 240,
    onUpdateDirectionCooldownMinutes: vi.fn(),
    strategicResearchFrequency: 0,
    onUpdateStrategicResearchFrequency: vi.fn(),
    healthCheckUrls: [],
    onUpdateHealthCheckUrls: vi.fn(),
    githubAccount: null,
    onConnectGitHub: vi.fn(),
    maxPlanAccounts: [],
    onToggleAccount: vi.fn(),
    onRemoveAccount: vi.fn(),
    onResetRateLimits: vi.fn(),
    onRefreshAccountModels: vi.fn(),
    newAccountName: '',
    setNewAccountName: vi.fn(),
    newAccountProvider: 'anthropic' as const,
    setNewAccountProvider: vi.fn(),
    newAccountAuthMode: 'oauth' as const,
    setNewAccountAuthMode: vi.fn(),
    newAccountToken: '',
    setNewAccountToken: vi.fn(),
    newAccountProviderMetadata: {},
    setNewAccountProviderMetadata: vi.fn(),
    newAccountClaudeConfigJson: '',
    setNewAccountClaudeConfigJson: vi.fn(),
    newAccountDailyTaskCap: null,
    setNewAccountDailyTaskCap: vi.fn(),
    newAccountMaxConcurrentTasks: null,
    setNewAccountMaxConcurrentTasks: vi.fn(),
    newAccountAutoDisableAfterRateLimits: null,
    setNewAccountAutoDisableAfterRateLimits: vi.fn(),
    onAddAccount: vi.fn(),
    onUpdateAccountCaps: vi.fn(),
    providerTierModelMap: {},
    taskTypeModelPreferences: {},
    providers: {},
    providerSettingsDefaults: {},
    taskTypeProviderOverrides: {},
    onUpdateProviderSettingsDefaults: vi.fn(),
    onUpdateTaskTypeProviderOverrides: vi.fn(),
    providerSelectionMode: 'balanced' as const,
    onUpdateProviderSelectionMode: vi.fn(),
    taskTypeCategories: {},
    onUpdateTaskTypeCategories: vi.fn(),
    categoryRouting: {},
    onUpdateCategoryRouting: vi.fn(),
    taskTypeRouting: {},
    onUpdateTaskTypeRouting: vi.fn(),
    providerSettingsByEffort: {},
    onUpdateProviderSettingsByEffort: vi.fn(),
    refreshInterval: 30000,
    onUpdateRefreshInterval: vi.fn(),
    pageSize: 50,
    onUpdatePageSize: vi.fn(),
    apiHealthy: true,
    wsConnected: true,
    lastFetchTime: Date.now(),
    onRestartOrchestrator: vi.fn(),
    anyAutonomousEnabled: false,
    onPauseAllAutonomous: vi.fn(),
    deployState: null,
    onPausePipeline: vi.fn(),
    onResumePipeline: vi.fn(),
    onResetHealth: vi.fn(),
    onRollback: vi.fn(),
    onMarkStable: vi.fn(),
    onRepairState: vi.fn(),
    zoneHealth: null,
    notificationConfig: { webhook_url: '', enabled_events: [], throttle_seconds: 60 },
    onUpdateNotificationConfig: vi.fn(),
    dailyCostCeilingUsd: 0,
    onUpdateDailyCostCeiling: vi.fn(),
    userRole: 'admin' as const,
    userEmail: 'admin@example.com',
    onSignOut: vi.fn(),
    showUserManagement: false,
    onToggleUserManagement: vi.fn(),
    showProjectMembers: false,
    onToggleProjectMembers: vi.fn(),
    selectedProjectMyRole: 'owner' as const,
    selectedProjectId: 'proj-1',
    selectedProjectOwner: 'myorg',
    selectedProjectRepo: 'myrepo',
    buildConfig: null,
    onUpdateBuildConfig: vi.fn(),
    onDetectBuildConfig: vi.fn(),
    autoRemediationConfig: null,
    autoRemediationStats: null,
    onUpdateAutoRemediationConfig: vi.fn(),
    issueImplementationConfig: null,
    onUpdateIssueImplementationConfig: vi.fn(),
    autoReviewConfig: null,
    onUpdateAutoReviewConfig: vi.fn(),
    ...overrides,
  };
}

describe('SettingsDropdown — Project Members trigger', () => {
  it('renders the Manage Project Members button in Accounts section for project owners', () => {
    const props = makeProps();
    render(<SettingsDropdown {...(props as Parameters<typeof SettingsDropdown>[0])} />);

    // Navigate to Accounts section
    fireEvent.click(screen.getByRole('button', { name: 'Accounts' }));

    expect(screen.getByTestId('toggle-project-members')).toBeInTheDocument();
    expect(screen.getByText('Manage Project Members')).toBeInTheDocument();
  });

  it('calls onToggleProjectMembers and onToggleSettings when the button is clicked', () => {
    const onToggleProjectMembers = vi.fn();
    const onToggleSettings = vi.fn();
    const props = makeProps({ onToggleProjectMembers, onToggleSettings });
    render(<SettingsDropdown {...(props as Parameters<typeof SettingsDropdown>[0])} />);

    fireEvent.click(screen.getByRole('button', { name: 'Accounts' }));
    fireEvent.click(screen.getByTestId('toggle-project-members'));

    expect(onToggleProjectMembers).toHaveBeenCalledOnce();
    expect(onToggleSettings).toHaveBeenCalledOnce();
  });

  it('does not render the Project Members button when no project is selected', () => {
    const props = makeProps({ selectedProjectId: null });
    render(<SettingsDropdown {...(props as Parameters<typeof SettingsDropdown>[0])} />);

    fireEvent.click(screen.getByRole('button', { name: 'Accounts' }));

    expect(screen.queryByTestId('toggle-project-members')).toBeNull();
  });

  it('does not render the Project Members button for non-owner, non-admin users', () => {
    const props = makeProps({ userRole: 'user' as const, selectedProjectMyRole: 'member' });
    render(<SettingsDropdown {...(props as Parameters<typeof SettingsDropdown>[0])} />);

    fireEvent.click(screen.getByRole('button', { name: 'Accounts' }));

    expect(screen.queryByTestId('toggle-project-members')).toBeNull();
  });
});
