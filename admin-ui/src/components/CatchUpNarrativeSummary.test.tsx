import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CatchUpNarrativeSummary, type CatchUpNarrativeSummaryProps } from './CatchUpNarrativeSummary';

// The component only reads narrative_summary / trend / window_hours; cast a
// minimal payload to the prop shape rather than constructing a full DigestData.
function makeProps(narrative: string | null): CatchUpNarrativeSummaryProps {
  return {
    digest: { narrative_summary: narrative } as CatchUpNarrativeSummaryProps['digest'],
    activeWindow: '24h',
  };
}

describe('CatchUpNarrativeSummary convergence verdict', () => {
  it('surfaces the convergence verdict as its own line and removes it from the prose', () => {
    const verdict =
      'Review/fix churn is rising; the main blocker is churn (2/2 non-converged PR chains).';
    render(
      <CatchUpNarrativeSummary
        {...makeProps(`2 tasks completed including Add retry. ${verdict} Pipeline steady.`)}
      />,
    );

    // The verdict appears in its own labelled line, verbatim.
    const verdictLine = screen.getByLabelText('Review/fix convergence verdict');
    expect(verdictLine.textContent).toBe(verdict);

    // It is NOT duplicated inside the surrounding prose paragraph.
    const matches = screen.getAllByText(/Review\/fix churn/);
    expect(matches).toHaveLength(1);
    expect(screen.getByText(/Pipeline steady\./).textContent).not.toContain('churn');
  });

  it('renders no convergence line when the narrative carries no churn verdict (thin window)', () => {
    render(<CatchUpNarrativeSummary {...makeProps('2 tasks completed. Pipeline steady.')} />);

    expect(screen.queryByLabelText('Review/fix convergence verdict')).toBeNull();
    expect(screen.queryByText(/non-converged PR chains/)).toBeNull();
    // The full narrative is still shown.
    expect(screen.getByText(/2 tasks completed\. Pipeline steady\./)).toBeTruthy();
  });
});
