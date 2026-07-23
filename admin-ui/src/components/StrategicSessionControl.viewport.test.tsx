import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { StrategicSessionControl } from './StrategicSessionControl';
import { expectFullViewportAt, expectNoOverflowAt } from '../test/viewport';

describe('StrategicSessionControl — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('Workday controls remain contained at 375px', async () => {
    await expectFullViewportAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <StrategicSessionControl
            currentMode="continuous"
            sessionState="ready"
            sessionRunners={[{
              id: 'runner-1',
              name: 'Primary Workday runner',
              hostname: 'workday-primary',
              online: true,
              agent_ready: true,
              eligible: true,
            }]}
            sessionSelectedRunnerId="runner-1"
            onSelectRunner={() => {}}
            onStartSession={() => {}}
            onModeChange={() => {}}
          />,
        );
      });
    });
  });

  test('active Workday controls remain contained at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <StrategicSessionControl
            currentMode="continuous"
            sessionState="active"
            sessionActive
            onStopSession={() => {}}
            onModeChange={() => {}}
          />,
        );
      });
    });
  });
});
