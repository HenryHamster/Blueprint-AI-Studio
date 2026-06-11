import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, act } from '@testing-library/react';
import { OrchestratorHealthBar } from './OrchestratorHealthBar';

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

    const pill = screen.getByTitle(/Daily cost: \$5\.50\. Click/);
    expect(pill).toBeTruthy();
    expect(pill.textContent).toContain('$5.50');
    expect(pill.textContent).not.toContain('/');
    expect(pill.className).not.toContain('red');
    expect(pill.className).not.toContain('amber');
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
