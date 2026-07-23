import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, test, vi } from 'vitest';
import { StrategicSessionControl } from './StrategicSessionControl';

describe('StrategicSessionControl lifecycle authority', () => {
  test.each([
    ['blocked', 'Blocked'],
    ['error', 'Error'],
  ])('keeps %s status authoritative and offers stop-then-restart for a live stale snapshot', (sessionState, statusLabel) => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionStatus="active"
        sessionState={sessionState}
        sessionReason="runner_offline"
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.getByRole('status')).toHaveTextContent(statusLabel);
    expect(screen.getByTitle('Stop Workday session before restarting')).toBeInTheDocument();
    expect(screen.queryByTitle('Start Workday session')).not.toBeInTheDocument();
  });

  test.each([
    ['blocked', 'Blocked'],
    ['error', 'Error'],
  ])('offers Start for %s when no live session blob remains', (sessionState, statusLabel) => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive={false}
        sessionStatus="ended"
        sessionState={sessionState}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.getByRole('status')).toHaveTextContent(statusLabel);
    expect(screen.getByTitle('Start Workday session')).toBeInTheDocument();
    expect(screen.queryByTitle(/Stop Workday session/)).not.toBeInTheDocument();
  });

  test.each([
    ['starting', 'Starting'],
    ['stopping', 'Stopping'],
  ])('shows the %s lifecycle instead of stale Active', (sessionState, statusLabel) => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionStatus="active"
        sessionState={sessionState}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.getByRole('status')).toHaveTextContent(statusLabel);
    expect(screen.getByTitle('Stop Workday session')).toBeEnabled();
    expect(screen.queryByTitle('Start Workday session')).not.toBeInTheDocument();
  });

  test('keeps the legacy active fallback when lifecycle state is absent', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionStatus="active"
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.getByRole('status')).toHaveTextContent('Active');
    expect(screen.getByTitle('Stop Workday session')).toBeInTheDocument();
  });
});

