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

  it('renders the durable check-in action without confusing it with mode selection', async () => {
    const onCheckIn = vi.fn();
    const user = userEvent.setup();
    const checkedAt = new Date(Date.now() - 2 * 60 * 60 * 1000).toISOString();

    render(
      <StrategicSessionControl
        currentMode="check_in"
        lastCheckIn={{ message_id: 'msg-check', created_at: checkedAt, kind: 'manual' }}
        sinceLastCheckIn={{
          version: 1,
          available: true,
          since: checkedAt,
          until: new Date().toISOString(),
          messages: { total: 2 },
          tasks: { created: 1, updated: 4 },
          alerts: { raised: 1 },
          strategic: { cycles_completed: 0 },
        }}
        onModeChange={vi.fn()}
        onCheckIn={onCheckIn}
      />,
    );

    expect(screen.getByRole('button', { name: /switch strategic mode to check-in/i })).toHaveAttribute('aria-pressed', 'true');
    const checkIn = screen.getByRole('button', { name: /mark strategic session checked/i });
    expect(checkIn).toHaveTextContent('Mark checked');
    expect(checkIn).toHaveTextContent('7');

    await user.click(checkIn);
    expect(onCheckIn).toHaveBeenCalledTimes(1);
  });

  it('shows first-run and saving states for the durable check-in action', async () => {
    const onCheckIn = vi.fn();
    const user = userEvent.setup();

    render(
      <StrategicSessionControl
        currentMode="chat"
        sinceLastCheckIn={{ version: 1, available: false, reason: 'no_prior_check_in' }}
        checkingIn
        onModeChange={vi.fn()}
        onCheckIn={onCheckIn}
      />,
    );

    const checkIn = screen.getByRole('button', { name: /mark strategic session checked: never checked/i });
    expect(checkIn).toBeDisabled();

    await user.click(checkIn);
    expect(onCheckIn).not.toHaveBeenCalled();
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
    expect(devices).toHaveAttribute('title', 'Open devices: 2/3 devices');
    expect(devices.getAttribute('aria-label')?.match(/2\/3 devices/g)).toHaveLength(1);

    await user.click(devices);
    expect(onOpenDevices).toHaveBeenCalledTimes(1);
  });

  it('surfaces local continuation and runner capacity in the devices control', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        localRunnerStatus={{
          mode: 'local_ready',
          strategic_runner_count: 3,
          live_runner_count: 2,
          eligible_live_count: 1,
          busy_runner_count: 1,
          continuation: { enabled: true, fresh: true, preferred_runner_id: 'runner-abcdef123456' },
        }}
        onModeChange={vi.fn()}
        onOpenDevices={vi.fn()}
      />,
    );

    expect(
      screen.getByRole('button', {
        name: /open devices: runner ready, continuation ready on runner runner-a, 2\/3 live, 1 ready, 1 busy, continue fresh/i,
      }),
    ).toBeInTheDocument();
  });

  it('treats stale local continuation as a warning state', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        localRunnerStatus={{
          mode: 'pending_local',
          continuation: { enabled: true, fresh: false, preferred_runner_id: 'runner-stale123' },
        }}
        onModeChange={vi.fn()}
        onOpenDevices={vi.fn()}
      />,
    );

    const devices = screen.getByRole('button', {
      name: /open devices: runner waiting, continuation is stale, continue stale/i,
    });
    expect(devices.className).toContain('border-amber-500/30');
  });

  it('renders backend runner reason codes as operator-readable detail', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        localRunnerStatus={{
          mode: 'pending_local',
          status_reason: 'at_capacity',
          fallback_reason: 'at_capacity',
          continuation: { enabled: true, fresh: true },
          busy_runner_count: 1,
        }}
        onModeChange={vi.fn()}
        onOpenDevices={vi.fn()}
      />,
    );

    expect(screen.getByText('Runner busy with active work')).toBeInTheDocument();
    expect(screen.queryByText('at_capacity')).not.toBeInTheDocument();
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
    expect(compute).toHaveAttribute('title', 'Open compute: 2 active / 1 queued');

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
    expect(compute).toHaveAttribute('title', 'Open compute: 1 active');
    expect(screen.getByText('Active')).toBeInTheDocument();

    await user.click(compute);
    expect(onOpenCompute).toHaveBeenCalledTimes(1);
  });

  it('surfaces local runner holds as runtime work that needs attention', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        runtimeWorkStatus={{ local_runner_holds: { count: 2 } }}
        onModeChange={vi.fn()}
        onOpenCompute={vi.fn()}
      />,
    );

    const compute = screen.getByRole('button', {
      name: /open compute: runtime work needs check, 2 local runner holds/i,
    });
    expect(compute.className).toContain('border-amber-500/30');
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
