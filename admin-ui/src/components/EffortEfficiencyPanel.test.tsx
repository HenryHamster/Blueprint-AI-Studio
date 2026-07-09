import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { EffortEfficiencyPanel } from './EffortEfficiencyPanel';
import type {
  EffortModelEfficiency,
  EfficiencyBucket,
  EffortEfficiencyRow,
  ModelEfficiencyRow,
  ProviderRoutingVerdict,
  ProviderRoutingShare,
  CostTelemetryReconciliation,
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

  it('renders ADOPT verdict callout with green badge, cost ratio, and merge rate delta', () => {
    const verdict: ProviderRoutingVerdict = {
      state: 'ADOPT',
      supporting: {
        deepseek_run_count: 400,
        deepseek_pr_eligible_run_count: 380,
        deepseek_verified_landed_count: 300,
        deepseek_verified_landed_rate: 0.789,
        deepseek_cost_per_merge: 1.2,
        deepseek_total_cost_usd: 400,
        anthropic_run_count: 200,
        anthropic_pr_eligible_run_count: 190,
        anthropic_verified_landed_count: 152,
        anthropic_verified_landed_rate: 0.8,
        anthropic_cost_per_merge: 3.0,
        anthropic_total_cost_usd: 500,
        cost_ratio: 0.42,
        verified_rate_delta_pct: -1.1,
      },
      recommended_action: 'ADOPT — enable routing for all low/medium tasks',
    };
    render(<EffortEfficiencyPanel data={makeData({ provider_routing_verdict: verdict })} />);
    // Badge with ADOPT state visible.
    expect(screen.getByText('ADOPT')).toBeInTheDocument();
    // Section heading.
    expect(screen.getAllByText(/Provider Routing A\/B/).length).toBeGreaterThan(0);
    // Cost ratio formatted.
    expect(screen.getByText(/0.42x/)).toBeInTheDocument();
    // Merge rate delta shown (negative here).
    expect(screen.getByText(/-1.1pp/)).toBeInTheDocument();
    // Recommended action.
    expect(screen.getByText(/enable routing for all low\/medium tasks/)).toBeInTheDocument();
  });

  it('renders REJECT verdict with red badge and positive delta (A regressed)', () => {
    const verdict: ProviderRoutingVerdict = {
      state: 'REJECT',
      supporting: {
        deepseek_run_count: 100,
        deepseek_pr_eligible_run_count: 95,
        deepseek_verified_landed_count: 40,
        deepseek_verified_landed_rate: 0.421,
        deepseek_cost_per_merge: 2.5,
        deepseek_total_cost_usd: 120,
        anthropic_run_count: 200,
        anthropic_pr_eligible_run_count: 195,
        anthropic_verified_landed_count: 156,
        anthropic_verified_landed_rate: 0.8,
        anthropic_cost_per_merge: 3.0,
        anthropic_total_cost_usd: 500,
        cost_ratio: 0.24,
        verified_rate_delta_pct: -37.9,
      },
      recommended_action: 'REJECT — quality regression (verified rate -37.9pp)',
    };
    render(<EffortEfficiencyPanel data={makeData({ provider_routing_verdict: verdict })} />);
    expect(screen.getByText('REJECT')).toBeInTheDocument();
    expect(screen.getByText(/0.24x/)).toBeInTheDocument();
    // The delta appears in both the span and the recommended_action text.
    expect(screen.getAllByText(/-37.9pp/).length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByText(/quality regression/).length).toBeGreaterThan(0);
  });

  it('renders INSUFFICIENT_DATA verdict with amber badge', () => {
    const verdict: ProviderRoutingVerdict = {
      state: 'INSUFFICIENT_DATA',
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
      recommended_action: 'INSUFFICIENT_DATA — only 5 DeepSeek runs (need ≥ 30)',
    };
    render(<EffortEfficiencyPanel data={makeData({ provider_routing_verdict: verdict })} />);
    expect(screen.getByText('INSUFFICIENT_DATA')).toBeInTheDocument();
    expect(screen.getByText(/0.02x/)).toBeInTheDocument();
    // -20.0 is rendered as -20 by JS number formatting.
    expect(screen.getByText(/-20pp/)).toBeInTheDocument();
    expect(screen.getByText(/only 5 DeepSeek runs/)).toBeInTheDocument();
  });

  it('renders ADOPT verdict with positive delta showing + prefix', () => {
    const verdict: ProviderRoutingVerdict = {
      state: 'ADOPT',
      supporting: {
        deepseek_run_count: 400,
        deepseek_pr_eligible_run_count: 380,
        deepseek_verified_landed_count: 320,
        deepseek_verified_landed_rate: 0.842,
        deepseek_cost_per_merge: 1.2,
        deepseek_total_cost_usd: 400,
        anthropic_run_count: 200,
        anthropic_pr_eligible_run_count: 190,
        anthropic_verified_landed_count: 152,
        anthropic_verified_landed_rate: 0.8,
        anthropic_cost_per_merge: 3.0,
        anthropic_total_cost_usd: 500,
        cost_ratio: 0.42,
        verified_rate_delta_pct: 4.2,
      },
      recommended_action: 'ADOPT — DeepSeek cheaper with no quality regression',
    };
    render(<EffortEfficiencyPanel data={makeData({ provider_routing_verdict: verdict })} />);
    expect(screen.getByText('ADOPT')).toBeInTheDocument();
    // Positive delta should show the '+' prefix.
    expect(screen.getByText(/\+4\.2pp/)).toBeInTheDocument();
    expect(screen.getByText(/0.42x/)).toBeInTheDocument();
  });

  it('renders provider routing distribution table with per-provider stats and totals', () => {
    const share: ProviderRoutingShare = {
      by_provider: [
        { provider: 'deepseek', run_count: 30, pr_eligible_run_count: 28, verified_landed_count: 20, verified_landed_rate: 0.714, total_cost_usd: 12.5, cost_per_merge: 0.63 },
        { provider: 'anthropic', run_count: 10, pr_eligible_run_count: 8, verified_landed_count: 6, verified_landed_rate: 0.75, total_cost_usd: 5.0, cost_per_merge: 0.83 },
      ],
      total_run_count: 40,
      total_pr_eligible_run_count: 36,
    };
    render(<EffortEfficiencyPanel data={makeData({ provider_routing_share: share })} />);
    // Section heading.
    expect(screen.getByText(/Provider Routing Distribution/)).toBeInTheDocument();
    // Per-provider rows: lowercase provider names (CSS capitalize, not text transform).
    expect(screen.getByText('deepseek')).toBeInTheDocument();
    expect(screen.getByText('anthropic')).toBeInTheDocument();
    // DeepSeek-specific numbers unique to the routing share.
    expect(screen.getByText('30')).toBeInTheDocument();  // deepseek run_count
    expect(screen.getByText('28')).toBeInTheDocument();  // deepseek pr_eligible
    // Anthropic-specific numbers.
    expect(screen.getByText('8')).toBeInTheDocument();   // anthropic pr_eligible
    // Verified rates.
    expect(screen.getByText(/71%/)).toBeInTheDocument();  // deepseek rate
    expect(screen.getByText(/75%/)).toBeInTheDocument();  // anthropic rate
    // Cost per merge
    expect(screen.getByText(/\$0\.63/)).toBeInTheDocument();
    expect(screen.getByText(/\$0\.83/)).toBeInTheDocument();
    // Totals row.
    expect(screen.getByText('Total')).toBeInTheDocument();
    expect(screen.getByText('36')).toBeInTheDocument();  // total_pr_eligible
  });

  it('renders a single provider row for routing distribution', () => {
    const share: ProviderRoutingShare = {
      by_provider: [
        { provider: 'deepseek', run_count: 5, pr_eligible_run_count: 5, verified_landed_count: 3, verified_landed_rate: 0.6, total_cost_usd: 10.0, cost_per_merge: 3.33 },
      ],
      total_run_count: 5,
      total_pr_eligible_run_count: 5,
    };
    render(<EffortEfficiencyPanel data={makeData({ provider_routing_share: share })} />);
    expect(screen.getByText(/Provider Routing Distribution/)).toBeInTheDocument();
    expect(screen.getByText('deepseek')).toBeInTheDocument();
    expect(screen.getByText('Total')).toBeInTheDocument();
    // Single provider totals match.
    expect(screen.getAllByText('5').length).toBeGreaterThanOrEqual(2); // runs + pr-eligible + total
  });

  it('hides the routing distribution table when provider_routing_share is absent', () => {
    render(<EffortEfficiencyPanel data={makeData({ provider_routing_share: undefined })} />);
    expect(screen.queryByText(/Provider Routing Distribution/)).not.toBeInTheDocument();
  });

  it('hides the verdict callout when provider_routing_verdict is absent', () => {
    render(<EffortEfficiencyPanel data={makeData({ provider_routing_verdict: undefined })} />);
    expect(screen.queryByText(/Provider Routing A\/B/)).not.toBeInTheDocument();
  });

  // --- Cost telemetry reconciliation trust indicator ---

  function reconciliationData(
    overrides: Partial<CostTelemetryReconciliation> = {},
  ): CostTelemetryReconciliation {
    return {
      total_cost_snapshot_path: 50.0,
      total_cost_direct_path: 50.000001,
      absolute_delta: 0.000001,
      relative_delta: 2e-8,
      within_tolerance: true,
      tolerance: 1e-6,
      total_input_tokens_snapshot_path: 1000000,
      total_input_tokens_direct_path: 1000000,
      total_cache_read_input_tokens_snapshot_path: 200000,
      total_cache_read_input_tokens_direct_path: 200000,
      total_cache_creation_input_tokens_snapshot_path: 50000,
      total_cache_creation_input_tokens_direct_path: 50000,
      cache_hit_rate_snapshot_path: 0.2,
      cache_hit_rate_direct_path: 0.2,
      cache_hit_rate_delta: 0,
      cache_hit_rate_within_tolerance: true,
      task_count: 15,
      per_task: [],
      ...overrides,
    };
  }

  it('renders cost telemetry reconciled badge when within tolerance', () => {
    render(
      <EffortEfficiencyPanel
        data={makeData({ cost_telemetry_reconciliation: reconciliationData() })}
      />,
    );
    // Green badge indicates reconciliation passed.
    expect(screen.getByText(/✓ Reconciled/)).toBeInTheDocument();
    // Section label visible.
    expect(screen.getByText(/Cost Telemetry/)).toBeInTheDocument();
    // Delta = 0.0000010 (toFixed(7)) and tolerance = 0.000001 shown.
    expect(screen.getAllByText(/\$0\.0000010?/).length).toBeGreaterThanOrEqual(2);
    // Task count shown.
    expect(screen.getByText('15')).toBeInTheDocument();
    // Trustworthy message.
    expect(screen.getByText(/telemetry is trustworthy/)).toBeInTheDocument();
  });

  it('renders cost telemetry warning badge when out of tolerance', () => {
    render(
      <EffortEfficiencyPanel
        data={makeData({
          cost_telemetry_reconciliation: reconciliationData({
            within_tolerance: false,
            absolute_delta: 0.05,
          }),
        })}
      />,
    );
    // Amber badge indicates mismatch.
    expect(screen.getByText(/⚠ Mismatch/)).toBeInTheDocument();
    // Diverged delta value.
    expect(screen.getByText(/\$0\.0500000/)).toBeInTheDocument();
    // Cost tolerance shown.
    expect(screen.getByText(/±\$0\.000001/)).toBeInTheDocument();
    // Investigate message.
    expect(screen.getByText(/telemetry paths diverge/)).toBeInTheDocument();
  });

  it('renders cost telemetry warning when cache hit rate diverges but cost within tolerance', () => {
    render(
      <EffortEfficiencyPanel
        data={makeData({
          cost_telemetry_reconciliation: reconciliationData({
            within_tolerance: true,
            cache_hit_rate_within_tolerance: false,
            cache_hit_rate_delta: 0.15,
          }),
        })}
      />,
    );
    // Mismatch badge (cache hit rate divergence overrides cost agreement).
    expect(screen.getByText(/⚠ Mismatch/)).toBeInTheDocument();
    // Cache-hit rate divergence noted.
    expect(screen.getByText(/diverged/)).toBeInTheDocument();
  });

  it('hides cost telemetry callout when reconciliation is undefined', () => {
    render(
      <EffortEfficiencyPanel
        data={makeData({ cost_telemetry_reconciliation: undefined })}
      />,
    );
    expect(screen.queryByText(/Cost Telemetry/)).not.toBeInTheDocument();
    expect(screen.queryByText(/✓ Reconciled/)).not.toBeInTheDocument();
    expect(screen.queryByText(/⚠ Mismatch/)).not.toBeInTheDocument();
  });
});
