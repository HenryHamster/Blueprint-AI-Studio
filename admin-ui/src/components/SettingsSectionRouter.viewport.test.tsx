import { afterEach, beforeAll, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SettingsSectionRouter, type SettingsSectionRouterProps } from './SettingsSectionRouter';
import type { SectionId } from './settingsSections';
import { expectNoOverflowAt } from '../test/viewport';

// Sections rendered by this router (e.g. 'deploy' → MergeGradeGateSettings,
// 'orchestrator' → SettingsOrchestratorSection) fire apiFetch calls on mount.
// Without this stub, those promises escape as unhandled rejections and fail the
// whole viewport browser-mode job even though every individual test passes.
// This mirrors the pattern used in AutonomousStatus.viewport.test.tsx and other
// composed viewport tests that render components with async mount effects.
vi.mock('../api', async () => {
  const actual = await vi.importActual<typeof import('../api')>('../api');
  return {
    ...actual,
    apiFetch: vi.fn(async () => new Response('{}', { status: 200 })),
    fetchEvergreenFillSettings: vi.fn(async () => ({
      evergreen_fill_mode: 'off',
      evergreen_fill_interval_minutes: 15,
      evergreen_fill_util_threshold: 0.5,
      evergreen_fill_min_free_slots: 2,
      evergreen_fill_max_active: 1,
      evergreen_fill_daily_cap: 3,
    })),
  };
});

/**
 * Builds a complete prop set for {@link SettingsSectionRouter} with neutral
 * defaults. The router renders exactly one section at a time keyed off
 * `activeSection`, so each test overrides only that field. All section-specific
 * props are present (the router threads them through to whichever section is
 * active) so any section can be exercised without TypeScript gaps.
 */
