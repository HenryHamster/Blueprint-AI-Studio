import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { RemediationFunnelSection } from './RemediationFunnelSection';

describe('RemediationFunnelSection', () => {
  it('renders nothing when no investigations have been spawned', () => {
    const { container } = render(
      <RemediationFunnelSection
        funnel={{ investigations_spawned: 0 }}
        conversion={null}
      />
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders nothing when funnel is null', () => {
    const { container } = render(<RemediationFunnelSection funnel={null} conversion={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('renders stage counts and conversion percentages', () => {
    render(
      <RemediationFunnelSection
        funnel={{
          investigations_spawned: 10,
          investigations_completed: 8,
          investigations_with_diagnosis: 6,
          remediations_spawned: 4,
          remediations_merged: 2,
          remediations_failed: 1,
          active_investigations: 3,
          avg_time_to_remediation_hours: 1.5,
        }}
        conversion={null}
      />
    );
    expect(screen.getByText('Remediation Funnel')).toBeInTheDocument();
    expect(screen.getByText('Spawned')).toBeInTheDocument();
    expect(screen.getByText('10')).toBeInTheDocument();
    // 8/10 = 80%
    expect(screen.getByText(/80%/)).toBeInTheDocument();
    expect(screen.getByText('3 active')).toBeInTheDocument();
    expect(screen.getByText(/1.5h/)).toBeInTheDocument();
    expect(screen.getByText('1 failed')).toBeInTheDocument();
  });

  it('renders per-failure-class breakdown rows', () => {
    render(
      <RemediationFunnelSection
        funnel={{
          investigations_spawned: 5,
          investigations_completed: 4,
          investigations_spawned_by_class: { CODE_ERROR: 3, AGENT_TIMEOUT: 2 },
          investigations_completed_by_class: { CODE_ERROR: 2, AGENT_TIMEOUT: 2 },
          remediations_spawned_by_class: { CODE_ERROR: 2 },
          merged_remediations_by_trigger_class: { CODE_ERROR: 1 },
          failed_remediations_by_class: { AGENT_TIMEOUT: 1 },
        }}
        conversion={null}
      />
    );
    expect(screen.getByText('CODE_ERROR')).toBeInTheDocument();
    expect(screen.getByText('AGENT_TIMEOUT')).toBeInTheDocument();
    expect(screen.getByText('By failure class')).toBeInTheDocument();
  });

  it('renders drop-off reasons and structural filter pass rate', () => {
    render(
      <RemediationFunnelSection
        funnel={{
          investigations_spawned: 5,
          diagnosis_drop_off_by_reason: { structural: 2, low_confidence: 1 },
        }}
        conversion={{ parseable: 4, structural: 2 }}
      />
    );
    expect(screen.getByText('Diagnosis drop-off')).toBeInTheDocument();
    expect(screen.getByText('structural filter pass')).toBeInTheDocument();
    expect(screen.getByText('50%')).toBeInTheDocument();
    expect(screen.getByText('structural')).toBeInTheDocument();
    expect(screen.getByText('low conf')).toBeInTheDocument();
  });

  it('renders success-by-confidence rates', () => {
    render(
      <RemediationFunnelSection
        funnel={{
          investigations_spawned: 5,
          investigations_by_confidence: { high: 3, low: 1 },
          remediation_success_by_confidence: {
            high: { merged: 2, failed: 1 },
            low: { merged: 0, failed: 2 },
          },
        }}
        conversion={null}
      />
    );
    expect(screen.getByText('Confidence distribution')).toBeInTheDocument();
    expect(screen.getByText('Success by confidence')).toBeInTheDocument();
    expect(screen.getByText('2/3')).toBeInTheDocument();
    expect(screen.getByText('0/2')).toBeInTheDocument();
  });
});
