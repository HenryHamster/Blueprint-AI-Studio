import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { SystemHealthCard, type HealthOverviewResponse } from './SystemHealthCard';
import type { Task } from '../types/task';

const mockTask: Task = {
  id: 'task-1',
  title: 'Test Task',
  description: '',
  status: 'COMPLETED',
  created_by: 'test',
  requires_approval: false,
  created_at: '2024-01-01',
};

const mockHealthyResponse: HealthOverviewResponse = {
  overall_status: 'healthy',
  success_rate: { current: 0.95, trend: 'improving', window_hours: 24 },
  queue_depth: { queued: 0, active: 5, evaluating: 2 },
  actionable_issues: [],
  precomputed: true,
  precomputed_at: '2024-01-01T00:00:00',
  precompute_age_seconds: 30,
};

const mockDegradedResponse: HealthOverviewResponse = {
  overall_status: 'degraded',
  success_rate: { current: 0.85, trend: 'declining', window_hours: 24 },
  queue_depth: { queued: 10, active: 15, evaluating: 3 },
  actionable_issues: [
    {
      severity: 'warning',
      title: 'High queue depth',
      hint: 'Consider scaling resources',
      task_ids: ['task-1'],
      category: 'performance',
    },
  ],
  precomputed: true,
  precomputed_at: '2024-01-01T00:00:00',
  precompute_age_seconds: 30,
};

describe('SystemHealthCard', () => {
  const mockOnViewTask = vi.fn();

  it('renders loading state with skeleton', () => {
    render(
      <SystemHealthCard data={null} loading={true} tasks={[]} onViewTask={mockOnViewTask} />
    );
    expect(screen.queryByText('Health data unavailable')).not.toBeInTheDocument();
  });

  it('renders unavailable state when data is null and not loading', () => {
    render(
      <SystemHealthCard data={null} loading={false} tasks={[]} onViewTask={mockOnViewTask} />
    );
    expect(screen.getByText('Health data unavailable')).toBeInTheDocument();
  });

  it('renders all systems healthy when issues list is empty', () => {
    render(
      <SystemHealthCard data={mockHealthyResponse} loading={false} tasks={[]} onViewTask={mockOnViewTask} />
    );
    expect(screen.getByText('All systems healthy')).toBeInTheDocument();
  });

  it('renders health badge for healthy status', () => {
    render(
      <SystemHealthCard data={mockHealthyResponse} loading={false} tasks={[]} onViewTask={mockOnViewTask} />
    );
    expect(screen.getByText('Healthy')).toBeInTheDocument();
  });

  it('renders health badge for degraded status', () => {
    render(
      <SystemHealthCard data={mockDegradedResponse} loading={false} tasks={[]} onViewTask={mockOnViewTask} />
    );
    expect(screen.getByText('Degraded')).toBeInTheDocument();
  });

  it('renders actionable issues with severity styling', () => {
    render(
      <SystemHealthCard data={mockDegradedResponse} loading={false} tasks={[mockTask]} onViewTask={mockOnViewTask} />
    );
    expect(screen.getByText('High queue depth')).toBeInTheDocument();
    expect(screen.getByText('Consider scaling resources')).toBeInTheDocument();
  });

  it('displays success rate when present', () => {
    render(
      <SystemHealthCard data={mockHealthyResponse} loading={false} tasks={[]} onViewTask={mockOnViewTask} />
    );
    expect(screen.getByText('95%')).toBeInTheDocument();
  });

  it('displays queue depth metrics', () => {
    render(
      <SystemHealthCard data={mockHealthyResponse} loading={false} tasks={[]} onViewTask={mockOnViewTask} />
    );
    expect(screen.getByTitle('queued')).toHaveTextContent('0q');
    expect(screen.getByTitle('active')).toHaveTextContent('5a');
    expect(screen.getByTitle('evaluating')).toHaveTextContent('2e');
  });

  it('should not show all systems healthy when data is null', () => {
    render(
      <SystemHealthCard data={null} loading={false} tasks={[]} onViewTask={mockOnViewTask} />
    );
    expect(screen.queryByText('All systems healthy')).not.toBeInTheDocument();
  });

  it('marks the verdict stale when served_stale is true even at a recent age', () => {
    // The compute-failure bug: a recent (age 30s) payload that was actually
    // served from the last-good fallback must NOT be shown as a live verdict.
    const servedStale: HealthOverviewResponse = {
      ...mockHealthyResponse,
      precompute_age_seconds: 30, // under SNAPSHOT_STALE_AFTER_SECONDS (120)
      served_stale: true,
    };
    render(
      <SystemHealthCard data={servedStale} loading={false} tasks={[]} onViewTask={mockOnViewTask} />
    );
    const asOf = screen.getByText(/as of/);
    expect(asOf.textContent).toContain('stale');
    expect(asOf.className).toContain('text-amber-400');
    // The card body dims/desaturates so the reading reads as not-live.
    expect(document.querySelector('.opacity-60.saturate-50')).toBeInTheDocument();
  });

  it('does not mark stale when served_stale is false and age is recent', () => {
    render(
      <SystemHealthCard data={mockHealthyResponse} loading={false} tasks={[]} onViewTask={mockOnViewTask} />
    );
    const asOf = screen.getByText(/as of/);
    expect(asOf.textContent).not.toContain('stale');
    expect(document.querySelector('.opacity-60.saturate-50')).not.toBeInTheDocument();
  });
});
