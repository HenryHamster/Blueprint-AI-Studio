import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { CapabilityTrialsPanel } from './CapabilityTrialsPanel';

describe('CapabilityTrialsPanel', () => {
  it('renders verdict rows and progress summary', () => {
    render(<CapabilityTrialsPanel digest={{ capability_adoption_verdicts: {
      'repo-map-navigation': { verdict: 'adopt', lift: 0.1234, n_with: 18, n_without: 7, evaluated_at: '2026-06-09T12:00:00Z' },
    } } as never} />);
    expect(screen.getByText('Capability Trials')).toBeTruthy();
    // Component renders both a mobile (sm:hidden) and desktop (hidden sm:table) subtree;
    // getAllByText handles both matches and we assert at least one is present.
    expect(screen.getAllByText('repo-map-navigation')[0]).toBeTruthy();
    expect(screen.getAllByText('adopt')[0]).toBeTruthy();
    expect(screen.getAllByText('+0.123')[0]).toBeTruthy();
    expect(screen.getAllByText('18/30')[0]).toBeTruthy();
  });

  it('uses thresholds.min_n_per_arm when present instead of hardcoded 30', () => {
    render(<CapabilityTrialsPanel digest={{ capability_adoption_verdicts: {
      'repo-map-navigation': { verdict: 'continue-trial', lift: null, n_with: 18, n_without: 7, thresholds: { min_n_per_arm: 50 } },
    } } as never} />);
    expect(screen.getAllByText('18/50')[0]).toBeTruthy();
    // Should NOT show 18/30 when threshold is 50
    expect(screen.queryByText('18/30')).toBeNull();
  });

  it('renders verdict_reason as muted label below the verdict', () => {
    render(<CapabilityTrialsPanel digest={{ capability_adoption_verdicts: {
      'repo-map-navigation': { verdict: 'continue-trial', lift: null, n_with: 18, n_without: 7, verdict_reason: 'insufficient_data' },
    } } as never} />);
    expect(screen.getAllByText('insufficient_data')[0]).toBeTruthy();
  });

  it('renders accrual_projection line when present', () => {
    render(<CapabilityTrialsPanel digest={{ capability_adoption_verdicts: {
      'repo-map-navigation': {
        verdict: 'continue-trial', lift: null, n_with: 18, n_without: 7,
        accrual_projection: { with_7d: 4, without_7d: 2, window_days: 7, holdback_fraction: 0.1, days_to_min_n: 12.5 },
      },
    } } as never} />);
    expect(screen.getAllByText(/4 with \/ 2 without in last 7d/)[0]).toBeTruthy();
    expect(screen.getAllByText(/est\. 12\.5d to threshold at 10% holdback/)[0]).toBeTruthy();
  });

  it('shows an em dash for days_to_min_n when null', () => {
    render(<CapabilityTrialsPanel digest={{ capability_adoption_verdicts: {
      'repo-map-navigation': {
        verdict: 'continue-trial', lift: null, n_with: 18, n_without: 7,
        accrual_projection: { with_7d: 0, without_7d: 0, window_days: 7, holdback_fraction: 0.1, days_to_min_n: null },
      },
    } } as never} />);
    expect(screen.getAllByText(/est\. — to threshold/)[0]).toBeTruthy();
  });

  it('shows the empty state when no trials are running', () => {
    render(<CapabilityTrialsPanel digest={{ capability_adoption_verdicts: {} } as never} />);
    expect(screen.getByText('No capability trials are currently running.')).toBeTruthy();
  });
});
