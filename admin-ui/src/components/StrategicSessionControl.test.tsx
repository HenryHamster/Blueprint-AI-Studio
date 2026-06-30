import { describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { StrategicSessionControl } from './StrategicSessionControl';

describe('StrategicSessionControl', () => {
  it('renders the selected Strategic interaction mode', () => {
    render(<StrategicSessionControl currentMode="continuous" onModeChange={vi.fn()} />);

    const workday = screen.getByRole('button', { name: /switch strategic mode to workday/i });
    expect(workday).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: /switch strategic mode to chat/i })).toHaveAttribute('aria-pressed', 'false');
  });

  it('calls onModeChange with backend mode values', async () => {
    const onModeChange = vi.fn();
    const user = userEvent.setup();

    render(<StrategicSessionControl currentMode="chat" onModeChange={onModeChange} />);

    await user.click(screen.getByRole('button', { name: /switch strategic mode to check-in/i }));
    await user.click(screen.getByRole('button', { name: /switch strategic mode to monitor/i }));
    await user.click(screen.getByRole('button', { name: /switch strategic mode to workday/i }));

    expect(onModeChange).toHaveBeenNthCalledWith(1, 'check_in');
    expect(onModeChange).toHaveBeenNthCalledWith(2, 'monitor');
    expect(onModeChange).toHaveBeenNthCalledWith(3, 'continuous');
  });

  it('disables mode changes while saving', async () => {
    const onModeChange = vi.fn();
    const user = userEvent.setup();

    render(<StrategicSessionControl currentMode="monitor" saving onModeChange={onModeChange} />);

    const modeGroup = screen.getByRole('group', { name: /strategic interaction mode/i });
    for (const button of within(modeGroup).getAllByRole('button')) {
      expect(button).toBeDisabled();
    }
    expect(screen.getByRole('status', { name: /saving strategic session mode/i })).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: /switch strategic mode to chat/i }));
    expect(onModeChange).not.toHaveBeenCalled();
  });

  it('renders a local runner pill that opens devices', async () => {
    const onOpenDevices = vi.fn();
    const user = userEvent.setup();

    render(
      <StrategicSessionControl
        currentMode="chat"
        localRunnerStatus={{ status: 'online', onlineDevices: 2, totalDevices: 3 }}
        onModeChange={vi.fn()}
        onOpenDevices={onOpenDevices}
      />,
    );

    const devices = screen.getByRole('button', { name: /open devices: runner online, 2\/3 devices/i });
    expect(devices).toHaveAttribute('title', 'Open devices');

    await user.click(devices);
    expect(onOpenDevices).toHaveBeenCalledTimes(1);
  });

  it('renders active runtime work as a compute button', async () => {
    const onOpenCompute = vi.fn();
    const user = userEvent.setup();

    render(
      <StrategicSessionControl
        currentMode="check_in"
        runtimeWorkStatus={{ status: 'running', runningCount: 2, queuedCount: 1 }}
        onModeChange={vi.fn()}
        onOpenCompute={onOpenCompute}
      />,
    );

    const compute = screen.getByRole('button', { name: /open compute: runtime work active, 2 active \/ 1 queued/i });
    expect(compute).toHaveAttribute('title', 'Open compute');

    await user.click(compute);
    expect(onOpenCompute).toHaveBeenCalledTimes(1);
  });

  it('test_active_count_matches_rail_for_parked_wakes', async () => {
    const onOpenCompute = vi.fn();
    const user = userEvent.setup();

    render(
      <StrategicSessionControl
        currentMode="monitor"
        runtimeWorkStatus={{
          status: 'running',
          compute_jobs: { counts: { active: 0 } },
          parked_wakes: { counts: { active: 1 } },
        }}
        onModeChange={vi.fn()}
        onOpenCompute={onOpenCompute}
      />,
    );

    const compute = screen.getByRole('button', { name: /open compute: runtime work active, 1 active/i });
    expect(compute).toHaveAttribute('title', 'Open compute');
    expect(screen.getByText('Active')).toBeInTheDocument();

    await user.click(compute);
    expect(onOpenCompute).toHaveBeenCalledTimes(1);
  });

  it('does not make idle runtime work open compute', () => {
    render(
      <StrategicSessionControl
        currentMode="monitor"
        runtimeWorkStatus={{ status: 'idle' }}
        onModeChange={vi.fn()}
        onOpenCompute={vi.fn()}
      />,
    );

    expect(screen.queryByRole('button', { name: /open compute/i })).toBeNull();
    expect(screen.getByLabelText(/runtime work idle/i)).toBeInTheDocument();
  });
});
