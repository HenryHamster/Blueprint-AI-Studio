import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { TaskDurationWidget, type TaskDurationStats } from './TaskDurationWidget';

function makeData(overrides: Partial<TaskDurationStats> = {}): TaskDurationStats {
  return {
    stats: { create: { p50_seconds: 120, sample_count: 10 }, research: { p50_seconds: 300, sample_count: 5 } },
    scope_duration_stats: {
      small: { p50_seconds: 90, p90_seconds: 180, sample_size: 8 },
      medium: { p50_seconds: 240, p90_seconds: 600, sample_size: 4 },
    },
    phase_duration_stats: {
      provisioning: { p50_seconds: 20, sample_size: 12 },
      execution: { p50_seconds: 100, sample_size: 12 },
    },
    ...overrides,
  };
}

describe('TaskDurationWidget', () => {
  it('renders nothing when data is null', () => {
    const { container } = render(<TaskDurationWidget data={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when all stats are empty', () => {
    const { container } = render(
      <TaskDurationWidget data={{ stats: {}, scope_duration_stats: {}, phase_duration_stats: {} }} />
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders task type durations', () => {
    render(<TaskDurationWidget data={makeData()} />);
    expect(screen.getByText('create')).toBeDefined();
    expect(screen.getByText('research')).toBeDefined();
    expect(screen.getAllByText('2m').length).toBeGreaterThan(0); // 120s p50
  });

  it('renders scope rows with p50/p90', () => {
    render(<TaskDurationWidget data={makeData()} />);
    expect(screen.getByText('small')).toBeDefined();
    expect(screen.getByText('medium')).toBeDefined();
  });

  it('renders vm phase rows', () => {
    render(<TaskDurationWidget data={makeData()} />);
    expect(screen.getByText('prov')).toBeDefined();
    expect(screen.getByText('exec')).toBeDefined();
  });

  it('applies amber class when execution p50 > 30 min', () => {
    const data = makeData({ phase_duration_stats: { execution: { p50_seconds: 2000, sample_size: 5 } } });
    render(<TaskDurationWidget data={data} />);
    const execSpan = screen.getByTitle(/slow execution/);
    expect(execSpan.className).toContain('amber');
  });

  it('does not apply amber when execution is within threshold', () => {
    render(<TaskDurationWidget data={makeData()} />);
    const execSpan = screen.getByText('exec').closest('span');
    expect(execSpan?.className).not.toContain('amber');
  });
});
