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

  it('shows the empty state when no trials are running', () => {
    render(<CapabilityTrialsPanel digest={{ capability_adoption_verdicts: {} } as never} />);
    expect(screen.getByText('No capability trials are currently running.')).toBeTruthy();
  });
});
