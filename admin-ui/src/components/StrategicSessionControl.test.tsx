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

  it('offers only the Chat and Workday modes (check-in / monitor retired)', () => {
    render(<StrategicSessionControl currentMode="chat" onModeChange={vi.fn()} />);

    const modeGroup = screen.getByRole('group', { name: /strategic interaction mode/i });
    const modeButtons = within(modeGroup).getAllByRole('button');
    expect(modeButtons).toHaveLength(2);
    expect(within(modeGroup).getByRole('button', { name: /switch strategic mode to chat/i })).toBeInTheDocument();
    expect(within(modeGroup).getByRole('button', { name: /switch strategic mode to workday/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /switch strategic mode to check-in/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /switch strategic mode to monitor/i })).toBeNull();
  });

  it('renders a compact header control without a check-in button', () => {
    const { container } = render(
      <StrategicSessionControl
        currentMode="continuous"
        density="compact"
        showMonitoringControls={false}
        onModeChange={vi.fn()}
      />,
    );

    expect(screen.getByRole('button', { name: /switch strategic mode to workday/i })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByRole('button', { name: /mark strategic session checked/i })).toBeNull();
    expect(container.querySelector('[data-testid="strategic-session-control"]')?.textContent).not.toContain('Mark checked');
    expect(screen.queryByRole('button', { name: /open devices/i })).toBeNull();
  });

  it('calls onModeChange with backend mode values', async () => {
    const onModeChange = vi.fn();
    const user = userEvent.setup();

    const { rerender } = render(<StrategicSessionControl currentMode="chat" onModeChange={onModeChange} />);

    await user.click(screen.getByRole('button', { name: /switch strategic mode to workday/i }));
    expect(onModeChange).toHaveBeenNthCalledWith(1, 'continuous');

    rerender(<StrategicSessionControl currentMode="continuous" onModeChange={onModeChange} />);
    await user.click(screen.getByRole('button', { name: /switch strategic mode to chat/i }));
    expect(onModeChange).toHaveBeenNthCalledWith(2, 'chat');
  });

  it('disables mode changes while saving', async () => {
    const onModeChange = vi.fn();
    const user = userEvent.setup();

    render(<StrategicSessionControl currentMode="continuous" saving onModeChange={onModeChange} />);

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
    expect(devices).toHaveAttribute('title', 'Open Devices: 2/3 devices');
    expect(devices).toHaveTextContent('Devices');
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
          runners: [{ id: 'runner-abcdef123456', hostname: 'operator-desk', is_preferred: true }],
        }}
        onModeChange={vi.fn()}
        onOpenDevices={vi.fn()}
      />,
    );

    expect(
      screen.getByRole('button', {
        name: /open devices: runner ready, fresh continuation, preferred runner operator-desk runner-a, 2\/3 live, 1 eligible live, 1 busy/i,
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
      name: /open devices: runner waiting, stale continuation, preferred runner runner-s/i,
    });
    expect(devices.className).toContain('border-amber-500/30');
  });

  it('does not label unknown local continuation freshness as fresh', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        localRunnerStatus={{
          mode: 'pending_local',
          continuation: { enabled: true, preferred_runner_id: 'runner-unknown123' },
        }}
        onModeChange={vi.fn()}
        onOpenDevices={vi.fn()}
      />,
    );

    const devices = screen.getByRole('button', {
      name: /open devices: runner waiting, continuation status unknown, preferred runner runner-u/i,
    });
    expect(devices).toHaveTextContent('Continuation status unknown');
    expect(devices).not.toHaveTextContent('Fresh continuation');
    expect(devices.className).toContain('border-amber-500/30');
  });

  it('surfaces a workday marker owned by another operator without showing the owner id', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        localRunnerStatus={{
          mode: 'pending_local',
          continuation: {
            enabled: true,
            fresh: true,
            owner_user_id: 'other-operator',
            owned_by_current_operator: false,
            ownership: 'other_operator',
          },
        }}
        onModeChange={vi.fn()}
        onOpenDevices={vi.fn()}
      />,
    );

    const devices = screen.getByRole('button', {
      name: /open devices: runner waiting, workday owned by another operator, fresh continuation/i,
    });
    expect(devices.className).toContain('border-amber-500/30');
    expect(devices).toHaveTextContent('Workday owned by another operator');
    expect(devices.getAttribute('aria-label')).not.toContain('other-operator');
    expect(screen.queryByText('other-operator')).not.toBeInTheDocument();
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

  it('renders compact account spend health and opens accounts by default', async () => {
    const onOpenAccounts = vi.fn();
    const onOpenSpend = vi.fn();
    const user = userEvent.setup();

    render(
      <StrategicSessionControl
        currentMode="chat"
        accountSpend={{
          totalAccounts: 3,
          availableAccounts: 2,
          enabledAccounts: 2,
          activeAccountTasks: 1,
          configuredCapAccounts: 1,
          disabledModelCount: 2,
          trackedSpendUsd: 12.34,
          dailyCostCeilingUsd: 25,
        }}
        onModeChange={vi.fn()}
        onOpenAccounts={onOpenAccounts}
        onOpenSpend={onOpenSpend}
      />,
    );

    const accounts = screen.getByRole('button', { name: /open accounts: accounts cooldowns/i });
    expect(accounts).toHaveTextContent('Accounts');
    expect(accounts).toHaveTextContent('Cooldowns');
    expect(accounts).toHaveTextContent(
      /2\/3 available.*1 active.*1 with caps.*2 disabled models.*tracked \$12\.34.*daily ceiling \$25\.00/i,
    );

    await user.click(accounts);
    expect(onOpenAccounts).toHaveBeenCalledTimes(1);
    expect(onOpenSpend).not.toHaveBeenCalled();
  });

  it('surfaces no enabled accounts as a blocked account spend state', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        accountSpend={{
          totalAccounts: 4,
          availableAccounts: 0,
          enabledAccounts: 0,
          cooldownAccounts: 1,
          configuredCapAccounts: 2,
          disabledModelCount: 5,
          trackedSpendUsd: 29,
          dailyCostCeilingUsd: 30,
        }}
        onModeChange={vi.fn()}
        onOpenAccounts={vi.fn()}
      />,
    );

    const accounts = screen.getByRole('button', { name: /open accounts: accounts blocked/i });
    expect(accounts).toHaveTextContent('Blocked');
    expect(accounts).toHaveTextContent(
      /0\/4 available.*1 cooldown.*2 with caps.*5 disabled models.*tracked \$29\.00.*daily ceiling \$30\.00/i,
    );
    expect(accounts.className).toContain('border-red-500/30');
  });

  it('keeps configured account caps as neutral detail when availability is healthy', () => {
    render(
      <StrategicSessionControl
        currentMode="chat"
        accountSpend={{
          totalAccounts: 2,
          availableAccounts: 2,
          enabledAccounts: 2,
          configuredCapAccounts: 2,
          trackedSpendUsd: 0,
          dailyCostCeilingUsd: 0,
        }}
        onModeChange={vi.fn()}
        onOpenAccounts={vi.fn()}
      />,
    );

    const accounts = screen.getByRole('button', { name: /open accounts: accounts 2\/2 available/i });
    expect(accounts).toHaveTextContent('2/2 available');
    expect(accounts).toHaveTextContent('2 with caps');
    expect(accounts.className).not.toContain('border-amber-500/30');
  });

  it('does not describe an empty account set as ready', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        accountSpend={{
          totalAccounts: 0,
          availableAccounts: 0,
          enabledAccounts: 0,
          trackedSpendUsd: 0,
          dailyCostCeilingUsd: 0,
        }}
        onModeChange={vi.fn()}
        onOpenAccounts={vi.fn()}
      />,
    );

    const accounts = screen.getByRole('button', { name: /open accounts: accounts no accounts/i });
    expect(accounts).toHaveTextContent('No accounts');
    expect(accounts).not.toHaveTextContent('0/0 ready');
    expect(accounts).not.toHaveTextContent('$0.00 / $0.00 daily');
  });

  it('renders active runtime work as a compute button', async () => {
    const onOpenCompute = vi.fn();
    const user = userEvent.setup();

    render(
      <StrategicSessionControl
        currentMode="chat"
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
        currentMode="continuous"
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

  it('breaks out Strategic local runner holds when backend exposes them', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        runtimeWorkStatus={{ local_runner_holds: { count: 3, strategic_count: 2, compute_count: 1 } }}
        onModeChange={vi.fn()}
        onOpenCompute={vi.fn()}
      />,
    );

    const compute = screen.getByRole('button', {
      name: /open compute: runtime work needs check, 2 strategic holds \/ 1 compute hold/i,
    });
    expect(compute).toHaveTextContent('2 Strategic holds / 1 compute hold');
  });

  it('uses split local runner hold counts when aggregate count is absent', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        runtimeWorkStatus={{ local_runner_holds: { strategic_count: 2, compute_count: 1 } }}
        onModeChange={vi.fn()}
        onOpenCompute={vi.fn()}
      />,
    );

    const compute = screen.getByRole('button', {
      name: /open compute: runtime work needs check, 2 strategic holds \/ 1 compute hold/i,
    });
    expect(compute).toHaveTextContent('2 Strategic holds / 1 compute hold');
  });

  it('does not make idle runtime work open compute', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        runtimeWorkStatus={{ status: 'idle' }}
        onModeChange={vi.fn()}
        onOpenCompute={vi.fn()}
      />,
    );

    expect(screen.queryByRole('button', { name: /open compute/i })).toBeNull();
    expect(screen.getByLabelText(/runtime work idle/i)).toBeInTheDocument();
  });

  it('shows an off resident-session chip when the feature is off', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionState="off"
        sessionReason="sessions_disabled"
        onModeChange={vi.fn()}
      />,
    );

    expect(
      screen.getByRole('status', { name: /resident strategic session: sessions off/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /start resident strategic session/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /stop resident strategic session/i })).toBeNull();
  });

  it('still renders the legacy dark state as sessions off', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionState="dark"
        sessionReason="sessions_disabled"
        onModeChange={vi.fn()}
      />,
    );

    expect(
      screen.getByRole('status', { name: /resident strategic session: sessions off/i }),
    ).toBeInTheDocument();
  });

  it('shows a starting resident session with Stop and no Start', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionState="starting"
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    expect(
      screen.getByRole('status', { name: /resident strategic session: starting/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/agent not yet streaming/i)).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /stop resident strategic session/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /start resident strategic session/i })).toBeNull();
  });

  it('surfaces a no-runner reason with a Start fallback in Workday mode', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionState="blocked"
        sessionReason="no_runner"
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
      />,
    );

    expect(screen.getByText('No live local runner')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /start resident strategic session/i }),
    ).toBeInTheDocument();
  });

  it('offers an Add runner fix-it link for blocked:no_runner when Devices is wired', async () => {
    const onOpenDevices = vi.fn();
    const user = userEvent.setup();

    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionState="blocked"
        sessionReason="no_runner"
        showMonitoringControls={false}
        onModeChange={vi.fn()}
        onOpenDevices={onOpenDevices}
      />,
    );

    const addRunner = screen.getByRole('button', { name: /add a local runner: open devices/i });
    expect(addRunner).toHaveTextContent('Add runner');

    await user.click(addRunner);
    expect(onOpenDevices).toHaveBeenCalledTimes(1);
  });

  it('does not offer an Add runner link when Devices is not wired', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionState="blocked"
        sessionReason="no_runner"
        showMonitoringControls={false}
        onModeChange={vi.fn()}
      />,
    );

    expect(screen.queryByRole('button', { name: /add a local runner/i })).toBeNull();
  });

  it('surfaces a start_failed reason as an error state', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionState="error"
        sessionReason="start_failed: no task to host session"
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
      />,
    );

    expect(
      screen.getByRole('status', { name: /resident strategic session: start failed/i }),
    ).toBeInTheDocument();
    expect(screen.getByText(/start failed: no task to host session/i)).toBeInTheDocument();
  });

  it('shows an active resident session with Stop and no Start', () => {
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

    expect(
      screen.getByRole('status', { name: /resident strategic session: active/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /stop resident strategic session/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /start resident strategic session/i })).toBeNull();
  });

  it('shows a ready resident session that auto-starts on Workday entry', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionState="ready"
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
      />,
    );

    expect(
      screen.getByRole('status', { name: /resident strategic session: ready/i }),
    ).toBeInTheDocument();
  });

  it('renders the device picker and overrides the runner on selection', async () => {
    const onSelectRunner = vi.fn();
    const user = userEvent.setup();
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionState="ready"
        sessionRunners={[
          { id: 'r-1', name: 'desk-live', online: true, agent_ready: true, eligible: true },
          { id: 'r-2', name: 'desk-off', online: false, agent_ready: true, eligible: false },
        ]}
        sessionSelectedRunnerId="r-1"
        onSelectRunner={onSelectRunner}
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
      />,
    );

    const picker = screen.getByRole('combobox', { name: /workday device/i });
    expect(picker).toHaveValue('r-1');
    // The offline device is annotated in the option label.
    expect(screen.getByRole('option', { name: /desk-off — offline/i })).toBeInTheDocument();
    // Auto-pick is offered.
    expect(screen.getByRole('option', { name: /auto \(recommended\)/i })).toBeInTheDocument();

    await user.selectOptions(picker, 'r-2');
    expect(onSelectRunner).toHaveBeenCalledWith('r-2');

    await user.selectOptions(picker, '');
    expect(onSelectRunner).toHaveBeenLastCalledWith(null);
  });

  it('surfaces the restart prompt when the pinned device goes offline mid-session', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionStatus="active"
        sessionState="blocked"
        sessionReason="runner_offline"
        sessionRunners={[
          { id: 'r-1', name: 'desk-live', online: false, agent_ready: true, eligible: false },
        ]}
        sessionSelectedRunnerId="r-1"
        onSelectRunner={vi.fn()}
        onModeChange={vi.fn()}
        onStartSession={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );

    // Named device + explicit restart prompt (no auto-migration).
    expect(
      screen.getByText(/device desk-live offline — restart the session to pick another device/i),
    ).toBeInTheDocument();
    // Stop stays available so the operator can restart; Start does not show for a
    // still-"active" blob.
    expect(
      screen.getByRole('button', { name: /stop resident strategic session/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /start resident strategic session/i })).toBeNull();
    // The device dropdown remains so a new device can be chosen for the restart.
    expect(screen.getByRole('combobox', { name: /workday device/i })).toBeInTheDocument();
  });

  it('renders the operator-decision capture chip when a summary is present', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionStatus="active"
        sessionState="active"
        operatorDecisionSummary={{ total: 8, captured: 7, missed: 1, capture_rate: 88 }}
        onModeChange={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );
    expect(screen.getByText(/op-decision capture: 7\/8 \(88%\)/i)).toBeInTheDocument();
    // Healthy rate: chip reports corrections are captured (neutral, not a warning).
    expect(
      screen.getByRole('status', { name: /op-decision capture: 7\/8 \(88%\).*captured into the durable decisions register/i }),
    ).toBeInTheDocument();
  });

  it('warns on the capture chip when the rate is chronically low', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionStatus="active"
        sessionState="active"
        operatorDecisionSummary={{ total: 6, captured: 2, missed: 4, capture_rate: 33 }}
        onModeChange={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );
    expect(screen.getByText(/op-decision capture: 2\/6 \(33%\)/i)).toBeInTheDocument();
    expect(
      screen.getByRole('status', {
        name: /op-decision capture: 2\/6 \(33%\).*chronically low/i,
      }),
    ).toBeInTheDocument();
  });

  it('hides the operator-decision capture chip when no summary is present', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionStatus="active"
        sessionState="active"
        operatorDecisionSummary={null}
        onModeChange={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );
    expect(screen.queryByText(/op-decision capture/i)).toBeNull();
  });

  it('renders the turn-latency chip showing operator turn stats', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionStatus="active"
        sessionState="active"
        turnLatencySummary={{
          operator: { count: 3, mean_turn_ms: 2500, max_turn_ms: 4500, mean_ttft_ms: 400, interrupted_count: 0, total_tool_calls: 7, total_tool_call_duration_ms: 1200, mean_inter_token_ms: 85, mean_pickup_lag_ms: 320 },
          autonomous: { count: 5, mean_turn_ms: 8000, max_turn_ms: 15000, mean_ttft_ms: null, interrupted_count: 2, total_tool_calls: 12, total_tool_call_duration_ms: 3500, mean_inter_token_ms: null, mean_pickup_lag_ms: null },
        }}
        onModeChange={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );
    expect(screen.getByText(/operator:/i)).toBeInTheDocument();
    expect(screen.getByText(/3 turns/i)).toBeInTheDocument();
    expect(screen.getByText(/avg 2\.5s/i)).toBeInTheDocument();
  });

  it('renders the turn-latency chip with autonomous-only data when operator turns are absent', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionStatus="active"
        sessionState="active"
        turnLatencySummary={{
          operator: { count: 0, mean_turn_ms: null, max_turn_ms: 0, mean_ttft_ms: null, interrupted_count: 0, total_tool_calls: 0, total_tool_call_duration_ms: 0, mean_inter_token_ms: null, mean_pickup_lag_ms: null },
          autonomous: { count: 2, mean_turn_ms: 30000, max_turn_ms: 60000, mean_ttft_ms: null, interrupted_count: 0, total_tool_calls: 4, total_tool_call_duration_ms: 800, mean_inter_token_ms: null, mean_pickup_lag_ms: null },
        }}
        onModeChange={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );
    expect(screen.getByText(/autonomous:/i)).toBeInTheDocument();
    expect(screen.getByText(/2 turns/i)).toBeInTheDocument();
  });

  it('hides the turn-latency chip when the summary is absent', () => {
    render(
      <StrategicSessionControl
        currentMode="continuous"
        sessionActive
        sessionStatus="active"
        sessionState="active"
        turnLatencySummary={null}
        onModeChange={vi.fn()}
        onStopSession={vi.fn()}
      />,
    );
    expect(screen.queryByText(/turn/i)).toBeNull();
  });
});
