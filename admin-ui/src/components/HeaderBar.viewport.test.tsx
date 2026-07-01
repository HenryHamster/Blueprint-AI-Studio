import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { HeaderBar, PrimaryTabNav } from './HeaderBar';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';
import type { UserLaunchableTaskType } from '../taskCapabilities';

vi.mock('../api', () => ({
  apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
  API_BASE_URL: 'http://localhost',
  authHeaders: vi.fn(async () => ({})),
}));

vi.mock('../firebase', () => ({
  auth: { currentUser: null, onAuthStateChanged: () => () => {} },
  signInWithGoogle: vi.fn(),
  signOut: vi.fn(),
}));

// AutonomousStatus and SettingsDropdown are complex sub-trees;
// stub them to keep the test focused on HeaderBar's own mobile layout.
vi.mock('./AutonomousStatus', () => ({
  AutonomousStatus: () => <div data-testid="autonomous-status" />,
}));
vi.mock('./SettingsDropdown', () => ({
  SettingsDropdown: () => <div data-testid="settings-dropdown" />,
}));

const noop = () => {};
const noopAsync = async () => {};

function makeProps() {
  return {
    tasks: [],
    runningTasks: [],
    queuedTasks: [],
    globalActiveCount: 0,
    globalEvaluatingCount: 0,
    globalQueuedCount: 0,
    maxConcurrentVMs: 4,
    lastRefresh: null,
    onRefresh: noop,
    viewMode: 'kanban' as const,
    onSetViewMode: noop,
    showResearchBrowser: false,
    onToggleResearchBrowser: noop,
    showKnowledgeBrowser: false,
    onToggleKnowledgeBrowser: noop,
    openMessageCount: 0,
    onOpenNewTaskModal: noop,
    onSignOut: noop,
    userEmail: 'test@example.com',
    wsRef: { current: null },
    showSettings: false,
    onToggleSettings: noop,
    settingsRef: { current: null },
    defaultTaskType: 'create' as UserLaunchableTaskType,
    onUpdateDefaultTaskType: noop,
    notifyOnComplete: false,
    onUpdateNotifyOnComplete: noop,
    notifyOnFail: false,
    onUpdateNotifyOnFail: noop,
    notifyOnApprovalNeeded: false,
    onUpdateNotifyOnApprovalNeeded: noop,
    soundOnComplete: false,
    onUpdateSoundOnComplete: noop,
    notifyOnHighSignalMessage: false,
    onUpdateNotifyOnHighSignalMessage: noop,
    maxRetries: 3,
    maxAutonomousTasksPerHour: 10,
    requireApprovalGates: false,
    autoArchiveEnabled: false,
    autoArchiveTerminalAfterDays: 7,
    autoArchiveMaxTerminalTasksPerProject: 50,
    autoArchiveIntervalMinutes: 60,
    onUpdateAutoArchiveEnabled: noop,
    onUpdateAutoArchiveTerminalAfterDays: noop,
    onUpdateAutoArchiveMaxTerminalTasksPerProject: noop,
    onUpdateAutoArchiveIntervalMinutes: noop,
    onUpdateMaxVMs: noop,
    onUpdateRequireApprovalGates: noop,
    onUpdateMaxRetries: noop,
    onUpdateMaxAutonomousRate: noop,
    maxGlobalAutonomousTasksPerHour: 50,
    onUpdateMaxGlobalAutonomousRate: noop,
    maxPipelineSlots: 4,
    onUpdateMaxPipelineSlots: noop,
    reservedSlotsForTypes: { create: 1, research: 1 },
    onUpdateReservedSlotsForTypes: noop,
    evaluatingPipelineTimeoutMinutes: 120,
    onUpdateEvaluatingPipelineTimeout: noop,
    evaluatingFailHours: 48,
    onUpdateEvaluatingFailHours: noop,
    ciBackstopTimeoutMinutes: 60,
    onUpdateCiBackstopTimeout: noop,
    autonomousResearchIntervalMinutes: 30,
    onUpdateAutonomousResearchInterval: noop,
    directionCooldownMinutes: 60,
    onUpdateDirectionCooldownMinutes: noop,
    strategicResearchFrequency: 5,
    onUpdateStrategicResearchFrequency: noop,
    healthCheckUrls: [],
    onUpdateHealthCheckUrls: noop,
    githubAccount: null,
    onConnectGitHub: noop,
    maxPlanAccounts: [],
    onToggleAccount: noop,
    onRemoveAccount: noop,
    onResetRateLimits: noop,
    onRefreshAccountModels: noop,
    onUpdateAccount: noop,
    newAccountName: '',
    setNewAccountName: noop,
    newAccountProvider: 'anthropic' as const,
    setNewAccountProvider: noop,
    newAccountAuthMode: 'api_key' as const,
    setNewAccountAuthMode: noop,
    newAccountToken: '',
    setNewAccountToken: noop,
    newAccountProviderMetadata: {},
    setNewAccountProviderMetadata: noop,
    newAccountClaudeConfigJson: '',
    setNewAccountClaudeConfigJson: noop,
    newAccountDailyTaskCap: null,
    setNewAccountDailyTaskCap: noop,
    newAccountMaxConcurrentTasks: null,
    setNewAccountMaxConcurrentTasks: noop,
    newAccountAutoDisableAfterRateLimits: null,
    setNewAccountAutoDisableAfterRateLimits: noop,
    onAddAccount: noop,
    onUpdateAccountCaps: noop,
    providerTierModelMap: {},
    taskTypeModelPreferences: {},
    providers: {},
    providerSettingsDefaults: {} as import('../types/settings').ProviderSettingsDefaults,
    taskTypeProviderOverrides: {} as import('../types/settings').TaskTypeProviderOverrides,
    onUpdateProviderSettingsDefaults: noopAsync,
    onUpdateTaskTypeProviderOverrides: noopAsync,
    providerSelectionMode: 'balanced' as const,
    onUpdateProviderSelectionMode: noopAsync,
    taskTypeCategories: {},
    onUpdateTaskTypeCategories: noopAsync,
    categoryRouting: {},
    onUpdateCategoryRouting: noopAsync,
    taskTypeRouting: {},
    onUpdateTaskTypeRouting: noopAsync,
    providerSettingsByEffort: {} as import('../types/settings').ProviderSettingsByEffort,
    onUpdateProviderSettingsByEffort: noopAsync,
    refreshInterval: 10000,
    onUpdateRefreshInterval: noop,
    pageSize: 50,
    onUpdatePageSize: noop,
    apiHealthy: true,
    wsConnected: true,
    lastFetchTime: 0,
    onRestartOrchestrator: noopAsync,
    anyAutonomousEnabled: false,
    onPauseAllAutonomous: noopAsync,
    deployState: null,
    onPausePipeline: noopAsync,
    onResumePipeline: noopAsync,
    onResetHealth: noopAsync,
    onRollback: noopAsync,
    onMarkStable: noopAsync,
    onRepairState: noopAsync,
    failedRollback: null,
    zoneHealth: null,
    showUserManagement: false,
    onToggleUserManagement: noop,
    showProjectMembers: false,
    onToggleProjectMembers: noop,
    selectedProjectMyRole: null,
    userRole: null,
    notificationConfig: {} as import('./SettingsNotificationsSection').NotificationConfig,
    onUpdateNotificationConfig: noop,
    dailyCostCeilingUsd: 0,
    onUpdateDailyCostCeiling: noop,
    selectedProjectId: null,
    selectedProjectOwner: null,
    selectedProjectRepo: null,
    buildConfig: null,
    onUpdateBuildConfig: noopAsync,
    onDetectBuildConfig: noopAsync,
    autoRemediationConfig: null,
    autoRemediationStats: null,
    onUpdateAutoRemediationConfig: noopAsync,
    issueImplementationConfig: null,
    onUpdateIssueImplementationConfig: noopAsync,
    autoReviewConfig: null,
    onUpdateAutoReviewConfig: noopAsync,
    resourceEnvelopeData: null,
    resourceEnvelopeLoading: false,
    resourceEnvelopeError: null,
    onUpdateResourceEnvelope: noopAsync,
    integrationBranchData: null,
    onUpdateIntegrationBranch: noopAsync,
    promotionTargetData: null,
    onUpdatePromotionTarget: noopAsync,
    projects: [],
    onSelectProject: noop,
    activeTab: 'chat' as const,
    onTabChange: noop,
    health: null,
    detailedHealth: null,
  };
}

