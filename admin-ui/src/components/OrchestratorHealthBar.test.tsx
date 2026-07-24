import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { OrchestratorHealthBar } from './OrchestratorHealthBar';
import type { Task } from './TaskCard';

const { apiFetchMock } = vi.hoisted(() => ({
  apiFetchMock: vi.fn(),
}));

vi.mock('../api', () => ({
  apiFetch: (path: string, init?: RequestInit) => apiFetchMock(path, init),
}));

const baseProps = {
  health: null,
  detailedHealth: null,
  apiHealthy: true,
  wsConnected: true,
  lastFetchTime: Date.now(),
  tasks: [],
  pipelineThrottle: null,
};

function makeCostResponse(daily_cost_usd: number, daily_cost_ceiling_usd: number) {
  return {
    ok: true,
    json: async () => ({ daily_cost_usd, daily_cost_ceiling_usd }),
  } as Response;
}

afterEach(() => {
  vi.useRealTimers();
});

describe('OrchestratorHealthBar cost pill', () => {
  beforeEach(() => {
    apiFetchMock.mockResolvedValue(makeCostResponse(0, 0));
  });

  it('shows neutral pill with no ceiling (absolute spend only)', async () => {
    apiFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ daily_cost_usd: 5.50, daily_cost_ceiling_usd: 0 }),
    } as Response);

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} />); });

    expect(apiFetchMock.mock.calls[0]?.[0]).toBe('/api/admin/digest?hours=24');

    const pill = screen.getByTitle('Daily cost: $5.50.');
    expect(pill).toBeTruthy();
    expect(pill.textContent).toContain('$5.50');
    expect(pill.textContent).not.toContain('/');
    expect(pill.className).not.toContain('red');
    expect(pill.className).not.toContain('amber');
  });

  it('only promises cost-section navigation when the active surface exposes it', async () => {
    apiFetchMock.mockResolvedValue(makeCostResponse(5.5, 0));
    const anchor = document.createElement('div');
    anchor.id = 'cost-section';
    document.body.appendChild(anchor);

    try {
      await act(async () => { render(<OrchestratorHealthBar {...baseProps} />); });
      expect(screen.queryByTitle(/Click to view cost section/)).toBeNull();
    } finally {
      anchor.remove();
    }
  });

  it('promises cost-section navigation when costSectionAvailable is true', async () => {
    apiFetchMock.mockResolvedValue(makeCostResponse(5.5, 0));

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} costSectionAvailable />); });
    const pill = screen.getByTitle(/Daily cost: \$5\.50\. Click to view cost section\./);
    expect(pill).toBeTruthy();
  });

  it('shows neutral pill when under 60% of ceiling', async () => {
    apiFetchMock.mockResolvedValue(makeCostResponse(25, 100));

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} />); });

    const pill = screen.getByTitle(/Daily cost: \$25\.00 \/ \$100\.00 ceiling \(25%\)/);
    expect(pill).toBeTruthy();
    expect(pill.className).not.toContain('red');
    expect(pill.className).not.toContain('amber');
  });

  it('shows amber pill between 60% and 90% of ceiling', async () => {
    apiFetchMock.mockResolvedValue(makeCostResponse(70, 100));

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} />); });

    const pill = screen.getByTitle(/\(70%\)/);
    expect(pill.className).toContain('amber');
    expect(pill.className).not.toContain('red');
  });

  it('shows red pill at or above 90% of ceiling', async () => {
    apiFetchMock.mockResolvedValue(makeCostResponse(95, 100));

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} />); });

    const pill = screen.getByTitle(/\(95%\)/);
    expect(pill.className).toContain('red');
  });

  it('shows red pill with warning when at or above ceiling', async () => {
    apiFetchMock.mockResolvedValue(makeCostResponse(110, 100));

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} />); });

    const pill = screen.getByTitle(/\(110%\)/);
    expect(pill.className).toContain('red');
    expect(pill.textContent).toContain('⚠');
  });

  it('shows dollar amounts in pill label', async () => {
    apiFetchMock.mockResolvedValue(makeCostResponse(12.43, 50));

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} />); });

    const pill = screen.getByTitle(/\$12\.43 \/ \$50\.00/);
    expect(pill.textContent).toContain('$12.43 / $50.00');
  });

  it('marks the cost pill stale after a failed digest poll and clears it on recovery', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-13T12:00:00Z'));
    apiFetchMock
      .mockResolvedValueOnce(makeCostResponse(12.43, 50))
      .mockResolvedValueOnce({ ok: false, json: async () => ({}) } as Response)
      .mockResolvedValueOnce(makeCostResponse(15, 50));

    render(<OrchestratorHealthBar {...baseProps} />);
    await act(async () => {});

    expect(screen.getByTitle(/Daily cost: \$12\.43 \/ \$50\.00/).textContent).not.toContain('as of');

    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });

    const stalePill = screen.getByTitle(/Last digest poll failed; showing values as of 1m ago/);
    expect(stalePill).toHaveTextContent('$12.43 / $50.00');
    expect(stalePill).toHaveTextContent('as of 1m ago');
    expect(stalePill.className).toContain('opacity-75');

    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });

    const recoveredPill = screen.getByTitle(/Daily cost: \$15\.00 \/ \$50\.00/);
    expect(recoveredPill).toHaveTextContent('$15.00 / $50.00');
    expect(recoveredPill).not.toHaveTextContent('as of');
    expect(recoveredPill.className).not.toContain('opacity-75');
  });

  it('dims cost pill when 200 response contains served_stale:true', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-13T12:00:00Z'));
    apiFetchMock
      .mockResolvedValueOnce(makeCostResponse(12.43, 50))
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ daily_cost_usd: 12.43, daily_cost_ceiling_usd: 50, served_stale: true }),
      } as Response);

    render(<OrchestratorHealthBar {...baseProps} />);
    await act(async () => {});

    expect(screen.getByTitle(/Daily cost: \$12\.43 \/ \$50\.00/).textContent).not.toContain('as of');

    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });

    const stalePill = screen.getByTitle(/Last digest poll failed; showing values as of 1m ago/);
    expect(stalePill).toHaveTextContent('$12.43 / $50.00');
    expect(stalePill).toHaveTextContent('as of 1m ago');
    expect(stalePill.className).toContain('opacity-75');
  });

  it('renders a gated-task summary chip when per-type gates are active', async () => {
    await act(async () => {
      render(
        <OrchestratorHealthBar
          {...baseProps}
          gatedTaskTypes={[
            { task_type: 'create', failure_rate: 0.83, sample_size: 12, threshold: 0.6 },
            { task_type: 'fix', failure_rate: 0.67, sample_size: 5, threshold: 0.6 },
          ]}
        />,
      );
    });

    const chip = screen.getByTitle(/Per-type dispatch gates active:/);
    expect(chip).toHaveTextContent('Gated:');
    expect(chip).toHaveTextContent('create');
    expect(chip).toHaveTextContent('fix');
    expect(chip.getAttribute('title')).toContain('83% failure rate, n=12');
    expect(chip.getAttribute('title')).toContain('67% failure rate, n=5');
  });
});