function makeProps(activeSection: SectionId): SettingsSectionRouterProps {
  return {
    activeSection,
    isAdmin: true,
    onNavigateToSection: () => {},
    onToggleSettings: () => {},
    allModelOptions: [],
    healthCheckUrlsInput: '',
    onHealthCheckUrlsInputChange: () => {},
    onUpdateHealthCheckUrls: () => {},

    // General
    defaultTaskType: 'research',
    onUpdateDefaultTaskType: () => {},
    viewMode: 'kanban',
    onSetViewMode: () => {},
    refreshInterval: 30000,
    onUpdateRefreshInterval: () => {},
    pageSize: 50,
    onUpdatePageSize: () => {},
    apiHealthy: true,
    wsConnected: true,
    lastFetchTime: Date.now(),

    // Notifications (General quick-toggles)
    notifyOnComplete: true,
    onUpdateNotifyOnComplete: () => {},
    notifyOnFail: true,
    onUpdateNotifyOnFail: () => {},
    notifyOnApprovalNeeded: false,
    onUpdateNotifyOnApprovalNeeded: () => {},
    soundOnComplete: false,
    onUpdateSoundOnComplete: () => {},
    notifyOnHighSignalMessage: false,
    onUpdateNotifyOnHighSignalMessage: () => {},

    // Orchestrator
    maxConcurrentVMs: 5,
    onUpdateMaxVMs: () => {},
    maxRetries: 3,
    onUpdateMaxRetries: () => {},
    requireApprovalGates: false,
    onUpdateRequireApprovalGates: () => {},
    autoArchiveEnabled: false,
    onUpdateAutoArchiveEnabled: () => {},
    autoArchiveTerminalAfterDays: 7,
    onUpdateAutoArchiveTerminalAfterDays: () => {},
    autoArchiveMaxTerminalTasksPerProject: 0,
    onUpdateAutoArchiveMaxTerminalTasksPerProject: () => {},
    autoArchiveIntervalMinutes: 60,
    onUpdateAutoArchiveIntervalMinutes: () => {},
    maxAutonomousTasksPerHour: 10,
    onUpdateMaxAutonomousRate: () => {},
    maxGlobalAutonomousTasksPerHour: 50,
    onUpdateMaxGlobalAutonomousRate: () => {},
    maxPipelineSlots: 4,
    onUpdateMaxPipelineSlots: () => {},
    reservedSlotsForTypes: { create: 1, research: 1 },
    onUpdateReservedSlotsForTypes: () => {},
    evaluatingPipelineTimeoutMinutes: 120,
    onUpdateEvaluatingPipelineTimeout: () => {},
    evaluatingFailHours: 48,
    onUpdateEvaluatingFailHours: () => {},
    ciBackstopTimeoutMinutes: 60,
    onUpdateCiBackstopTimeout: () => {},
    autonomousResearchIntervalMinutes: 30,
    onUpdateAutonomousResearchInterval: () => {},
    directionCooldownMinutes: 240,
    onUpdateDirectionCooldownMinutes: () => {},
    strategicResearchFrequency: 0,
    onUpdateStrategicResearchFrequency: () => {},
    anyAutonomousEnabled: false,
    onPauseAllAutonomous: async () => {},
    onRestartOrchestrator: async () => {},
    directionChurnWindowCycles: 5,
    onUpdateDirectionChurnWindowCycles: () => {},
    directionChurnNonMergeThreshold: 15,
    onUpdateDirectionChurnNonMergeThreshold: () => {},
    directionChurnMinMergeRate: 0.10,
    onUpdateDirectionChurnMinMergeRate: () => {},
    directionLifetimeNonMergeThreshold: 150,
    onUpdateDirectionLifetimeNonMergeThreshold: () => {},
    directionLifetimeMinMergeRate: 0.10,
    onUpdateDirectionLifetimeMinMergeRate: () => {},
    directionReviewRecommendationThreshold: 2,
    onUpdateDirectionReviewRecommendationThreshold: () => {},

    // Accounts
    githubAccount: null,
    onConnectGitHub: () => {},
    maxPlanAccounts: [],
    onToggleAccount: () => {},
    onRemoveAccount: () => {},
    onResetRateLimits: () => {},
    onRefreshAccountModels: () => {},
    onUpdateAccount: () => {},
    onUpdateAccountCaps: () => {},
    showUserManagement: false,
    onToggleUserManagement: () => {},
    showProjectMembers: false,
    onToggleProjectMembers: () => {},
    selectedProjectMyRole: null,
    newAccountName: '',
    setNewAccountName: () => {},
    newAccountProvider: 'anthropic',
    setNewAccountProvider: () => {},
    newAccountAuthMode: 'api_key',
    setNewAccountAuthMode: () => {},
    newAccountToken: '',
    setNewAccountToken: () => {},
    newAccountProviderMetadata: {},
    setNewAccountProviderMetadata: () => {},
    newAccountClaudeConfigJson: '',
    setNewAccountClaudeConfigJson: () => {},
    newAccountDailyTaskCap: null,
    setNewAccountDailyTaskCap: () => {},
    newAccountMaxConcurrentTasks: null,
    setNewAccountMaxConcurrentTasks: () => {},
    newAccountAutoDisableAfterRateLimits: null,
    setNewAccountAutoDisableAfterRateLimits: () => {},
    onAddAccount: () => {},

    // Provider settings
    providers: {},
    providerSettingsDefaults: {},
    taskTypeProviderOverrides: {},
    providerSettingsByEffort: {},
    onUpdateProviderSettingsDefaults: async () => {},
    onUpdateTaskTypeProviderOverrides: async () => {},
    onUpdateProviderSettingsByEffort: async () => {},

    // Provider routing
    providerSelectionMode: 'balanced',
    onUpdateProviderSelectionMode: async () => {},
    taskTypeCategories: {},
    onUpdateTaskTypeCategories: async () => {},
    categoryRouting: {},
    onUpdateCategoryRouting: async () => {},
    taskTypeRouting: {},
    onUpdateTaskTypeRouting: async () => {},

    // Deploy & health
    deployState: null,
    onPausePipeline: async () => {},
    onResumePipeline: async () => {},
    onResetHealth: async () => {},
    onRollback: async () => {},
    onMarkStable: async () => {},
    onRepairState: async () => {},
    failedRollback: null,
    zoneHealth: null,

    // Notifications (full panel)
    notificationConfig: {
      webhook_url: '',
      enabled_events: [],
      throttle_seconds: 0,
    },
    onUpdateNotificationConfig: () => {},
    dailyCostCeilingUsd: 0,
    onUpdateDailyCostCeiling: () => {},

    // Project-scoped config
    selectedProjectId: 'project-123',
    selectedProjectOwner: 'myorg',
    selectedProjectRepo: 'myrepo',
    selectedProjectIsSelfRepo: false,
    selectedProjectRole: 'owner',
    buildConfig: null,
    onUpdateBuildConfig: async () => {},
    onDetectBuildConfig: async () => {},
    autoRemediationConfig: null,
    autoRemediationStats: null,
    onUpdateAutoRemediationConfig: async () => {},
    issueImplementationConfig: null,
    onUpdateIssueImplementationConfig: async () => {},
    autoReviewConfig: null,
    onUpdateAutoReviewConfig: async () => {},
    resourceEnvelopeData: null,
    resourceEnvelopeLoading: false,
    resourceEnvelopeError: null,
    onUpdateResourceEnvelope: async () => {},
    integrationBranchData: null,
    onUpdateIntegrationBranch: async () => {},
    promotionTargetData: null,
    onUpdatePromotionTarget: async () => {},
  };
}

