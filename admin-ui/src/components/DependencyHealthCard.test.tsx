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

  it('renders nothing when all counts are zero', () => {
    const { container } = render(
      <DependencyHealthCard
        data={makeData({
          parked_blockers: { total: 0, tasks: [] },
          duplicate_foundation_candidates: { total: 0, candidates: [] },
          unresumable_cancellations: { total: 0, tasks: [] },
        })}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('renders nothing when data is null', () => {
    const { container } = render(<DependencyHealthCard data={null} />);
    expect(container).toBeEmptyDOMElement();
  });
});
