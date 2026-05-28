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
});
