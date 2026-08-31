import { render, screen, fireEvent } from '@testing-library/react';
import { describe, expect, test, vi } from 'vitest';
import { StrategicSessionControl } from './StrategicSessionControl';

describe('StrategicSessionControl keeps status readout in the utility bar', () => {
  test('renders lifecycle controls without a second Workday status readout', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionState="active"
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    // Exactly one legible mode control: a single group of mode toggle buttons.
    expect(screen.getAllByRole('group', { name: 'Strategic interaction mode' })).toHaveLength(1);
    expect(screen.getByLabelText('Switch Strategic mode to Chat')).toBeInTheDocument();
    expect(screen.getByLabelText('Switch Strategic mode to Workday')).toBeInTheDocument();

    expect(screen.queryByRole('status')).toBeNull();
  });

  test('does not derive a status label from session lifecycle inputs', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionState="active"
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByText(/Workday requested|2 open requests/)).toBeNull();
  });

  test('does not render a Ready fallback readout', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive={false}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByText('Ready')).toBeNull();
  });

  test('shows a spinner on the lifecycle button while sessionBusy is true', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionBusy
        onStartSession={vi.fn()}
      />,
    );

    expect(screen.getByTitle('Start Workday session').querySelector('.animate-spin')).not.toBeNull();
  });

  test('does not add a status glyph for an unavailable lifecycle state', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionState="error"
        sessionActive
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.getByTitle('Stop Workday session before restarting')).toBeInTheDocument();
  });

  test('offers stop-then-restart when the session lifecycle is blocked or errored, independent of the status label', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionState="blocked"
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(screen.getByTitle('Stop Workday session before restarting')).toBeInTheDocument();
    expect(screen.queryByTitle('Start Workday session')).not.toBeInTheDocument();
  });

  test('offers explicit Start for a terminal reply-recovery hold', () => {
    const onStartSession = vi.fn();
    render(<StrategicSessionControl currentMode="continuous" sessionActive={false}
      sessionState="recovery_required" onStartSession={onStartSession} onStopSession={vi.fn()} />);

    const start = screen.getByTitle('Start Workday session');
    expect(start).toBeInTheDocument();
    expect(screen.queryByTitle(/Stop Workday session/)).not.toBeInTheDocument();
    fireEvent.click(start);
    expect(onStartSession).toHaveBeenCalledTimes(1);
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

describe('StrategicSessionControl chat mode', () => {
  test('chat mode renders no duplicate live-status chip', () => {
    render(
      <StrategicSessionControl
        currentMode="chat"
        onModeChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('group', { name: 'Strategic interaction mode' })).toBeInTheDocument();
    expect(screen.queryByRole('status')).toBeNull();
    expect(screen.queryByTestId('strategic-chat-live-status')).toBeNull();
  });
});