// Sections rendered by the router that present operator-facing layout/controls
// purely from primitive props (no async fetch). These cover the General,
// Orchestrator, Deploy, and Advanced bodies — the widest forms the router emits.
const SECTIONS_UNDER_TEST = ['general', 'orchestrator', 'deploy', 'advanced', 'resource-envelope', 'access'] as const satisfies readonly SectionId[];

// A section-specific marker proves the lazy module has resolved and committed.
// Without this, the overflow assertion can inspect the Suspense skeleton and let
// Vitest close the browser while Vite is still resolving that section's mocks.
const SECTION_READY_MARKER: Record<(typeof SECTIONS_UNDER_TEST)[number], string> = {
  general: 'Connection Status',
  orchestrator: 'Pipeline Settings',
  deploy: 'Deploy Health',
  advanced: 'Health Check URLs',
  'resource-envelope': 'Repository',
  access: 'User Management',
};

beforeAll(async () => {
  await Promise.all([
    import('./SettingsGeneralSection'),
    import('./SettingsOrchestratorSection'),
    import('./SettingsDeploySection'),
    import('./SettingsAdvancedSection'),
    import('./ProjectResourceEnvelopeSection'),
  ]);
});

describe('SettingsSectionRouter — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(async () => {
    if (root) {
      await act(async () => { root?.unmount(); });
    }
    root = null;
    host?.remove();
    host = null;
  });

  // Overflow is the operator-mobile failure mode the gate enforces at 375px.
  // Touch-target is intentionally not asserted here: these settings sections use
  // compact 34px-tall action buttons by design (mirrors the per-section sibling
  // tests, e.g. SettingsOrchestratorSection.viewport.test.tsx, which assert only
  // overflow), so adding a 44px touch-target assertion would flag accepted,
  // pre-existing styling unrelated to this decomposition.
  for (const section of SECTIONS_UNDER_TEST) {
    test(`renders the "${section}" section without horizontal overflow at 375px`, async () => {
      await expectNoOverflowAt(375, async () => {
        host = document.createElement('div');
        document.body.appendChild(host);
        root = createRoot(host);
        await act(async () => {
          root?.render(<SettingsSectionRouter {...makeProps(section)} />);
        });
        await vi.waitFor(() => {
          if (!host?.textContent?.includes(SECTION_READY_MARKER[section])) {
            throw new Error(`Settings section "${section}" has not finished loading`);
          }
        });
      });
    });
  }
});
