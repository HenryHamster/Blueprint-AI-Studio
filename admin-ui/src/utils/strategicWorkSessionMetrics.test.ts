import { describe, expect, test } from 'vitest';
import {
  formatStrategicMetricValue,
  rankStrategicWorkSessionMetrics,
} from './strategicWorkSessionMetrics';
import type { StrategicWorkSessionMetric } from '../strategicStateApi';

function metric(overrides: Partial<StrategicWorkSessionMetric>): StrategicWorkSessionMetric {
  return {
    id: overrides.id ?? 'metric',
    label: overrides.label ?? 'metric',
    value: overrides.value ?? 0,
    tone: overrides.tone ?? 'neutral',
    target: overrides.target,
    unit: overrides.unit,
  };
}

describe('strategicWorkSessionMetrics', () => {
  test('ranks actionable metrics and drops neutral zero fillers', () => {
    const ranked = rankStrategicWorkSessionMetrics([
      metric({ id: 'neutral.zero', label: 'neutral zero', value: 0, target: { surface: 'messages' } }),
      metric({ id: 'neutral.positive', label: 'neutral positive', value: 3, target: { surface: 'pipeline' } }),
      metric({ id: 'warning.zero', label: 'warning zero', value: 0, tone: 'warning', target: { surface: 'runtime' } }),
      metric({ id: 'critical.two', label: 'critical two', value: 2, tone: 'critical', target: { surface: 'runtime' } }),
      metric({ id: 'missing.target', label: 'missing target', value: 8 }),
    ], 3);

    expect(ranked.map(item => item.id)).toEqual([
      'critical.two',
      'warning.zero',
      'neutral.positive',
    ]);
  });

  test('formats numeric values with optional units', () => {
    expect(formatStrategicMetricValue(metric({ value: 3.125, unit: 'USD' }))).toBe('3.13 USD');
    expect(formatStrategicMetricValue(metric({ value: 4 }))).toBe('4');
  });
});