describe('OrchestratorHealthBar cycle liveness verdict', () => {
  beforeEach(() => {
    apiFetchMock.mockResolvedValue(makeCostResponse(0, 0));
  });

  function healthWith(overrides: Record<string, unknown>) {
    return {
      last_cycle_seconds_ago: 0,
      queued_tasks: 0,
      active_tasks: 0,
      pool_info: {},
      ...overrides,
    };
  }

  it('renders green/healthy when progressing with a fresh cycle', async () => {
    const health = healthWith({
      last_cycle_seconds_ago: 5,
      orchestrator_alive: true,
      orchestrator_hung: false,
      orchestrator_progress_age_seconds: 3,
      orchestrator_cycle_age_seconds: 5,
      orchestrator_liveness_reason: 'progressing',
    });

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} health={health} />); });

    const cycle = screen.getByTitle(/Orchestrator progressing .* healthy/);
    expect(cycle.innerHTML).toContain('bg-emerald-500');
    expect(cycle.innerHTML).not.toContain('bg-red-500');
    expect(cycle.innerHTML).not.toContain('bg-amber-400');
    expect(cycle.textContent).not.toContain('⚠');
  });

  it('renders amber/degraded (NOT down) for a slow-but-progressing cycle', async () => {
    // Cycle completion is stale (well past the legacy 120s "down" threshold)
    // but the progress heartbeat is fresh, so the worker is healthy.
    const health = healthWith({
      last_cycle_seconds_ago: 600,
      orchestrator_alive: true,
      orchestrator_hung: false,
      orchestrator_progress_age_seconds: 4,
      orchestrator_cycle_age_seconds: 600,
      orchestrator_liveness_reason: 'progressing',
    });

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} health={health} />); });

    const cycle = screen.getByTitle(/Slow cycle .* still progressing .* healthy/);
    expect(cycle.innerHTML).toContain('bg-amber-400');
    expect(cycle.innerHTML).not.toContain('bg-red-500');
    // Slow-cycle is informational only — no down warning marker.
    expect(cycle.textContent).not.toContain('⚠');
  });

  it('stays green/healthy (NOT amber) for a 60-119s progressing cycle — matches backend 120s slow-cycle contract', async () => {
    // Backend surfaces slow/degraded only at >=120s (main.py: slow_cycle =
    // cycle_age >= 120). A 90s progressing cycle must read healthy here too,
    // otherwise operator surfaces contradict each other in the 60-119s band.
    const health = healthWith({
      last_cycle_seconds_ago: 90,
      orchestrator_alive: true,
      orchestrator_hung: false,
      orchestrator_progress_age_seconds: 3,
      orchestrator_cycle_age_seconds: 90,
      orchestrator_liveness_reason: 'progressing',
    });

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} health={health} />); });

    const cycle = screen.getByTitle(/Orchestrator progressing .* healthy/);
    expect(cycle.innerHTML).toContain('bg-emerald-500');
    expect(cycle.innerHTML).not.toContain('bg-amber-400');
    expect(cycle.innerHTML).not.toContain('bg-red-500');
    expect(cycle.textContent).not.toContain('~');
  });

  it('renders red/down when the orchestrator is genuinely wedged (hung)', async () => {
    const health = healthWith({
      last_cycle_seconds_ago: 30,
      orchestrator_alive: false,
      orchestrator_hung: true,
      orchestrator_progress_age_seconds: 900,
      orchestrator_cycle_age_seconds: 30,
      orchestrator_liveness_reason: 'no progress heartbeat for 900s',
    });

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} health={health} />); });

    const cycle = screen.getByTitle(/Orchestrator not progressing/);
    expect(cycle.innerHTML).toContain('bg-red-500');
    expect(cycle.innerHTML).not.toContain('bg-emerald-500');
    expect(cycle.textContent).toContain('⚠');
  });

  it('does NOT go down for a stale cycle when progress is fresh (no false wedge)', async () => {
    const health = healthWith({
      last_cycle_seconds_ago: 130,
      orchestrator_alive: true,
      orchestrator_hung: false,
      orchestrator_progress_age_seconds: 2,
      orchestrator_cycle_age_seconds: 130,
      orchestrator_liveness_reason: 'progressing',
    });

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} health={health} />); });

    // Would have been red under the old cycle-age-only verdict (≥120s).
    const cycle = screen.getByTitle(/still progressing .* healthy/);
    expect(cycle.innerHTML).not.toContain('bg-red-500');
  });

  it('falls back to legacy cycle-age thresholds when liveness fields are absent', async () => {
    const health = healthWith({ last_cycle_seconds_ago: 132 });

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} health={health} />); });

    const cycle = screen.getByTitle(/Last cycle 132s ago — down \(threshold: ≥120s\)/);
    expect(cycle.innerHTML).toContain('bg-red-500');
    expect(cycle.textContent).toContain('⚠');
  });

  it('stays green on cold-start (alive=false, stalled=false) — not a genuine hang', async () => {
    // Before the first cycle completes, evaluate_orchestrator_liveness returns
    // alive=false, stalled=false. The dot must stay green so cold-start does
    // not trigger a false-positive red indicator.
    const health = healthWith({
      last_cycle_seconds_ago: null,
      orchestrator_alive: false,
      orchestrator_hung: false,
      orchestrator_progress_age_seconds: null,
      orchestrator_liveness_reason: 'no cycle seen',
    });

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} health={health} />); });

    const cycle = screen.getByTitle(/Orchestrator starting — no cycle recorded yet/);
    expect(cycle.innerHTML).toContain('bg-emerald-500');
    expect(cycle.innerHTML).not.toContain('bg-red-500');
    expect(cycle.textContent).not.toContain('⚠');
  });

  it('shows "starting" tooltip hint on cold-start (alive=false, not stalled)', async () => {
    const health = healthWith({
      last_cycle_seconds_ago: null,
      orchestrator_alive: false,
      orchestrator_hung: false,
      orchestrator_progress_age_seconds: null,
      orchestrator_liveness_reason: 'no cycle seen',
    });

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} health={health} />); });

    expect(screen.getByTitle(/Orchestrator starting — no cycle recorded yet/)).toBeTruthy();
  });
});

