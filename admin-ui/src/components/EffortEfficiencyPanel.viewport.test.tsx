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
    verified_landed_count: 4,
    verified_landed_rate: 0.4,
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
      {
        ...bucket(),
        effort: 'max',
        // Worst-case per-tier divergence: a requested 'max' tier that resolved
        // onto several models including a long-named unpriced fallback.
        resolved_models: [
          { provider: 'anthropic', model: 'claude-opus-4-8', run_count: 5, landed_count: 3, priced: true },
          { provider: 'anthropic', model: 'claude-sonnet-4-6', run_count: 3, landed_count: 1, priced: true },
          { provider: 'openai', model: 'gpt-5.3-codex-spark-extra-long-name', run_count: 2, landed_count: 0, priced: false },
        ],
      },
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
    // Worst-case provider-routing callout: INSUFFICIENT_DATA with negative delta
    // and long recommended_action text, so viewport tests catch overflow.
    provider_routing_verdict: {
      state: 'INSUFFICIENT_DATA' as const,
      supporting: {
        deepseek_run_count: 5,
        deepseek_pr_eligible_run_count: 5,
        deepseek_verified_landed_count: 3,
        deepseek_verified_landed_rate: 0.6,
        deepseek_cost_per_merge: 2.0,
        deepseek_total_cost_usd: 10,
        anthropic_run_count: 200,
        anthropic_pr_eligible_run_count: 195,
        anthropic_verified_landed_count: 156,
        anthropic_verified_landed_rate: 0.8,
        anthropic_cost_per_merge: 3.0,
        anthropic_total_cost_usd: 500,
        cost_ratio: 0.02,
        verified_rate_delta_pct: -20.0,
      },
      recommended_action: 'INSUFFICIENT_DATA — only 5 DeepSeek runs (need ≥ 30) — keep collecting data before deciding',
    },
    provider_routing_share: {
      by_provider: [
        {
          provider: 'deepseek',
          run_count: 5,
          pr_eligible_run_count: 5,
          verified_landed_count: 3,
          verified_landed_rate: 0.6,
          total_cost_usd: 10.0,
          cost_per_merge: 3.33,
        },
        {
          provider: 'anthropic',
          run_count: 200,
          pr_eligible_run_count: 195,
          verified_landed_count: 156,
          verified_landed_rate: 0.8,
          total_cost_usd: 500.0,
          cost_per_merge: 3.21,
        },
      ],
      total_run_count: 205,
      total_pr_eligible_run_count: 200,
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
      // Expand every resolved-model reveal so the breakdown is measured, not
      // just the collapsed summary.
      container.querySelectorAll('details').forEach((d) => {
        d.open = true;
      });
    });
  });

  test('renders without overflow at 768px (tablet)', async () => {
    await expectNoOverflowAt(768, async () => {
      act(() => {
        root.render(<EffortEfficiencyPanel data={makeData()} />);
      });
      // Expand every resolved-model reveal so the breakdown is measured, not
      // just the collapsed summary.
      container.querySelectorAll('details').forEach((d) => {
        d.open = true;
      });
    });
  });

  test('renders without overflow at 1024px (desktop)', async () => {
    await expectNoOverflowAt(1024, async () => {
      act(() => {
        root.render(<EffortEfficiencyPanel data={makeData()} />);
      });
      // Expand every resolved-model reveal so the breakdown is measured, not
      // just the collapsed summary.
      container.querySelectorAll('details').forEach((d) => {
        d.open = true;
      });
    });
  });

  test('renders without overflow at 1280px (desktop wide)', async () => {
    await expectNoOverflowAt(1280, async () => {
      act(() => {
        root.render(<EffortEfficiencyPanel data={makeData()} />);
      });
      // Expand every resolved-model reveal so the breakdown is measured, not
      // just the collapsed summary.
      container.querySelectorAll('details').forEach((d) => {
        d.open = true;
      });
    });
  });
});
