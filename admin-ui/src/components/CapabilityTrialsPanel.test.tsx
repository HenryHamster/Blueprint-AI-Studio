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

  it('renders lane pilot verdict metrics without generic arms', () => {
    render(<CapabilityTrialsPanel digest={{ capability_adoption_verdicts: {
      review_fix_lane: {
        verdict: 'ADOPT',
        n_lane: 42,
        n_control: 40,
        lane_merge_rate: 0.45,
        control_merge_rate: 0.375,
        lane_cost_per_merged: 12.5,
        control_cost_per_merged: 15,
        checkpoint_loss_rate: 0.025,
        gate_integrity_violations: 0,
        immediate_alert: false,
        evaluated_at: '2026-06-10T10:00:00Z',
        verdict_reason: 'adopt_criteria_met',
        thresholds: { min_n: 30, kill_merge_rate_floor: -0.03, kill_cost_ceiling: 0.1, kill_gate_violations_max: 3, kill_checkpoint_loss_ceiling: 0.05, futility_n: 90, adopt_merge_lift_floor: -0.01, adopt_cost_improvement: 0.05 },
      },
    } } as never} />);
    expect(screen.getAllByText('review_fix_lane')[0]).toBeTruthy();
    expect(screen.getAllByText('ADOPT')[0]).toBeTruthy();
    expect(screen.getAllByText('adopt_criteria_met')[0]).toBeTruthy();
    expect(screen.getAllByText('42')[0]).toBeTruthy();
    expect(screen.getAllByText('40')[0]).toBeTruthy();
    expect(screen.getAllByText('45.0%')[0]).toBeTruthy();
    expect(screen.getAllByText('37.5%')[0]).toBeTruthy();
    expect(screen.getAllByText('$12.50')[0]).toBeTruthy();
    expect(screen.getAllByText('$15.00')[0]).toBeTruthy();
    expect(screen.getAllByText('2.5%')[0]).toBeTruthy();
    expect(screen.getAllByText('0')[0]).toBeTruthy();
    expect(screen.queryByText('No capability trials are currently running.')).toBeNull();
  });

  it('renders lane pilot alert verdicts without n_with or n_without', () => {
    render(<CapabilityTrialsPanel digest={{ capability_adoption_verdicts: {
      review_fix_lane: {
        verdict: 'KILL',
        n_lane: 2,
        n_control: 1,
        lane_merge_rate: null,
        control_merge_rate: null,
        lane_cost_per_merged: null,
        control_cost_per_merged: null,
        checkpoint_loss_rate: null,
        gate_integrity_violations: 3,
        immediate_alert: true,
        verdict_reason: 'gate_integrity_violations_exceeded',
      },
    } } as never} />);
    expect(screen.getAllByText('KILL')[0]).toBeTruthy();
    expect(screen.getAllByText('Immediate alert')[0]).toBeTruthy();
    expect(screen.getAllByText('gate_integrity_violations_exceeded')[0]).toBeTruthy();
    expect(screen.queryByText('No capability trials are currently running.')).toBeNull();
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

  it('renders lane_relay_comparison wall-clock row when present', () => {
    render(<CapabilityTrialsPanel digest={{ capability_adoption_verdicts: {
      'review-fix-lane-pilot': {
        verdict: 'CONTINUE',
        n_lane: 10,
        n_control: 10,
        lane_merge_rate: 0.5,
        control_merge_rate: 0.4,
        lane_relay_comparison: {
          source_version: { version_number: 42, captured_at: null, terminal_status: 'promoted' },
          relay: { wall_clock_seconds: 3600, active_seconds: 1800, gap_seconds: 1800, task_count: 5, pr_count: 3 },
          lane: { wall_clock_seconds: 2700, active_seconds: 1500, gap_seconds: 1200, task_count: 4, pr_count: 3 },
          deltas: { wall_clock_seconds: -900, active_seconds: -300, gap_seconds: -600, task_count: -1, pr_count: 0 },
        },
      },
    } } as never} />);
    // Desktop summary line includes wall-clock relay comparison
    expect(screen.getAllByText(/Wall-clock.*lane.*relay/)[0]).toBeTruthy();
    expect(screen.getAllByText(/2700s.*3600s/)[0]).toBeTruthy();
    expect(screen.getAllByText(/-900s/)[0]).toBeTruthy();
    // Version label is shown
    expect(screen.getAllByText(/v42/)[0]).toBeTruthy();
  });

  it('renders gracefully when lane_relay_comparison is null', () => {
    render(<CapabilityTrialsPanel digest={{ capability_adoption_verdicts: {
      'review-fix-lane-pilot': {
        verdict: 'CONTINUE',
        n_lane: 5,
        n_control: 5,
        lane_merge_rate: 0.4,
        control_merge_rate: 0.4,
        lane_relay_comparison: null,
      },
    } } as never} />);
    expect(screen.getAllByText('review-fix-lane-pilot')[0]).toBeTruthy();
    // No relay comparison rendered, no crash
    expect(screen.queryByText(/Wall-clock.*relay/)).toBeNull();
  });

  it('shows the empty state when no trials are running', () => {
    render(<CapabilityTrialsPanel digest={{ capability_adoption_verdicts: {} } as never} />);
    expect(screen.getByText('No capability trials are currently running.')).toBeTruthy();
  });

  it('renders nothing when digest is null', () => {
    const { container } = render(<CapabilityTrialsPanel digest={null} />);
    expect(container.firstChild).toBeNull();
    expect(screen.queryByText('No capability trials are currently running.')).toBeNull();
    expect(screen.queryByText('Capability Trials')).toBeNull();
  });
});
