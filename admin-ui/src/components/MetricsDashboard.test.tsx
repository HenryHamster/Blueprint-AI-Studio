import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import MetricsDashboard from './MetricsDashboard';
import type { Task } from '../types/task';

vi.mock('recharts', () => ({
  ResponsiveContainer: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  AreaChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  LineChart: ({ children }: { children: React.ReactNode }) => <div>{children}</div>,
  Area: () => null,
  Line: () => null,
  XAxis: () => null,
  YAxis: () => null,
  CartesianGrid: () => null,
  Tooltip: () => null,
  ReferenceLine: () => null,
  Legend: () => null,
}));

function makeTask(status: Task['status'], created_at: string): Task {
  return {
    id: `${status}-${created_at}`,
    title: status,
    description: status,
    status,
    created_by: 'agent',
    requires_approval: false,
    created_at,
  };
}

describe('MetricsDashboard', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-04-24T12:00:00Z'));
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders empty-state charts and summary cards without crashing', () => {
    render(
      <MetricsDashboard
        metricsHistory={[]}
        cumulativeCost={0}
        cumulativeVmHours={0}
        maxConcurrentVMs={4}
        tasks={[]}
        metricsWindow="1h"
        onWindowChange={vi.fn()}
      />,
    );

    expect(screen.getAllByText('Collecting data…').length).toBeGreaterThan(0);
    expect(screen.getByText('Total VM-Hours')).toBeInTheDocument();
    expect(screen.getByText('Tasks Completed')).toBeInTheDocument();
    expect(screen.getByText('Success Rate')).toBeInTheDocument();
  });

  it('shows values, threshold styling, and window selection controls', () => {
    const onWindowChange = vi.fn();

    render(
      <MetricsDashboard
        metricsHistory={[
          { timestamp: Date.now() - 600000, activeVMs: 2, costPerHr: 1.25, completedCount: 2, failedCount: 0 },
          { timestamp: Date.now() - 300000, activeVMs: 3, costPerHr: 2.5, completedCount: 2, failedCount: 1 },
        ]}
        cumulativeCost={12.34}
        cumulativeVmHours={56.78}
        maxConcurrentVMs={4}
        tasks={[
          makeTask('COMPLETED', '2026-04-24T11:50:00Z'),
          makeTask('COMPLETED', '2026-04-24T11:40:00Z'),
          makeTask('COMPLETED', '2026-04-24T08:00:00Z'),
          makeTask('COMPLETED', '2026-04-24T02:00:00Z'),
          makeTask('FAILED', '2026-04-24T09:30:00Z'),
        ]}
        metricsWindow="1h"
        onWindowChange={onWindowChange}
      />,
    );

    expect(screen.getByText('56.78')).toBeInTheDocument();
    expect(screen.getByText('Cumulative VM Cost:')).toBeInTheDocument();
    expect(screen.getByText('$12.34')).toBeInTheDocument();
    expect(screen.getByText('2', { exact: true })).toBeInTheDocument();

    expect(screen.getByText('100.0%').className).toContain('text-emerald-400');
    expect(screen.getByText('80.0%').className).toContain('text-amber-400');

    fireEvent.click(screen.getByRole('button', { name: '6h' }));
    expect(onWindowChange).toHaveBeenCalledWith('6h');
  });
});
