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

  // Acceptance gate: the resident-session panel must render the turn-latency
  // chip legibly at the declared viewport width without horizontal overflow.
  function renderWithTurnLatency() {
    host = document.createElement('div');
    host.style.padding = '8px';
    document.body.appendChild(host);
    const mountedRoot = createRoot(host);
    root = mountedRoot;

    act(() => {
      mountedRoot.render(
        <StrategicSessionControl
          currentMode="continuous"
          sessionActive
          sessionState="active"
          sessionStatus="active"
          turnLatencySummary={{
            operator: { count: 5, mean_turn_ms: 3200, max_turn_ms: 7100, mean_ttft_ms: 800, interrupted_count: 0, total_tool_calls: 12, total_tool_call_duration_ms: 15000, mean_inter_token_ms: 150, mean_pickup_lag_ms: 60 },
            autonomous: { count: 3, mean_turn_ms: 5000, max_turn_ms: 8000, mean_ttft_ms: null, interrupted_count: 1, total_tool_calls: 6, total_tool_call_duration_ms: 24000, mean_inter_token_ms: 200, mean_pickup_lag_ms: null },
          }}
          operatorDecisionSummary={{ total: 5, captured: 4, missed: 1, capture_rate: 80 }}
          onModeChange={vi.fn()}
          onStopSession={vi.fn()}
          onOpenDevices={vi.fn()}
        />,
      );
    });
  }

  test('renders turn-latency chip without overflow at 1280px', async () => {
    await expectNoOverflowAt(1280, renderWithTurnLatency);

    const control = host?.querySelector('[data-testid="strategic-session-control"]');
    expect(control).toBeTruthy();
    const latencyChip = control?.querySelector('[role="status"][aria-label*="Operator:"]');
    expect(latencyChip).toBeTruthy();
    const label = latencyChip?.textContent ?? '';
    expect(label).toContain('avg 3.2s');
    expect(label).toContain('max 7.1s');
  });
});