describe('OrchestratorHealthBar deploy state', () => {
  beforeEach(() => {
    apiFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ daily_cost_usd: 0, daily_cost_ceiling_usd: 0 }),
    } as Response);
  });

  it('shows amber PAUSED indicator with pause reason', async () => {
    const health = {
      last_cycle_seconds_ago: 0,
      queued_tasks: 0,
      active_tasks: 0,
      pool_info: {},
      deploy_state: {
        status: 'PAUSED',
        rollback_count: 0,
        rollback_reason: null,
        pause_reason: 'External dependency down: payment service unavailable',
        monitoring_since: '2026-05-06T20:00:00Z',
        recent_outcomes: [],
      },
      deploy_monitoring: {
        external_health: {},
      },
    };

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} health={health} />); });

    const deployIndicator = screen.getByTitle(/Paused: External dependency down/);
    expect(deployIndicator).toBeTruthy();
    expect(deployIndicator.innerHTML).toContain('bg-amber-400');
    expect(deployIndicator.textContent).toContain('Paused');
    expect(deployIndicator.textContent).toContain('External dependency down');
  });
});

describe('OrchestratorHealthBar stale heartbeat warning', () => {
  beforeEach(() => {
    apiFetchMock.mockResolvedValue({
      ok: true,
      json: async () => ({ daily_cost_usd: 0, daily_cost_ceiling_usd: 0 }),
    } as Response);
  });

  function tasksWithStaleHeartbeats(count: number): Task[] {
    return Array.from({ length: count }, (_, idx) => ({
      id: `task-${idx + 1}`,
      title: `Task ${idx + 1}`,
      description: '',
      created_by: 'test',
      requires_approval: false,
      created_at: '2026-05-06T00:00:00Z',
      status: 'IN_PROGRESS' as const,
      last_heartbeat: '2026-05-06T22:30:00Z',
    }));
  }

  it('uses the fleet digest stale-heartbeat count instead of the page slice', async () => {
    apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ daily_cost_usd: 0, daily_cost_ceiling_usd: 0, stale_heartbeat_count: 7 }),
    } as Response);

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} tasks={tasksWithStaleHeartbeats(2)} />); });

    const warning = screen.getByTitle(/7 IN_PROGRESS tasks with no heartbeat in the last 5 minutes \(fleet-wide\)/);
    expect(warning.textContent).toContain('7 stale');
    expect(warning.textContent).not.toContain('2 stale');
  });

  it('hides the warning when the fleet digest reports zero stale heartbeats', async () => {
    apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ daily_cost_usd: 0, daily_cost_ceiling_usd: 0, stale_heartbeat_count: 0 }),
    } as Response);

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} tasks={tasksWithStaleHeartbeats(2)} />); });

    expect(screen.queryByText(/stale$/)).toBeNull();
  });

  it('falls back to the current page slice when the digest omits stale_heartbeat_count', async () => {
    apiFetchMock.mockResolvedValueOnce({
      ok: true,
      json: async () => ({ daily_cost_usd: 0, daily_cost_ceiling_usd: 0 }),
    } as Response);

    await act(async () => { render(<OrchestratorHealthBar {...baseProps} tasks={tasksWithStaleHeartbeats(2)} />); });

    const warning = screen.getByTitle(/2 IN_PROGRESS tasks with no heartbeat in the last 5 minutes \(current page only — fleet count unavailable\)/);
    expect(warning.textContent).toContain('2 stale');
  });

  it('marks the fleet stale-heartbeat count stale after a failed digest poll and clears it on recovery', async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-06-13T12:00:00Z'));
    apiFetchMock
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ daily_cost_usd: 0, daily_cost_ceiling_usd: 0, stale_heartbeat_count: 7 }),
      } as Response)
      .mockRejectedValueOnce(new Error('digest unavailable'))
      .mockResolvedValueOnce({
        ok: true,
        json: async () => ({ daily_cost_usd: 0, daily_cost_ceiling_usd: 0, stale_heartbeat_count: 3 }),
      } as Response);

    render(<OrchestratorHealthBar {...baseProps} tasks={tasksWithStaleHeartbeats(2)} />);
    await act(async () => {});

    expect(screen.getByTitle(/7 IN_PROGRESS tasks .* \(fleet-wide\)/)).toHaveTextContent('7 stale');

    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });

    const staleWarning = screen.getByTitle(/7 IN_PROGRESS tasks .* Last digest poll failed; showing values as of 1m ago/);
    expect(staleWarning).toHaveTextContent('7 stale');
    expect(staleWarning).toHaveTextContent('as of 1m ago');
    expect(staleWarning.className).toContain('opacity-75');

    await act(async () => { await vi.advanceTimersByTimeAsync(60_000); });

    const recoveredWarning = screen.getByTitle(/3 IN_PROGRESS tasks .* \(fleet-wide\)/);
    expect(recoveredWarning).toHaveTextContent('3 stale');
    expect(recoveredWarning).not.toHaveTextContent('as of');
    expect(recoveredWarning.className).not.toContain('opacity-75');
  });
});
