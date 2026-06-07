import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { EffortEfficiencyPanel } from './EffortEfficiencyPanel';
import type {
  EffortModelEfficiency,
  EfficiencyBucket,
  EffortEfficiencyRow,
  ModelEfficiencyRow,
} from '../types/digest';

function bucket(overrides: Partial<EfficiencyBucket> = {}): EfficiencyBucket {
  return {
    run_count: 10,
    landed_count: 6,
    landed_rate: 0.6,
    verified_landed_count: 6,
    verified_landed_rate: 0.6,
    total_cost_usd: 12.5,
    cost_per_merge: 2.08,
    input_tokens: 1_200_000,
    output_tokens: 300_000,
    llm_call_count: 40,
    priced_run_count: 10,
    unpriced_run_count: 0,
    unpriced_coverage_fraction: 1,
    ...overrides,
  };
}

function effortRow(effort: string, overrides: Partial<EffortEfficiencyRow> = {}): EffortEfficiencyRow {
  return { ...bucket(), effort, resolved_models: [], ...overrides };
}

function modelRow(provider: string, model: string, overrides: Partial<ModelEfficiencyRow> = {}): ModelEfficiencyRow {
  return { ...bucket(), provider, model, priced: true, ...overrides };
}

function makeData(overrides: Partial<EffortModelEfficiency> = {}): EffortModelEfficiency {
  return {
    by_effort: [
      effortRow('low', {
        landed_count: 8,
        landed_rate: 0.9,
        verified_landed_count: 8,
        verified_landed_rate: 0.9,
        cost_per_merge: 1.0,
      }),
      effortRow('high', {
        landed_count: 3,
        landed_rate: 0.3,
        verified_landed_count: 3,
        verified_landed_rate: 0.3,
        cost_per_merge: 25.0,
      }),
    ],
    by_model: [
      modelRow('anthropic', 'claude-opus-4-8'),
      modelRow('anthropic', 'claude-haiku-4-5-20251001'),
    ],
    totals: bucket({ run_count: 20, landed_count: 11 }),
    unpriced_combos: [],
    fallbacks: { total: 0, combos: [] },
    ...overrides,
  };
}

