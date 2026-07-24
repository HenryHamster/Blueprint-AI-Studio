import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt } from '../test/viewport';
import { AutonomousConfigSection } from './AutonomousConfigSection';

describe('AutonomousConfigSection — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { const r = root; act(() => r.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px (expanded with realistic values)', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <AutonomousConfigSection
            configCooldownHours={24}
            setConfigCooldownHours={vi.fn()}
            configProductiveCooldownMinutes={45}
            setConfigProductiveCooldownMinutes={vi.fn()}
            configIntervalMinutes={60}
            setConfigIntervalMinutes={vi.fn()}
            configMaxParallel={5}
            setConfigMaxParallel={vi.fn()}
            configMaxTotalResearch={20}
            setConfigMaxTotalResearch={vi.fn()}
            configMaxTasksPerHour={10}
            setConfigMaxTasksPerHour={vi.fn()}
            configMaxConcurrentAutoRemediations={3}
            setConfigMaxConcurrentAutoRemediations={vi.fn()}
            configResidualRemediationCooldownHours={48}
            setConfigResidualRemediationCooldownHours={vi.fn()}
            configResidualAuditDeferEscalationThreshold={5}
            setConfigResidualAuditDeferEscalationThreshold={vi.fn()}
            configMaxActiveDirections={12}
            setConfigMaxActiveDirections={vi.fn()}
            configArchitectureReviewFrequency={5}
            setConfigArchitectureReviewFrequency={vi.fn()}
            configMaxProvisionRetries={3}
            setConfigMaxProvisionRetries={vi.fn()}
            configMaxRollupRetries={5}
            setConfigMaxRollupRetries={vi.fn()}
            configStrategicMaxIntervalMinutes={120}
            setConfigStrategicMaxIntervalMinutes={vi.fn()}
            configDriftOverClaimMinClaims={3}
            setConfigDriftOverClaimMinClaims={vi.fn()}
            configDriftConsistencyThreshold={0.4}
            setConfigDriftConsistencyThreshold={vi.fn()}
            configDriftStallMinClaims={5}
            setConfigDriftStallMinClaims={vi.fn()}
            autonomousConfigOpen={true}
            setAutonomousConfigOpen={vi.fn()}
            savingConfigField={null}
            saveConfigField={vi.fn(async () => {})}
            clearResidualFingerprints={vi.fn(async () => 0)}
          />,
        );
      });
    });
  });
});
