import { afterEach, describe, test, vi } from 'vitest';
import { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { StrategicSessionControl } from './StrategicSessionControl';
import { expectMinTouchTargetAt, expectNoOverflowAt } from '../test/viewport';

describe('StrategicSessionControl viewport', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const mountedRoot = root;
    if (mountedRoot) {
      act(() => {
        mountedRoot.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  function renderControl() {
    host = document.createElement('div');
    host.style.padding = '8px';
    document.body.appendChild(host);
    const mountedRoot = createRoot(host);
    root = mountedRoot;

    act(() => {
      mountedRoot.render(
        <StrategicSessionControl
          currentMode="continuous"
          localRunnerStatus={{ status: 'online', onlineDevices: 12, totalDevices: 14 }}
          runtimeWorkStatus={{ status: 'running', runningCount: 3, queuedCount: 2 }}
          onModeChange={vi.fn()}
          onOpenDevices={vi.fn()}
          onOpenCompute={vi.fn()}
        />,
      );
    });
  }

  test('does not horizontally overflow at 320px', async () => {
    await expectNoOverflowAt(320, renderControl);
  });

  test('does not horizontally overflow at 375px', async () => {
    await expectNoOverflowAt(375, renderControl);
  });

  test('keeps mobile buttons at touch-target size', async () => {
    await expectMinTouchTargetAt(375, renderControl);
  });
});
