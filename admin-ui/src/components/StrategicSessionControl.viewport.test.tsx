import { afterEach, describe, expect, test, vi } from 'vitest';
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

  // Mirrors the chat-header usage in App.tsx: the compact control is shown on
  // mobile with `hideDetailsOnMobile` so the operator can switch into Workday
  // from a narrow screen. The Chat/Workday toggle must stay visible while the
  // wider detail/session chips collapse (max-md:hidden) so nothing overflows.
  function renderCollapsibleControl() {
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
          sessionActive
          sessionState="active"
          onModeChange={vi.fn()}
          onStopSession={vi.fn()}
          onOpenDevices={vi.fn()}
          onOpenCompute={vi.fn()}
          density="compact"
          hideDetailsOnMobile
          className="flex"
        />,
      );
    });
  }

  test('with hideDetailsOnMobile: keeps the Workday toggle but collapses detail chips at 375px', async () => {
    await expectNoOverflowAt(375, renderCollapsibleControl);

    const control = host?.querySelector('[data-testid="strategic-session-control"]');
    expect(control).toBeTruthy();
    const workdayToggle = control?.querySelector('button[aria-label="Switch Strategic mode to Workday"]');
    expect(workdayToggle).toBeTruthy();
    expect(getComputedStyle(workdayToggle as Element).display).not.toBe('none');

    // The detail/session-chip block is the mode-toggle group's sibling; it must
    // be display:none below md so the wide chips never stretch the mobile header.
    const detailBlock = control?.querySelector('[role="group"][aria-label="Strategic interaction mode"]')?.nextElementSibling;
    expect(detailBlock).toBeTruthy();
    expect(getComputedStyle(detailBlock as Element).display).toBe('none');
  });
});