describe('HeaderBar — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => { r.unmount(); });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('collapsed mobile hamburger state renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<HeaderBar {...makeProps()} />); });
    });
  });

  test('expanded mobile menu renders without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      // Give enough height so the absolute dropdown doesn't extend below viewport
      host.style.minHeight = '600px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<HeaderBar {...makeProps()} />); });
      // Click the mobile menu hamburger button to open the dropdown
      const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
      if (hamburger) {
        await act(async () => { hamburger.click(); });
      }
    });
  });

  test('mobile menu buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      host.style.minHeight = '600px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<HeaderBar {...makeProps()} />); });
      // Open the mobile menu to expose its buttons
      const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
      if (hamburger) {
        await act(async () => { hamburger.click(); });
      }
    });
  });

  test('header text labels render without silent clipping at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<HeaderBar {...makeProps()} />); });
    });
  });

  test('desktop layout renders without overflow at 768px (md: breakpoint)', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => { r.render(<HeaderBar {...makeProps()} />); });
    });
  });

  test('desktop activity stream launcher calls its opener', async () => {
    const onOpenActivityFeed = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} onOpenActivityFeed={onOpenActivityFeed} />); });
    const activityButton = host.querySelector<HTMLButtonElement>('button[title="Chronological activity stream"]');
    expect(activityButton).toBeTruthy();
    await act(async () => { activityButton?.click(); });
    expect(onOpenActivityFeed).toHaveBeenCalledOnce();
  });

  test('desktop command palette launcher is not duplicated in the global header', async () => {
    const onOpenCommandPalette = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} onOpenCommandPalette={onOpenCommandPalette} />); });
    const commandButton = host.querySelector<HTMLButtonElement>('button[title="Open command palette (Ctrl+K)"]');
    expect(commandButton).toBeNull();
    expect(onOpenCommandPalette).not.toHaveBeenCalled();
  });

  test('desktop header keeps the Blueprint brand icon-only and project switcher primary', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} />); });

    expect(host.textContent).not.toContain('Blueprint');
    expect(host.querySelector('[aria-label="Blueprint"]')).not.toBeNull();
  });

  test('primary mode label is Chat, not a duplicate Strategic entry point', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} activeTab="chat" />); });

    const chatTab = Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.trim() === 'Chat');
    expect(chatTab).toBeTruthy();
    const oldStrategicTab = Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.trim() === 'Strategic');
    expect(oldStrategicTab).toBeUndefined();
  });

  test('mobile menu does not duplicate the Strategic composer command launcher', async () => {
    const onOpenCommandPalette = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    host.style.minHeight = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} onOpenCommandPalette={onOpenCommandPalette} />); });
    const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
    await act(async () => { hamburger?.click(); });

    const commandButton = Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
      .find(button => button.textContent?.includes('Command palette'));
    expect(commandButton).toBeUndefined();
    expect(onOpenCommandPalette).not.toHaveBeenCalled();
  });

  test('mobile menu does not duplicate primary mode navigation', async () => {
    const onTabChange = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    host.style.minHeight = '600px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} onTabChange={onTabChange} />); });
    const hamburger = host.querySelector<HTMLButtonElement>('button[title="More options"]');
    await act(async () => { hamburger?.click(); });

    const menu = hamburger?.nextElementSibling;
    const modeButtons = Array.from(menu?.querySelectorAll<HTMLButtonElement>('button') ?? [])
      .filter(button => ['Chat', 'Pipeline', 'Project', 'Runtime'].includes(button.textContent?.trim() ?? ''));

    expect(modeButtons).toHaveLength(0);
    expect(onTabChange).not.toHaveBeenCalled();
  });

  test('mobile primary nav switches modes and shows badges', async () => {
    const onTabChange = vi.fn();
    host = document.createElement('div');
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(
        <PrimaryTabNav
          activeTab="chat"
          onTabChange={onTabChange}
          openMessageCount={3}
          needsAttentionCount={5}
          placement="mobile"
        />,
      );
    });

    const buttons = Array.from(host.querySelectorAll<HTMLButtonElement>('button'));
    expect(buttons.map(button => button.textContent?.trim())).toEqual([
      'Chat3',
      'Pipeline5',
      'Project',
      'Runtime',
    ]);
    await act(async () => { buttons[1].click(); });
    expect(onTabChange).toHaveBeenCalledWith('pipeline');
  });

  test('pipeline tab shows attention badge from existing count prop', async () => {
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<HeaderBar {...makeProps()} needsAttentionCount={7} />);
    });

    const pipelineTab = Array.from(host.querySelectorAll('button'))
      .find(button => button.textContent?.includes('Pipeline'));
    expect(pipelineTab?.textContent).toContain('7');
  });

  test('desktop new task launcher remains available outside pipeline', async () => {
    const onOpenNewTaskModal = vi.fn();
    host = document.createElement('div');
    host.style.padding = '12px';
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => { r.render(<HeaderBar {...makeProps()} onOpenNewTaskModal={onOpenNewTaskModal} activeTab="chat" />); });

    const newTaskButton = host.querySelector<HTMLButtonElement>('button[title="New task"]');
    expect(newTaskButton).toBeTruthy();
    await act(async () => { newTaskButton?.click(); });
    expect(onOpenNewTaskModal).toHaveBeenCalledOnce();

    await act(async () => { r.render(<HeaderBar {...makeProps()} onOpenNewTaskModal={onOpenNewTaskModal} activeTab="pipeline" />); });
    expect(host.querySelector<HTMLButtonElement>('button[title="New task"]')).toBeNull();
  });
});
