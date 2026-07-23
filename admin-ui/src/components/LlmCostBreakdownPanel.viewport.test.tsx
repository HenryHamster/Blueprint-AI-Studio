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
    rework_attribution: {
      rework_share: 0.31,
      first_pass_llm_cost_usd: 169.58,
      rework_llm_cost_usd: 76.19,
      top_rework_prs: [
        { pr_number: 9801, rework_llm_cost_usd: 18.42, rework_vm_cost_usd: 0.14 },
        { pr_number: 9788, rework_llm_cost_usd: 12.07, rework_vm_cost_usd: 0.09 },
      ],
    },
    by_task_type: [
      {
        task_type: 'create',
        total_input_tokens: 10_000_000,
        total_output_tokens: 2_500_000,
        total_cost_usd: 157.5,
        llm_call_count: 1200,
        task_count: 312,
        merged_count: 240,
        merge_rate: 0.769,
        verified_merged_count: 228,
        verified_merge_rate: 0.731,
        cost_per_merge: 0.69,
        share_of_total_cost: 0.641,
      },
      {
        task_type: 'review',
        total_input_tokens: 6_500_000,
        total_output_tokens: 1_600_000,
        total_cost_usd: 88.27,
        llm_call_count: 620,
        task_count: 134,
        merged_count: 66,
        merge_rate: 0.493,
        verified_merged_count: 60,
        verified_merge_rate: 0.448,
        cost_per_merge: 1.47,
        share_of_total_cost: 0.359,
      },
    ],
  })),
}));

describe('LlmCostBreakdownPanel — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(() => {
    if (root) { const r = root; act(() => r.unmount()); root = null; }
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
          merged_count: 0,
          merge_rate: null,
          verified_merged_count: 0,
          verified_merge_rate: null,
          priced: false,
          approximate_priced: false,
          cost_per_merge: null,
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
        fallback_task_count: 0,
        fallback_cost_usd: 0,
      },
      unpriced_combos: ['custom-provider:custom-model'],
      approximate_priced_combos: [],
      by_task_type: [],
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

  test('renders rework attribution section with headline share and top-PR list', async () => {
    if (!host) {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
    }
    const r = root;
    await act(async () => {
      r?.render(<LlmCostBreakdownPanel selectedProjectId="project-rework" />);
    });
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 0));
    });

    const text = host?.textContent ?? '';
    expect(text).toContain('LLM Cost Rework Attribution');
    expect(text).toContain('31%');
    expect(text).toContain('$169.58');
    expect(text).toContain('$76.19');
    expect(text).toContain('#9801');
    expect(text).toContain('#9788');
    expect(text).toContain('$18.42');
    expect(text).toContain('$12.07');
  });

  test('hides rework section gracefully when rework_attribution is absent (older backend)', async () => {
    const withoutRework = {
      window_days: 7,
      generated_at: '2026-05-29T12:00:00Z',
      by_provider: [],
      by_model: [
        {
          provider: 'anthropic',
          model: 'claude-sonnet-4-6',
          total_input_tokens: 1_000_000,
          total_output_tokens: 200_000,
          total_cost_usd: 10.0,
          mean_cost_per_task: 0.5,
          share_of_total_cost: 1.0,
          task_count: 20,
          llm_call_count: 80,
          merged_count: 16,
          merge_rate: 0.8,
          verified_merged_count: 14,
          verified_merge_rate: 0.7,
          priced: true,
          approximate_priced: false,
          cost_per_merge: 0.62,
        },
      ],
      totals: {
        total_input_tokens: 1_000_000,
        total_output_tokens: 200_000,
        total_cost_usd: 10.0,
        task_count: 20,
        merged_count: 16,
        merge_rate: 0.8,
        verified_merged_count: 14,
        verified_merge_rate: 0.7,
        priced_task_count: 20,
        unpriced_task_count: 0,
        unpriced_coverage_fraction: 1.0,
        fallback_task_count: 0,
        fallback_cost_usd: 0,
      },
      unpriced_combos: [],
      approximate_priced_combos: [],
      by_task_type: [],
      // rework_attribution intentionally omitted — older backend
    };
    vi.mocked(api.fetchLlmCostBreakdown).mockResolvedValueOnce(withoutRework);

    if (!host) {
      host = document.createElement('div');
      host.style.padding = '12px';
      document.body.appendChild(host);
      root = createRoot(host);
    }
    const r = root;
    await act(async () => {
      r?.render(<LlmCostBreakdownPanel selectedProjectId="project-no-rework" />);
    });
    await act(async () => {
      await new Promise(resolve => setTimeout(resolve, 0));
    });

    const text = host?.textContent ?? '';
    expect(text).not.toContain('Rework Attribution');
    expect(text).toContain('claude-sonnet-4-6');
  });
});
