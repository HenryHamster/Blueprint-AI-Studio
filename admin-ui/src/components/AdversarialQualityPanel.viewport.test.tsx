import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { AdversarialQualityPanel } from './AdversarialQualityPanel';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

let axesPayload: unknown = null;

vi.mock('../api', () => ({
  fetchEfficiencyAxes: vi.fn(async () => axesPayload),
}));

const BASE_AXES = {
  axes: {},
  baseline_captured_at: null,
  last_snapshot_at: null,
};

axesPayload = {
  ...BASE_AXES,
  panel_review_effectiveness: {
    challenged_confirmed: 8,
    challenged_revised: 2,
    challenged_rejected: 3,
    total_challenged: 13,
    overturn_rate: 0.2308,
    cost_per_challenge_usd: 0.0412,
    challenged_escalated: 2,
  },
};

describe('AdversarialQualityPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  async function mount(width: number) {
    host = document.createElement('div');
    host.style.width = `${width}px`;
    document.body.appendChild(host);
    const r = createRoot(host);
    root = r;
    await act(async () => {
      r.render(<AdversarialQualityPanel projectId="proj-1" />);
    });
    // resolve the async fetch effect
    await act(async () => {});
  }

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => { await mount(375); });
  });

  test('renders without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => { await mount(768); });
  });

  test('no interactive controls — touch-target check with none selector at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => { await mount(375); }, { selector: 'none' });
  });

  test('metrics are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => { await mount(375); });
  });
});
