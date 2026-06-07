import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SettingsDropdown } from './SettingsDropdown';
import { expectNoOverflowAt, expectNoTextTruncationAt, expectMinTouchTargetAt } from '../test/viewport';

describe('SettingsDropdown — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  const settingsRef: React.RefObject<HTMLDivElement> = { current: null };

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders without horizontal overflow at 375px in open state (admin + project)', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <SettingsDropdown
            showSettings={true}
            onToggleSettings={() => {}}
            settingsRef={settingsRef}
            defaultTaskType="research"
            onUpdateDefaultTaskType={() => {}}
            viewMode="kanban"
            onSetViewMode={() => {}}
            notifyOnComplete={true}
            onUpdateNotifyOnComplete={() => {}}
            notifyOnFail={true}
            onUpdateNotifyOnFail={() => {}}
            notifyOnApprovalNeeded={false}
            onUpdateNotifyOnApprovalNeeded={() => {}}
            soundOnComplete={false}
            onUpdateSoundOnComplete={() => {}}
            maxConcurrentVMs={5}
            onUpdateMaxVMs={() => {}}
            maxRetries={3}
            onUpdateMaxRetries={() => {}}
            requireApprovalGates={false}
            onUpdateRequireApprovalGates={() => {}}
            autoArchiveEnabled={false}
            onUpdateAutoArchiveEnabled={() => {}}
            autoArchiveTerminalAfterDays={7}
            onUpdateAutoArchiveTerminalAfterDays={() => {}}
            autoArchiveMaxTerminalTasksPerProject={0}
            onUpdateAutoArchiveMaxTerminalTasksPerProject={() => {}}
            autoArchiveIntervalMinutes={60}
            onUpdateAutoArchiveIntervalMinutes={() => {}}
            maxAutonomousTasksPerHour={10}
            onUpdateMaxAutonomousRate={() => {}}
            maxGlobalAutonomousTasksPerHour={50}
            onUpdateMaxGlobalAutonomousRate={() => {}}
            maxPipelineSlots={4}
            onUpdateMaxPipelineSlots={() => {}}
            reservedSlotsForTypes={{ create: 1, research: 1 }}
            onUpdateReservedSlotsForTypes={() => {}}
            evaluatingPipelineTimeoutMinutes={120}
            onUpdateEvaluatingPipelineTimeout={() => {}}
            evaluatingFailHours={48}
            onUpdateEvaluatingFailHours={() => {}}
            ciBackstopTimeoutMinutes={60}
            onUpdateCiBackstopTimeout={() => {}}
            autonomousResearchIntervalMinutes={30}
            onUpdateAutonomousResearchInterval={() => {}}
            directionCooldownMinutes={240}
            onUpdateDirectionCooldownMinutes={() => {}}
            strategicResearchFrequency={0}
            onUpdateStrategicResearchFrequency={() => {}}
            healthCheckUrls={[]}
            onUpdateHealthCheckUrls={() => {}}
            githubAccount={null}
            onConnectGitHub={() => {}}
            maxPlanAccounts={[]}
            onToggleAccount={() => {}}
            onRemoveAccount={() => {}}
            onResetRateLimits={() => {}}
            onRefreshAccountModels={() => {}}
            newAccountName=""
            setNewAccountName={() => {}}
            newAccountProvider="anthropic"
            setNewAccountProvider={() => {}}
            newAccountAuthMode="api_key"
            setNewAccountAuthMode={() => {}}
            newAccountToken=""
            setNewAccountToken={() => {}}
            newAccountProviderMetadata={{}}
            setNewAccountProviderMetadata={() => {}}
            newAccountClaudeConfigJson=""
            setNewAccountClaudeConfigJson={() => {}}
            newAccountDailyTaskCap={null}
            setNewAccountDailyTaskCap={() => {}}
            newAccountMaxConcurrentTasks={null}
            setNewAccountMaxConcurrentTasks={() => {}}
            newAccountAutoDisableAfterRateLimits={null}
            setNewAccountAutoDisableAfterRateLimits={() => {}}
            onAddAccount={() => {}}
            onUpdateAccountCaps={() => {}}
            providerTierModelMap={{}}
            taskTypeModelPreferences={{}}
            providers={{}}
            providerSettingsDefaults={{}}
            taskTypeProviderOverrides={{}}
            onUpdateProviderSettingsDefaults={async () => {}}
            onUpdateTaskTypeProviderOverrides={async () => {}}
            providerSelectionMode="balanced"
            onUpdateProviderSelectionMode={async () => {}}
            taskTypeCategories={{}}
            onUpdateTaskTypeCategories={async () => {}}
            categoryRouting={{}}
            onUpdateCategoryRouting={async () => {}}
            taskTypeRouting={{}}
            onUpdateTaskTypeRouting={async () => {}}
            providerSettingsByEffort={{}}
            onUpdateProviderSettingsByEffort={async () => {}}
            refreshInterval={30000}
            onUpdateRefreshInterval={() => {}}
            pageSize={50}
            onUpdatePageSize={() => {}}
            apiHealthy={true}
            wsConnected={true}
            lastFetchTime={Date.now()}
            onRestartOrchestrator={async () => {}}
            anyAutonomousEnabled={false}
            onPauseAllAutonomous={async () => {}}
            deployState={null}
            onPausePipeline={async () => {}}
            onResumePipeline={async () => {}}
            onResetHealth={async () => {}}
            onRollback={async () => {}}
            onMarkStable={async () => {}}
            onRepairState={async () => {}}
            failedRollback={null}
            zoneHealth={null}
            notificationConfig={{
              webhook_url: '',
              enabled_events: [],
              throttle_seconds: 0,
            }}
            onUpdateNotificationConfig={() => {}}
            dailyCostCeilingUsd={0}
            onUpdateDailyCostCeiling={() => {}}
            userRole="admin"
            userEmail="admin@example.com"
            onSignOut={() => {}}
            showUserManagement={false}
            onToggleUserManagement={() => {}}
            showProjectMembers={false}
            onToggleProjectMembers={() => {}}
            selectedProjectMyRole={null}
            selectedProjectId="project-123"
            selectedProjectOwner="myorg"
            selectedProjectRepo="myrepo"
            buildConfig={null}
            onUpdateBuildConfig={async () => {}}
            onDetectBuildConfig={async () => {}}
            autoRemediationConfig={null}
            autoRemediationStats={null}
            onUpdateAutoRemediationConfig={async () => {}}
            issueImplementationConfig={null}
            onUpdateIssueImplementationConfig={async () => {}}
            selectedProjectRole="owner"
            autoReviewConfig={null}
            onUpdateAutoReviewConfig={async () => {}}
          />
        );
      });
    });
  });

  test('no silently-clipped text on tab labels and section headers at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <SettingsDropdown
            showSettings={true}
            onToggleSettings={() => {}}
            settingsRef={settingsRef}
            defaultTaskType="research"
            onUpdateDefaultTaskType={() => {}}
            viewMode="kanban"
            onSetViewMode={() => {}}
            notifyOnComplete={true}
            onUpdateNotifyOnComplete={() => {}}
            notifyOnFail={true}
            onUpdateNotifyOnFail={() => {}}
            notifyOnApprovalNeeded={false}
            onUpdateNotifyOnApprovalNeeded={() => {}}
            soundOnComplete={false}
            onUpdateSoundOnComplete={() => {}}
            maxConcurrentVMs={5}
            onUpdateMaxVMs={() => {}}
            maxRetries={3}
            onUpdateMaxRetries={() => {}}
            requireApprovalGates={false}
            onUpdateRequireApprovalGates={() => {}}
            autoArchiveEnabled={false}
            onUpdateAutoArchiveEnabled={() => {}}
            autoArchiveTerminalAfterDays={7}
            onUpdateAutoArchiveTerminalAfterDays={() => {}}
            autoArchiveMaxTerminalTasksPerProject={0}
            onUpdateAutoArchiveMaxTerminalTasksPerProject={() => {}}
            autoArchiveIntervalMinutes={60}
            onUpdateAutoArchiveIntervalMinutes={() => {}}
            maxAutonomousTasksPerHour={10}
            onUpdateMaxAutonomousRate={() => {}}
            maxGlobalAutonomousTasksPerHour={50}
            onUpdateMaxGlobalAutonomousRate={() => {}}
            maxPipelineSlots={4}
            onUpdateMaxPipelineSlots={() => {}}
            reservedSlotsForTypes={{ create: 1, research: 1 }}
            onUpdateReservedSlotsForTypes={() => {}}
            evaluatingPipelineTimeoutMinutes={120}
            onUpdateEvaluatingPipelineTimeout={() => {}}
            evaluatingFailHours={48}
            onUpdateEvaluatingFailHours={() => {}}
            ciBackstopTimeoutMinutes={60}
            onUpdateCiBackstopTimeout={() => {}}
            autonomousResearchIntervalMinutes={30}
            onUpdateAutonomousResearchInterval={() => {}}
            directionCooldownMinutes={240}
            onUpdateDirectionCooldownMinutes={() => {}}
            strategicResearchFrequency={0}
            onUpdateStrategicResearchFrequency={() => {}}
            healthCheckUrls={[]}
            onUpdateHealthCheckUrls={() => {}}
            githubAccount={null}
            onConnectGitHub={() => {}}
            maxPlanAccounts={[]}
            onToggleAccount={() => {}}
            onRemoveAccount={() => {}}
            onResetRateLimits={() => {}}
            onRefreshAccountModels={() => {}}
            newAccountName=""
            setNewAccountName={() => {}}
            newAccountProvider="anthropic"
            setNewAccountProvider={() => {}}
            newAccountAuthMode="api_key"
            setNewAccountAuthMode={() => {}}
            newAccountToken=""
            setNewAccountToken={() => {}}
            newAccountProviderMetadata={{}}
            setNewAccountProviderMetadata={() => {}}
            newAccountClaudeConfigJson=""
            setNewAccountClaudeConfigJson={() => {}}
            newAccountDailyTaskCap={null}
            setNewAccountDailyTaskCap={() => {}}
            newAccountMaxConcurrentTasks={null}
            setNewAccountMaxConcurrentTasks={() => {}}
            newAccountAutoDisableAfterRateLimits={null}
            setNewAccountAutoDisableAfterRateLimits={() => {}}
            onAddAccount={() => {}}
            onUpdateAccountCaps={() => {}}
            providerTierModelMap={{}}
            taskTypeModelPreferences={{}}
            providers={{}}
            providerSettingsDefaults={{}}
            taskTypeProviderOverrides={{}}
            onUpdateProviderSettingsDefaults={async () => {}}
            onUpdateTaskTypeProviderOverrides={async () => {}}
            providerSelectionMode="balanced"
            onUpdateProviderSelectionMode={async () => {}}
            taskTypeCategories={{}}
            onUpdateTaskTypeCategories={async () => {}}
            categoryRouting={{}}
            onUpdateCategoryRouting={async () => {}}
            taskTypeRouting={{}}
            onUpdateTaskTypeRouting={async () => {}}
            providerSettingsByEffort={{}}
            onUpdateProviderSettingsByEffort={async () => {}}
            refreshInterval={30000}
            onUpdateRefreshInterval={() => {}}
            pageSize={50}
            onUpdatePageSize={() => {}}
            apiHealthy={true}
            wsConnected={true}
            lastFetchTime={Date.now()}
            onRestartOrchestrator={async () => {}}
            anyAutonomousEnabled={false}
            onPauseAllAutonomous={async () => {}}
            deployState={null}
            onPausePipeline={async () => {}}
            onResumePipeline={async () => {}}
            onResetHealth={async () => {}}
            onRollback={async () => {}}
            onMarkStable={async () => {}}
            onRepairState={async () => {}}
            failedRollback={null}
            zoneHealth={null}
            notificationConfig={{
              webhook_url: '',
              enabled_events: [],
              throttle_seconds: 0,
            }}
            onUpdateNotificationConfig={() => {}}
            dailyCostCeilingUsd={0}
            onUpdateDailyCostCeiling={() => {}}
            userRole="admin"
            userEmail="admin@example.com"
            onSignOut={() => {}}
            showUserManagement={false}
            onToggleUserManagement={() => {}}
            showProjectMembers={false}
            onToggleProjectMembers={() => {}}
            selectedProjectMyRole={null}
            selectedProjectId="project-123"
            selectedProjectOwner="myorg"
            selectedProjectRepo="myrepo"
            buildConfig={null}
            onUpdateBuildConfig={async () => {}}
            onDetectBuildConfig={async () => {}}
            autoRemediationConfig={null}
            autoRemediationStats={null}
            onUpdateAutoRemediationConfig={async () => {}}
            issueImplementationConfig={null}
            onUpdateIssueImplementationConfig={async () => {}}
            selectedProjectRole="owner"
            autoReviewConfig={null}
            onUpdateAutoReviewConfig={async () => {}}
          />
        );
      });
    }, { selector: 'button, h2, h3' });
  });

  test('close button and tab buttons meet 44px touch target at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <SettingsDropdown
            showSettings={true}
            onToggleSettings={() => {}}
            settingsRef={settingsRef}
            defaultTaskType="research"
            onUpdateDefaultTaskType={() => {}}
            viewMode="kanban"
            onSetViewMode={() => {}}
            notifyOnComplete={true}
            onUpdateNotifyOnComplete={() => {}}
            notifyOnFail={true}
            onUpdateNotifyOnFail={() => {}}
            notifyOnApprovalNeeded={false}
            onUpdateNotifyOnApprovalNeeded={() => {}}
            soundOnComplete={false}
            onUpdateSoundOnComplete={() => {}}
            maxConcurrentVMs={5}
            onUpdateMaxVMs={() => {}}
            maxRetries={3}
            onUpdateMaxRetries={() => {}}
            requireApprovalGates={false}
            onUpdateRequireApprovalGates={() => {}}
            autoArchiveEnabled={false}
            onUpdateAutoArchiveEnabled={() => {}}
            autoArchiveTerminalAfterDays={7}
            onUpdateAutoArchiveTerminalAfterDays={() => {}}
            autoArchiveMaxTerminalTasksPerProject={0}
            onUpdateAutoArchiveMaxTerminalTasksPerProject={() => {}}
            autoArchiveIntervalMinutes={60}
            onUpdateAutoArchiveIntervalMinutes={() => {}}
            maxAutonomousTasksPerHour={10}
            onUpdateMaxAutonomousRate={() => {}}
            maxGlobalAutonomousTasksPerHour={50}
            onUpdateMaxGlobalAutonomousRate={() => {}}
            maxPipelineSlots={4}
            onUpdateMaxPipelineSlots={() => {}}
            reservedSlotsForTypes={{ create: 1, research: 1 }}
            onUpdateReservedSlotsForTypes={() => {}}
            evaluatingPipelineTimeoutMinutes={120}
            onUpdateEvaluatingPipelineTimeout={() => {}}
            evaluatingFailHours={48}
            onUpdateEvaluatingFailHours={() => {}}
            ciBackstopTimeoutMinutes={60}
            onUpdateCiBackstopTimeout={() => {}}
            autonomousResearchIntervalMinutes={30}
            onUpdateAutonomousResearchInterval={() => {}}
            directionCooldownMinutes={240}
            onUpdateDirectionCooldownMinutes={() => {}}
            strategicResearchFrequency={0}
            onUpdateStrategicResearchFrequency={() => {}}
            healthCheckUrls={[]}
            onUpdateHealthCheckUrls={() => {}}
            githubAccount={null}
            onConnectGitHub={() => {}}
            maxPlanAccounts={[]}
            onToggleAccount={() => {}}
            onRemoveAccount={() => {}}
            onResetRateLimits={() => {}}
            onRefreshAccountModels={() => {}}
            newAccountName=""
            setNewAccountName={() => {}}
            newAccountProvider="anthropic"
            setNewAccountProvider={() => {}}
            newAccountAuthMode="api_key"
            setNewAccountAuthMode={() => {}}
            newAccountToken=""
            setNewAccountToken={() => {}}
            newAccountProviderMetadata={{}}
            setNewAccountProviderMetadata={() => {}}
            newAccountClaudeConfigJson=""
            setNewAccountClaudeConfigJson={() => {}}
            newAccountDailyTaskCap={null}
            setNewAccountDailyTaskCap={() => {}}
            newAccountMaxConcurrentTasks={null}
            setNewAccountMaxConcurrentTasks={() => {}}
            newAccountAutoDisableAfterRateLimits={null}
            setNewAccountAutoDisableAfterRateLimits={() => {}}
            onAddAccount={() => {}}
            onUpdateAccountCaps={() => {}}
            providerTierModelMap={{}}
            taskTypeModelPreferences={{}}
            providers={{}}
            providerSettingsDefaults={{}}
            taskTypeProviderOverrides={{}}
            onUpdateProviderSettingsDefaults={async () => {}}
            onUpdateTaskTypeProviderOverrides={async () => {}}
            providerSelectionMode="balanced"
            onUpdateProviderSelectionMode={async () => {}}
            taskTypeCategories={{}}
            onUpdateTaskTypeCategories={async () => {}}
            categoryRouting={{}}
            onUpdateCategoryRouting={async () => {}}
            taskTypeRouting={{}}
            onUpdateTaskTypeRouting={async () => {}}
            providerSettingsByEffort={{}}
            onUpdateProviderSettingsByEffort={async () => {}}
            refreshInterval={30000}
            onUpdateRefreshInterval={() => {}}
            pageSize={50}
            onUpdatePageSize={() => {}}
            apiHealthy={true}
            wsConnected={true}
            lastFetchTime={Date.now()}
            onRestartOrchestrator={async () => {}}
            anyAutonomousEnabled={false}
            onPauseAllAutonomous={async () => {}}
            deployState={null}
            onPausePipeline={async () => {}}
            onResumePipeline={async () => {}}
            onResetHealth={async () => {}}
            onRollback={async () => {}}
            onMarkStable={async () => {}}
            onRepairState={async () => {}}
            failedRollback={null}
            zoneHealth={null}
            notificationConfig={{
              webhook_url: '',
              enabled_events: [],
              throttle_seconds: 0,
            }}
            onUpdateNotificationConfig={() => {}}
            dailyCostCeilingUsd={0}
            onUpdateDailyCostCeiling={() => {}}
            userRole="admin"
            userEmail="admin@example.com"
            onSignOut={() => {}}
            showUserManagement={false}
            onToggleUserManagement={() => {}}
            showProjectMembers={false}
            onToggleProjectMembers={() => {}}
            selectedProjectMyRole={null}
            selectedProjectId="project-123"
            selectedProjectOwner="myorg"
            selectedProjectRepo="myrepo"
            buildConfig={null}
            onUpdateBuildConfig={async () => {}}
            onDetectBuildConfig={async () => {}}
            autoRemediationConfig={null}
            autoRemediationStats={null}
            onUpdateAutoRemediationConfig={async () => {}}
            issueImplementationConfig={null}
            onUpdateIssueImplementationConfig={async () => {}}
            selectedProjectRole="owner"
            autoReviewConfig={null}
            onUpdateAutoReviewConfig={async () => {}}
          />
        );
      });
    }, { selector: 'button' });
  });
});
