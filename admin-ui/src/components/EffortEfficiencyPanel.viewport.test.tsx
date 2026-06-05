import { afterEach, beforeEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { EffortEfficiencyPanel } from './EffortEfficiencyPanel';
import { expectNoOverflowAt } from '../test/viewport';
import type { EffortModelEfficiency, EfficiencyBucket } from '../types/digest';

function bucket(overrides: Partial<EfficiencyBucket> = {}): EfficiencyBucket {
  return {
    run_count: 10,
    landed_count: 6,
    landed_rate: 0.6,
    total_cost_usd: 12.5,
    cost_per_merge: 2.08,
    input_tokens: 1_200_000,
    output_tokens: 300_000,
    llm_call_count: 40,
    priced_run_count: 9,
    unpriced_run_count: 1,
    unpriced_coverage_fraction: 0.9,
    ...overrides,
  };
}

function makeData(): EffortModelEfficiency {
  return {
    by_effort: [
      { ...bucket(), effort: 'low', resolved_models: [] },
      { ...bucket(), effort: 'medium', resolved_models: [] },
      { ...bucket(), effort: 'high', resolved_models: [] },
      { ...bucket(), effort: 'max', resolved_models: [] },
    ],
    by_model: [
      { ...bucket(), provider: 'anthropic', model: 'claude-haiku-4-5-20251001', priced: true },
      { ...bucket(), provider: 'openai', model: 'gpt-5.3-codex-spark-extra-long-name', priced: false },
    ],
    totals: bucket({ run_count: 40 }),
    unpriced_combos: ['openai:gpt-5.3-codex-spark-extra-long-name'],
    fallbacks: {
      total: 3,
      combos: [{ requested: 'opus', effective: 'sonnet', provider: 'anthropic', reason: 'rate_limit', count: 3 }],
    },
  };
}

describe('EffortEfficiencyPanel viewport', () => {
  let root: Root;
  let container: HTMLDivElement;

  beforeEach(() => {
    container = document.createElement('div');
    container.style.padding = '12px';
    document.body.appendChild(container);
    root = createRoot(container);
  });

  afterEach(() => {
    act(() => {
      root.unmount();
    });
    document.body.removeChild(container);
  });

  test('renders without overflow at 375px (mobile)', async () => {
    await expectNoOverflowAt(375, async () => {
      act(() => {
        root.render(<EffortEfficiencyPanel data={makeData()} />);
      });
    });
  });

  test('renders without overflow at 768px (tablet)', async () => {
    await expectNoOverflowAt(768, async () => {
      act(() => {
        root.render(<EffortEfficiencyPanel data={makeData()} />);
      });
    });
  });

  test('renders without overflow at 1024px (desktop)', async () => {
    await expectNoOverflowAt(1024, async () => {
      act(() => {
        root.render(<EffortEfficiencyPanel data={makeData()} />);
      });
    });
  });
});
