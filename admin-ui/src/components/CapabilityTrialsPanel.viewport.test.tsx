import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { CapabilityTrialsPanel } from './CapabilityTrialsPanel';
import { expectNoOverflowAt, expectMinTouchTargetAt, expectNoTextTruncationAt } from '../test/viewport';

const sampleDigest = {
  capability_adoption_verdicts: {
    'repo-map-navigation': {
      verdict: 'adopt',
      lift: 0.1234,
      n_with: 18,
      n_without: 7,
      evaluated_at: '2026-06-09T12:00:00Z',
    },
    'tree-sitter-parsing': {
      verdict: 'pending',
      lift: -0.05,
      n_with: 5,
      n_without: 12,
      evaluated_at: '2026-06-08T08:00:00Z',
    },
    review_fix_lane: {
      verdict: 'CONTINUE',
      n_lane: 28,
      n_control: 26,
      lane_merge_rate: 0.32,
      control_merge_rate: 0.29,
      lane_cost_per_merged: 18.25,
      control_cost_per_merged: 20.1,
      checkpoint_loss_rate: 0.01,
      gate_integrity_violations: 0,
      immediate_alert: false,
      verdict_reason: 'accumulating_data',
      evaluated_at: '2026-06-10T08:00:00Z',
    },
  },
} as never;

describe('CapabilityTrialsPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) { act(() => { r.unmount(); }); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.width = '375px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<CapabilityTrialsPanel digest={sampleDigest} />);
      });
    });
  });

  test('renders without horizontal overflow at 768px', async () => {
    await expectNoOverflowAt(768, async () => {
      host = document.createElement('div');
      host.style.width = '768px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<CapabilityTrialsPanel digest={sampleDigest} />);
      });
    });
  });

  test('no interactive controls — touch-target check with none selector at 375px', async () => {
    await expectMinTouchTargetAt(375, async () => {
      host = document.createElement('div');
      host.style.width = '375px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<CapabilityTrialsPanel digest={sampleDigest} />);
      });
    }, { selector: 'none' });
  });

  test('capability names are not silently clipped at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.width = '375px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(<CapabilityTrialsPanel digest={sampleDigest} />);
      });
    });
  });
});