describe('StrategicSessionControl click-to-talk affordance', () => {
  test('chat mode shows Workday toggle but no Start or Stop button', () => {
    render(
      <StrategicSessionControl
        currentMode="chat"
        sessionActive={false}
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.getByLabelText('Switch Strategic mode to Workday')).toBeInTheDocument();
    expect(screen.queryByTitle('Start Workday session')).not.toBeInTheDocument();
    expect(screen.queryByTitle(/Stop Workday session/)).not.toBeInTheDocument();
  });

  test('clicking Workday toggle calls onModeChange with continuous', () => {
    const onModeChange = vi.fn();
    render(
      <StrategicSessionControl
        currentMode="chat"
        sessionActive={false}
        onModeChange={onModeChange}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByLabelText('Switch Strategic mode to Workday'));

    expect(onModeChange).toHaveBeenCalledTimes(1);
    expect(onModeChange).toHaveBeenCalledWith('continuous');
  });

  test('clicking already-selected Chat toggle does not call onModeChange', () => {
    const onModeChange = vi.fn();
    render(
      <StrategicSessionControl
        currentMode="chat"
        sessionActive={false}
        onModeChange={onModeChange}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByLabelText('Switch Strategic mode to Chat'));

    expect(onModeChange).not.toHaveBeenCalled();
  });

  test('continuous mode with no active session shows Start button and not Stop', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive={false}
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.getByTitle('Start Workday session')).toBeInTheDocument();
    expect(screen.queryByTitle(/Stop Workday session/)).not.toBeInTheDocument();
  });

  test('clicking Start Workday session calls onStartSession', () => {
    const onStartSession = vi.fn();
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive={false}
        onModeChange={vi.fn()}
        onStartSession={onStartSession}
        onStopSession={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByTitle('Start Workday session'));

    expect(onStartSession).toHaveBeenCalledTimes(1);
  });

  test('Start button is disabled when sessionBusy is true', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive={false}
        sessionBusy
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.getByTitle('Start Workday session')).toBeDisabled();
  });

  test('Start button is disabled when onStartSession is not provided', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive={false}
        onModeChange={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.getByTitle('Start Workday session')).toBeDisabled();
  });

  test('full click-to-talk sequence: toggle to Workday then click Start', () => {
    const onModeChange = vi.fn();
    const onStartSession = vi.fn();
    const { rerender } = render(
      <StrategicSessionControl
        currentMode="chat"
        sessionActive={false}
        onModeChange={onModeChange}
        onStartSession={onStartSession}
        onStopSession={vi.fn()}
      />,
    );

    // Step 1: Start button not visible in chat mode
    expect(screen.queryByTitle('Start Workday session')).not.toBeInTheDocument();

    // Step 2: Click Workday toggle
    fireEvent.click(screen.getByLabelText('Switch Strategic mode to Workday'));
    expect(onModeChange).toHaveBeenCalledWith('continuous');

    // Step 3: Parent updates mode (simulate mode change completing)
    rerender(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive={false}
        onModeChange={onModeChange}
        onStartSession={onStartSession}
        onStopSession={vi.fn()}
      />,
    );

    // Step 4: Start button is now visible and enabled
    const startBtn = screen.getByTitle('Start Workday session');
    expect(startBtn).toBeInTheDocument();
    expect(startBtn).not.toBeDisabled();

    // Step 5: Click Start — this is the operator's click-to-talk action
    fireEvent.click(startBtn);
    expect(onStartSession).toHaveBeenCalledTimes(1);
  });

  test('Workday toggle button is aria-pressed true when in continuous mode', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive={false}
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.getByLabelText('Switch Strategic mode to Workday')).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByLabelText('Switch Strategic mode to Chat')).toHaveAttribute('aria-pressed', 'false');
  });

  test('Workday toggle is disabled when saving is true', () => {
    render(
      <StrategicSessionControl
        currentMode="chat"
        saving
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.getByLabelText('Switch Strategic mode to Workday')).toBeDisabled();
    expect(screen.getByLabelText('Switch Strategic mode to Chat')).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Walkthrough harness selectors — FLOW 5 affordance contract
// ---------------------------------------------------------------------------
// The v26-live-walkthrough.cjs FLOW 5 probes the StrategicSessionControl with
// specific Playwright selectors.  These tests pin the exact attributes so a
// component refactor that breaks the selectors shows up immediately in the
// unit suite rather than surfacing only during a live walkthrough run.
//
// FLOW 5d: [data-testid="strategic-session-control"] button[title*="Stop Workday"]
// FLOW 5f: [data-testid="strategic-session-control"] + aria-label + title="Start Workday session"
// ---------------------------------------------------------------------------

describe('StrategicSessionControl walkthrough selector contract', () => {
  test('root element carries data-testid="strategic-session-control"', () => {
    const { container } = render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive={false}
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    // The walkthrough uses [data-testid="strategic-session-control"] as the
    // scoping ancestor for all child selectors.  It must be on the root div.
    expect(container.querySelector('[data-testid="strategic-session-control"]')).not.toBeNull();
  });

  test('Stop button title matches FLOW 5d selector button[title*="Stop Workday"]', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionStatus="active"
        sessionState="active"
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    // FLOW 5d looks for button[title*="Stop Workday"] inside the control root.
    // Both "Stop Workday session" and "Stop Workday session before restarting"
    // satisfy title*="Stop Workday", so the title must start with that prefix.
    const stopBtn = screen.getByTitle('Stop Workday session');
    expect(stopBtn.tagName).toBe('BUTTON');
    expect(stopBtn.title).toMatch(/^Stop Workday/);
  });

  test('Start button title matches FLOW 5f selector button[title="Start Workday session"]', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive={false}
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    // FLOW 5f looks for button[title="Start Workday session"] — exact match.
    const startBtn = screen.getByTitle('Start Workday session');
    expect(startBtn.tagName).toBe('BUTTON');
    expect(startBtn.title).toBe('Start Workday session');
  });

  test('Workday toggle aria-label matches FLOW 5f selector aria-label="Switch Strategic mode to Workday"', () => {
    render(
      <StrategicSessionControl
        currentMode="chat"
        sessionActive={false}
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    // FLOW 5f locates the Workday toggle via
    //   aria-label="Switch Strategic mode to Workday"
    // The label must be an exact string match for the Playwright locator.
    const toggle = screen.getByLabelText('Switch Strategic mode to Workday');
    expect(toggle.tagName).toBe('BUTTON');
    expect(toggle.getAttribute('aria-label')).toBe('Switch Strategic mode to Workday');
  });

  test('FLOW 5f full selector chain: scope → toggle aria-label → start title → click fires callback', () => {
    // Simulates the exact selector sequence FLOW 5f uses:
    //   1. Scope to [data-testid="strategic-session-control"]
    //   2. Find button[aria-label="Switch Strategic mode to Workday"] and click it
    //   3. After mode switch, find button[title="Start Workday session"] and click it
    const onModeChange = vi.fn();
    const onStartSession = vi.fn();

    const { container, rerender } = render(
      <StrategicSessionControl
        currentMode="chat"
        sessionActive={false}
        onModeChange={onModeChange}
        onStartSession={onStartSession}
        onStopSession={vi.fn()}
      />,
    );

    const root = container.querySelector('[data-testid="strategic-session-control"]');
    expect(root).not.toBeNull();
    if (!root) throw new Error('strategic-session-control root not found');

    // Click the Workday toggle scoped inside the root.
    const workdayToggle = root.querySelector('button[aria-label="Switch Strategic mode to Workday"]') as HTMLButtonElement;
    expect(workdayToggle).not.toBeNull();
    fireEvent.click(workdayToggle);
    expect(onModeChange).toHaveBeenCalledWith('continuous');

    // Re-render in continuous mode (parent would update props after mode change).
    rerender(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive={false}
        onModeChange={onModeChange}
        onStartSession={onStartSession}
        onStopSession={vi.fn()}
      />,
    );

    // Start button now available — click it scoped inside the root.
    const startBtn = root.querySelector('button[title="Start Workday session"]') as HTMLButtonElement;
    expect(startBtn).not.toBeNull();
    expect(startBtn.disabled).toBe(false);
    fireEvent.click(startBtn);
    expect(onStartSession).toHaveBeenCalledTimes(1);
  });
});
