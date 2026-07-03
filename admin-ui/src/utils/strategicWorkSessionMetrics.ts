import type { StrategicWorkSessionMetric } from '../strategicStateApi';

export function formatStrategicMetricValue(metric: StrategicWorkSessionMetric): string {
  const raw = metric.value;
  const value = typeof raw === 'number'
    ? (Number.isInteger(raw) ? String(raw) : raw.toFixed(2))
    : String(raw ?? '');
  const unit = metric.unit?.trim();
  return unit ? `${value} ${unit}` : value;
}

export function isActionableStrategicMetric(metric: StrategicWorkSessionMetric): boolean {
  return Boolean(metric.target) && (
    Number(metric.value) > 0 ||
    (metric.tone != null && metric.tone !== 'neutral')
  );
}

export function strategicMetricPriority(metric: StrategicWorkSessionMetric): number {
  if (metric.tone === 'critical') return 50;
  if (metric.tone === 'warning') return 40;
  if (metric.tone === 'active') return 30;
  if (metric.tone === 'watch') return 20;
  if (Number(metric.value) > 0) return 10;
  return 0;
}

export function rankStrategicWorkSessionMetrics(
  metrics: StrategicWorkSessionMetric[],
  limit: number,
): StrategicWorkSessionMetric[] {
  return metrics
    .filter(isActionableStrategicMetric)
    .sort((a, b) => strategicMetricPriority(b) - strategicMetricPriority(a) || Number(b.value) - Number(a.value))
    .slice(0, limit);
}
