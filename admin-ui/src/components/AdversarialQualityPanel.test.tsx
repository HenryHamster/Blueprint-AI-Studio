import { afterEach, describe, expect, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { AdversarialQualityPanel } from './AdversarialQualityPanel';

let axesPayload: unknown = null;

vi.mock('../api', () => ({
  fetchEfficiencyAxes: vi.fn(async () => axesPayload),
}));

const BASE_AXES = {
  axes: {},
  baseline_captured_at: null,
  last_snapshot_at: null,
};

function makeEffectiveness(overrides: object = {}) {
  return {
    challenged_confirmed: 8,
    challenged_revised: 2,
    challenged_rejected: 3,
    total_challenged: 13,
    overturn_rate: 0.2308,
    cost_per_challenge_usd: 0.0412,
    challenged_escalated: 0,
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

  test('shows NO DATA badge when no gate invocations', async () => {
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({ total_challenged: 0, challenged_confirmed: 0, challenged_revised: 0, challenged_rejected: 0 }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('NO DATA');
  });

  test('shows INSUFFICIENT badge when sample below minimum', async () => {
    // 13 challenges < 30 minimum — not enough data to confirm <=5%
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({ challenged_escalated: 0 }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('INSUFFICIENT');
  });

  test('shows HEALTHY badge when no fail-opens and sufficient sample', async () => {
    // total_challenged=35 >= 30 minimum, challenged_degraded=0 → confirmed-zero
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({ total_challenged: 35, challenged_confirmed: 30, challenged_revised: 3, challenged_rejected: 2 }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('HEALTHY');
  });

  test('shows PARTIAL badge when fail-open rate is between 1% and 5%', async () => {
    // 1 degraded out of 49 normal = 1/50 = 2% — between 1% PARTIAL and 5% DEGRADED thresholds
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({
        challenged_degraded: 1,
        total_challenged: 49,
      }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('PARTIAL');
  });

  test('shows DEGRADED badge when fail-open rate >= 5%', async () => {
    // 1 degraded out of 19 normal = 1/20 = 5% — at the operator-action threshold
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({
        challenged_degraded: 1,
        total_challenged: 19,
      }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('DEGRADED');
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
