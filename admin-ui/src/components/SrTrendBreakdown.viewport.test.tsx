import { afterEach, describe, expect, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { SrTrendBreakdown, type SrTrendData } from './SrTrendBreakdown';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';

const sampleTrends: SrTrendData = {
  computed_at: '2026-05-06T00:00:00Z',
  bucket_mode: 'cycle',
  current_cycle_id: 'cycle-ccc333',
  previous_cycle_id: 'cycle-bbb222',
  prompt_version: 'a1b2c3d',
  categories: {
    correctness: {
      current_hit_rate: 0.72,
      previous_hit_rate: 0.81,
      current_human_count: 42,
      previous_human_count: 48,
      miss_rate: 0.28,
      weighted_miss_rate: 0.31,
      false_positive_rate: 0.12,
      precision: 0.88,
      trend: 'decreasing',
      rate_history: [0.85, 0.82, 0.79, 0.75, 0.72],
    },
    database_connection_timeout_handling: {
      current_hit_rate: 0.55,
      previous_hit_rate: 0.48,
      current_human_count: 35,
      previous_human_count: 32,
      miss_rate: 0.45,
      trend: 'increasing',
      rate_history: [0.42, 0.44, 0.48, 0.51, 0.55],
    },
    error_handling: {
      current_hit_rate: 0.63,
      previous_hit_rate: 0.65,
      current_human_count: 38,
      previous_human_count: 40,
      miss_rate: 0.37,
      trend: 'stable',
      rate_history: [0.66, 0.65, 0.64, 0.65, 0.63],
      examples: [
        { severity: 'CRITICAL', desc: 'Unhandled rejection in async fetch path — no .catch()' },
        { severity: 'MAJOR', desc: 'Missing null check on task.payloads before destructure' },
      ],
    },
    scope_drift: {
      current_hit_rate: 0.88,
      previous_hit_rate: 0.86,
      current_human_count: 28,
      previous_human_count: 30,
      miss_rate: 0.12,
      trend: 'increasing',
      rate_history: [0.84, 0.85, 0.86, 0.87, 0.88],
    },
    type_safety: {
      current_hit_rate: 0.41,
      previous_hit_rate: 0.44,
      current_human_count: 29,
      previous_human_count: 31,
      miss_rate: 0.59,
      trend: 'decreasing',
      rate_history: [0.5, 0.47, 0.45, 0.43, 0.41],
    },
  },
  sr_skip_rate: 0.08,
  sr_skip_reason_distribution: { no_diff: 4, trivial: 2 },
  by_fix_iteration: {
    '1': {
      correctness: { miss_rate: 0.18, sample_count: 42, all_iterations_miss_rate: 0.21 },
      'error-handling': { miss_rate: 0.22, sample_count: 33 },
      'type-safety': { miss_rate: 0.55, sample_count: 28 },
    },
    '2': {
      correctness: { miss_rate: 0.31, sample_count: 18 },
      'error-handling': { miss_rate: 0.27, sample_count: 14 },
    },
    '3+': {
      correctness: { miss_rate: 0.62, sample_count: 9, all_iterations_miss_rate: 0.7 },
      'error-handling': { miss_rate: 0.55, sample_count: 6 },
    },
  },
};

describe('SrTrendBreakdown — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    const r = root;
    if (r) {
      act(() => {
        r.unmount();
      });
      root = null;
    }
    if (host) {
      host.remove();
      host = null;
    }
  });

  test('card variant: category rows fit without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <SrTrendBreakdown trends={sampleTrends} variant="card" />,
        );
      });
    });
  });

  test('card variant: by-fix-iteration section toggles open and fits at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <SrTrendBreakdown trends={sampleTrends} variant="card" />,
        );
      });
      const section = host.querySelector('[data-testid="sr-by-fix-iteration-section"]');
      expect(section).not.toBeNull();
      const toggle = (section as HTMLElement).querySelector('button[aria-expanded]') as HTMLButtonElement;
      expect(toggle.getAttribute('aria-expanded')).toBe('false');
      await act(async () => {
        toggle.click();
      });
      expect(toggle.getAttribute('aria-expanded')).toBe('true');
    });
  });

  test('card variant: by-fix-iteration open state fits at 1024px', async () => {
    await expectNoOverflowAt(1024, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <SrTrendBreakdown trends={sampleTrends} variant="card" />,
        );
      });
      const toggle = host.querySelector(
        '[data-testid="sr-by-fix-iteration-section"] button[aria-expanded]',
      ) as HTMLButtonElement;
      await act(async () => {
        toggle.click();
      });
    });
  });

  test('inline variant: category rows fit without overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <SrTrendBreakdown trends={sampleTrends} variant="inline" />,
        );
      });
    });
  });

  test('category labels and rate text do not silently clip at 375px', async () => {
    await expectNoTextTruncationAt(375, async () => {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      const r = createRoot(host);
      root = r;
      await act(async () => {
        r.render(
          <SrTrendBreakdown trends={sampleTrends} variant="card" />,
        );
      });
    });
  });
});
