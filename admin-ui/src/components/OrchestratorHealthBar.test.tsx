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
