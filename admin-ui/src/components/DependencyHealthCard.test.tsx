import { describe, it, expect } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { DependencyHealthCard } from './DependencyHealthCard';
import type { DependencyHealthSignals } from '../types/digest';

function makeData(overrides: Partial<DependencyHealthSignals> = {}): DependencyHealthSignals {
  return {
    parked_blockers: {
      total: 2,
      tasks: [
        {
          task_id: 't1',
          title: 'Parked alpha',
          project_id: 'p1',
          blocker_kind: 'Base re-cut conflict',
          blocked_reason: 'held',
          blocked_at: '2026-06-01T00:00:00',
          recheck_after: null,
        },
      ],
    },
    duplicate_foundation_candidates: {
      total: 1,
      candidates: [{ task_ids: ['a', 'b'], overlap_ratio: 0.5, shared_files: ['foo.py'] }],
    },
    unresumable_cancellations: {
      total: 0,
      by_class: {},
      by_direction: {},
      tasks: [],
    },
    ...overrides,
  };
}

describe('DependencyHealthCard', () => {
  it('renders the three counts', () => {
    render(<DependencyHealthCard data={makeData()} />);
    expect(screen.getByText('Parked blockers')).toBeInTheDocument();
    expect(screen.getByText('Duplicate foundations')).toBeInTheDocument();
    expect(screen.getByText('Unresumable cancels')).toBeInTheDocument();
  });

  it('drills down to the capped task list on click', () => {
    render(<DependencyHealthCard data={makeData()} />);
    expect(screen.queryByText('Parked alpha')).not.toBeInTheDocument();
    fireEvent.click(screen.getByText('Parked blockers'));
    expect(screen.getByText('Parked alpha')).toBeInTheDocument();
    expect(screen.getByText(/Base re-cut conflict/)).toBeInTheDocument();
  });

  it('renders the affirmative healthy state when all failure counts are zero', () => {
    // No parked/duplicate/unresumable failures, but a live-blocked population
    // and resumes over the window — the card must render as a health gauge,
    // not disappear.
    render(
      <DependencyHealthCard
        data={makeData({
          parked_blockers: { total: 0, tasks: [] },
          duplicate_foundation_candidates: { total: 0, candidates: [] },
          unresumable_cancellations: { total: 0, tasks: [] },
          live_blocked: { total: 3, by_age: { '<1h': 2, '1-6h': 1 }, tasks: [] },
          resumes: { total: 5, by_resolved_by: { edge: 3, completed: 2 }, by_direction: {}, tasks: [] },
        })}
      />,
    );
    // Card renders (does not return null) — gauge title is present.
    expect(screen.getByText('Dependency Health')).toBeInTheDocument();
    // Affirmative line surfaces the live-blocked and resumed-this-window counts.
    const affirmative = screen.getByText(/blocked & waiting/);
    expect(affirmative).toHaveTextContent('3');
    expect(affirmative).toHaveTextContent('blocked & waiting');
    expect(affirmative).toHaveTextContent('5');
    expect(affirmative).toHaveTextContent('resumed this window');
  });

  it('renders nothing when data is null', () => {
    const { container } = render(<DependencyHealthCard data={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
