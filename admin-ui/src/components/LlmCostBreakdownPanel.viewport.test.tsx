import { afterEach, describe, test, vi, expect } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import { LlmCostBreakdownPanel } from './LlmCostBreakdownPanel';
import * as api from '../api';

// The panel renders `null` until fetchLlmCostBreakdown resolves with data, so we
// mock the API to return a populated breakdown that exercises the model table,
// the unpriced-row marker, and the pricing-coverage warning banner with combos.
vi.mock('../api', () => ({
  fetchLlmCostBreakdown: vi.fn(async () => ({
    window_days: 7,
    generated_at: '2026-05-29T12:00:00Z',
    by_provider: [],
    by_model: [
      {
        provider: 'anthropic',
        model: 'claude-opus-4-8-with-a-deliberately-long-model-identifier',
        total_input_tokens: 12_400_000,
        total_output_tokens: 3_200_000,
        total_cost_usd: 184.27,
        mean_cost_per_task: 0.742,
        share_of_total_cost: 0.71,
        task_count: 248,
        llm_call_count: 1820,
        merged_count: 186,
        merge_rate: 0.75,
        verified_merged_count: 182,
        verified_merge_rate: 0.734,
        priced: true,
        approximate_priced: false,
        cost_per_merge: 0.99,
      },
      {
        provider: 'openai',
        model: 'gpt-5-codex',
        total_input_tokens: 4_100_000,
        total_output_tokens: 900_000,
        total_cost_usd: 61.5,
        mean_cost_per_task: 0.31,
        share_of_total_cost: 0.24,
        task_count: 198,
        llm_call_count: 640,
        merged_count: 120,
        merge_rate: 0.61,
        verified_merged_count: 106,
        verified_merge_rate: 0.535,
        priced: false,
        approximate_priced: false,
        cost_per_merge: null,
      },
    ],
    totals: {
      total_input_tokens: 16_500_000,
      total_output_tokens: 4_100_000,
      total_cost_usd: 245.77,
      task_count: 446,
      merged_count: 306,
      merge_rate: 0.686,
      verified_merged_count: 288,
      verified_merge_rate: 0.646,
      priced_task_count: 248,
      unpriced_task_count: 198,
      unpriced_coverage_fraction: 0.556,
      fallback_task_count: 32,
      fallback_cost_usd: 18.4,
    },
    unpriced_combos: ['openai:gpt-5-codex', 'gemini:gemini-2.5-pro'],
    approximate_priced_combos: [],
  })),
}));

describe('LlmCostBreakdownPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { act(() => root.unmount()); root = null; }
    if (host) { host.remove(); host = null; }
  });

  test('renders without horizontal overflow at 375px with populated rows', async () => {
    const doRender = async () => {
      if (!host) {
        host = document.createElement('div');
        host.style.padding = '12px';
        document.body.appendChild(host);
        root = createRoot(host);
      }
      const r = root;
      await act(async () => {
        r?.render(<LlmCostBreakdownPanel selectedProjectId="project-test" />);
      });
      // Settle the mocked fetch + setData() — without this, data is still null
      // and the component returns null, making the test trivially pass.
      await act(async () => {
        await new Promise(resolve => setTimeout(resolve, 0));
      });
    };

    await expectNoOverflowAt(375, doRender);
    await expectNoTextTruncationAt(375, async () => {});
  });

  test('renders unpriced-spend caveat instead of empty state when all tasks are unpriced', async () => {
    const unpriced = {
      window_days: 7,
      generated_at: '2026-05-29T12:00:00Z',
      by_provider: [],
      by_model: [
        {
          provider: 'custom-provider',
          model: 'custom-model',
          total_input_tokens: 5_000_000,
          total_output_tokens: 1_200_000,
          total_cost_usd: 0,
          mean_cost_per_task: 0,
          share_of_total_cost: null,
          task_count: 84,
          llm_call_count: 320,
          priced: false,
          approximate_priced: false,
        },
      ],
      totals: {
        total_input_tokens: 5_000_000,
        total_output_tokens: 1_200_000,
        total_cost_usd: 0,
        task_count: 84,
        merged_count: 0,
        verified_merged_count: 0,
        merge_rate: 0,
        verified_merge_rate: 0,
        priced_task_count: 0,
        unpriced_task_count: 84,
        unpriced_coverage_fraction: 1.0,
      },
      unpriced_combos: ['custom-provider:custom-model'],
      approximate_priced_combos: [],
    };
    vi.mocked(api.fetchLlmCostBreakdown).mockResolvedValueOnce(unpriced);

    if (!host) {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
    }
    const r = root;
    await act(async () => {
      r?.render(<LlmCostBreakdownPanel selectedProjectId="project-test-unpriced" />);
    });
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 0));
    });

    // Panel should show the model row with unpriced marker and caveat, not empty state
    const caveatText = host?.textContent;
    expect(caveatText).toContain('Cost data unavailable');
    expect(caveatText).toContain('custom-model');
    expect(caveatText).toContain('unpriced');
    expect(caveatText).toContain('Verified');
    expect(caveatText).not.toContain('No priced LLM token spend');
  });
});