describe('EffortEfficiencyPanel', () => {
  it('renders nothing when data is null/undefined', () => {
    const { container: c1 } = render(<EffortEfficiencyPanel data={null} />);
    expect(c1).toBeEmptyDOMElement();
    const { container: c2 } = render(<EffortEfficiencyPanel data={undefined} />);
    expect(c2).toBeEmptyDOMElement();
  });

  it('renders nothing when there are no rows and no runs', () => {
    const { container } = render(
      <EffortEfficiencyPanel data={makeData({ by_effort: [], by_model: [], totals: bucket({ run_count: 0 }) })} />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders effort-tier and model rows with run, merge-rate, cost, $/merge and tokens', () => {
    render(<EffortEfficiencyPanel data={makeData()} />);
    // Effort tiers (desktop table + mobile cards both present in DOM).
    expect(screen.getAllByText('low').length).toBeGreaterThan(0);
    expect(screen.getAllByText('high').length).toBeGreaterThan(0);
    // Actual models labelled "model · provider".
    expect(screen.getAllByText(/claude-opus-4-8 · anthropic/).length).toBeGreaterThan(0);
    // Merge-rate rendered as a percentage.
    expect(screen.getAllByText(/90%/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/30%/).length).toBeGreaterThan(0);
    // Cost-per-merge formatting.
    expect(screen.getAllByText(/\$25\.00/).length).toBeGreaterThan(0);
    // Token volume in compact form (1.5M = 1.2M in + 0.3M out).
    expect(screen.getAllByText(/1\.5M/).length).toBeGreaterThan(0);
  });

  it('shows the fallback/downgrade caveat with combo details', () => {
    render(
      <EffortEfficiencyPanel
        data={makeData({
          fallbacks: {
            total: 4,
            combos: [
              { requested: 'opus', effective: 'sonnet', provider: 'anthropic', reason: 'rate_limit', count: 4 },
            ],
          },
        })}
      />,
    );
    expect(screen.getByText(/4 runs fell back \/ downgraded/)).toBeInTheDocument();
    expect(screen.getByText(/rate_limit/)).toBeInTheDocument();
  });

  it('uses singular wording for a single fallback run', () => {
    render(
      <EffortEfficiencyPanel
        data={makeData({ fallbacks: { total: 1, combos: [] } })}
      />,
    );
    expect(screen.getByText(/1 run fell back \/ downgraded/)).toBeInTheDocument();
  });

  it('shows the unpriced-coverage caveat when token-bearing runs lack pricing', () => {
    render(
      <EffortEfficiencyPanel
        data={makeData({
          totals: bucket({
            run_count: 20,
            priced_run_count: 15,
            unpriced_run_count: 5,
            unpriced_coverage_fraction: 0.75,
          }),
          unpriced_combos: ['openai:gpt-9'],
        })}
      />,
    );
    expect(screen.getByText(/Pricing coverage/)).toBeInTheDocument();
    expect(screen.getByText(/75%/)).toBeInTheDocument();
    expect(screen.getByText(/openai:gpt-9/)).toBeInTheDocument();
  });

  it('hides both caveats when coverage is full and there are no fallbacks', () => {
    render(<EffortEfficiencyPanel data={makeData()} />);
    expect(screen.queryByText(/Pricing coverage/)).not.toBeInTheDocument();
    expect(screen.queryByText(/fell back \/ downgraded/)).not.toBeInTheDocument();
  });

  it('flags an unpriced model row in its label', () => {
    render(
      <EffortEfficiencyPanel
        data={makeData({ by_model: [modelRow('openai', 'gpt-9', { priced: false })] })}
      />,
    );
    expect(screen.getAllByText(/gpt-9 · openai \(unpriced\)/).length).toBeGreaterThan(0);
  });

  it('reveals a per-tier resolved-model breakdown with a downgraded + unpriced model', () => {
    render(
      <EffortEfficiencyPanel
        data={makeData({
          by_effort: [
            effortRow('max', {
              resolved_models: [
                { provider: 'anthropic', model: 'claude-opus-4-8', run_count: 6, landed_count: 4, priced: true },
                { provider: 'anthropic', model: 'claude-sonnet-4-6', run_count: 3, landed_count: 1, priced: true },
                { provider: 'openai', model: 'gpt-9', run_count: 1, landed_count: 0, priced: false },
              ],
            }),
          ],
        })}
      />,
    );
    // Summary advertises the count of actual models the tier resolved onto.
    expect(screen.getAllByText(/Resolved models \(3\)/).length).toBeGreaterThan(0);
    // The downgraded model and the unpriced flag are visible in the breakdown.
    expect(screen.getAllByText(/claude-sonnet-4-6 · anthropic/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/\(unpriced\)/).length).toBeGreaterThan(0);
    // Per-model landed/run counts surfaced.
    expect(screen.getAllByText(/4\/6 landed/).length).toBeGreaterThan(0);
  });

  it('omits the resolved-model reveal when a tier carries no breakdown', () => {
    render(
      <EffortEfficiencyPanel
        data={makeData({ by_effort: [effortRow('low', { resolved_models: [] })] })}
      />,
    );
    expect(screen.queryByText(/Resolved models/)).not.toBeInTheDocument();
  });

  it('shows the verified merge figure as primary with the raw count when they diverge', () => {
    render(
      <EffortEfficiencyPanel
        data={makeData({
          by_effort: [
            effortRow('high', {
              run_count: 10,
              landed_count: 6,
              landed_rate: 0.6,
              verified_landed_count: 4,
              verified_landed_rate: 0.4,
            }),
          ],
          by_model: [],
        })}
      />,
    );
    // Verified figure is the headline (4/10 = 40%), not the raw 6/10.
    expect(screen.getAllByText(/4\/10/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/40%/).length).toBeGreaterThan(0);
    // Raw landed count is still visible (2 degraded fall-open merges excluded).
    expect(screen.getAllByText(/raw 6/).length).toBeGreaterThan(0);
  });

  it('falls back to raw landed figures when verified fields are absent (legacy backend)', () => {
    const legacy = bucket();
    delete legacy.verified_landed_count;
    delete legacy.verified_landed_rate;
    render(
      <EffortEfficiencyPanel
        data={makeData({
          by_effort: [{ ...legacy, effort: 'low', landed_count: 7, landed_rate: 0.7, resolved_models: [] }],
          by_model: [],
        })}
      />,
    );
    // Displays the raw landed rate with no "raw N" annotation.
    expect(screen.getAllByText(/70%/).length).toBeGreaterThan(0);
    expect(screen.queryByText(/raw 7/)).not.toBeInTheDocument();
  });
});
