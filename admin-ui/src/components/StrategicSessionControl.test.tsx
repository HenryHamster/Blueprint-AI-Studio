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
