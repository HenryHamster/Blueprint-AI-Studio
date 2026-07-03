import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { AdversarialQualityPanel } from './AdversarialQualityPanel';

let axesPayload: unknown = null;

vi.mock('../api', async () => {
  const { createApiMock } = await import('../test/api-mock-complete');
  return createApiMock({
    fetchEfficiencyAxes: vi.fn(async () => axesPayload),
  });
});

const BASE_AXES = {
  axes: {},
  baseline_captured_at: null,
  last_snapshot_at: null,
};

function makeGateHealth(state: string, overrides: object = {}) {
  return { state, fail_open_rate: null, total_invocations: 0, min_sample: 30, ...overrides };
}

function makeEffectiveness(overrides: object = {}) {
  return {
    challenged_confirmed: 8,
    challenged_revised: 2,
    challenged_rejected: 3,
    total_challenged: 13,
    overturn_rate: 0.2308,
    cost_per_challenge_usd: 0.0412,
    challenged_escalated: 0,
    gate_health: makeGateHealth('INSUFFICIENT', { fail_open_rate: 0, total_invocations: 13 }),
    ...overrides,
  };
}

let container: HTMLDivElement;
let root: Root;

afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

function render(ui: React.ReactElement) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => root.render(ui));
  return container;
}

describe('AdversarialQualityPanel', () => {
  test('renders nothing when projectId is null', async () => {
    axesPayload = { ...BASE_AXES, panel_review_effectiveness: makeEffectiveness() };
    const el = render(<AdversarialQualityPanel projectId={null} />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="adversarial-quality-panel"]')).toBeNull();
  });

  test('renders nothing when panel_review_effectiveness is absent', async () => {
    axesPayload = { ...BASE_AXES, panel_review_effectiveness: null };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="adversarial-quality-panel"]')).toBeNull();
  });

  test('shows NO DATA badge when gate_health is absent from payload', async () => {
    // Frontend renders NO DATA when backendVerdict is not present (gate_health field missing)
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({ total_challenged: 0, challenged_confirmed: 0, challenged_revised: 0, challenged_rejected: 0, gate_health: undefined }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('NO DATA');
  });

  test('shows NO DATA badge when gate_health.state is NO_DATA', async () => {
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({
        total_challenged: 0, challenged_confirmed: 0, challenged_revised: 0, challenged_rejected: 0,
        gate_health: makeGateHealth('NO_DATA'),
      }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('NO DATA');
  });

  test('shows INSUFFICIENT badge when gate_health.state is INSUFFICIENT', async () => {
    // 13 challenges < 30 minimum — backend sets state=INSUFFICIENT
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({
        challenged_escalated: 0,
        gate_health: makeGateHealth('INSUFFICIENT', { fail_open_rate: 0, total_invocations: 13 }),
      }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('INSUFFICIENT');
  });

  test('shows HEALTHY badge when gate_health.state is HEALTHY', async () => {
    // total_challenged=35 >= 30 minimum, challenged_degraded=0 → backend sets state=HEALTHY
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({
        total_challenged: 35, challenged_confirmed: 30, challenged_revised: 3, challenged_rejected: 2,
        gate_health: makeGateHealth('HEALTHY', { fail_open_rate: 0, total_invocations: 35 }),
      }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('HEALTHY');
  });

  test('shows PARTIAL badge when gate_health.state is PARTIAL', async () => {
    // 1 degraded out of 49 normal = 2% — between 1% PARTIAL and 5% DEGRADED thresholds
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({
        challenged_degraded: 1,
        total_challenged: 49,
        gate_health: makeGateHealth('PARTIAL', { fail_open_rate: 0.02, total_invocations: 50 }),
      }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('PARTIAL');
  });

  test('shows DEGRADED badge when gate_health.state is DEGRADED', async () => {
    // 1 degraded out of 19 normal = 5% — at the operator-action threshold
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({
        challenged_degraded: 1,
        total_challenged: 19,
        gate_health: makeGateHealth('DEGRADED', { fail_open_rate: 0.05, total_invocations: 20 }),
      }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('DEGRADED');
  });

  test('renders HEALTHY from gate_health even when raw counts alone would say INSUFFICIENT', async () => {
    // Proof that frontend delegates verdict to backendVerdict, not its own threshold logic.
    // Only 5 invocations (below the old frontend GATE_MIN_SAMPLE=30), but gate_health.state=HEALTHY
    // from the backend — the badge must show HEALTHY, not INSUFFICIENT.
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({
        total_challenged: 5,
        challenged_confirmed: 5, challenged_revised: 0, challenged_rejected: 0,
        gate_health: makeGateHealth('HEALTHY', { fail_open_rate: 0, total_invocations: 5 }),
      }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('HEALTHY');
  });

  test('shows fail-open row when challenged_degraded > 0', async () => {
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({ challenged_degraded: 2, total_challenged: 10 }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    const row = el.querySelector('[data-testid="fail-open-row"]');
    expect(row).not.toBeNull();
    expect(row?.textContent).toContain('fail-open: 2');
  });

  test('shows distribution bar segments for non-zero counts', async () => {
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness(),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="bar-confirmed"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="bar-revised"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="bar-rejected"]')).not.toBeNull();
  });
});
