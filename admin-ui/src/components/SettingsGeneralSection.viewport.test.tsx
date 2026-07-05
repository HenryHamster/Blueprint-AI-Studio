import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SettingsGeneralSection } from './SettingsGeneralSection';
import { expectNoOverflowAt } from '../test/viewport';

const baseProps = {
  apiHealthy: true,
  wsConnected: true,
  lastFetchTime: Date.now(),
  defaultTaskType: 'create' as const,
  onUpdateDefaultTaskType: () => {},
  viewMode: 'kanban' as const,
  onSetViewMode: () => {},
  allModelOptions: ['claude-sonnet-4-6', 'claude-opus-4-7'],
  refreshInterval: 30000,
  onUpdateRefreshInterval: () => {},
  pageSize: 50,
  onUpdatePageSize: () => {},
  notifyOnComplete: false,
  onUpdateNotifyOnComplete: () => {},
  notifyOnFail: false,
  onUpdateNotifyOnFail: () => {},
  notifyOnApprovalNeeded: false,
  onUpdateNotifyOnApprovalNeeded: () => {},
  soundOnComplete: false,
  onUpdateSoundOnComplete: () => {},
  notifyOnHighSignalMessage: false,
  onUpdateNotifyOnHighSignalMessage: () => {},
  onNavigateToNotifications: () => {},
};

describe('SettingsGeneralSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<SettingsGeneralSection {...baseProps} />);
      });
    });
  });

  test('renders without horizontal overflow at 375px with API error state', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<SettingsGeneralSection {...baseProps} apiHealthy={false} wsConnected={false} />);
      });
    });
  });

  test('renders without horizontal overflow at 375px with onboarding progress', async () => {
    const onboardingProof = {
      repo_added_at: '2024-01-01T00:00:00Z',
      bootstrap_completed_at: '2024-01-02T00:00:00Z',
      first_task_spawned_at: '2024-01-03T00:00:00Z',
      first_pr_opened_at: '2024-01-04T00:00:00Z',
      first_merge_gate_verdict_at: '2024-01-05T00:00:00Z',
      first_merge_gate_executed_tests: true,
      first_merge_gate_unverified_reason: null,
      first_pr_merged_at: '2024-01-06T00:00:00Z',
      first_autonomous_improvement_at: null,
    };
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<SettingsGeneralSection {...baseProps} selectedProjectOnboardingProof={onboardingProof} />);
      });
    });
  });
});
