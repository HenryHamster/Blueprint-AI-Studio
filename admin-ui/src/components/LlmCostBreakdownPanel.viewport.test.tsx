import { afterEach, describe, test, vi } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import { expectNoOverflowAt, expectNoTextTruncationAt } from '../test/viewport';
import { LlmCostBreakdownPanel } from './LlmCostBreakdownPanel';

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
        priced: true,
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
        priced: false,
      },
    ],
    totals: {
      total_input_tokens: 16_500_000,
      total_output_tokens: 4_100_000,
      total_cost_usd: 245.77,
      task_count: 446,
      priced_task_count: 248,
      unpriced_task_count: 198,
      unpriced_coverage_fraction: 0.556,
    },
    unpriced_combos: ['openai:gpt-5-codex', 'gemini:gemini-2.5-pro'],
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
});
