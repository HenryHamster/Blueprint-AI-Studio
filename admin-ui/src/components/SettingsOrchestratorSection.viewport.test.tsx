import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SettingsOrchestratorSection } from './SettingsOrchestratorSection';
import { expectNoOverflowAt } from '../test/viewport';

const baseProps = {
  maxConcurrentVMs: 4,
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
  autonomousResearchIntervalMinutes: 30,
  onUpdateAutonomousResearchInterval: () => {},
  directionCooldownMinutes: 240,
  onUpdateDirectionCooldownMinutes: () => {},
  strategicResearchFrequency: 0,
  onUpdateStrategicResearchFrequency: () => {},
  anyAutonomousEnabled: true,
  onPauseAllAutonomous: async () => {},
  onRestartOrchestrator: async () => {},
};

describe('SettingsOrchestratorSection — viewport regression', () => {
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
        root?.render(<SettingsOrchestratorSection {...baseProps} />);
      });
    });
  });

  test('renders without horizontal overflow at 375px with auto-archive enabled', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(<SettingsOrchestratorSection {...baseProps} autoArchiveEnabled={true} />);
      });
    });
  });
});
