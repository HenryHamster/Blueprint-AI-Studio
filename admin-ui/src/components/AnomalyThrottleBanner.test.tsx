import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { AnomalyThrottleBanner } from './AnomalyThrottleBanner';

describe('AnomalyThrottleBanner', () => {
  it('renders nothing when there is no active throttle or gated type', () => {
    const { container } = render(<AnomalyThrottleBanner digest={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('renders gated types without an auto-throttle card', () => {
    render(
      <AnomalyThrottleBanner
        digest={null}
        gatedTaskTypes={[
          { task_type: 'create', failure_rate: 0.83, sample_size: 12, threshold: 0.6 },
        ]}
      />,
    );

    expect(screen.getByText(/Partial dispatch gate — 1 task type blocked/)).toBeInTheDocument();
    expect(screen.getByText('create')).toBeInTheDocument();
    expect(screen.queryByText('Pipeline auto-throttled')).not.toBeInTheDocument();
  });

  it('renders the auto-throttle card and the partial gate card together', () => {
    render(
      <AnomalyThrottleBanner
        digest={{
          anomaly_adjustment: {
            active: true,
            adjusted_at: '2026-07-05T12:00:00Z',
            trigger_anomalies: ['spike'],
            pre_adjustment_max_vms: 4,
            adjusted_max_vms: 2,
            pre_adjustment_failure_gate: 0.8,
            failure_gate_threshold: 0.6,
            forced_restoration_count: 1,
            forced_restored_at: '2026-07-05T11:55:00Z',
          },
        } as never}
        gatedTaskTypes={[
          { task_type: 'create', failure_rate: 0.83, sample_size: 12, threshold: 0.6 },
          { task_type: 'fix', failure_rate: 0.67, sample_size: 5, threshold: 0.6 },
        ]}
      />,
    );

    expect(screen.getByText('Pipeline auto-throttled')).toBeInTheDocument();
    expect(screen.getByText(/Partial dispatch gate — 2 task types blocked/)).toBeInTheDocument();
    expect(screen.getByText(/83% failure rate \(n=12, threshold=60%\)/)).toBeInTheDocument();
    expect(screen.getByText(/67% failure rate \(n=5, threshold=60%\)/)).toBeInTheDocument();
  });
});
