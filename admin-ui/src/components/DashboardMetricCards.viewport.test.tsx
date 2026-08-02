import { afterEach, describe, test } from 'vitest';
import { createRoot, type Root } from 'react-dom/client';
import { act } from 'react';
import {
  MetricCard,
  FailedCard,
  SuccessRateCard,
  ConversionRateCard,
  AcceptanceRateCard,
  CostCard,
  SrCatchRateCard,
} from './DashboardMetricCards';
import type { SparkPoint, FailureGroup } from './DashboardHome';
import type { SrTrendWithFixIterations } from './ReviewQualitySection';
import { expectNoOverflowAt } from '../test/viewport';

const spark: SparkPoint[] = Array.from({ length: 8 }, (_, i) => ({
  active: i,
  queued: i * 2,
  failed: i % 3,
}));

const failureGroups: FailureGroup[] = [
  { failure_class: 'CODE_ERROR', label: 'Code Error', count: 137, tasks: [] },
  { failure_class: 'PROVISION_FAILURE', label: 'Provision', count: 42, tasks: [] },
  { failure_class: 'AGENT_TIMEOUT', label: 'Timeout', count: 18, tasks: [] },
];

// Worst-case trends payload: high finding count, SR-alone + reviewer lift lines,
// stale badge — the densest layout the card can produce.
const trends = {
  categories: { correctness: { current_reviewer_finding_count: 64 } },
  previous_overall_hit_rate: 0.61,
  overall_trend_verdict: 'declining',
  is_stale: true,
} as unknown as SrTrendWithFixIterations;

describe('DashboardMetricCards — viewport regression', () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;
  afterEach(() => {
    if (root) act(() => { root?.unmount(); });
    root = null;
    host?.remove();
    host = null;
  });

  test('renders the full metric-card grid without horizontal overflow at 375px', async () => {
    await expectNoOverflowAt(375, async () => {
      host = document.createElement('div');
      document.body.appendChild(host);
      root = createRoot(host);
      await act(async () => {
        root?.render(
          <div className="grid grid-cols-2 sm:grid-cols-3 gap-2">
            <MetricCard label="Active Research Cycles" value={1284} color="emerald" spark={spark} dataKey="active" loading={false} />
            <FailedCard value={197} groups={failureGroups} spark={spark} loading={false} />
            <SuccessRateCard rate={0.736} trend="declining" trendHistory={spark.map(p => ({ success_rate: p.active / 10, completed: p.active, failed: p.failed }))} loading={false} />
            <ConversionRateCard rate={0.42} loading={false} />
            <AcceptanceRateCard rate={0.58} loading={false} />
            <CostCard totalCost={1284.56} trendHistory={spark.map(p => ({ period_cost_usd: p.queued }))} loading={false} />
            <SrCatchRateCard rate={0.34} trends={trends} loading={false} />
          </div>,
        );
      });
    });
  });
});
