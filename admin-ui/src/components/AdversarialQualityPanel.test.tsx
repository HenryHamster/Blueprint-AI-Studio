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

  test('shows HEALTHY badge when no fail-opens', async () => {
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({ challenged_escalated: 0 }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('HEALTHY');
  });

  test('shows DEGRADED badge when fail-open rate >= 30%', async () => {
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({
        challenged_escalated: 6,
        total_challenged: 10,
      }),
    };
    const el = render(<AdversarialQualityPanel projectId="proj-1" />);
    await act(async () => {});
    expect(el.querySelector('[data-testid="health-badge"]')?.textContent).toBe('DEGRADED');
  });

  test('shows fail-open row when escalated > 0', async () => {
    axesPayload = {
      ...BASE_AXES,
      panel_review_effectiveness: makeEffectiveness({ challenged_escalated: 2, total_challenged: 10 }),
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
