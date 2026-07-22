import { render, screen } from '@testing-library/react';
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
